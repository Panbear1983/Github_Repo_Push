from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from github_repo_push.models import RepoConfig, RepoVisibility


@dataclass
class RemoteRepoInfo:
    name: str
    full_name: str
    description: Optional[str]
    is_private: bool
    default_branch: str
    size_kb: int
    pushed_at: Optional[datetime]
    url: str
    clone_url: str
    ssh_url: str
    stargazers_count: int
    forks_count: int
    topics: list[str]
    primary_language: Optional[str]


class GitHubAPI:
    def __init__(self, owner: str = "Panbear1983"):
        self.owner = owner

    def _run_gh(self, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["gh"] + args,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or f"exit code {result.returncode}"
            raise RuntimeError(f"gh {' '.join(args)} failed: {detail}")
        return result

    def check_auth(self) -> bool:
        result = self._run_gh(["auth", "status", "-h", "github.com"], check=False)
        return result.returncode == 0

    def get_repo(self, repo_name: str) -> Optional[RemoteRepoInfo]:
        result = self._run_gh(
            ["repo", "view", f"{self.owner}/{repo_name}", "--json",
             "name,description,isPrivate,defaultBranchRef,diskUsage,pushedAt,url,sshUrl,stargazerCount,forkCount,repositoryTopics,primaryLanguage"],
            check=False
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return RemoteRepoInfo(
            name=data["name"],
            full_name=f"{self.owner}/{data['name']}",
            description=data.get("description"),
            is_private=data.get("isPrivate", False),
            default_branch=data.get("defaultBranchRef", {}).get("name", "main") if data.get("defaultBranchRef") else "main",
            size_kb=data.get("diskUsage", 0),
            pushed_at=datetime.fromisoformat(data["pushedAt"].replace("Z", "+00:00")) if data.get("pushedAt") else None,
            url=data.get("url", ""),
            clone_url=f"https://github.com/{self.owner}/{data['name']}.git",
            ssh_url=data.get("sshUrl", ""),
            stargazers_count=data.get("stargazerCount", 0),
            forks_count=data.get("forkCount", 0),
            topics=data.get("repositoryTopics", []),
            primary_language=data.get("primaryLanguage", {}).get("name") if data.get("primaryLanguage") else None,
        )

    def list_repos(self, limit: int = 200) -> list[RemoteRepoInfo]:
        result = self._run_gh(
            ["repo", "list", self.owner, "--limit", str(limit), "--json",
             "name,description,isPrivate,defaultBranchRef,diskUsage,pushedAt,url,sshUrl,stargazerCount,forkCount,repositoryTopics,primaryLanguage"],
            check=False
        )
        if result.returncode != 0:
            return []
        repos = json.loads(result.stdout)
        return [
            RemoteRepoInfo(
                name=r["name"],
                full_name=f"{self.owner}/{r['name']}",
                description=r.get("description"),
                is_private=r.get("isPrivate", False),
                default_branch=r.get("defaultBranchRef", {}).get("name", "main") if r.get("defaultBranchRef") else "main",
                size_kb=r.get("diskUsage", 0),
                pushed_at=datetime.fromisoformat(r["pushedAt"].replace("Z", "+00:00")) if r.get("pushedAt") else None,
                url=r.get("url", ""),
                clone_url=f"https://github.com/{self.owner}/{r['name']}.git",
                ssh_url=r.get("sshUrl", ""),
                stargazers_count=r.get("stargazerCount", 0),
                forks_count=r.get("forkCount", 0),
                topics=r.get("repositoryTopics", []),
                primary_language=r.get("primaryLanguage", {}).get("name") if r.get("primaryLanguage") else None,
            )
            for r in repos
        ]

    def create_repo(self, config: RepoConfig, source_path: Optional[Path] = None) -> RemoteRepoInfo:
        visibility_flag = "--private" if config.visibility == RepoVisibility.PRIVATE else "--public"
        args = ["repo", "create", f"{self.owner}/{config.repo_name}", visibility_flag]
        if source_path:
            args.extend(["--source", str(source_path), "--remote", "origin"])
        result = self._run_gh(args)
        # Parse output to get repo info
        return self.get_repo(config.repo_name) or RemoteRepoInfo(
            name=config.repo_name,
            full_name=f"{self.owner}/{config.repo_name}",
            description=None,
            is_private=config.visibility == RepoVisibility.PRIVATE,
            default_branch=config.default_branch,
            size_kb=0,
            pushed_at=None,
            url=f"https://github.com/{self.owner}/{config.repo_name}",
            clone_url=f"https://github.com/{self.owner}/{config.repo_name}.git",
            ssh_url=f"git@github.com:{self.owner}/{config.repo_name}.git",
            stargazers_count=0,
            forks_count=0,
            topics=[],
            primary_language=None,
        )

    def repo_exists(self, repo_name: str) -> bool:
        result = self._run_gh(["repo", "view", f"{self.owner}/{repo_name}"], check=False)
        return result.returncode == 0

    def delete_repo(self, repo_name: str, confirm: bool = False) -> bool:
        if not confirm:
            raise ValueError("Must confirm deletion")
        result = self._run_gh(["repo", "delete", f"{self.owner}/{repo_name}", "--yes"], check=False)
        return result.returncode == 0

    def update_repo_description(self, repo_name: str, description: str) -> bool:
        result = self._run_gh(["repo", "edit", f"{self.owner}/{repo_name}", "--description", description], check=False)
        return result.returncode == 0

    def update_repo_topics(self, repo_name: str, topics: list[str]) -> bool:
        topics_str = ",".join(topics)
        result = self._run_gh(["repo", "edit", f"{self.owner}/{repo_name}", "--topics", topics_str], check=False)
        return result.returncode == 0

    def get_readme(self, repo_name: str) -> Optional[str]:
        result = self._run_gh(["api", f"repos/{self.owner}/{repo_name}/readme", "--jq", ".content"], check=False)
        if result.returncode != 0:
            return None
        import base64
        content = result.stdout.strip()
        try:
            return base64.b64decode(content).decode("utf-8")
        except Exception:
            return None

    def update_readme(self, repo_name: str, content: str, message: str, branch: str = "main") -> bool:
        import base64
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            result = self._run_gh([
                "api", "--method", "PUT",
                f"repos/{self.owner}/{repo_name}/contents/README.md",
                "-f", f"message={message}",
                "-f", f"content={base64.b64encode(content.encode()).decode()}",
                "-f", f"branch={branch}",
            ], check=False)
            return result.returncode == 0
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def clone_repo(self, repo_name: str, target_path: Path) -> bool:
        result = self._run_gh(["repo", "clone", f"{self.owner}/{repo_name}", str(target_path)], check=False)
        return result.returncode == 0


def get_github_api(owner: str = "Panbear1983") -> GitHubAPI:
    return GitHubAPI(owner)