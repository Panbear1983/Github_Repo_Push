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
@click.argument("repo_name")
def pull(repo_name):
    """Pull a single repository — fast-forward only.

    Refuses on a dirty worktree or on anything that isn't a clean
    fast-forward (diverged history); never force-merges, never touches
    uncommitted changes.
    """
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    config = registry.get_repo(repo_name)
    if not config:
        click.echo(f"Error: Repository '{repo_name}' not found in registry.", err=True)
        raise click.Abort()
    result = syncer.pull_repo(config)
    if result.success:
        click.echo(f"✓ {result.message}")
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
@click.argument("repo_name", required=False)
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON instead of a table.")
def prs(repo_name, as_json):
    """List open pull requests on your registered repos (read-only, on-demand).

    Not part of the daily drift report — run this manually whenever you want
    to check for PRs opened by others against your repos.
    """
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)

    if repo_name:
        config = registry.get_repo(repo_name)
        if not config:
            click.echo(f"Error: Repository '{repo_name}' not found in registry.", err=True)
            raise click.Abort()
        targets = [config]
    else:
        targets = [c for c in registry.repos if c.enabled]

    results = {}
    had_error = False
    for config in targets:
        try:
            results[config.name] = syncer.github_api.list_open_prs(config.repo_name)
        except Exception as exc:
            results[config.name] = None
            had_error = True
            click.echo(f"{config.name}: error fetching PRs — {exc}", err=True)

    if as_json:
        import json as _json
        click.echo(_json.dumps({
            name: ([{"number": pr.number, "title": pr.title, "author": pr.author,
                      "url": pr.url, "branch": pr.head_branch, "draft": pr.is_draft}
                     for pr in prs] if prs is not None else None)
            for name, prs in results.items()
        }, indent=2))
    else:
        total = 0
        for name, prs in results.items():
            if not prs:
                continue
            click.echo(f"{name}:")
            for pr in prs:
                draft = " (draft)" if pr.is_draft else ""
                click.echo(f"  #{pr.number} {pr.title}{draft} — by {pr.author} [{pr.head_branch}]")
                click.echo(f"      {pr.url}")
            total += len(prs)
        if total == 0 and not had_error:
            scope = f"on {repo_name}" if repo_name else "on any registered repo"
            click.echo(f"No open pull requests {scope}.")

    if had_error:
        raise SystemExit(1)


@cli.command()
def profile_preview():
    """Preview the profile README without pushing.

    Touches the network: public repos' badges (pushed date, open PRs) are
    fetched live, so this is no longer purely local/instant.
    """
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    from github_repo_push.profile_readme import preview_profile_readme
    content = preview_profile_readme(registry, syncer)
    click.echo(content, nl=False)


@cli.command()
@click.option("--push", is_flag=True, help="Push the updated profile README to GitHub.")
def profile_update(push):
    """Update the profile README."""
    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    from github_repo_push.profile_readme import update_profile_readme
    success, message = update_profile_readme(registry, syncer, dry_run=not push)
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
    """Launch the Textual TUI control panel.

    Rows stream in as repo states arrive; key bindings push and add repos
    through the same guarded Syncer path as the CLI (p = push, u = pull
    (fast-forward only), a = add, enter = expand/collapse a repo's folders
    or reveal a file in Finder, / = search every repo for a name,
    o = open PRs, r = refresh, q = quit).
    """
    try:
        import textual  # noqa: F401
    except ImportError:
        click.echo("Textual not installed. Install with: pip install textual")
        return

    from github_repo_push.tui_app import run_dashboard

    run_dashboard(CONFIG_DIR, DATA_DIR)


@cli.command()
@click.option("--dry-run", is_flag=True, help="Report drift without pushing anything.")
@click.option("--no-push", is_flag=True, help="Report only; never push, even when ahead.")
@click.option("--no-notify", is_flag=True, help="Skip the Telegram report.")
def drift(dry_run, no_push, no_notify):
    """Report fleet drift and push work that is already committed.

    This is the unattended daily path. It never commits on your behalf: only
    repos whose local branch is AHEAD of origin get pushed, and dirty worktrees
    are reported, not staged. BEHIND/DIVERGED repos are reported, never forced.
    """
    from github_repo_push.notify import format_report, format_reconcile, send

    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)

    entries = syncer.drift_sweep(dry_run=dry_run, push=not no_push)
    report = format_report(entries)

    # Registry-vs-GitHub check rides along on every drift run so a repo
    # deleted or created outside ghrp gets flagged here automatically,
    # instead of only surfacing later as a confusing UNTRACKED row.
    reconcile_report = syncer.reconcile()
    full_report = report + "\n\n" + format_reconcile(reconcile_report)
    click.echo(full_report)

    if not no_notify and not dry_run:
        if send(full_report, DATA_DIR):
            click.echo("\nTelegram report sent.")
        else:
            click.echo("\nTelegram not configured (set GHRP_TELEGRAM_BOT_TOKEN / "
                       "GHRP_TELEGRAM_CHAT_ID) — report shown above only.")

    failures = [e for e in entries if e.error]
    if failures:
        raise SystemExit(1)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON instead of a plain report.")
@click.option("--prune", is_flag=True, help="Remove orphaned entries (repo gone on GitHub) from the registry.")
def reconcile(as_json, prune):
    """Compare the registry against your live GitHub account.

    Read-only by default. Flags two things `ghrp` could not see on its own
    before: a registered repo whose GitHub remote is gone (orphaned — e.g. it
    was deleted outside ghrp), and a real repo on your account that was never
    added to the registry (unregistered), so it never gets included in
    push-all/drift at all.
    """
    from github_repo_push.notify import format_reconcile

    registry = Registry(CONFIG_DIR)
    registry.load()
    syncer = Syncer(registry, DATA_DIR)
    result = syncer.reconcile()

    if as_json:
        import json as _json
        click.echo(_json.dumps(
            {"orphaned": result.orphaned, "unregistered": result.unregistered, "error": result.error},
            indent=2,
        ))
    else:
        click.echo(format_reconcile(result))

    if prune and result.orphaned:
        for name in result.orphaned:
            registry.remove_repo(name)
        click.echo(f"\nRemoved {len(result.orphaned)} orphaned "
                   f"entr{'y' if len(result.orphaned) == 1 else 'ies'} from {registry.repos_file}")

    if result.error or (result.orphaned and not prune):
        raise SystemExit(1)


@cli.group()
def routine():
    """Manage the daily drift job (launchd)."""


@routine.command("on")
@click.option("--at", "at_time", default="09:00", help="Local time to run, HH:MM (default 09:00).")
def routine_on(at_time):
    """Install and load the daily drift job."""
    from github_repo_push import routine as routine_mod

    try:
        hour, minute = (int(part) for part in at_time.split(":", 1))
    except ValueError:
        raise click.BadParameter(f"--at must be HH:MM, got {at_time!r}")

    routine_mod.install(_REPO_ROOT, hour=hour, minute=minute)
    click.echo(f"Routine installed: {routine_mod.status().describe()}")
    click.echo(f"  plist: {routine_mod.PLIST_PATH}")


@routine.command("off")
def routine_off():
    """Unload and remove the daily drift job."""
    from github_repo_push import routine as routine_mod

    if routine_mod.uninstall():
        click.echo("Routine removed.")
    else:
        click.echo("Routine was not installed.")


@routine.command("status")
def routine_status():
    """Show whether the daily drift job is installed and loaded."""
    from github_repo_push import routine as routine_mod

    click.echo(f"Routine: {routine_mod.status().describe()}")


if __name__ == "__main__":
    cli()