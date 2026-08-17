from __future__ import annotations

import subprocess
import yaml
from pathlib import Path
from typing import Any, Optional

from github_repo_push.models import (
    RepoConfig,
    RepoState,
    RepoVisibility,
    SyncStatus,
    GitDiffStat,
    ProfileReadmeConfig,
    ProfileSectionConfig,
    ProfileRepoConfig,
    PushRulesConfig,
)


class Registry:
    def __init__(self, config_dir: Path):
        self.config_dir = config_dir
        self.repos_file = config_dir / "repos.yaml"
        self.profile_file = config_dir / "profile_readme.yaml"
        self.push_rules_file = config_dir / "push_rules.yaml"
        self._repos: list[RepoConfig] = []
        self._profile_config: Optional[ProfileReadmeConfig] = None
        self._push_rules: Optional[PushRulesConfig] = None

    def load(self) -> None:
        """Load all config files."""
        self._load_repos()
        self._load_profile()
        self._load_push_rules()

    def _load_repos(self) -> None:
        if not self.repos_file.exists():
            self._repos = []
            return
        with open(self.repos_file) as f:
            data = yaml.safe_load(f) or {}
        self._repos = [RepoConfig(**repo) for repo in data.get("repos", [])]

    def _load_profile(self) -> None:
        if not self.profile_file.exists():
            self._profile_config = ProfileReadmeConfig()
            return
        with open(self.profile_file) as f:
            data = yaml.safe_load(f) or {}
        # Parse sections
        sections = []
        for sec in data.get("sections", []):
            repos = []
            for repo in sec.get("repos", []):
                repos.append(ProfileRepoConfig(**repo))
            sections.append(ProfileSectionConfig(
                name=sec["name"],
                description=sec.get("description", ""),
                order=sec.get("order", 0),
                repos=repos,
            ))
        self._profile_config = ProfileReadmeConfig(
            profile_repo=data.get("profile_repo", "Panbear1983"),
            sections=sections,
            template=data.get("template", ""),
            ai_enabled=data.get("ai_enabled", False),
            ai_provider=data.get("ai_provider", "openai"),
            ai_model=data.get("ai_model", "gpt-4o-mini"),
        )

    def _load_push_rules(self) -> None:
        if not self.push_rules_file.exists():
            self._push_rules = PushRulesConfig()
            return
        with open(self.push_rules_file) as f:
            data = yaml.safe_load(f) or {}
        self._push_rules = PushRulesConfig(defaults=data.get("defaults", {}))

    def save_repos(self) -> None:
        """Save repos to YAML."""
        data = {"repos": [repo.model_dump() for repo in self._repos]}
        with open(self.repos_file, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)

    def save_profile(self) -> None:
        """Save profile config to YAML."""
        if not self._profile_config:
            return
        data = {
            "profile_repo": self._profile_config.profile_repo,
            "sections": [
                {
                    "name": sec.name,
                    "description": sec.description,
                    "order": sec.order,
                    "repos": [r.model_dump() for r in sec.repos],
                }
                for sec in self._profile_config.sections
            ],
            "template": self._profile_config.template,
            "ai_enabled": self._profile_config.ai_enabled,
            "ai_provider": self._profile_config.ai_provider,
            "ai_model": self._profile_config.ai_model,
        }
        with open(self.profile_file, "w") as f:
            yaml.dump(data, f, sort_keys=False, default_flow_style=False)

    @property
    def repos(self) -> list[RepoConfig]:
        return self._repos

    @property
    def profile_config(self) -> Optional[ProfileReadmeConfig]:
        return self._profile_config

    @property
    def push_rules(self) -> Optional[PushRulesConfig]:
        return self._push_rules

    def get_repo(self, name: str) -> Optional[RepoConfig]:
        for repo in self._repos:
            if repo.name == name:
                return repo
        return None

    def add_repo(self, repo: RepoConfig) -> None:
        """Add or update a repo."""
        existing = self.get_repo(repo.name)
        if existing:
            idx = self._repos.index(existing)
            self._repos[idx] = repo
        else:
            self._repos.append(repo)
        self.save_repos()

    def remove_repo(self, name: str) -> bool:
        repo = self.get_repo(name)
        if repo:
            self._repos.remove(repo)
            self.save_repos()
            return True
        return False

    def init_from_scan(self, local_base: Path, gh_owner: str = "Panbear1983") -> list[RepoConfig]:
        """Scan local directories and GitHub API to initialize registry."""
        # Get local repos
        local_repos = self._scan_local_repos(local_base)
        # Get remote repos
        remote_repos = self._fetch_remote_repos(gh_owner)
        # Match and create configs
        new_repos = []
        for local_name, local_path in local_repos.items():
            remote = remote_repos.get(local_name)
            if remote:
                repo_config = RepoConfig(
                    name=local_name,
                    local_path=str(local_path),
                    remote=f"{gh_owner}/{local_name}",
                    default_branch=remote.get("default_branch", "main"),
                    visibility=RepoVisibility.PRIVATE.value.lower() if remote.get("isPrivate") else RepoVisibility.PUBLIC.value.lower(),
                    profile_section=self._infer_profile_section(local_name),
                    profile_featured=self._should_feature(local_name),
                    profile_description=remote.get("description"),
                )
                new_repos.append(repo_config)
                self.add_repo(repo_config)
        return new_repos

    def _scan_local_repos(self, base: Path) -> dict[str, Path]:
        """Find all git repos under base path."""
        repos = {}
        for item in base.iterdir():
            if item.is_dir() and (item / ".git").exists():
                repos[item.name] = item
        return repos

    def _fetch_remote_repos(self, owner: str) -> dict[str, dict]:
        """Fetch all repos for owner via gh CLI."""
        import json
        try:
            result = subprocess.run(
                ["gh", "repo", "list", owner, "--limit", "200", "--json", "name,description,isPrivate,defaultBranchRef"],
                capture_output=True,
                text=True,
                check=True,
            )
            repos = json.loads(result.stdout)
            return {r["name"]: {
                "description": r.get("description"),
                "isPrivate": r.get("isPrivate", False),
                "default_branch": r.get("defaultBranchRef", {}).get("name", "main") if r.get("defaultBranchRef") else "main",
            } for r in repos}
        except subprocess.CalledProcessError:
            return {}

    def _infer_profile_section(self, repo_name: str) -> str:
        """Infer profile section from repo name."""
        security_keywords = ["SOC", "Security", "Threat", "Hunt", "Agent"]
        automation_keywords = ["Bot", "Trader", "Automator", "Pipeline", "Reporting", "Database", "gemini"]
        ml_keywords = ["ML_", "Machine_Learning", "Object_Recognition", "Lung_Cancer", "Used_Car", "Barbell"]

        if any(kw in repo_name for kw in security_keywords):
            return "Featured Security Work"
        if any(kw in repo_name for kw in automation_keywords):
            return "Applied Automation"
        if any(kw in repo_name for kw in ml_keywords):
            return "Earlier Data and ML Work"
        return "Other Projects"

    def _should_feature(self, repo_name: str) -> bool:
        """Determine if repo should be featured in profile."""
        featured_keywords = [
            "SOC_Agent", "Kash_Realestate", "Financial_Reporting",
            "Alpaca_Paper_Trader", "Github_Push_Automator",
            "gemini-token-status", "Machine_Learning_Projects"
        ]
        return any(kw in repo_name for kw in featured_keywords)


def load_registry(config_dir: Path) -> Registry:
    registry = Registry(config_dir)
    registry.load()
    return registry