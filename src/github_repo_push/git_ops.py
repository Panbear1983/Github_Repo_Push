from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from github_repo_push.models import GitDiffStat, SyncStatus, parse_git_diff_stat


@dataclass
class GitRepo:
    path: Path

    def run(self, args: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """Run git command in repo."""
        result = subprocess.run(
            ["git"] + args,
            cwd=self.path,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or f"exit code {result.returncode}"
            raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
        return result

    def is_repo(self) -> bool:
        result = self.run(["rev-parse", "--git-dir"], check=False)
        return result.returncode == 0

    def current_branch(self) -> str:
        result = self.run(["branch", "--show-current"])
        branch = result.stdout.strip()
        if not branch:
            # Try to create main if no branch exists
            self.run(["checkout", "-b", "main"])
            return "main"
        return branch

    def has_uncommitted_changes(self) -> bool:
        result = self.run(["status", "--porcelain"])
        return bool(result.stdout.strip())

    def get_status(self) -> str:
        result = self.run(["status", "--porcelain"])
        return result.stdout.strip()

    def diff_shortstat(self, remote: str = "origin", branch: str = "main") -> GitDiffStat:
        """Get diff stat between local and remote branch."""
        # Fetch first
        self.run(["fetch", remote], check=False)
        # Compare local HEAD to remote/branch
        result = self.run(["diff", "--shortstat", f"{remote}/{branch}...HEAD"], check=False)
        return parse_git_diff_stat(result.stdout)

    def get_commit_hash(self, ref: str = "HEAD") -> Optional[str]:
        result = self.run(["rev-parse", ref], check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def get_remote_url(self, name: str = "origin") -> Optional[str]:
        result = self.run(["remote", "get-url", name], check=False)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    def get_remote_branches(self, remote: str = "origin") -> list[str]:
        result = self.run(["branch", "-r", "--list", f"{remote}/*"], check=False)
        branches = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and not line.endswith("->"):
                # Remove "origin/" prefix
                if line.startswith(f"{remote}/"):
                    branches.append(line[len(remote)+1:])
        return branches

    def add_all(self) -> None:
        self.run(["add", "-A"])

    def commit(self, message: str) -> bool:
        """Commit changes. Returns True if commit was made."""
        if not self.has_uncommitted_changes():
            return False
        self.run(["commit", "-m", message])
        return True

    def push(self, remote: str = "origin", branch: str = "main", force: bool = False) -> None:
        args = ["push"]
        if force:
            args.append("--force-with-lease")
        args.extend([remote, branch])
        self.run(args, capture=False)

    def fetch(self, remote: str = "origin") -> None:
        self.run(["fetch", remote], check=False)

    def get_size_kb(self) -> int:
        """Get repo size in KB (approximate)."""
        result = self.run(["count-objects", "-v"], check=False)
        size_kb = 0
        for line in result.stdout.split("\n"):
            if line.startswith("size-pack:"):
                size_kb = int(line.split(":")[1].strip())
                break
        return size_kb

    def get_remote_size_kb(self, remote: str = "origin") -> int:
        """Get remote repo size via gh API (placeholder)."""
        # This would need gh api call
        return 0

    def sync_status(self, remote: str = "origin", branch: str = "main") -> SyncStatus:
        """Determine sync status between local and remote."""
        self.run(["fetch", remote], check=False)

        local_hash = self.get_commit_hash("HEAD")
        remote_hash = self.get_commit_hash(f"{remote}/{branch}")

        if not local_hash:
            return SyncStatus.UNTRACKED
        if not remote_hash:
            return SyncStatus.AHEAD  # Local has commits, remote doesn't

        if local_hash == remote_hash:
            return SyncStatus.SYNCED

        # Check if local is ancestor of remote (behind)
        result = self.run(["merge-base", "--is-ancestor", local_hash, f"{remote}/{branch}"], check=False)
        if result.returncode == 0:
            return SyncStatus.BEHIND

        # Check if remote is ancestor of local (ahead)
        result = self.run(["merge-base", "--is-ancestor", f"{remote}/{branch}", local_hash], check=False)
        if result.returncode == 0:
            return SyncStatus.AHEAD

        return SyncStatus.DIVERGED

    def diff_files(self, remote: str = "origin", branch: str = "main") -> list[str]:
        """List files that differ between local and remote."""
        self.run(["fetch", remote], check=False)
        result = self.run(["diff", "--name-only", f"{remote}/{branch}...HEAD"], check=False)
        return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]

    def log_oneline(self, count: int = 10) -> list[str]:
        result = self.run(["log", "--oneline", f"-{count}"])
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]

    def ensure_identity(self, name: str, email: str) -> None:
        self.run(["config", "user.name", name], check=False)
        self.run(["config", "user.email", email], check=False)

    def init(self) -> None:
        self.run(["init"])

    def add_remote(self, name: str, url: str) -> None:
        self.run(["remote", "add", name, url], check=False)

    def set_upstream(self, branch: str, remote: str = "origin") -> None:
        self.run(["branch", "--set-upstream-to", f"{remote}/{branch}", branch], check=False)