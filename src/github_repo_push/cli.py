"""Main CLI entry point for github-repo-push."""

import os

import click

from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer
from pathlib import Path


# Canonical config lives in this repo's config/ directory (version-controlled).
# Runtime data (push history) stays under the Hermes orchestrator profile.
# Both are overridable for tests/automation via environment variables.
_REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("GHRP_CONFIG_DIR", _REPO_ROOT / "config"))
DATA_DIR = Path(
    os.environ.get(
        "GHRP_DATA_DIR",
        Path.home() / ".hermes" / "profiles" / "orchestrator" / "github_repo_push" / "data",
    )
)


@click.group()
def cli():
    """Github Repo Push - Manage and sync your GitHub repositories."""
    pass


@cli.command()
def registry_init():
    """Initialize the repository registry by scanning local and remote repos."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    registry = Registry(CONFIG_DIR)
    repos = registry.init_from_scan(Path.home() / "Desktop" / "Old_Projects" / "GitHub")
    click.echo(f"Initialized registry with {len(repos)} repositories.")
    for repo in repos:
        click.echo(f"  - {repo.name} ({repo.remote})")


@cli.command()
def registry_list():
    """List all registered repositories."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    for repo in registry.repos:
        click.echo(f"{repo.name}: {repo.local_path} -> {repo.remote}")


@cli.command()
def status_all():
    """Show sync status of all repositories."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    for config in registry.repos:
        if not config.enabled:
            click.echo(f"{config.name}: disabled (skipped)")
            continue
        state = syncer.check_repo_state(config)
        click.echo(f"{config.name}: {state.sync_status.value} (local: {state.local_branch}, remote: {state.remote_branch})")


@cli.command()
@click.argument("repo_name")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
@click.option("--message", "-m", help="Commit message to use.")
@click.option("--force", is_flag=True, help="Force push with --force-with-lease.")
@click.option("--skip-profile", is_flag=True, help="Skip updating the profile README.")
@click.option("--update-profile", is_flag=True, help="Surgically update this repo's profile README entry after pushing.")
def push(repo_name, dry_run, message, force, skip_profile, update_profile):
    """Push a single repository."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    config = registry.get_repo(repo_name)
    if not config:
        click.echo(f"Error: Repository '{repo_name}' not found in registry.", err=True)
        raise click.Abort()
    result = syncer.push_repo(config, message=message, dry_run=dry_run, force=force, skip_profile=skip_profile, update_profile=update_profile)
    if result.success:
        click.echo(f"✓ {result.message}")
        if result.record.dry_run:
            click.echo("  (This was a dry run - no changes were made)")
    else:
        click.echo(f"✗ {result.message}", err=True)
        raise click.Abort()


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
@click.option("--message", "-m", help="Commit message to use.")
@click.option("--only-changed", is_flag=True, help="Only push repositories that have changes.")
@click.option("--commit", is_flag=True, help="Auto-commit dirty worktrees (default: skip them).")
@click.option("--parallel", default=1, help="Number of repositories to push in parallel.")
def push_all(dry_run, message, only_changed, commit, parallel):
    """Push all repositories. Dirty repos are skipped unless --commit is given."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    results = syncer.push_all(dry_run=dry_run, message=message, only_changed=only_changed, commit=commit, parallel=parallel)
    success_count = sum(1 for r in results if r.success)
    fail_count = len(results) - success_count
    click.echo(f"Push complete: {success_count} succeeded, {fail_count} failed.")
    for result in results:
        if not result.success:
            click.echo(f"  ✗ {result.record.repo}: {result.message}", err=True)
        elif result.record.dry_run:
            click.echo(f"  → {result.record.repo}: {result.message} (dry run)")
        else:
            click.echo(f"  ✓ {result.record.repo}: {result.message}")


@cli.command()
def profile_preview():
    """Preview the profile README without pushing."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    from github_repo_push.profile_readme import preview_profile_readme
    content = preview_profile_readme(registry)
    click.echo(content, nl=False)


