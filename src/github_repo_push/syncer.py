from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.github_api import GitHubAPI, get_github_api
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
            state.diff_stat = git_repo.diff_shortstat("origin", config.push_branch)

        # Check remote
        remote_info = self.github_api.get_repo(config.repo_name)
        state.remote_exists = remote_info is not None
        if remote_info:
            state.remote_size_kb = remote_info.size_kb
            state.remote_branch = remote_info.default_branch

        state.last_sync_check = datetime.now()
        return state

    def push_repo(
        self,
        config: RepoConfig,
        message: Optional[str] = None,
        dry_run: bool = False,
        force: bool = False,
        skip_profile: bool = False,
    ) -> PushResult:
        """Push a single repo with full workflow."""
        start_time = time.time()
        local_path = config.get_full_local_path()

        # Initialize record
        record = PushRecord(
            repo=config.name,
            local_path=str(local_path),
            remote=config.remote,
            branch=config.push_branch,
            dry_run=dry_run,
        )

        try:
            # Validate
            if not local_path.exists():
                raise RuntimeError(f"Local path does not exist: {local_path}")

            git_repo = GitRepo(local_path)
            if not git_repo.is_repo():
                git_repo.init()

            # Ensure git identity
            owner = config.owner
            git_repo.ensure_identity(owner, f"{owner}@users.noreply.github.com")

            # Ensure remote
            remote_url = git_repo.get_remote_url("origin")
            if not remote_url:
                # Check if remote exists
                if self.github_api.repo_exists(config.repo_name):
                    git_repo.add_remote("origin", f"https://github.com/{config.remote}.git")
                else:
                    if dry_run:
                        record.status = PushStatus.DRY_RUN
                        record.message = "Would create remote repo"
                        return PushResult(True, record, "Dry run: would create remote repo")
                    # Create remote repo
                    self.github_api.create_repo(config, local_path)
                    git_repo.add_remote("origin", f"https://github.com/{config.remote}.git")

            # Get size before
            record.size_before_kb = git_repo.get_size_kb()

            # Stage and commit changes
            commit_msg = message or self.registry.push_rules.defaults.get("commit_message_template", "chore: automated update {timestamp}").format(timestamp=datetime.now().isoformat())
            git_repo.add_all()
            committed = git_repo.commit(commit_msg)
            record.commit_message = commit_msg
            record.commit_sha = git_repo.get_commit_hash("HEAD")

            if not committed and not dry_run:
                record.status = PushStatus.SKIPPED
                record.message = "No changes to commit"
                record.duration_ms = int((time.time() - start_time) * 1000)
                self._save_history(record)
                return PushResult(True, record, "No changes to commit")

            # Push
            if not dry_run:
                git_repo.push("origin", config.push_branch, force=force)
                record.status = PushStatus.SUCCESS
            else:
                record.status = PushStatus.DRY_RUN

            # Get size after
            record.size_after_kb = git_repo.get_size_kb()
            record.duration_ms = int((time.time() - start_time) * 1000)

            # Update profile README if needed (placeholder)
            if not dry_run and not skip_profile and config.profile_section:
                record.triggered_profile_update = False

            # Save history
            self._save_history(record)

            return PushResult(True, record, f"Push {'simulated' if dry_run else 'completed'} successfully")

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
    ) -> list[PushResult]:
        """Push multiple repos."""
        results = []
        for config in self.registry.repos:
            if only_changed:
                state = self.check_repo_state(config)
                if state.sync_status == SyncStatus.SYNCED and not state.uncommitted_changes:
                    continue
            result = self.push_repo(config, message=message, dry_run=dry_run)
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