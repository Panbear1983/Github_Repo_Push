"""Textual TUI control panel for github-repo-push.

Reflects AND drives the push machinery: rows stream in progressively, and
key bindings run the same guarded Syncer path as the CLI (ignore-pattern
staging, secret scan, protected-branch and diverged refusals, audit log).

Bindings: p = push selected repo (confirm modal with dry-run option),
a = add/register a local repo and publish it (gh repo create, no browser),
r = refresh, q = quit.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.ignore import filter_paths
from github_repo_push.models import RepoConfig
from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer

STATUS_PRIORITY = {"error": 0, "diverged": 1, "behind": 2, "ahead": 3, "untracked": 4, "synced": 5}
COLUMNS = ("Repo", "Status", "Dirty", "Local Branch", "Remote Branch", "Size (KB)", "GitHub Push", "ghrp Push")
DEFAULT_LOCAL_BASE = Path.home() / "Desktop" / "Old_Projects" / "GitHub"


def build_row(syncer: Syncer, config: RepoConfig) -> tuple:
    """One table row for a repo. Network: git fetch + one gh lookup."""
    try:
        state = syncer.check_repo_state(config)
        pushes = syncer.get_recent_pushes(config.name, count=1)
        ghrp_push = pushes[-1].timestamp.strftime("%Y-%m-%d %H:%M") if pushes else "Never"
        if state.remote_pushed_at:
            github_push = state.remote_pushed_at.astimezone().strftime("%Y-%m-%d %H:%M")
        else:
            github_push = "-" if state.remote_exists else "no remote"
        status = state.sync_status.value if config.enabled else f"{state.sync_status.value} (disabled)"
        return (
            config.name,
            status,
            "yes" if state.uncommitted_changes else "",
            state.local_branch or "",
            state.remote_branch or "",
            str(state.local_size_kb),
            github_push,
            ghrp_push,
        )
    except Exception as exc:  # noqa: BLE001 - one bad repo must not kill the dashboard
        return (config.name, "error", "", "", "", "0", "", str(exc)[:60])


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
    from textual.containers import Horizontal, Vertical
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

    class RepoDashboard(App):
        TITLE = "Github Repo Push"
        BINDINGS = [
            Binding("p", "push_repo", "Push repo"),
            Binding("a", "add_repo", "Add repo"),
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
                        row = future.result()
                        rows.append(row)
                        self.call_from_thread(self._add_row, table, row)
                        self.call_from_thread(self._show_progress, len(rows))
                rows.sort(key=lambda r: (STATUS_PRIORITY.get(r[1].split()[0], 99), r[0].lower()))
                self.call_from_thread(self._show_final, table, rows)
            finally:
                self._loading_active = False

        def _add_row(self, table, row: tuple) -> None:
            try:
                table.remove_row(row[0])
            except Exception:
                pass
            table.add_row(*row, key=row[0])

        def _show_progress(self, done: int) -> None:
            self.sub_title = f"loading {done}/{len(self.registry.repos)} repos…"

        def _show_final(self, table, rows: list[tuple]) -> None:
            table.clear()
            for row in rows:
                table.add_row(*row, key=row[0])
            self.sub_title = f"{len(rows)} repos · updated {datetime.now().strftime('%H:%M:%S')}"

        # ---- helpers -----------------------------------------------------

        def _log_line(self, text: str) -> None:
            self.query_one(Log).write_line(f"{datetime.now().strftime('%H:%M:%S')}  {text}")

        def _selected_repo(self) -> Optional[RepoConfig]:
            table = self.query_one(DataTable)
            if table.row_count == 0 or table.cursor_row is None:
                return None
            try:
                from textual.coordinate import Coordinate
                key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key.value
            except Exception:
                return None
            return self.registry.get_repo(str(key))

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
            row = build_row(self.syncer, config)
            self.call_from_thread(self._add_row, self.query_one(DataTable), row)

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
