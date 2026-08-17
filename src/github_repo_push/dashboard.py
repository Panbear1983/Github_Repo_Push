from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from github_repo_push.models import DashboardEntry, PushRecord, RepoConfig, RepoState, SyncStatus
from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer


@dataclass
class DashboardData:
    entries: list[DashboardEntry]
    total_repos: int
    synced_count: int
    ahead_count: int
    behind_count: int
    diverged_count: int
    untracked_count: int
    error_count: int
    total_pushes: int
    last_updated: datetime


class Dashboard:
    def __init__(self, registry: Registry, syncer: Syncer):
        self.registry = registry
        self.syncer = syncer
        self._cache: Optional[DashboardData] = None

    def refresh(self) -> DashboardData:
        """Refresh dashboard data from all sources."""
        entries = []
        total_pushes = 0

        for config in self.registry.repos:
            state = self.syncer.check_repo_state(config)
            recent_pushes = self.syncer.get_recent_pushes(config.name, count=20)
            total_pushes += len([p for p in recent_pushes if p.status.value == "success"])

            entry = DashboardEntry(repo=config, state=state, recent_pushes=recent_pushes)
            entries.append(entry)

        # Sort: errors first, then by sync status priority, then by name
        status_priority = {
            SyncStatus.ERROR: 0,
            SyncStatus.DIVERGED: 1,
            SyncStatus.BEHIND: 2,
            SyncStatus.AHEAD: 3,
            SyncStatus.UNTRACKED: 4,
            SyncStatus.SYNCED: 5,
        }
        entries.sort(key=lambda e: (status_priority.get(e.state.sync_status, 99), e.repo.name.lower()))

        data = DashboardData(
            entries=entries,
            total_repos=len(entries),
            synced_count=sum(1 for e in entries if e.state.sync_status == SyncStatus.SYNCED),
            ahead_count=sum(1 for e in entries if e.state.sync_status == SyncStatus.AHEAD),
            behind_count=sum(1 for e in entries if e.state.sync_status == SyncStatus.BEHIND),
            diverged_count=sum(1 for e in entries if e.state.sync_status == SyncStatus.DIVERGED),
            untracked_count=sum(1 for e in entries if e.state.sync_status == SyncStatus.UNTRACKED),
            error_count=sum(1 for e in entries if e.state.sync_status == SyncStatus.ERROR),
            total_pushes=total_pushes,
            last_updated=datetime.now(),
        )
        self._cache = data
        return data

    def get_cached(self) -> Optional[DashboardData]:
        return self._cache

    def export_json(self) -> str:
        """Export dashboard data as JSON."""
        import json
        data = self._cache or self.refresh()
        return json.dumps({
            "total_repos": data.total_repos,
            "synced_count": data.synced_count,
            "ahead_count": data.ahead_count,
            "behind_count": data.behind_count,
            "diverged_count": data.diverged_count,
            "untracked_count": data.untracked_count,
            "error_count": data.error_count,
            "total_pushes": data.total_pushes,
            "last_updated": data.last_updated.isoformat(),
            "entries": [
                {
                    "name": e.repo.name,
                    "local_path": e.repo.local_path,
                    "remote": e.repo.remote,
                    "sync_status": e.state.sync_status.value,
                    "local_branch": e.state.local_branch,
                    "remote_branch": e.state.remote_branch,
                    "local_commit": e.state.local_commit,
                    "remote_commit": e.state.remote_commit,
                    "local_size_kb": e.state.local_size_kb,
                    "remote_size_kb": e.state.remote_size_kb,
                    "uncommitted_changes": e.state.uncommitted_changes,
                    "diff_stat": {
                        "files_changed": e.state.diff_stat.files_changed,
                        "insertions": e.state.diff_stat.insertions,
                        "deletions": e.state.diff_stat.deletions,
                    },
                    "profile_section": e.repo.profile_section,
                    "profile_featured": e.repo.profile_featured,
                    "last_push": e.last_push_str,
                    "push_count": e.push_count,
                    "tags": e.repo.tags,
                    "cron_schedule": e.repo.cron_schedule,
                    "cron_enabled": e.repo.cron_enabled,
                }
                for e in data.entries
            ],
        }, indent=2)

    def export_csv(self) -> str:
        """Export dashboard data as CSV."""
        import csv
        import io
        data = self._cache or self.refresh()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Name", "Local Path", "Remote", "Sync Status", "Local Branch", "Remote Branch",
            "Local Commit", "Local Size (KB)", "Remote Size (KB)", "Uncommitted Changes",
            "Files Changed", "Insertions", "Deletions", "Profile Section", "Featured",
            "Last Push", "Push Count", "Tags", "Cron Schedule", "Cron Enabled"
        ])
        for e in data.entries:
            writer.writerow([
                e.repo.name,
                e.repo.local_path,
                e.repo.remote,
                e.state.sync_status.value,
                e.state.local_branch or "",
                e.state.remote_branch or "",
                e.state.local_commit or "",
                e.state.local_size_kb,
                e.state.remote_size_kb,
                e.state.uncommitted_changes,
                e.state.diff_stat.files_changed,
                e.state.diff_stat.insertions,
                e.state.diff_stat.deletions,
                e.repo.profile_section or "",
                e.repo.profile_featured,
                e.last_push_str,
                e.push_count,
                ", ".join(e.repo.tags),
                e.repo.cron_schedule or "",
                e.repo.cron_enabled,
            ])
        return output.getvalue()