from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.github_api import GitHubAPI, get_github_api
from github_repo_push.models import ProfileReadmeConfig, ProfileSectionConfig, ProfileRepoConfig, RepoConfig, RepoVisibility
from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer


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


def render_entry(owner: str, repo: ProfileRepoConfig, badge: Optional[str] = None) -> str:
    url = render_entry_url(owner, repo)
    if "(" in url or ")" in url:
        url = f"<{url}>"
    text = repo.title or repo.name
    link = f"[{text}]({url})"
    if repo.featured:
        link = f"**{link}**"
    suffix = f" — {repo.description}" if repo.description else ""
    if badge:
        suffix += f" · {badge}"
    return f"- {link}{suffix}"


def build_profile_badges(registry: Registry, syncer: Syncer) -> dict:
    """Pushed-date + open-PR badge text, public repos only. Never raises per-repo.

    Kept deliberately separate from anything the daily unattended drift job
    touches (check_repo_state/drift_sweep) — this only runs when Peter runs
    profile-preview/profile-update by hand.
    """
    badges: dict[str, str] = {}
    for config in registry.repos:
        if not config.enabled or config.visibility != RepoVisibility.PUBLIC.value:
            continue
        parts = []
        try:
            remote = syncer.github_api.get_repo(config.repo_name)
            if remote and remote.pushed_at:
                parts.append(f"pushed {remote.pushed_at.astimezone().strftime('%Y-%m-%d')}")
        except Exception:
            pass
        try:
            prs = syncer.github_api.list_open_prs(config.repo_name)
            if prs:
                parts.append(f"{len(prs)} open PR{'s' if len(prs) != 1 else ''}")
        except Exception:
            pass
        if parts:
            # Keyed by the actual GitHub repo name (not the local registry name)
            # since that's what render_entry_url/ProfileRepoConfig.name use to build
            # links — the two differ for at least one repo (Kash_Realestate_Property
            # locally vs. Kash_Realestate_Property_Database on GitHub).
            badges[config.repo_name] = " · ".join(parts)
    return badges


# Marks the hand-editable zone in the live README. generate_profile_readme()
# never writes prose inside these markers itself — it only ever preserves
# whatever is already there (or DEFAULT_MANUAL_SECTION, the first time), so a
# manual edit made via `ghrp profile-intro` / the TUI's 'e' key can never be
# silently overwritten by an automatic project-listing update.
MANUAL_SECTION_START = "<!-- ghrp:manual-start -->"
MANUAL_SECTION_END = "<!-- ghrp:manual-end -->"

DEFAULT_MANUAL_SECTION = "\n".join([
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
])


def extract_manual_section(readme_content: Optional[str]) -> Optional[str]:
    """Pull the hand-edited text out from between the markers, or None if the
    markers aren't present (first run, or a README that predates this)."""
    if not readme_content:
        return None
    start = readme_content.find(MANUAL_SECTION_START)
    end = readme_content.find(MANUAL_SECTION_END)
    if start == -1 or end == -1 or end < start:
        return None
    return readme_content[start + len(MANUAL_SECTION_START):end].strip("\n")


def generate_profile_readme(config: ProfileReadmeConfig, badges: Optional[dict] = None,
                            manual_section: Optional[str] = None) -> str:
    """Generate profile README content from config.

    `manual_section` is opaque hand-written text (usually lifted straight from
    the current live file) — it's placed verbatim between the markers, never
    parsed or regenerated. Falls back to DEFAULT_MANUAL_SECTION only when
    nothing was supplied at all (e.g. a brand-new profile repo).
    """
    badges = badges or {}
    manual_text = manual_section if manual_section is not None else DEFAULT_MANUAL_SECTION
    lines = [
        MANUAL_SECTION_START,
        manual_text.strip("\n"),
        MANUAL_SECTION_END,
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
            lines.append(render_entry(config.owner, repo, badges.get(repo.name)))
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


def update_profile_readme(
    registry: Registry,
    syncer: Optional[Syncer] = None,
    dry_run: bool = False,
    manual_override: Optional[str] = None,
) -> tuple[bool, str]:
    """Update the profile README repo.

    Always preserves whatever hand-written text currently lives between the
    manual-section markers on the LIVE page (fetched fresh here, not from any
    locally cached copy) — this function never regenerates that text itself.
    Pass `manual_override` only from the explicit "Peter just edited this"
    path (`set_manual_section`) to replace it; every other caller leaves it
    None and the live text survives untouched.

    `syncer` is optional — badges (pushed-date, open-PR count) are skipped
    without one rather than requiring every caller to build a full Syncer
    just to publish a project-listing or intro change.
    """
    profile_config = load_profile_config(registry)
    owner = profile_config.owner
    github_api = get_github_api(owner)
    profile_repo_name = profile_config.profile_repo

    badges = build_profile_badges(registry, syncer) if syncer else {}
    live_readme = github_api.get_readme(profile_repo_name)
    manual_text = manual_override if manual_override is not None else extract_manual_section(live_readme)
    content = generate_profile_readme(profile_config, badges, manual_section=manual_text)

    if dry_run:
        return True, content

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


def preview_profile_readme(registry: Registry, syncer: Syncer) -> str:
    """Preview the profile README without pushing. Touches the network (badges)."""
    profile_config = load_profile_config(registry)
    badges = build_profile_badges(registry, syncer)
    live_readme = syncer.github_api.get_readme(profile_config.profile_repo)
    manual_text = extract_manual_section(live_readme)
    return generate_profile_readme(profile_config, badges, manual_section=manual_text)


def fetch_current_manual_section(registry: Registry, github_api: Optional[GitHubAPI] = None) -> str:
    """What's actually live right now — the text to seed an edit screen with."""
    profile_config = load_profile_config(registry)
    api = github_api or get_github_api(profile_config.owner)
    live_readme = api.get_readme(profile_config.profile_repo)
    return extract_manual_section(live_readme) or DEFAULT_MANUAL_SECTION


def set_manual_section(registry: Registry, new_text: str, dry_run: bool = False) -> tuple[bool, str]:
    """Publish hand-edited intro text through the normal publish path.

    This is the ONLY call site allowed to pass manual_override into
    update_profile_readme — every other caller (project-listing updates,
    badge refreshes) always leaves it alone, so a hand edit can never be
    silently clobbered by an unrelated automatic update.
    """
    return update_profile_readme(registry, dry_run=dry_run, manual_override=new_text)