"""Target-repo README generation.

Ported from Github_Push_Automator (github_push_automator/cli.py) as part of
the 2026-08-17 consolidation. Generates a serviceable README.md for repos
that lack one, with a description inferred from the project's files.
"""

from __future__ import annotations

from pathlib import Path

README_FILENAMES = ("README.md", "readme.md", "Readme.md")


def find_readme(path: Path) -> Path | None:
    for filename in README_FILENAMES:
        candidate = path / filename
        if candidate.exists():
            return candidate
    return None


def visible_project_files(path: Path, limit: int = 30) -> list[str]:
    ignored_dirs = {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
    }
    files: list[str] = []
    for item in sorted(path.rglob("*")):
        if any(part in ignored_dirs for part in item.relative_to(path).parts):
            continue
        if item.is_file():
            files.append(str(item.relative_to(path)))
        if len(files) >= limit:
            break
    return files


def infer_description(path: Path, repo_name: str) -> str:
    normalized = repo_name.replace("_", " ").replace("-", " ").strip()
    files = {p.name.lower() for p in path.iterdir() if p.is_file()}
    directories = {p.name.lower() for p in path.iterdir() if p.is_dir()}

    if "package.json" in files:
        return f"{normalized} is a JavaScript or TypeScript project managed with npm-compatible tooling."
    if "pyproject.toml" in files or "requirements.txt" in files or any(p.endswith(".py") for p in visible_project_files(path)):
        return f"{normalized} is a Python project for automating a focused local workflow."
    if "dockerfile" in files or "docker-compose.yml" in files or "compose.yml" in files:
        return f"{normalized} packages infrastructure or services for repeatable local execution."
    if "src" in directories:
        return f"{normalized} contains source code for a local software project."
    return f"{normalized} is a local project repository prepared for GitHub publishing."


def readme_text(repo_name: str, description: str, files: list[str]) -> str:
    file_lines = "\n".join(f"- `{file}`" for file in files[:12]) or "- Project files will be listed here as the repository grows."
    return f"""# {repo_name}

{description}

## Purpose

This repository is maintained locally and published to GitHub with an automated workflow. It includes the source files, scripts, and supporting project assets needed to understand and continue the work.

## Project Layout

{file_lines}

## Usage

Review the project files and run the relevant scripts or commands for this repository. If this README was generated automatically, update it with project-specific setup and operating notes as the project matures.
"""


def readme_description(text: str, repo_name: str) -> str | None:
    """Extract the first meaningful description line from an existing README."""
    repo_title = repo_name.replace("_", " ").replace("-", " ").strip().lower()
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        if clean.startswith("#"):
            continue
        if clean.startswith("[!") or clean.startswith("!["):
            continue
        return clean
    return None


def ensure_readme(path: Path, repo_name: str, description: str | None = None) -> tuple[Path, str, bool]:
    """Ensure a README exists. Returns (path, description, created)."""
    existing = find_readme(path)
    if existing:
        text = existing.read_text(encoding="utf-8", errors="replace")
        return existing, readme_description(text, repo_name) or infer_description(path, repo_name), False

    description = description or infer_description(path, repo_name)
    readme = path / "README.md"
    readme.write_text(readme_text(repo_name, description, visible_project_files(path)), encoding="utf-8")
    return readme, description, True
