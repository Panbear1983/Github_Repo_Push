"""Surgical profile-README editing.

Ported from Github_Push_Automator (github_push_automator/cli.py) as part of
the 2026-08-17 consolidation. Unlike profile_readme.py, which regenerates the
whole README from config, this module edits exactly one bullet in place and
leaves everything else untouched — the safe default for per-push updates.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from github_repo_push.git_ops import GitRepo
from github_repo_push.github_api import get_github_api

DEFAULT_SECTION = "Applied Automation"


def heading_level(line: str) -> int | None:
    match = re.match(r"^(#{1,6})\s+", line)
    return len(match.group(1)) if match else None


def profile_entry(repo_name: str, owner: str, description: str) -> str:
    return f"- [{repo_name}](https://github.com/{owner}/{repo_name}) - {description}"


def update_profile_markdown(
    markdown: str,
    section_name: str,
    repo_name: str,
    owner: str,
    description: str,
) -> str:
    """Insert or update one repo bullet inside the named section."""
    lines = markdown.splitlines()
    entry = profile_entry(repo_name, owner, description)
    section_index = None
    section_level = None

    for index, line in enumerate(lines):
        level = heading_level(line)
        if level and line.strip("# ").strip().lower() == section_name.lower():
            section_index = index
            section_level = level
            break

    if section_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"## {section_name}", "", entry])
        return "\n".join(lines).rstrip() + "\n"

    end_index = len(lines)
    for index in range(section_index + 1, len(lines)):
        level = heading_level(lines[index])
        if level and section_level and level <= section_level:
            end_index = index
            break

    repo_pattern = re.compile(rf"^\s*[-*]\s+(?:\[{re.escape(repo_name)}\]\([^)]+\)|{re.escape(repo_name)})\s+-\s+.*$")
    for index in range(section_index + 1, end_index):
        if repo_pattern.match(lines[index]):
            lines[index] = entry
            return "\n".join(lines).rstrip() + "\n"

    insert_index = section_index + 1
    while insert_index < end_index and not lines[insert_index].strip():
        insert_index += 1
    lines.insert(insert_index, entry)
    return "\n".join(lines).rstrip() + "\n"


def apply_profile_entry(
    owner: str,
    profile_repo: str,
    section: str,
    repo_name: str,
    description: str,
) -> bool:
    """Clone the profile repo, surgically update one entry, push if changed.

    Returns True if a change was pushed.
    """
    github_api = get_github_api(owner)
    with tempfile.TemporaryDirectory(prefix="github-profile-readme-") as tmp:
        clone_path = Path(tmp) / profile_repo
        if not github_api.clone_repo(profile_repo, clone_path):
            raise RuntimeError(f"Failed to clone {owner}/{profile_repo}")
        readme = clone_path / "README.md"
        original = readme.read_text(encoding="utf-8") if readme.exists() else f"# {profile_repo}\n\n"
        updated = update_profile_markdown(original, section, repo_name, owner, description)
        if updated == original:
            return False
        readme.write_text(updated, encoding="utf-8")
        git_repo = GitRepo(clone_path)
        git_repo.ensure_identity(owner, f"{owner}@users.noreply.github.com")
        git_repo.add_all()
        if not git_repo.commit(f"Add {repo_name} to profile README"):
            return False
        git_repo.push("origin", git_repo.current_branch())
        return True
