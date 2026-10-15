"""README screenshot engine.

A repo opts in by shipping `.ghrp/screenshots.py`: run as
`<python> .ghrp/screenshots.py <out_dir>` it must drive its own dashboard on
DEMO data only, write one `<name>.svg` per scene into out_dir, and a
`scenes.json` listing `[{"name": ..., "caption": ...}]` in display order.

The engine turns those into PNGs and holds them as DRAFTS under the ghrp data
directory — outside every repo's working tree, so neither the 09:00 drift job
nor a dashboard push (which auto-stages dirty files) can ever publish them.
Only `approve()` writes them into the repo: PNGs under docs/screenshots/ and a
managed block in README.md between the markers below, then commits exactly
those paths and pushes through the normal guarded push path.

Pictures bypass the text secret scan, which is why scene scripts must never
render real data — Peter's rule, 2026-10-15.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from github_repo_push.git_ops import GitRepo
from github_repo_push.models import RepoConfig, SyncStatus

SCENE_SCRIPT = Path(".ghrp") / "screenshots.py"
PUBLISHED_DIR = Path("docs") / "screenshots"
README = Path("README.md")
BLOCK_START = "<!-- ghrp:screenshots-start -->"
BLOCK_END = "<!-- ghrp:screenshots-end -->"
PLACEHOLDER_MARKER = "If this README was generated automatically"
CAPTURE_TIMEOUT_SECONDS = 300


@dataclass
class Shot:
    name: str
    caption: str
    png: str  # absolute path of the draft PNG


@dataclass
class DraftSet:
    repo: str
    fingerprint: str
    created_at: str
    shots: list[Shot] = field(default_factory=list)


@dataclass
class RefreshResult:
    repo: str
    status: str  # no-scenes | unchanged | identical | drafted | error
    message: str = ""


# ---- paths & state ---------------------------------------------------------

def drafts_root(data_dir: Path) -> Path:
    return data_dir / "screenshot_drafts"


def _drafts_dir(data_dir: Path, repo: str) -> Path:
    return drafts_root(data_dir) / repo


def _state_file(data_dir: Path) -> Path:
    return data_dir / "screenshot_state.json"


def _load_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_file(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    _state_file(data_dir).parent.mkdir(parents=True, exist_ok=True)
    _state_file(data_dir).write_text(json.dumps(state, indent=2, sort_keys=True))


def has_scenes(config: RepoConfig) -> bool:
    return (config.get_full_local_path() / SCENE_SCRIPT).is_file()


def load_drafts(data_dir: Path, repo: str) -> Optional[DraftSet]:
    manifest = _drafts_dir(data_dir, repo) / "manifest.json"
    try:
        raw = json.loads(manifest.read_text())
    except (OSError, ValueError):
        return None
    return DraftSet(
        repo=raw["repo"], fingerprint=raw["fingerprint"], created_at=raw["created_at"],
        shots=[Shot(**s) for s in raw["shots"]],
    )


def pending(data_dir: Path) -> dict[str, DraftSet]:
    root = drafts_root(data_dir)
    if not root.is_dir():
        return {}
    found = {}
    for child in sorted(root.iterdir()):
        drafts = load_drafts(data_dir, child.name)
        if drafts and drafts.shots:
            found[child.name] = drafts
    return found


def discard(data_dir: Path, repo: str) -> bool:
    target = _drafts_dir(data_dir, repo)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


# ---- fingerprint -----------------------------------------------------------

def fingerprint(config: RepoConfig) -> str:
    """Hash of everything committed at HEAD except what the engine itself writes.

    A cheap "has anything changed since we last looked" check so the daily job
    doesn't launch a dashboard and a browser for every repo every morning.
    The byte-for-byte comparison in refresh() is what actually decides whether
    a new draft is worth Peter's attention.
    """
    git_repo = GitRepo(config.get_full_local_path())
    listing = git_repo.run(["ls-tree", "-r", "HEAD"], check=False).stdout
    kept = [
        line for line in listing.splitlines()
        if not line.endswith(f"\t{README}") and f"\t{PUBLISHED_DIR}/" not in line
    ]
    return hashlib.sha256("\n".join(kept).encode()).hexdigest()


# ---- capture ---------------------------------------------------------------

def repo_python(config: RepoConfig) -> str:
    """The scene script runs under the repo's own environment (its dashboard's
    dependencies live there); fall back to ghrp's interpreter."""
    root = config.get_full_local_path()
    for candidate in (root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def svg_to_png(svgs: list[Path]) -> list[Path]:
    """Render each SVG at 2x, cropped to its own drawing area.

    PNG rather than SVG for the README: an SVG shown through GitHub's <img>
    can't load the web font Textual embeds, so the columns drift.
    """
    from playwright.sync_api import sync_playwright

    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(device_scale_factor=2)
            for svg in svgs:
                page.goto(svg.resolve().as_uri())
                w, h = page.evaluate(
                    """() => {
                        const s = document.querySelector('svg'); const vb = s.viewBox.baseVal;
                        s.setAttribute('width', vb.width); s.setAttribute('height', vb.height);
                        return [Math.ceil(vb.width), Math.ceil(vb.height)];
                    }"""
                )
                page.set_viewport_size({"width": w, "height": h})
                png = svg.with_suffix(".png")
                page.locator("svg").first.screenshot(path=str(png))
                out.append(png)
        finally:
            browser.close()
    return out


def capture(config: RepoConfig, out_dir: Path) -> list[Shot]:
    """Run the repo's scene script and convert its output. Raises on failure."""
    root = config.get_full_local_path()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [repo_python(config), str(SCENE_SCRIPT), str(out_dir)],
            cwd=root, capture_output=True, text=True, timeout=CAPTURE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"scene script took longer than {CAPTURE_TIMEOUT_SECONDS}s")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise RuntimeError("scene script failed: " + " | ".join(detail))
    try:
        scenes = json.loads((out_dir / "scenes.json").read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"scene script wrote no usable scenes.json: {exc}")

    svgs = []
    for scene in scenes:
        svg = out_dir / f"{scene['name']}.svg"
        if not svg.is_file():
            raise RuntimeError(f"scene {scene['name']!r} produced no {svg.name}")
        svgs.append(svg)
    pngs = svg_to_png(svgs)
    return [
        Shot(name=scene["name"], caption=scene.get("caption", ""), png=str(png))
        for scene, png in zip(scenes, pngs)
    ]


