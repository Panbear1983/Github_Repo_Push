from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.github_api import GitHubAPI, get_github_api
from github_repo_push.models import ProfileReadmeConfig, ProfileSectionConfig, ProfileRepoConfig, RepoConfig
from github_repo_push.registry import Registry


def load_profile_config(registry: Registry) -> ProfileReadmeConfig:
    """Load profile README config from registry."""
    if registry.profile_config:
        return registry.profile_config
    return ProfileReadmeConfig()


def render_entry_url(owner: str, repo: ProfileRepoConfig) -> str:
    """Build the entry's URL, honoring sub-paths the way GitHub renders them.

    Sub-paths use /tree/main/ for directories and /blob/main/ for files
    (inferred from the last component having an extension), percent-encoded
    except parentheses, matching the live README's link style.
    """
    from urllib.parse import quote

    url = f"https://github.com/{owner}/{repo.name}"
    if repo.path:
        kind = "blob" if "." in repo.path.rsplit("/", 1)[-1] else "tree"
        url += f"/{kind}/main/" + quote(repo.path, safe="/()")
    return url


def render_entry(owner: str, repo: ProfileRepoConfig) -> str:
    url = render_entry_url(owner, repo)
    if "(" in url or ")" in url:
        url = f"<{url}>"
    text = repo.title or repo.name
    link = f"[{text}]({url})"
    if repo.featured:
        link = f"**{link}**"
    suffix = f" — {repo.description}" if repo.description else ""
    return f"- {link}{suffix}"


def generate_profile_readme(config: ProfileReadmeConfig) -> str:
    """Generate profile README content from config."""
    lines = [
        "# Peter W. Pan",
        "",
        "**Security operations + automation | Threat hunting | Python, KQL, Microsoft Sentinel & Defender | Local AI | EN / 中文**",
        "",
        "I work at the intersection of security operations, workflow automation, and applied AI. I build Python tools and reproducible labs for threat hunting, detection workflows, vulnerability management, and privacy-preserving local analysis.",
        "",
        "My goal is practical security engineering: clear analyst workflows, explicit guardrails, measurable results, and automation that supports—rather than replaces—human judgment.",
        "",
        "## Current Focus",
        "",
        "- Security operations and incident-triage workflows",
        "- Threat hunting with KQL, Microsoft Sentinel, and Microsoft Defender",
        "- Python automation for reporting, monitoring, and decision support",
        "- Vulnerability management and network-security labs",
        "- Local LLMs, agent isolation, and tool-use guardrails",
        "",
    ]

    # Sort sections by order
    sorted_sections = sorted(config.sections, key=lambda s: s.order)

    for section in sorted_sections:
        lines.append(f"{'#' * section.level} {section.name}")
        lines.append("")
        if section.description:
            lines.append(section.description)
            lines.append("")
        if section.preamble:
            lines.append(section.preamble)
            lines.append("")
        for repo in section.repos:
            lines.append(render_entry(config.owner, repo))
        if section.repos:
            lines.append("")

    # Credentials
    lines.extend([
        "## Credentials",
        "",
        "- **[CompTIA Security+ (SY0-701)](https://www.credly.com/earner/earned/badge/af63ab88-bb91-47c8-b390-0bbba3817702)**",
        "- **[Google Cybersecurity Professional Certificate](https://www.coursera.org/account/accomplishments/specialization/1MHZD401CMYA)**",
        "- **[Cisco Networking Basics](https://www.credly.com/badges/b328db9e-cc05-4e6f-a7eb-d8e5bd927552)**",
        "",
        "## Connect",
        "",
        "- **[LinkedIn](https://www.linkedin.com/in/peter-w-pan-49a961200/)**",
        "",
    ])

    return "\n".join(lines).rstrip() + "\n"


def update_profile_readme(registry: Registry, dry_run: bool = False) -> tuple[bool, str]:
    """Update the profile README repo."""
    profile_config = load_profile_config(registry)
    content = generate_profile_readme(profile_config)

    if dry_run:
        return True, content

    # Clone profile repo, update README, push
    owner = profile_config.owner
    github_api = get_github_api(owner)
    profile_repo_name = profile_config.profile_repo

    with tempfile.TemporaryDirectory(prefix="github-profile-") as tmp:
        tmp_path = Path(tmp)
        clone_path = tmp_path / profile_repo_name

        # Clone
        if not github_api.clone_repo(profile_repo_name, clone_path):
            raise RuntimeError(f"Failed to clone {owner}/{profile_repo_name}")

        # Update README
        readme_path = clone_path / "README.md"
        readme_path.write_text(content, encoding="utf-8")

        # Commit and push
        git_repo = GitRepo(clone_path)
        git_repo.ensure_identity(owner, f"{owner}@users.noreply.github.com")

        if git_repo.has_uncommitted_changes():
            git_repo.add_all()
            git_repo.commit("Update profile README")
            branch = git_repo.current_branch()
            git_repo.push("origin", branch)
            return True, "Profile README updated and pushed"
        else:
            return False, "No changes to profile README"


def update_profile_for_repo(registry: Registry, repo_config: RepoConfig) -> bool:
    """Update profile README entry for a specific repo after push."""
    profile_config = load_profile_config(registry)

    # Find the repo in profile config
    found = False
    for section in profile_config.sections:
        for profile_repo in section.repos:
            if profile_repo.name == repo_config.name:
                # Update description from repo config if provided
                if repo_config.profile_description:
                    profile_repo.description = repo_config.profile_description
                found = True
                break
        if found:
            break

    if not found:
        # Add to appropriate section
        section_name = repo_config.profile_section or "Other Projects"
        section = next((s for s in profile_config.sections if s.name == section_name), None)
        if not section:
            section = ProfileSectionConfig(name=section_name, description="", order=99)
            profile_config.sections.append(section)

        profile_repo = ProfileRepoConfig(
            name=repo_config.name,
            featured=repo_config.profile_featured,
            description=repo_config.profile_description or "",
            ai_prompt=repo_config.profile_ai_prompt,
        )
        section.repos.append(profile_repo)

    # Save updated profile config
    registry.save_profile()

    # Regenerate and push
    try:
        update_profile_readme(registry, dry_run=False)
        return True
    except Exception:
        return False


def preview_profile_readme(registry: Registry) -> str:
    """Preview the profile README without pushing."""
    profile_config = load_profile_config(registry)
    return generate_profile_readme(profile_config)