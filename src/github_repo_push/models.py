from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class SyncStatus(str, Enum):
    SYNCED = "synced"
    AHEAD = "ahead"
    BEHIND = "behind"
    DIVERGED = "diverged"
    UNTRACKED = "untracked"
    ERROR = "error"


class PushStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"


class RepoVisibility(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclass
class GitDiffStat:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0

    def __bool__(self) -> bool:
        return self.files_changed > 0 or self.insertions > 0 or self.deletions > 0


class RepoConfig(BaseModel):
    name: str
    local_path: str
    remote: str  # format: "owner/repo"
    default_branch: str = "main"
    push_branch: str = "main"
    protected_branches: list[str] = Field(default_factory=lambda: ["main", "master"])
    require_pr: bool = False
    ignore_patterns: list[str] = Field(default_factory=list)
    profile_section: Optional[str] = None
    profile_featured: bool = False
    profile_description: Optional[str] = None
    profile_ai_prompt: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    cron_schedule: Optional[str] = None  # cron expression
    cron_enabled: bool = False
    visibility: str = Field(default="public")

    @field_validator('visibility')
    @classmethod
    def validate_visibility(cls, v):
        v_lower = v.lower()
        if v_lower not in ("public", "private"):
            raise ValueError(f"Visibility must be one of ['public', 'private']")
        return v_lower

    @property
    def owner(self) -> str:
        return self.remote.split("/")[0] if "/" in self.remote else "Panbear1983"

    @property
    def repo_name(self) -> str:
        return self.remote.split("/")[1] if "/" in self.remote else self.name

    @field_validator("local_path", mode="before")
    @classmethod
    def expand_path(cls, v: str) -> str:
        return str(Path(v).expanduser())

    def get_full_local_path(self) -> Path:
        return Path(self.local_path).expanduser().resolve()

    @property
    def visibility_enum(self) -> RepoVisibility:
        return RepoVisibility(self.visibility)


class RepoState(BaseModel):
    config: RepoConfig
    local_exists: bool = False
    remote_exists: bool = False
    local_branch: Optional[str] = None
    remote_branch: Optional[str] = None
    local_commit: Optional[str] = None
    remote_commit: Optional[str] = None
    local_size_kb: int = 0
    remote_size_kb: int = 0
    uncommitted_changes: bool = False
    sync_status: SyncStatus = SyncStatus.UNTRACKED
    diff_stat: GitDiffStat = Field(default_factory=GitDiffStat)
    last_push: Optional[datetime] = None
    last_sync_check: Optional[datetime] = None
    error: Optional[str] = None


class PushRecord(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.now)
    repo: str
    local_path: str
    remote: str
    branch: str
    commit_sha: Optional[str] = None
    commit_message: Optional[str] = None
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    size_before_kb: int = 0
    size_after_kb: int = 0
    duration_ms: int = 0
    status: PushStatus = PushStatus.SUCCESS
    dry_run: bool = False
    triggered_profile_update: bool = False
    profile_commit_sha: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None


class ProfileSectionConfig(BaseModel):
    name: str
    description: str = ""
    order: int = 0
    repos: list[ProfileRepoConfig] = Field(default_factory=list)


class ProfileRepoConfig(BaseModel):
    name: str
    path: Optional[str] = None  # subpath within repo for sub-entries
    featured: bool = True
    description: str = ""
    ai_prompt: Optional[str] = None


class ProfileReadmeConfig(BaseModel):
    profile_repo: str = "Panbear1983"
    sections: list[ProfileSectionConfig] = Field(default_factory=list)
    template: str = ""
    ai_enabled: bool = False
    ai_provider: str = "openai"
    ai_model: str = "gpt-4o-mini"


class PushRulesConfig(BaseModel):
    defaults: dict[str, Any] = Field(default_factory=dict)


@dataclass
class DashboardEntry:
    repo: RepoConfig
    state: RepoState
    recent_pushes: list[PushRecord] = field(default_factory=list)

    @property
    def status_badge(self) -> str:
        status = self.state.sync_status
        badges = {
            SyncStatus.SYNCED: "[green]✓ Synced[/green]",
            SyncStatus.AHEAD: "[yellow]↑ Ahead[/yellow]",
            SyncStatus.BEHIND: "[red]↓ Behind[/red]",
            SyncStatus.DIVERGED: "[red]↕ Diverged[/red]",
            SyncStatus.UNTRACKED: "[dim]? Untracked[/dim]",
            SyncStatus.ERROR: "[red]✗ Error[/red]",
        }
        return badges.get(status, "[dim]?[/dim]")

    @property
    def last_push_str(self) -> str:
        if not self.recent_pushes:
            return "Never"
        last = self.recent_pushes[-1]
        return last.timestamp.strftime("%Y-%m-%d %H:%M")

    @property
    def push_count(self) -> int:
        return len([p for p in self.recent_pushes if p.status == PushStatus.SUCCESS])


def parse_git_diff_stat(output: str) -> GitDiffStat:
    """Parse git diff --shortstat output."""
    stat = GitDiffStat()
    # Example: " 12 files changed, 456 insertions(+), 78 deletions(-)"
    match = re.search(r"(\d+)\s+files? changed", output)
    if match:
        stat.files_changed = int(match.group(1))
    match = re.search(r"(\d+)\s+insertions?\(\+\)", output)
    if match:
        stat.insertions = int(match.group(1))
    match = re.search(r"(\d+)\s+deletions?\(\-\)", output)
    if match:
        stat.deletions = int(match.group(1))
    return stat