# ---- README block ----------------------------------------------------------

def render_block(shots: list[Shot]) -> str:
    lines = [
        BLOCK_START,
        "## Screenshots",
        "",
        "> Taken automatically from demo data by the push tool's screenshot engine,"
        " and published only after review.",
        "",
    ]
    for shot in shots:
        rel = f"{PUBLISHED_DIR.as_posix()}/{shot.name}.png"
        alt = shot.caption or shot.name
        lines += [f"![{alt}]({rel})", ""]
        if shot.caption:
            lines += [f"*{shot.caption}*", ""]
    lines.append(BLOCK_END)
    return "\n".join(lines)


def extract_block(readme_text: str) -> Optional[str]:
    start = readme_text.find(BLOCK_START)
    end = readme_text.find(BLOCK_END)
    if start == -1 or end == -1 or end < start:
        return None
    return readme_text[start:end + len(BLOCK_END)]


def apply_block(readme_text: str, block: str) -> str:
    """Replace the managed block, or insert it before the first second-level
    heading (after the intro) — appended at the end if there is none. Nothing
    outside the markers is ever changed."""
    existing = extract_block(readme_text)
    if existing is not None:
        return readme_text.replace(existing, block)
    match = re.search(r"^## ", readme_text, flags=re.MULTILINE)
    if match:
        return readme_text[:match.start()] + block + "\n\n" + readme_text[match.start():]
    sep = "" if readme_text.endswith("\n\n") else ("\n" if readme_text.endswith("\n") else "\n\n")
    return readme_text + sep + block + "\n"


def _published_paths(block: Optional[str]) -> list[str]:
    if not block:
        return []
    return re.findall(r"!\[[^\]]*\]\(([^)]+)\)", block)


def _is_identical(config: RepoConfig, shots: list[Shot]) -> bool:
    root = config.get_full_local_path()
    readme = root / README
    current = readme.read_text(encoding="utf-8") if readme.exists() else ""
    if extract_block(current) != render_block(shots):
        return False
    for shot in shots:
        published = root / PUBLISHED_DIR / f"{shot.name}.png"
        if not published.exists() or published.read_bytes() != Path(shot.png).read_bytes():
            return False
    return True


# ---- refresh / approve -----------------------------------------------------