@cli.command()
@click.option("--push", is_flag=True, help="Push the updated profile README to GitHub.")
def profile_update(push):
    """Update the profile README."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    from github_repo_push.profile_readme import update_profile_readme
    success, message = update_profile_readme(registry, dry_run=not push)
    if success:
        click.echo(f"✓ {message}")
        if push:
            click.echo("  Profile README pushed to GitHub.")
    else:
        click.echo(f"✗ {message}", err=True)
        raise click.Abort()


@cli.command()
@click.argument("path", required=False, default=".")
@click.option("--description", help="Description used if a README must be generated.")
def ensure_readme(path, description):
    """Create a README.md for PATH (default: current directory) if missing."""
    from github_repo_push.readme_gen import ensure_readme as _ensure_readme

    local = Path(path).expanduser().resolve()
    if local.is_file():
        local = local.parent
    if not local.exists():
        click.echo(f"Error: path does not exist: {local}", err=True)
        raise click.Abort()
    readme, final_description, created = _ensure_readme(local, local.name, description)
    click.echo(f"README {'created' if created else 'already present'}: {readme}")
    click.echo(f"Description: {final_description}")


@cli.command()
@click.argument("path", required=False, default=".")
@click.option("--description", help="Description for README/profile entry.")
@click.option("--visibility", type=click.Choice(["public", "private"]), default="public", help="Visibility if the GitHub repo must be created.")
@click.option("--message", "-m", help="Commit message to use.")
@click.option("--section", default="Applied Automation", help="Profile README section for the entry.")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
@click.option("--skip-profile", is_flag=True, help="Push only; do not touch the profile README.")
@click.option("--register", is_flag=True, help="Add this repo to the registry after a successful push.")
def adhoc(path, description, visibility, message, section, dry_run, skip_profile, register):
    """Push an arbitrary local repo (default: the one you're standing in).

    Registry-less one-off push, ported from Github_Push_Automator: ensures
    git repo + README, commits (honoring global ignore patterns), creates or
    reuses the GitHub repo, pushes, and surgically updates the profile README
    entry unless --skip-profile is given.
    """
    from github_repo_push.git_ops import GitRepo
    from github_repo_push.models import RepoConfig

    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)

    local = Path(path).expanduser().resolve()
    if local.is_file():
        local = local.parent
    if not local.exists():
        click.echo(f"Error: path does not exist: {local}", err=True)
        raise click.Abort()

    git_repo = GitRepo(local)
    toplevel = git_repo.run(["rev-parse", "--show-toplevel"], check=False)
    if toplevel.returncode == 0 and toplevel.stdout.strip():
        local = Path(toplevel.stdout.strip()).resolve()
        git_repo = GitRepo(local)

    branch = "main"
    if git_repo.is_repo():
        current = git_repo.run(["branch", "--show-current"], check=False).stdout.strip()
        if current:
            branch = current

    defaults = registry.push_rules.defaults if registry.push_rules else {}
    owner = defaults.get("owner", "Panbear1983")
    config = registry.get_repo(local.name) or RepoConfig(
        name=local.name,
        local_path=str(local),
        remote=f"{owner}/{local.name}",
        default_branch=branch,
        push_branch=branch,
        visibility=visibility,
        profile_section=section,
        profile_description=description,
    )

    result = syncer.push_repo(
        config,
        message=message,
        dry_run=dry_run,
        skip_profile=skip_profile,
        update_profile=not skip_profile,
    )
    if result.success:
        click.echo(f"✓ {result.message}")
        if result.record.skipped_files:
            click.echo(f"  Skipped by ignore rules: {', '.join(result.record.skipped_files)}")
        if result.record.triggered_profile_update:
            click.echo("  Profile README entry updated.")
        if register and not dry_run and not registry.get_repo(config.name):
            registry.add_repo(config)
            click.echo(f"  Registered {config.name} in {registry.repos_file}")
    else:
        click.echo(f"✗ {result.message}", err=True)
        raise click.Abort()


@cli.command()
def dashboard():
    """Launch the textual TUI dashboard.

    Rows appear as each repo's state arrives (states are fetched in parallel
    in a background worker), with progress shown in the header — the screen
    is never a silent blank while remotes are being queried.
    """
    try:
        from textual.app import App
        from textual.widgets import Header, Footer, DataTable
    except ImportError:
        click.echo("Textual not installed. Install with: pip install textual")
        return

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import datetime as _dt

    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    status_priority = {"error": 0, "diverged": 1, "behind": 2, "ahead": 3, "untracked": 4, "synced": 5}

    def fetch_row(config) -> tuple:
        try:
            state = syncer.check_repo_state(config)
            pushes = syncer.get_recent_pushes(config.name, count=1)
            last_push = pushes[-1].timestamp.strftime("%Y-%m-%d %H:%M") if pushes else "Never"
            status = state.sync_status.value if config.enabled else f"{state.sync_status.value} (disabled)"
            return (
                config.name,
                status,
                "yes" if state.uncommitted_changes else "",
                state.local_branch or "",
                state.remote_branch or "",
                str(state.local_size_kb),
                last_push,
            )
        except Exception as exc:  # noqa: BLE001 - a bad repo must not kill the dashboard
            return (config.name, "error", "", "", "", "0", str(exc)[:60])

    class RepoDashboard(App):
        TITLE = "Github Repo Push"

        def compose(self):
            yield Header()
            yield DataTable()
            yield Footer()

        def on_mount(self):
            table = self.query_one(DataTable)
            table.add_columns("Repo", "Status", "Dirty", "Local Branch", "Remote Branch", "Size (KB)", "Last Push")
            self.sub_title = f"loading 0/{len(registry.repos)} repos…"
            self.run_worker(self._load_rows, thread=True)

        def _load_rows(self):
            table = self.query_one(DataTable)
            rows: list[tuple] = []
            with ThreadPoolExecutor(max_workers=6) as pool:
                futures = [pool.submit(fetch_row, config) for config in registry.repos]
                for future in as_completed(futures):
                    row = future.result()
                    rows.append(row)
                    self.call_from_thread(table.add_row, *row)
                    self.call_from_thread(self._show_progress, len(rows))
            rows.sort(key=lambda r: (status_priority.get(r[1].split()[0], 99), r[0].lower()))
            self.call_from_thread(self._show_final, table, rows)

        def _show_progress(self, done: int):
            self.sub_title = f"loading {done}/{len(registry.repos)} repos…"

        def _show_final(self, table, rows: list[tuple]):
            table.clear()
            for row in rows:
                table.add_row(*row)
            self.sub_title = f"{len(rows)} repos · updated {_dt.now().strftime('%H:%M:%S')}"

    app = RepoDashboard()
    app.run()


if __name__ == "__main__":
    cli()