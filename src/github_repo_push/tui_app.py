"""Textual TUI control panel for github-repo-push.

Reflects AND drives the push machinery: rows stream in progressively, and
key bindings run the same guarded Syncer path as the CLI (ignore-pattern
staging, secret scan, protected-branch and diverged refusals, audit log).

Bindings: p = push selected repo (confirm modal with dry-run option),
u = pull selected repo (fast-forward only — refuses on a dirty worktree or
diverged history, no confirm modal since it can't do anything destructive),
a = add/register a local repo and publish it (gh repo create, no browser),
enter = expand/collapse a repo's folders in place, or reveal a file in
Finder, / = search file/folder names across every registered repo,
r = refresh, q = quit.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.github_api import PullRequestInfo
from github_repo_push.ignore import filter_paths
from github_repo_push.models import RepoConfig
from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer

STATUS_PRIORITY = {"error": 0, "diverged": 1, "behind": 2, "ahead": 3, "untracked": 4, "synced": 5}
COLUMNS = ("Repo", "Status", "Dirty", "Local Branch", "Remote Branch", "Size (KB)", "GitHub Push", "ghrp Push", "Open PRs")
DEFAULT_LOCAL_BASE = Path.home() / "Desktop" / "Old_Projects" / "GitHub"

# Same class of noise ghrp already keeps out of pushes (push_rules.yaml's
# ignore_patterns) — kept out of the folder tree and search too, since a repo
# like a Python project's checked-in .venv can bury the real content under
# thousands of vendored files.
TREE_SKIP = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".snapshots", ".cache", ".idea", ".vscode", "dist", "build",
}


def list_children(path: Path) -> list[tuple[str, Path, bool]]:
    """Immediate children of a directory for the dashboard's folder tree.

    Directories sort before files; both skip dotfiles and TREE_SKIP clutter.
    """
    try:
        entries = list(path.iterdir())
    except OSError:
        return []
    items = [
        (entry.name, entry, entry.is_dir())
        for entry in entries
        if not entry.name.startswith(".") and entry.name not in TREE_SKIP
    ]
    items.sort(key=lambda t: (not t[2], t[0].lower()))
    return items


def repo_name_for(registry: Registry, path: Path) -> str:
    """Which registered repo a filesystem path lives under, or '?' if none."""
    for config in registry.repos:
        try:
            path.relative_to(config.get_full_local_path())
            return config.name
        except ValueError:
            continue
    return "?"


def search_repos(registry: Registry, query: str, limit: int = 25) -> list[tuple[str, Path]]:
    """Case-insensitive substring search for a name across every registered repo.

    Fast enough to run synchronously (~0.1s across 15 repos on this machine) —
    the same TREE_SKIP prune that keeps the folder view readable also keeps
    this from crawling a checked-in virtualenv's thousands of files.
    """
    needle = query.strip().lower()
    if not needle:
        return []
    matches: list[Path] = []
    for config in registry.repos:
        root = config.get_full_local_path()
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in TREE_SKIP]
            for name in dirnames + filenames:
                if needle in name.lower():
                    matches.append(Path(dirpath) / name)
    matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return [(repo_name_for(registry, p), p) for p in matches[:limit]]


def build_row(syncer: Syncer, config: RepoConfig) -> tuple[tuple, Optional[list[PullRequestInfo]]]:
    """One table row for a repo, plus its open PRs.

    PR fetch is a second, independent gh call kept in its own try/except so a
    PR-fetch failure can't blank out an otherwise-good row. Returned prs is
    None if the fetch failed/timed out or was never attempted (no remote),
    [] if it succeeded and found none.
    """
    try:
        state = syncer.check_repo_state(config)
        pushes = syncer.get_recent_pushes(config.name, count=1)
        ghrp_push = pushes[-1].timestamp.strftime("%Y-%m-%d %H:%M") if pushes else "Never"
        if state.remote_pushed_at:
            github_push = state.remote_pushed_at.astimezone().strftime("%Y-%m-%d %H:%M")
        else:
            github_push = "-" if state.remote_exists else "no remote"
        status = state.sync_status.value if config.enabled else f"{state.sync_status.value} (disabled)"
        base_row = (
            config.name,
            status,
            "yes" if state.uncommitted_changes else "",
            state.local_branch or "",
            state.remote_branch or "",
            str(state.local_size_kb),
            github_push,
            ghrp_push,
        )
        remote_exists = state.remote_exists
    except Exception as exc:  # noqa: BLE001 - one bad repo must not kill the dashboard
        return (config.name, "error", "", "", "", "0", "", str(exc)[:60], "?"), None

    prs: Optional[list[PullRequestInfo]] = None
    pr_count = "-"
    if remote_exists:
        try:
            prs = syncer.github_api.list_open_prs(config.repo_name)
            pr_count = str(len(prs))
        except Exception:  # noqa: BLE001 - PR fetch failure must not blank the row
            pr_count = "?"
    return base_row + (pr_count,), prs


def push_preview(syncer: Syncer, config: RepoConfig) -> str:
    """Local-only summary of what a push would do. No network calls."""
    path = config.get_full_local_path()
    if not path.exists():
        return "Local path is missing — push will fail."
    git_repo = GitRepo(path)
    if not git_repo.is_repo():
        return "Not a git repo yet: push will git init, generate a README, commit and publish."
    dirty = git_repo.list_dirty_files()
    allowed, skipped = filter_paths(dirty, syncer._ignore_patterns(config))
    lines = [f"Branch: {config.push_branch}   Visibility: {config.visibility}"]
    if allowed:
        lines.append(f"Will commit {len(allowed)} file(s).")
    else:
        lines.append("No new changes to commit (pushes existing unpushed commits, if any).")
    if skipped:
        preview = ", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else "")
        lines.append(f"Skipped by ignore rules: {len(skipped)} ({preview})")
    if config.visibility == "public":
        lines.append("Secret scan will gate the push (public repo).")
    return "\n".join(lines)


def detect_branch(path: Path) -> str:
    git_repo = GitRepo(path)
    if git_repo.is_repo():
        current = git_repo.run(["branch", "--show-current"], check=False).stdout.strip()
        if current:
            return current
    return "main"


def make_app():
    """Build the App class lazily so `textual` stays an optional import."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Log, Select

    class ConfirmPushScreen(ModalScreen[Optional[str]]):
        """Confirm modal for pushing one repo. Dismisses 'push' | 'dry' | None."""

        BINDINGS = [Binding("escape", "cancel", "Cancel")]

        def action_cancel(self) -> None:
            self.dismiss(None)
        DEFAULT_CSS = """
        ConfirmPushScreen { align: center middle; }
        ConfirmPushScreen #dialog {
            width: 80; height: auto; max-height: 80%;
            border: thick $accent; background: $surface; padding: 1 2;
        }
        ConfirmPushScreen .row { height: auto; }
        ConfirmPushScreen Button { min-width: 12; margin-right: 2; }
        ConfirmPushScreen #preview { height: auto; margin-bottom: 1; }
        """

        def __init__(self, repo_name: str, preview: str):
            super().__init__()
            self._repo_name = repo_name
            self._preview = preview

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label(f"Push {self._repo_name} to GitHub?")
                yield Label(self._preview, id="preview")
                with Horizontal(classes="row"):
                    yield Button("Push", id="push", variant="warning")
                    yield Button("Dry-run", id="dry", variant="primary")
                    yield Button("Cancel", id="cancel")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(None if event.button.id == "cancel" else event.button.id)

    class PullRequestsScreen(ModalScreen[None]):
        """Read-only list of a repo's open PRs. Dismisses None."""

        BINDINGS = [Binding("escape", "cancel", "Close")]

        def action_cancel(self) -> None:
            self.dismiss(None)
        DEFAULT_CSS = """
        PullRequestsScreen { align: center middle; }
        PullRequestsScreen #dialog {
            width: 90; height: auto; max-height: 85%;
            border: thick $accent; background: $surface; padding: 1 2;
        }
        PullRequestsScreen .pr { height: auto; margin-bottom: 1; }
        PullRequestsScreen .row { height: auto; margin-top: 1; }
        PullRequestsScreen #error { color: $error; height: auto; }
        """

        def __init__(self, repo_name: str, prs: Optional[list[PullRequestInfo]]):
            super().__init__()
            self._repo_name = repo_name
            self._prs = prs

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label(f"Open PRs — {self._repo_name}")
                if self._prs is None:
                    yield Label("Could not fetch pull requests (gh error/timeout). Press r to refresh and retry.", id="error")
                elif not self._prs:
                    yield Label("No open pull requests.")
                else:
                    for pr in self._prs:
                        draft = " [draft]" if pr.is_draft else ""
                        with Vertical(classes="pr"):
                            yield Label(f"#{pr.number} {pr.title}{draft}")
                            yield Label(f"by {pr.author} · branch {pr.head_branch}")
                            yield Label(pr.url)
                with Horizontal(classes="row"):
                    yield Button("Close", id="close")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            self.dismiss(None)

    class AddRepoScreen(ModalScreen[Optional[dict]]):
        """Register + publish a local repo. Dismisses {path, description, visibility} | None."""

        BINDINGS = [Binding("escape", "cancel", "Cancel")]

        def action_cancel(self) -> None:
            self.dismiss(None)
        DEFAULT_CSS = """
        AddRepoScreen { align: center middle; }
        AddRepoScreen #dialog {
            width: 90; height: auto; max-height: 85%;
            border: thick $accent; background: $surface; padding: 1 2;
        }
        AddRepoScreen .row { height: auto; margin-bottom: 1; }
        AddRepoScreen Label { width: 100%; }
        AddRepoScreen Button { min-width: 12; margin-right: 2; }
        AddRepoScreen #error { color: $error; height: auto; }
        """

        def __init__(self, candidates: list[str], local_base: Path):
            super().__init__()
            self._candidates = candidates
            self._local_base = local_base

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Add a repository to GitHub (no browser needed)")
                with Vertical(classes="row"):
                    yield Label(f"Unregistered folders in {self._local_base}:")
                    yield Select(
                        [(name, name) for name in self._candidates],
                        prompt="choose a folder…",
                        id="candidate",
                    )
                with Vertical(classes="row"):
                    yield Label("…or a manual path (overrides the selection above):")
                    yield Input(placeholder="/path/to/repo", id="path")
                with Vertical(classes="row"):
                    yield Label("Description (used for README/GitHub description):")
                    yield Input(placeholder="Short project description.", id="description")
                with Vertical(classes="row"):
                    yield Label("Visibility on GitHub:")
                    yield Select(
                        [("private", "private"), ("public", "public")],
                        value="private",
                        allow_blank=False,
                        id="visibility",
                    )
                yield Label("", id="error")
                with Horizontal(classes="row"):
                    yield Button("Add + Publish", id="add", variant="warning")
                    yield Button("Cancel", id="cancel")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "cancel":
                self.dismiss(None)
                return
            manual = self.query_one("#path", Input).value.strip()
            selected = self.query_one("#candidate", Select).value
            if manual:
                path = Path(manual).expanduser().resolve()
            elif selected is not Select.NULL:
                path = (self._local_base / str(selected)).resolve()
            else:
                self.query_one("#error", Label).update("Pick a folder or enter a path.")
                return
            if not path.exists() or not path.is_dir():
                self.query_one("#error", Label).update(f"Not a directory: {path}")
                return
            self.dismiss(
                {
                    "path": path,
                    "description": self.query_one("#description", Input).value.strip(),
                    "visibility": str(self.query_one("#visibility", Select).value),
                }
            )

    class SearchScreen(ModalScreen[Optional[Path]]):
        """Search file/folder names across every registered repo.

        Type and press Enter to search (re-searching is cheap — a pruned walk
        of every repo takes well under a second on this machine). Selecting a
        result dismisses with its path; Escape dismisses with None.
        """

        BINDINGS = [Binding("escape", "cancel", "Cancel")]

        def action_cancel(self) -> None:
            self.dismiss(None)
        DEFAULT_CSS = """
        SearchScreen { align: center middle; }
        SearchScreen #dialog {
            width: 100; height: auto; max-height: 85%;
            border: thick $accent; background: $surface; padding: 1 2;
        }
        SearchScreen #results { height: auto; max-height: 20; margin-top: 1; }
        SearchScreen Button { width: 100%; text-align: left; margin-bottom: 1; }
        SearchScreen #status { color: $text-muted; height: auto; margin-top: 1; }
        """

        def __init__(self, registry: Registry):
            super().__init__()
            self._registry = registry
            self._results: list[Path] = []

        def compose(self) -> ComposeResult:
            with Vertical(id="dialog"):
                yield Label("Search every registered repo for a file or folder name")
                yield Input(placeholder="e.g. bridge takeover", id="query")
                yield Label("", id="status")
                yield VerticalScroll(id="results")

        def on_input_submitted(self, event: Input.Submitted) -> None:
            query = event.value
            self._results = [path for _repo, path in search_repos(self._registry, query)]
            results_box = self.query_one("#results", VerticalScroll)
            results_box.remove_children()
            status = self.query_one("#status", Label)
            if not query.strip():
                status.update("")
                return
            if not self._results:
                status.update(f"No matches for “{query}”.")
                return
            status.update(f"{len(self._results)} match(es) — pick one:")
            for i, path in enumerate(self._results):
                repo = repo_name_for(self._registry, path)
                try:
                    rel = path.relative_to(self._registry.get_repo(repo).get_full_local_path())
                except Exception:
                    rel = path
                results_box.mount(Button(f"{repo} › {rel}", id=f"result-{i}"))

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id and event.button.id.startswith("result-"):
                index = int(event.button.id.split("-", 1)[1])
                self.dismiss(self._results[index])

    class RepoDashboard(App):
        TITLE = "Github Repo Push"
        BINDINGS = [
            Binding("p", "push_repo", "Push repo"),
            Binding("u", "pull_repo", "Pull repo"),
            Binding("a", "add_repo", "Add repo"),
            Binding("o", "show_prs", "Open PRs"),
            Binding("slash", "search", "Search"),
            Binding("r", "refresh", "Refresh"),
            Binding("q", "quit", "Quit"),
        ]
        CSS = """
        DataTable { height: 1fr; }
        Log { height: 7; border-top: solid $accent; }
        """

        def __init__(self, config_dir: Path, data_dir: Path, local_base: Path = DEFAULT_LOCAL_BASE):
            super().__init__()
            self.registry = Registry(config_dir)
            self.registry.load()
            self.syncer = Syncer(self.registry, data_dir)
            self.local_base = local_base
            self._loading_active = False
            self._pr_cache: dict[str, Optional[list[PullRequestInfo]]] = {}
            self._rows: list[tuple] = []
            self._expanded: set[str] = set()

        def compose(self) -> ComposeResult:
            yield Header()
            yield DataTable()
            yield Log()
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.cursor_type = "row"
            table.add_columns(*COLUMNS)
            self.action_refresh()

        # ---- loading -----------------------------------------------------

        def action_refresh(self) -> None:
            if self._loading_active:
                self._log_line("Refresh already in progress.")
                return
            self._loading_active = True
            self._expanded.clear()
            table = self.query_one(DataTable)
            table.clear()
            self.sub_title = f"loading 0/{len(self.registry.repos)} repos…"
            self.run_worker(self._load_rows, thread=True)

        def _load_rows(self) -> None:
            table = self.query_one(DataTable)
            rows: list[tuple] = []
            try:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    futures = [pool.submit(build_row, self.syncer, config) for config in self.registry.repos]
                    for future in as_completed(futures):
                        row, prs = future.result()
                        rows.append(row)
                        self.call_from_thread(self._add_row, table, row)
                        if row[-1] != "-":  # "-" = no remote yet, never attempted; don't cache that as a failure
                            self.call_from_thread(self._store_prs, row[0], prs)
                        self.call_from_thread(self._show_progress, len(rows))
                rows.sort(key=lambda r: (STATUS_PRIORITY.get(r[1].split()[0], 99), r[0].lower()))
                self.call_from_thread(self._show_final, table, rows)
            finally:
                self._loading_active = False

        def _add_row(self, table, row: tuple) -> None:
            # Progressive loading only — the tree isn't active until _show_final,
            # so a plain append (no indentation bookkeeping) is fine here.
            try:
                table.remove_row(row[0])
            except Exception:
                pass
            table.add_row(*row, key=row[0])

        def _show_progress(self, done: int) -> None:
            self.sub_title = f"loading {done}/{len(self.registry.repos)} repos…"

        def _store_prs(self, repo_name: str, prs: Optional[list[PullRequestInfo]]) -> None:
            self._pr_cache[repo_name] = prs

        def _show_final(self, table, rows: list[tuple]) -> None:
            self._rows = rows
            self._rebuild_table()
            self.sub_title = (
                f"{len(rows)} repos · updated {datetime.now().strftime('%H:%M:%S')}"
                f" · routine {self._routine_state()}"
            )

        def _routine_state(self) -> str:
            """Show the real launchd state so the UI reflects what is scheduled."""
            try:
                from github_repo_push.routine import status

                return status().describe()
            except Exception:
                return "unknown"

        # ---- folder tree ---------------------------------------------------
        #
        # self._rows holds one row per registered repo (the source of truth).
        # self._expanded holds the keys — a repo name, or a full filesystem
        # path — of every node currently unfolded. _rebuild_table() replays
        # both into the DataTable from scratch each time, since DataTable can
        # only append rows, not insert one in the middle.

        def _rebuild_table(self) -> None:
            table = self.query_one(DataTable)
            selected_key = self._selected_row_key()
            table.clear()
            for repo_row in self._rows:
                name = repo_row[0]
                table.add_row(*repo_row, key=name)
                config = self.registry.get_repo(name)
                if config is not None and name in self._expanded:
                    self._add_children_rows(table, name, config.get_full_local_path(), depth=1)
            if selected_key is not None:
                self._restore_cursor(table, selected_key)

        def _add_children_rows(self, table, key: str, path: Path, depth: int) -> None:
            if key not in self._expanded:
                return
            children = list_children(path)
            blank = ("", "", "", "", "", "", "", "")
            if not children:
                table.add_row(f"{'  ' * depth}(empty)", *blank, key=f"{key}\0empty")
                return
            for name, child_path, is_dir in children:
                child_key = str(child_path)
                indent = "  " * depth
                if is_dir:
                    glyph = "▾" if child_key in self._expanded else "▸"
                    label = f"{indent}{glyph} {name}/"
                else:
                    label = f"{indent}  {name}"
                table.add_row(label, *blank, key=child_key)
                if is_dir:
                    self._add_children_rows(table, child_key, child_path, depth + 1)

        def _toggle_expand(self, key: str, path: Path) -> None:
            if key in self._expanded:
                self._expanded.discard(key)
                prefix = str(path) + "/"
                self._expanded = {k for k in self._expanded if not k.startswith(prefix)}
            else:
                self._expanded.add(key)
            self._rebuild_table()

        def _reveal_in_finder(self, path: Path) -> None:
            import subprocess

            subprocess.run(["open", "-R", str(path)], check=False)
            self._log_line(f"Revealed in Finder: {path}")

        def on_data_table_row_selected(self, event) -> None:
            key = str(event.row_key.value)
            if "\0" in key:  # placeholder row ("(empty)")
                return
            config = self.registry.get_repo(key)
            if config is not None:
                self._toggle_expand(key, config.get_full_local_path())
                return
            path = Path(key)
            if path.is_dir():
                self._toggle_expand(key, path)
            elif path.is_file():
                self._reveal_in_finder(path)

        # ---- search ---------------------------------------------------------

        def action_search(self) -> None:
            self.push_screen(SearchScreen(self.registry), self._on_search_result)

        def _on_search_result(self, path: Optional[Path]) -> None:
            if path is None:
                return
            if path.is_file():
                self._reveal_in_finder(path)
                return
            # A folder result: expand every ancestor down to it so it's
            # visible in the table, rather than just opening Finder.
            config = self.registry.get_repo(repo_name_for(self.registry, path))
            if config is None:
                self._reveal_in_finder(path)
                return
            self._expanded.add(config.name)
            relative = path.relative_to(config.get_full_local_path())
            walked = config.get_full_local_path()
            for part in relative.parts:
                walked = walked / part
                self._expanded.add(str(walked))
            self._rebuild_table()

        # ---- helpers -----------------------------------------------------

        def _log_line(self, text: str) -> None:
            self.query_one(Log).write_line(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

        def _selected_row_key(self) -> Optional[str]:
            table = self.query_one(DataTable)
            if table.row_count == 0 or table.cursor_row is None:
                return None
            try:
                from textual.coordinate import Coordinate
                return str(table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value)
            except Exception:
                return None

        def _restore_cursor(self, table, key: str) -> None:
            try:
                row_index = table.get_row_index(key)
            except Exception:
                return
            table.move_cursor(row=row_index)

        def _selected_repo(self) -> Optional[RepoConfig]:
            key = self._selected_row_key()
            if key is None:
                return None
            return self.registry.get_repo(key)

        # ---- push action -------------------------------------------------

        def action_push_repo(self) -> None:
            config = self._selected_repo()
            if config is None:
                self._log_line("No repo selected (row not in registry?).")
                return
            if not config.enabled:
                self._log_line(f"{config.name} is disabled in the registry — push refused.")
                return
            preview = push_preview(self.syncer, config)
            self.push_screen(
                ConfirmPushScreen(config.name, preview),
                partial(self._on_push_choice, config),
            )

        def action_show_prs(self) -> None:
            config = self._selected_repo()
            if config is None:
                self._log_line("No repo selected (row not in registry?).")
                return
            if config.name not in self._pr_cache:
                self._log_line(f"No PR data yet for {config.name} — press r to refresh.")
                return
            self.push_screen(PullRequestsScreen(config.name, self._pr_cache[config.name]))

        def _on_push_choice(self, config: RepoConfig, choice: Optional[str]) -> None:
            if choice not in ("push", "dry"):
                return
            dry = choice == "dry"
            self._log_line(f"{'Dry-run' if dry else 'Push'} of {config.name} started…")
            self.run_worker(partial(self._do_push, config, dry), thread=True)

        def _do_push(self, config: RepoConfig, dry: bool) -> None:
            result = self.syncer.push_repo(config, dry_run=dry, update_profile=False)
            marker = "✓" if result.success else "✗"
            line = f"{marker} {config.name}: {result.message}"
            if result.record.skipped_files:
                line += f" (skipped by rules: {len(result.record.skipped_files)})"
            self.call_from_thread(self._log_line, line)
            row, prs = build_row(self.syncer, config)
            if row[-1] != "-":
                self.call_from_thread(self._store_prs, config.name, prs)
            self.call_from_thread(self._refresh_row, row)

        # ---- pull action ---------------------------------------------------

        def action_pull_repo(self) -> None:
            config = self._selected_repo()
            if config is None:
                self._log_line("No repo selected (row not in registry?).")
                return
            self._log_line(f"Pulling {config.name}…")
            self.run_worker(partial(self._do_pull, config), thread=True)

        def _do_pull(self, config: RepoConfig) -> None:
            result = self.syncer.pull_repo(config)
            marker = "✓" if result.success else "✗"
            self.call_from_thread(self._log_line, f"{marker} {config.name}: {result.message}")
            row, prs = build_row(self.syncer, config)
            if row[-1] != "-":
                self.call_from_thread(self._store_prs, config.name, prs)
            self.call_from_thread(self._refresh_row, row)

        def _refresh_row(self, row: tuple) -> None:
            """Replace one repo's row in the source of truth and redraw the
            tree, so a post-push refresh doesn't clobber an expanded folder."""
            name = row[0]
            self._rows = [r for r in self._rows if r[0] != name] + [row]
            self._rebuild_table()

        # ---- add action --------------------------------------------------

        def action_add_repo(self) -> None:
            registered = {config.name for config in self.registry.repos}
            candidates = sorted(
                item.name
                for item in self.local_base.iterdir()
                if item.is_dir() and not item.name.startswith(".") and item.name not in registered
            ) if self.local_base.exists() else []
            self.push_screen(AddRepoScreen(candidates, self.local_base), self._on_add_submit)

        def _on_add_submit(self, data: Optional[dict]) -> None:
            if not data:
                return
            path: Path = data["path"]
            name = path.name
            if self.registry.get_repo(name):
                self._log_line(f"{name} is already registered.")
                return
            defaults = self.registry.push_rules.defaults if self.registry.push_rules else {}
            owner = defaults.get("owner", "Panbear1983")
            branch = detect_branch(path)
            config = RepoConfig(
                name=name,
                local_path=str(path),
                remote=f"{owner}/{name}",
                default_branch=branch,
                push_branch=branch,
                visibility=data["visibility"],
                profile_section=None,
                profile_description=data["description"] or None,
            )
            self.registry.add_repo(config)
            self._log_line(f"Registered {name} ({data['visibility']}); creating remote + pushing…")
            self.run_worker(partial(self._do_push, config, False), thread=True)

    return RepoDashboard


def run_dashboard(config_dir: Path, data_dir: Path) -> None:
    app_cls = make_app()
    app_cls(config_dir, data_dir).run()
