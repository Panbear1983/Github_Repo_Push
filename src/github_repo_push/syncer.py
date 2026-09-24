from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.github_api import GitHubAPI, get_github_api
from github_repo_push.ignore import filter_paths
from github_repo_push.profile_markdown import apply_profile_entry
from github_repo_push.readme_gen import ensure_readme, find_readme, infer_description
from github_repo_push.models import (
    PushRecord,
    PushStatus,
    RepoConfig,
    RepoState,
    RepoVisibility,
    SyncStatus,
    GitDiffStat,
)
from github_repo_push.registry import Registry


@dataclass
class PushResult:
    success: bool
    record: PushRecord
    message: str


@dataclass
class PullResult:
    success: bool
    message: str
    commits_pulled: int = 0


@dataclass
class ReconcileReport:
    """Diff between the registry and the live GitHub account.

    This is the check that would have caught Family_Budget_Agent sitting in
    repos.yaml as UNTRACKED after it was deleted outside ghrp: `orphaned` is
    a registered repo whose remote no longer exists, `unregistered` is a real
    GitHub repo nobody ever added to repos.yaml.
    """

    orphaned: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def is_clean(self) -> bool:
        return not self.orphaned and not self.unregistered and not self.error


@dataclass
class DriftEntry:
    """One repo's line in a drift sweep."""

    repo: str
    sync_status: SyncStatus
    dirty_files: int = 0
    pushed: bool = False
    commits_pushed: int = 0
    error: Optional[str] = None

    @property
    def needs_attention(self) -> bool:
        return bool(self.error) or self.dirty_files > 0 or self.sync_status in (
            SyncStatus.BEHIND,
            SyncStatus.DIVERGED,
        )

    def summary(self) -> str:
        if self.error:
            return f"FAILED — {self.error}"
        if self.sync_status is SyncStatus.BEHIND:
            base = "BEHIND origin"
        elif self.sync_status is SyncStatus.DIVERGED:
            base = "DIVERGED from origin"
        elif self.pushed:
            n = self.commits_pushed
            base = f"PUSHED {n} commit{'s' if n != 1 else ''}"
        elif self.sync_status is SyncStatus.AHEAD:
            base = "AHEAD (not pushed)"
        else:
            base = "synced"
        if self.dirty_files:
            base += f", DIRTY {self.dirty_files}"
        return base


