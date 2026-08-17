"""Main CLI entry point for github-repo-push."""

import click

from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer
from github_repo_push.dashboard import Dashboard
from pathlib import Path


CONFIG_DIR = Path.home() / ".hermes" / "profiles" / "orchestrator" / "github_repo_push"
DATA_DIR = CONFIG_DIR / "data"


@click.group()
def cli():
    """Github Repo Push - Manage and sync your GitHub repositories."""
    pass


@cli.command()
def registry_init():
    """Initialize the repository registry by scanning local and remote repos."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    registry = Registry(CONFIG_DIR / "config")
    repos = registry.init_from_scan(Path.home() / "Desktop" / "Old_Projects" / "GitHub")
    click.echo(f"Initialized registry with {len(repos)} repositories.")
    for repo in repos:
        click.echo(f"  - {repo.name} ({repo.remote})")


@cli.command()
def registry_list():
    """List all registered repositories."""
    registry = Registry(CONFIG_DIR / "config")
    registry.load()
    for repo in registry.repos:
        click.echo(f"{repo.name}: {repo.local_path} -> {repo.remote}")


@cli.command()
def status_all():
    """Show sync status of all repositories."""
    registry = Registry(CONFIG_DIR / "config")
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    for config in registry.repos:
        state = syncer.check_repo_state(config)
        click.echo(f"{config.name}: {state.sync_status.value} (local: {state.local_branch}, remote: {state.remote_branch})")


@cli.command()
@click.argument("repo_name")
@click.option("--dry-run", is_flag=True, help="Show what would be done without making changes.")
@click.option("--message", "-m", help="Commit message to use.")
@click.option("--force", is_flag=True, help="Force push with --force-with-lease.")
@click.option("--skip-profile", is_flag=True, help="Skip updating the profile README.")
def push(repo_name, dry_run, message, force, skip_profile):
    """Push a single repository."""
    registry = Registry(CONFIG_DIR / "config")
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    config = registry.get_repo(repo_name)
    if not config:
        click.echo(f"Error: Repository '{repo_name}' not found in registry.", err=True)
        raise click.Abort()
    result = syncer.push_repo(config, message=message, dry_run=dry_run, force=force, skip_profile=skip_profile)
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
@click.option("--parallel", default=1, help="Number of repositories to push in parallel.")
def push_all(dry_run, message, only_changed, parallel):
    """Push all repositories."""
    registry = Registry(CONFIG_DIR / "config")
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    results = syncer.push_all(dry_run=dry_run, message=message, only_changed=only_changed, parallel=parallel)
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
    registry = Registry(CONFIG_DIR / "config")
    registry.load()
    from github_repo_push.profile_readme import preview_profile_readme
    content = preview_profile_readme(registry)
    click.echo(content)


@cli.command()
@click.option("--push", is_flag=True, help="Push the updated profile README to GitHub.")
def profile_update(push):
    """Update the profile README."""
    registry = Registry(CONFIG_DIR / "config")
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
def dashboard():
    """Launch the textual TUI dashboard."""
    try:
        from textual.app import App
        from textual.widgets import Header, Footer, DataTable
    except ImportError:
        click.echo("Textual not installed. Install with: pip install textual")
        return

    class RepoDashboard(App):
        def compose(self):
            yield Header()
            yield DataTable()
            yield Footer()

        def on_mount(self):
            table = self.query_one(DataTable)
            table.add_columns("Repo", "Status", "Local Branch", "Remote Branch", "Size (KB)", "Last Push")
            registry = Registry(CONFIG_DIR / "config")
            registry.load()
            syncer = Syncer(registry, DATA_DIR)
            dashboard = Dashboard(registry, syncer)
            data = dashboard.refresh()
            for entry in data.entries:
                table.add_row(
                    entry.repo.name,
                    entry.state.sync_status.value,
                    entry.state.local_branch or "",
                    entry.state.remote_branch or "",
                    entry.state.local_size_kb,
                    entry.last_push_str,
                )

    app = RepoDashboard()
    app.run()


if __name__ == "__main__":
    cli()