"""README screenshot scenes for this repo's own dashboard.

Run as `python .ghrp/screenshots.py <out_dir>` (the screenshot engine does
this). Everything shown is DEMO data: six made-up projects backed by local
git repos with file:// origins, GitHub lookups stubbed, and the clock frozen,
so the same dashboard always renders the same pixels and a new draft only
appears when the dashboard itself changes.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from github_repo_push.github_api import PullRequestInfo, RemoteRepoInfo  # noqa: E402
from github_repo_push.tui_app import make_app  # noqa: E402

NOW = datetime(2026, 10, 15, 9, 30, 0)
WIDTH = 150

DEMO = {
    "weather-station": {"state": "synced", "dirs": {"sensors": ["bme280.py", "anemometer.py"], "reports": ["daily_report.py"]}},
    "recipe-planner": {"state": "dirty", "dirs": {"app": ["planner.py", "pantry.py"], "data": ["recipes.json"]}},
    "home-energy-monitor": {"state": "ahead", "dirs": {"collectors": ["meter.py"], "reports": ["monthly_report.md"]}},
    "trail-log": {"state": "behind", "dirs": {"maps": ["routes.geojson"], "notes": ["2026_season.md"]}},
    "photo-archive": {"state": "synced", "dirs": {"scripts": ["dedupe.py", "export.py"]}},
    "book-tracker": {"state": "synced", "dirs": {"src": ["tracker.py"], "docs": ["report_format.md"]}},
}
OPEN_PRS = {"weather-station": 2, "trail-log": 1}
SIZES_KB = {"weather-station": 1840, "recipe-planner": 612, "home-energy-monitor": 2375,
            "trail-log": 9120, "photo-archive": 48230, "book-tracker": 305}
GHRP_PUSH_HOURS_AGO = {"weather-station": 1, "recipe-planner": 50, "home-energy-monitor": 25,
                       "trail-log": 74, "photo-archive": 7, "book-tracker": 28}
GITHUB_PUSH_HOURS_AGO = {"weather-station": 1, "recipe-planner": 50, "home-energy-monitor": 25,
                         "trail-log": 4, "photo-archive": 7, "book-tracker": 28}

SCENES = [
    {"name": "1_overview",
     "caption": "Every project at a glance: sync status, uncommitted changes, last push times and open pull requests."},
    {"name": "2_folder_tree",
     "caption": "Open any project in place to browse its folders and files."},
    {"name": "3_search",
     "caption": "Search file and folder names across every project at once."},
]


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def build_fixture(root: Path):
    repos = []
    for name, spec in DEMO.items():
        origin = root / "origins" / f"{name}.git"
        origin.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        work = root / "work" / name
        work.mkdir(parents=True)
        git(work, "init", "-qb", "main")
        git(work, "config", "user.name", "Demo")
        git(work, "config", "user.email", "demo@example.com")
        (work / "README.md").write_text(f"# {name}\n")
        for folder, files in spec["dirs"].items():
            (work / folder).mkdir()
            for f in files:
                (work / folder / f).write_text("# demo\n")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "initial")
        git(work, "remote", "add", "origin", f"file://{origin}")
        git(work, "push", "-q", "-u", "origin", "main")

        if spec["state"] == "dirty":
            (work / "app" / "planner.py").write_text("# edited, not committed\n")
        elif spec["state"] == "ahead":
            (work / "CHANGELOG.md").write_text("- new meter support\n")
            git(work, "add", "-A")
            git(work, "commit", "-qm", "add meter support")
        elif spec["state"] == "behind":
            other = root / "other" / name
            git(root, "clone", "-q", str(origin), str(other))
            git(other, "config", "user.name", "Demo")
            git(other, "config", "user.email", "demo@example.com")
            (other / "notes" / "edit.md").write_text("edited on github\n")
            git(other, "add", "-A")
            git(other, "commit", "-qm", "web edit")
            git(other, "push", "-q", "origin", "main")

        repos.append({"name": name, "local_path": str(work), "remote": f"demo-user/{name}",
                      "default_branch": "main", "push_branch": "main", "visibility": "public"})

    config_dir = root / "config"
    config_dir.mkdir()
    (config_dir / "repos.yaml").write_text(yaml.dump({"repos": repos}))
    (config_dir / "push_rules.yaml").write_text(yaml.dump({"defaults": {"owner": "demo-user"}}))
    data_dir = root / "data"
    data_dir.mkdir()
    with open(data_dir / "push_history.jsonl", "w") as f:
        for name, hours in GHRP_PUSH_HOURS_AGO.items():
            f.write(json.dumps({"timestamp": (NOW - timedelta(hours=hours)).isoformat(),
                                "repo": name, "local_path": "", "remote": f"demo-user/{name}",
                                "branch": "main", "status": "success"}) + "\n")
    return config_dir, data_dir


def fake_remote(_self, name):
    pushed = (NOW - timedelta(hours=GITHUB_PUSH_HOURS_AGO[name])).astimezone(timezone.utc)
    return RemoteRepoInfo(name=name, full_name=f"demo-user/{name}", description=None, is_private=False,
                          default_branch="main", size_kb=0, pushed_at=pushed, url="", clone_url="",
                          ssh_url="", stargazers_count=0, forks_count=0, topics=[], primary_language="Python")


def fake_prs(_self, name, **_kw):
    return [PullRequestInfo(number=i + 1, title="demo", author="someone", url="", created_at=None,
                            head_branch="patch", is_draft=False) for i in range(OPEN_PRS.get(name, 0))]


async def capture(config_dir: Path, data_dir: Path, root: Path, out: Path):
    from textual.widgets import DataTable, Log

    app = make_app()(config_dir, data_dir, local_base=root / "work")
    with mock.patch("github_repo_push.github_api.GitHubAPI.get_repo", fake_remote), \
         mock.patch("github_repo_push.github_api.GitHubAPI.list_open_prs", fake_prs), \
         mock.patch("github_repo_push.git_ops.GitRepo.get_size_kb", lambda self: SIZES_KB[Path(self.path).name]), \
         mock.patch("github_repo_push.tui_app.datetime", FrozenDatetime), \
         mock.patch("github_repo_push.routine.status") as routine_status:
        routine_status.return_value.describe.return_value = "on — daily at 09:00, loaded"
        async with app.run_test(size=(WIDTH, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause(0.5)
            app.query_one(Log).display = False
            table = app.query_one(DataTable)

            await pilot.resize_terminal(WIDTH, table.row_count + 4)
            table.move_cursor(row=0)
            await pilot.pause()
            app.save_screenshot(filename="1_overview.svg", path=str(out))

            table.move_cursor(row=table.get_row_index("home-energy-monitor"))
            await pilot.press("enter")
            await pilot.pause()
            table.move_cursor(row=table.get_row_index(str(root / "work" / "home-energy-monitor" / "reports")))
            await pilot.press("enter")
            await pilot.resize_terminal(WIDTH, table.row_count + 4)
            await pilot.pause()
            app.save_screenshot(filename="2_folder_tree.svg", path=str(out))

            await pilot.resize_terminal(WIDTH, 30)
            await pilot.press("slash")
            await pilot.pause()
            for ch in "report":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.5)
            app.save_screenshot(filename="3_search.svg", path=str(out))


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ghrp-demo-") as tmp:
        root = Path(tmp).resolve()
        config_dir, data_dir = build_fixture(root)
        asyncio.run(capture(config_dir, data_dir, root, out))
    (out / "scenes.json").write_text(json.dumps(SCENES, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
