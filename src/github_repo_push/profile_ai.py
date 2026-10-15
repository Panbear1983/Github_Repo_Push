"""AI-drafted profile-page descriptions, grounded only in a repo's own README.

Never runs unattended (no daily-drift path calls into this module) and never
publishes on its own — callers always show the suggestion for approval before
writing it anywhere. Uses Peter's existing Claude subscription via the `claude
-p` headless CLI (see CLAUDE_CLIENT.py in Multi-Funtion_SOC_Agent_Research for
the same pattern elsewhere), not a metered API key.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Optional

from github_repo_push.github_api import GitHubAPI
from github_repo_push.models import ProfileReadmeConfig, RepoConfig

# Kept strict and small on purpose: the model only ever fills in these four
# fields, from material we hand it — it never sees the live profile repo or
# gets to run anything itself (see _run_claude's --disallowedTools).
SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string"},
        "section": {"type": "string"},
        "is_new_section": {"type": "boolean"},
        "grounding_note": {"type": "string"},
    },
    "required": ["description", "section", "is_new_section", "grounding_note"],
}

README_CHAR_LIMIT = 4000
DEFAULT_TIMEOUT_SECONDS = 90


@dataclass
class ProfileSuggestion:
    description: str
    section: str
    is_new_section: bool
    grounding_note: str
    current_description: Optional[str]  # what's live today; None if no entry exists yet
    is_new_entry: bool


def _existing_style_examples(profile_config: ProfileReadmeConfig, limit: int = 3) -> list[str]:
    """A few live entries, for wording style only — never fed to the model as
    facts about the project actually being described."""
    examples: list[str] = []
    for section in profile_config.sections:
        for repo in section.repos:
            if repo.description:
                examples.append(f"- **{repo.title or repo.name}** — {repo.description}")
            if len(examples) >= limit:
                return examples
    return examples


def build_prompt(
    repo_config: RepoConfig,
    readme_text: Optional[str],
    github_description: Optional[str],
    topics: list[str],
    primary_language: Optional[str],
    current_profile_description: Optional[str],
    section_names: list[str],
    style_examples: list[str],
) -> str:
    """Pure function — no I/O — so the prompt itself is unit-testable without
    ever invoking the model."""
    readme_block = (readme_text or "(no README found)")[:README_CHAR_LIMIT]
    lines = [
        "You are drafting ONE line for Peter's GitHub profile page, describing a single project.",
        "",
        "STRICT GROUNDING RULE: only use facts that are actually present in the material below. "
        "Never invent technologies, features, numbers, or claims that are not stated. If the "
        "material is too sparse to describe the project confidently, say so in grounding_note "
        "and write a short, generic, still-true description instead of guessing at specifics.",
        "",
        f"Project name: {repo_config.name}",
        f"Primary language: {primary_language or 'unknown'}",
        f"GitHub topics: {', '.join(topics) or 'none'}",
        f"GitHub's own one-line description: {github_description or '(none set)'}",
        "",
        "README content (source of truth for what this project does):",
        "---",
        readme_block,
        "---",
        "",
        f"Existing profile page sections: {', '.join(section_names) or '(none yet)'}",
    ]
    if current_profile_description:
        lines += [
            "",
            f'This project already has a profile entry: "{current_profile_description}"',
            "Update it only if the README above no longer matches it — otherwise keep it close "
            "to as-is.",
        ]
    else:
        lines += ["", "This project has no profile entry yet — this is a new introduction."]
    if style_examples:
        lines += ["", "Match this existing writing style (these are OTHER projects, not this one):"]
        lines += style_examples
    lines += [
        "",
        "Pick the best-fitting existing section by name. Only set is_new_section=true if none of "
        "the existing sections fit at all — proposing a new section is a real decision Peter "
        "makes, not something to do lightly.",
    ]
    return "\n".join(lines)


def _run_claude(prompt: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Headless, no tool access, no file/network reach beyond what's already in
    the prompt — the model is grounded purely in the text we assembled."""
    args = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        "--disallowedTools", "Bash Edit Write Read WebFetch WebSearch Glob Grep Task",
    ]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude did not respond within {timeout_seconds}s")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"claude exited {result.returncode}: {detail}")
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude returned non-JSON output: {exc}")
    structured = envelope.get("structured_output")
    if not structured:
        raise RuntimeError("claude did not return structured output")
    return structured


def _find_current_entry(profile_config: ProfileReadmeConfig, repo_name: str) -> Optional[str]:
    for section in profile_config.sections:
        for repo in section.repos:
            if repo.name == repo_name:
                return repo.description or None
    return None


def suggest_profile_entry(
    repo_config: RepoConfig,
    profile_config: ProfileReadmeConfig,
    github_api: GitHubAPI,
) -> ProfileSuggestion:
    """Fetch the repo's own README + GitHub metadata, ask the model for a
    grounded one-liner, and hand back a suggestion — nothing is written or
    published here."""
    readme_text = github_api.get_readme(repo_config.repo_name)
    remote = github_api.get_repo(repo_config.repo_name)
    github_description = remote.description if remote else None
    topics = remote.topics if remote else []
    primary_language = remote.primary_language if remote else None

    current_description = _find_current_entry(profile_config, repo_config.name)
    section_names = [s.name for s in profile_config.sections]
    style_examples = _existing_style_examples(profile_config)

    prompt = build_prompt(
        repo_config, readme_text, github_description, topics, primary_language,
        current_description, section_names, style_examples,
    )
    structured = _run_claude(prompt)

    return ProfileSuggestion(
        description=str(structured["description"]).strip(),
        section=str(structured["section"]).strip(),
        is_new_section=bool(structured["is_new_section"]),
        grounding_note=str(structured.get("grounding_note", "")).strip(),
        current_description=current_description,
        is_new_entry=current_description is None,
    )


def apply_suggestion(registry, repo_config: RepoConfig, suggestion: ProfileSuggestion) -> tuple[bool, str]:
    """Write the (possibly Peter-edited) suggestion into the registry and
    publish it — reuses the same profile-update path every other entry point
    already goes through, so this never becomes a second way to publish."""
    from github_repo_push.profile_readme import update_profile_for_repo

    repo_config.profile_description = suggestion.description
    repo_config.profile_section = suggestion.section
    registry.add_repo(repo_config)

    ok = update_profile_for_repo(registry, repo_config)
    return ok, ("Profile updated and pushed." if ok else "Profile update failed.")
