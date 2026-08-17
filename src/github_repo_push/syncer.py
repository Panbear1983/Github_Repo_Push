from __future__ import annotations

import time
from dataclasses import dataclass
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
        """Diff range covering everything a push would publish."""
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
        files = git_repo.run(["diff", "--name-only", rng], check=False).stdout
        env = os.environ.copy()
        env["STAGED_FILES"] = files
        result = sp.run(
            ["bash", str(script), "git", "diff", rng],
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