def refresh(config: RepoConfig, data_dir: Path, force: bool = False) -> RefreshResult:
    """Take new pictures if the repo changed; keep them only if they differ
    from what's published. Never touches the repo itself."""
    if not has_scenes(config):
        return RefreshResult(config.name, "no-scenes")

    state = _load_state(data_dir)
    fp = fingerprint(config)
    if not force and state.get(config.name, {}).get("fingerprint") == fp:
        return RefreshResult(config.name, "unchanged", "no changes since the last pictures")

    with tempfile.TemporaryDirectory(prefix="ghrp-shots-") as tmp:
        try:
            shots = capture(config, Path(tmp))
        except Exception as exc:  # noqa: BLE001 - one repo's scene must not stop the rest
            return RefreshResult(config.name, "error", str(exc))

        state[config.name] = {"fingerprint": fp, "checked_at": datetime.now().isoformat(timespec="seconds")}
        if _is_identical(config, shots):
            discard(data_dir, config.name)
            _save_state(data_dir, state)
            return RefreshResult(config.name, "identical", "pictures match what's already published")

        target = _drafts_dir(data_dir, config.name)
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        kept = []
        for shot in shots:
            dest = target / f"{shot.name}.png"
            shutil.copyfile(shot.png, dest)
            kept.append(Shot(name=shot.name, caption=shot.caption, png=str(dest)))
        drafts = DraftSet(repo=config.name, fingerprint=fp,
                          created_at=datetime.now().isoformat(timespec="seconds"), shots=kept)
        (target / "manifest.json").write_text(json.dumps(asdict(drafts), indent=2))
        _save_state(data_dir, state)
        return RefreshResult(config.name, "drafted", f"{len(kept)} new picture(s) waiting for approval")


def approve(config: RepoConfig, data_dir: Path, syncer) -> tuple[bool, str]:
    """Publish a repo's drafts: write PNGs + README block, commit exactly those
    paths, push through Syncer.push_repo (secret scan, behind/diverged refusal)."""
    drafts = load_drafts(data_dir, config.name)
    if not drafts or not drafts.shots:
        return False, "No pictures are waiting for approval."

    root = config.get_full_local_path()
    git_repo = GitRepo(root)
    touched = [str(README), str(PUBLISHED_DIR)]
    dirty = git_repo.run(["status", "--porcelain", "--", *touched], check=False).stdout.strip()
    if dirty:
        return False, ("README.md or docs/screenshots has unsaved edits — commit or undo them "
                       "first so they don't get swept into this update.")
    sync = git_repo.sync_status("origin", config.push_branch)
    if sync in (SyncStatus.BEHIND, SyncStatus.DIVERGED):
        return False, f"Local is {sync.value} relative to GitHub — pull first, then approve."

    readme = root / README
    current = readme.read_text(encoding="utf-8") if readme.exists() else f"# {config.name}\n"
    old_paths = set(_published_paths(extract_block(current)))
    new_paths = set()

    (root / PUBLISHED_DIR).mkdir(parents=True, exist_ok=True)
    for shot in drafts.shots:
        rel = f"{PUBLISHED_DIR.as_posix()}/{shot.name}.png"
        shutil.copyfile(shot.png, root / rel)
        new_paths.add(rel)
    # Only pictures this engine published before (listed in the old block) are
    # ever removed — nothing else under docs/ is touched.
    for stale in old_paths - new_paths:
        target = root / stale
        if target.is_file() and Path(stale).parent == PUBLISHED_DIR:
            target.unlink()

    readme.write_text(apply_block(current, render_block(drafts.shots)), encoding="utf-8")
    git_repo.run(["add", "-A", "--", *touched])
    if not git_repo.commit("Update README screenshots"):
        discard(data_dir, config.name)
        return True, "README already showed these pictures — nothing to publish."

    result = syncer.push_repo(config, auto_commit=False, skip_profile=True)
    if not result.success:
        return False, f"Committed locally but the push failed: {result.message}"
    discard(data_dir, config.name)
    return True, f"Published {len(drafts.shots)} picture(s) to GitHub."


# ---- README stats ----------------------------------------------------------

@dataclass
class ReadmeStats:
    exists: bool
    words: int
    last_updated: Optional[str]
    placeholder: bool
    published_pictures: int
    pending_pictures: int
    has_scenes: bool


def readme_stats(config: RepoConfig, data_dir: Path) -> ReadmeStats:
    root = config.get_full_local_path()
    readme = root / README
    text = readme.read_text(encoding="utf-8", errors="replace") if readme.exists() else ""
    last = GitRepo(root).run(["log", "-1", "--format=%cs", "--", str(README)], check=False).stdout.strip()
    drafts = load_drafts(data_dir, config.name)
    return ReadmeStats(
        exists=readme.exists(),
        words=len(text.split()),
        last_updated=last or None,
        placeholder=PLACEHOLDER_MARKER in text,
        published_pictures=len(_published_paths(extract_block(text))),
        pending_pictures=len(drafts.shots) if drafts else 0,
        has_scenes=has_scenes(config),
    )