class Syncer:
    def __init__(self, registry: Registry, data_dir: Path):
        self.registry = registry
        self.data_dir = data_dir
        self.history_file = data_dir / "push_history.jsonl"
        owner = "Panbear1983"
        if registry.push_rules and registry.push_rules.defaults:
            owner = registry.push_rules.defaults.get("owner", "Panbear1983")
        self.github_api = get_github_api(owner)

    def check_repo_state(self, config: RepoConfig) -> RepoState:
        """Check current state of a repo (local + remote)."""
        state = RepoState(config=config)
        local_path = config.get_full_local_path()

        # Check local
        state.local_exists = local_path.exists() and (local_path / ".git").exists()
        if state.local_exists:
            git_repo = GitRepo(local_path)
            state.local_branch = git_repo.current_branch()
            state.local_commit = git_repo.get_commit_hash("HEAD")
            state.local_size_kb = git_repo.get_size_kb()
            state.uncommitted_changes = git_repo.has_uncommitted_changes()
            state.sync_status = git_repo.sync_status("origin", config.push_branch)
            # sync_status just fetched; don't fetch again for the diff stat
            state.diff_stat = git_repo.diff_shortstat("origin", config.push_branch, fetch=False)

        # Check remote
        remote_info = self.github_api.get_repo(config.repo_name)
        state.remote_exists = remote_info is not None
        if remote_info:
            state.remote_size_kb = remote_info.size_kb
            state.remote_branch = remote_info.default_branch
            state.remote_pushed_at = remote_info.pushed_at

        state.last_sync_check = datetime.now()
        return state

    def _ignore_patterns(self, config: RepoConfig) -> list[str]:
        """Merge global push-rule ignore patterns with per-repo overrides."""
        patterns: list[str] = []
        if self.registry.push_rules and self.registry.push_rules.defaults:
            patterns.extend(self.registry.push_rules.defaults.get("ignore_patterns") or [])
        patterns.extend(config.ignore_patterns or [])
        return patterns

    def _profile_repo_name(self) -> str:
        if self.registry.push_rules and self.registry.push_rules.defaults:
            return self.registry.push_rules.defaults.get("profile_repo", "Panbear1983")
        return "Panbear1983"

    def _default_commit_message(self) -> str:
        template = "chore: automated update {timestamp}"
        if self.registry.push_rules and self.registry.push_rules.defaults:
            template = self.registry.push_rules.defaults.get("commit_message_template", template)
        return template.format(timestamp=datetime.now().isoformat())

    def _outgoing_range(self, git_repo: GitRepo, branch: str) -> str:
        """Commit range covering everything a push would publish."""
        if git_repo.get_commit_hash(f"origin/{branch}"):
            return f"origin/{branch}..HEAD"
        empty_tree = git_repo.run(["hash-object", "-t", "tree", "/dev/null"]).stdout.strip()
        return f"{empty_tree}..HEAD"

    def _secret_scan(self, git_repo: GitRepo, branch: str) -> None:
        """Block the push if the outgoing diff trips the secret-scan guardrail."""
        import os
        import subprocess as sp
        script = Path(__file__).resolve().parents[2] / "scripts" / "secret_scan.sh"
        if not script.exists():
            raise RuntimeError(f"Secret-scan script missing: {script} (refusing to push a public repo unscanned)")
        rng = self._outgoing_range(git_repo, branch)

        # Scan PER COMMIT (`log -p`), not endpoint-to-endpoint (`diff A..B`).
        # A two-dot diff shows only the NET change, so a secret added in one
        # commit and removed in the next is invisible to it — while both commits
        # still get pushed and the secret lives in the remote's history forever.
        # This is not hypothetical: on 2026-08-28 a key-shaped literal rode into
        # this repo's own public history exactly that way, because ghrp commits
        # before it scans and the follow-up commit cancelled it out of the diff.
        files = git_repo.run(
            ["log", "--pretty=format:", "--name-only", rng], check=False
        ).stdout
        env = os.environ.copy()
        env["STAGED_FILES"] = files
        result = sp.run(
            ["bash", str(script), "git", "log", "-p", "--no-color", rng],
            cwd=git_repo.path, env=env, text=True, capture_output=True,
        )
        if result.returncode != 0:
            detail = ((result.stdout or "") + (result.stderr or "")).strip()
            raise RuntimeError(f"Secret-scan guardrail blocked push:\n{detail}")

    def push_repo(
        self,
        config: RepoConfig,
        message: Optional[str] = None,
        dry_run: bool = False,
        force: bool = False,
        skip_profile: bool = False,
        auto_commit: bool = True,
        update_profile: bool = False,
    ) -> PushResult:
        """Push a single repo with full workflow.

        Dry-run is strictly read-only: nothing is staged, committed, created,
        or pushed — the record describes what a real run would do.
        """
        start_time = time.time()
        local_path = config.get_full_local_path()

        record = PushRecord(
            repo=config.name,
            local_path=str(local_path),
            remote=config.remote,
            branch=config.push_branch,
            dry_run=dry_run,
        )

        try:
            if not local_path.exists():
                raise RuntimeError(f"Local path does not exist: {local_path}")

            if config.require_pr and config.push_branch in config.protected_branches:
                raise RuntimeError(
                    f"Branch '{config.push_branch}' is protected and require_pr is set; direct push refused"
                )

            git_repo = GitRepo(local_path)
            if not git_repo.is_repo():
                if dry_run:
                    record.status = PushStatus.DRY_RUN
                    record.message = "Would git init and create remote repo"
                    self._save_history(record)
                    return PushResult(True, record, record.message)
                git_repo.init()

            owner = config.owner
            git_repo.ensure_identity(owner, f"{owner}@users.noreply.github.com")

            # Ensure remote
            remote_url = git_repo.get_remote_url("origin")
            if not remote_url:
                if self.github_api.repo_exists(config.repo_name):
                    if not dry_run:
                        git_repo.add_remote("origin", f"https://github.com/{config.remote}.git")
                else:
                    if dry_run:
                        record.status = PushStatus.DRY_RUN
                        record.message = "Would create remote repo"
                        self._save_history(record)
                        return PushResult(True, record, "Dry run: would create remote repo")
                    self.github_api.create_repo(config, local_path)
                    git_repo.add_remote("origin", f"https://github.com/{config.remote}.git")

            record.size_before_kb = git_repo.get_size_kb()

            # Ensure a README exists before publishing (ported from
            # Github_Push_Automator). Dry-run only reports what it would do.
            readme_description_text = config.profile_description
            if auto_commit:
                if dry_run:
                    if find_readme(local_path) is None:
                        record.message = "Would generate README.md; "
                else:
                    _, readme_description_text, _ = ensure_readme(
                        local_path, config.repo_name, config.profile_description
                    )

            # Stage and commit, honoring ignore patterns. Dry-run only inspects.
            committed = False
            if auto_commit:
                dirty = git_repo.list_dirty_files()
                allowed, skipped = filter_paths(dirty, self._ignore_patterns(config))
                record.skipped_files = skipped
                commit_msg = message or self._default_commit_message()
                if dry_run:
                    committed = bool(allowed)
                    if committed:
                        record.commit_message = commit_msg
                        record.message = (record.message or "") + f"Would commit {len(allowed)} file(s), skip {len(skipped)}"
                else:
                    if allowed:
                        git_repo.stage_files(allowed)
                    committed = git_repo.commit(commit_msg)
                    if committed:
                        record.commit_message = commit_msg
            record.commit_sha = git_repo.get_commit_hash("HEAD")

            # Decide whether pushing is needed and safe
            sync = git_repo.sync_status("origin", config.push_branch)
            if sync == SyncStatus.SYNCED and not committed:
                record.status = PushStatus.SKIPPED
                record.message = "Nothing to push (synced, no new changes)"
                record.duration_ms = int((time.time() - start_time) * 1000)
                self._save_history(record)
                return PushResult(True, record, record.message)
            if sync in (SyncStatus.BEHIND, SyncStatus.DIVERGED) and not force:
                raise RuntimeError(
                    f"Local is {sync.value} relative to origin/{config.push_branch}; "
                    "pull/resolve manually or rerun with --force (force-with-lease)"
                )

            if dry_run:
                record.status = PushStatus.DRY_RUN
                if not record.message:
                    record.message = f"Would push ({sync.value})"
            else:
                if config.visibility == RepoVisibility.PUBLIC.value:
                    self._secret_scan(git_repo, config.push_branch)
                git_repo.push("origin", config.push_branch, force=force)
                record.status = PushStatus.SUCCESS

                # Surgical per-push profile entry (opt-in via --update-profile)
                if update_profile and not skip_profile and config.profile_section:
                    description = readme_description_text or infer_description(local_path, config.repo_name)
                    record.triggered_profile_update = apply_profile_entry(
                        owner=config.owner,
                        profile_repo=self._profile_repo_name(),
                        section=config.profile_section,
                        repo_name=config.repo_name,
                        description=description,
                    )

            record.size_after_kb = git_repo.get_size_kb()
            record.duration_ms = int((time.time() - start_time) * 1000)
            self._save_history(record)
            return PushResult(True, record, record.message or f"Push {'simulated' if dry_run else 'completed'} successfully")

        except Exception as e:
            record.status = PushStatus.FAILED
            record.error = str(e)
            record.message = str(e)
            record.duration_ms = int((time.time() - start_time) * 1000)
            self._save_history(record)
            return PushResult(False, record, f"Push failed: {e}")

    def drift_sweep(self, dry_run: bool = False, push: bool = True) -> list[DriftEntry]:
        """Report drift across the fleet, pushing only work that is already committed.

        This is the unattended path used by the daily routine, so it never commits
        on the user's behalf — `push_repo(auto_commit=False)` skips all staging and
        commit logic and publishes existing commits only.

        Note this deliberately does NOT reuse `push_all`, which skips dirty repos
        wholesale: a repo that is both dirty *and* ahead would never get its
        committed work published. Here dirt is reported but does not block the push.
        BEHIND/DIVERGED keep push_repo's existing refusal and are never forced.
        """
        entries: list[DriftEntry] = []
        for config in self.registry.repos:
            if not config.enabled:
                continue
            entry = DriftEntry(repo=config.name, sync_status=SyncStatus.UNTRACKED)
            try:
                state = self.check_repo_state(config)
                entry.sync_status = state.sync_status
                if not state.local_exists:
                    entry.error = "local path missing"
                    entries.append(entry)
                    continue

                git_repo = GitRepo(config.get_full_local_path())
                entry.dirty_files = len(git_repo.list_dirty_files())

                if state.sync_status is SyncStatus.AHEAD:
                    entry.commits_pushed = self._count_outgoing(git_repo, config.push_branch)
                    if push and not dry_run:
                        result = self.push_repo(config, auto_commit=False, skip_profile=True)
                        entry.pushed = result.success
                        if not result.success:
                            entry.error = result.record.error or result.message
            except Exception as e:  # a single bad repo must not abort the sweep
                entry.error = str(e)
            entries.append(entry)
        return entries

    def pull_repo(self, config: RepoConfig) -> PullResult:
        """Bring a repo's local branch up to date with origin — fast-forward only.

        Mirrors push_repo's caution in the opposite direction: refuses outright
        on a dirty worktree (would risk clobbering uncommitted edits on merge)
        and on anything that isn't a clean ancestor relationship (DIVERGED),
        rather than ever creating a merge commit or force-touching local state.
        """
        local_path = config.get_full_local_path()
        if not local_path.exists():
            return PullResult(False, f"Local path does not exist: {local_path}")

        git_repo = GitRepo(local_path)
        if not git_repo.is_repo():
            return PullResult(False, "Not a git repo yet — nothing to pull.")

        sync = git_repo.sync_status("origin", config.push_branch)
        if sync == SyncStatus.SYNCED:
            return PullResult(True, "Already up to date.")
        if sync == SyncStatus.AHEAD:
            return PullResult(True, "Local is ahead of origin — nothing to pull.")
        if sync == SyncStatus.UNTRACKED:
            return PullResult(False, "No local commits yet on this branch — nothing to fast-forward.")
        if sync == SyncStatus.DIVERGED:
            return PullResult(
                False,
                "Local and origin have both changed (diverged) — a fast-forward "
                "isn't possible here; this needs a manual merge.",
            )

        # BEHIND: the only case a fast-forward can actually happen.
        if git_repo.has_uncommitted_changes():
            return PullResult(
                False,
                "Working tree has uncommitted changes — commit or stash them first "
                "so pulling can't clobber anything.",
            )
        incoming = self._count_incoming(git_repo, config.push_branch)
        try:
            git_repo.merge_ff_only("origin", config.push_branch)
        except RuntimeError as exc:
            return PullResult(False, f"Pull failed: {exc}")
        return PullResult(True, f"Pulled {incoming} commit{'s' if incoming != 1 else ''} from origin.",
                          commits_pulled=incoming)

    def _count_incoming(self, git_repo: GitRepo, branch: str) -> int:
        """How many commits origin/<branch> has that the local branch does not."""
        result = git_repo.run(["rev-list", "--count", f"HEAD..origin/{branch}"], check=False)
        try:
            return int(result.stdout.strip())
        except (ValueError, AttributeError):
            return 0

    def reconcile(self) -> ReconcileReport:
        """Compare the registry against the live GitHub account.

        Read-only: never touches repos.yaml or GitHub. A single `gh repo list`
        call, then a name diff against the registry — this is the piece that
        was missing before, which is why a repo deleted outside ghrp just sat
        there as UNTRACKED instead of ever being flagged on its own.
        """
        live_repos = self.github_api.list_repos()
        if not live_repos:
            # `list_repos` returns [] both for "account genuinely has zero
            # repos" and "the gh call failed" — Peter's account is never
            # empty, so treat this as inconclusive rather than reporting
            # every registered repo as deleted.
            return ReconcileReport(error="Could not reach GitHub (gh call returned no repos) — skipped reconcile check")

        live_names = {r.name for r in live_repos}
        registered_names = {c.repo_name for c in self.registry.repos}

        orphaned = sorted(
            c.name for c in self.registry.repos
            if c.enabled and c.owner == self.github_api.owner and c.repo_name not in live_names
        )
        unregistered = sorted(live_names - registered_names)

        return ReconcileReport(orphaned=orphaned, unregistered=unregistered)

    def _count_outgoing(self, git_repo: GitRepo, branch: str) -> int:
        """How many commits the local branch has that origin/<branch> does not."""
        result = git_repo.run(
            ["rev-list", "--count", f"origin/{branch}..HEAD"], check=False
        )
        try:
            return int(result.stdout.strip())
        except (ValueError, AttributeError):
            return 0

    def push_all(
        self,
        only_changed: bool = False,
        parallel: int = 3,
        dry_run: bool = False,
        message: Optional[str] = None,
        commit: bool = False,
    ) -> list[PushResult]:
        """Push multiple repos.

        Dirty repos are skipped unless `commit` is set — auto-committing a
        whole fleet is opt-in, per-repo pushes stay deliberate.
        """
        results = []
        for config in self.registry.repos:
            if not config.enabled:
                continue
            state = self.check_repo_state(config)
            if only_changed and state.sync_status == SyncStatus.SYNCED and not state.uncommitted_changes:
                continue
            if state.uncommitted_changes and not commit:
                record = PushRecord(
                    repo=config.name,
                    local_path=str(config.get_full_local_path()),
                    remote=config.remote,
                    branch=config.push_branch,
                    dry_run=dry_run,
                    status=PushStatus.SKIPPED,
                    message="Dirty worktree skipped (rerun with --commit to auto-commit)",
                )
                results.append(PushResult(True, record, record.message))
                continue
            result = self.push_repo(config, message=message, dry_run=dry_run, auto_commit=commit)
            results.append(result)
        return results

    def _save_history(self, record: PushRecord) -> None:
        """Append push record to history file."""
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "a") as f:
            f.write(record.model_dump_json() + "\n")

    def load_history(self, repo: Optional[str] = None, limit: Optional[int] = None) -> list[PushRecord]:
        """Load push history from file."""
        if not self.history_file.exists():
            return []
        records = []
        with open(self.history_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = PushRecord.model_validate_json(line)
                    if repo is None or record.repo == repo:
                        records.append(record)
                except Exception:
                    continue
        records.sort(key=lambda r: r.timestamp)
        if limit:
            records = records[-limit:]
        return records

    def get_recent_pushes(self, repo: str, count: int = 10) -> list[PushRecord]:
        """Get recent pushes for a repo."""
        return self.load_history(repo=repo, limit=count)