"""Pilot + unit tests for the TUI control panel.

Uses a temp registry pointing at a throwaway repo with a file:// bare origin,
so pushes are fully exercised without touching GitHub. The repo is private in
the fixture so the secret-scan gate (public-only) stays out of the way.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer
from github_repo_push.tui_app import build_row, make_app, push_preview


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip()


class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = root / "work_repo"
        self.work.mkdir()
        (self.work / "a.txt").write_text("hello\n")
        (self.work / "x.log").write_text("noise\n")
        _git(self.work, "init", "-qb", "main")
        _git(self.work, "config", "user.name", "Test")
        _git(self.work, "config", "user.email", "test@example.com")
        origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        _git(self.work, "remote", "add", "origin", f"file://{origin}")
        self.origin = origin

        self.config_dir = root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "repos.yaml").write_text(yaml.dump({
            "repos": [{
                "name": "work_repo",
                "local_path": str(self.work),
                "remote": "Panbear1983/work_repo",
                "default_branch": "main",
                "push_branch": "main",
                "visibility": "private",
            }]
        }))
        (self.config_dir / "push_rules.yaml").write_text(yaml.dump({
            "defaults": {
                "owner": "Panbear1983",
                "commit_message_template": "test: update {timestamp}",
                "ignore_patterns": ["*.log"],
            }
        }))
        self.data_dir = root / "data"
        self.data_dir.mkdir()

    def registry(self) -> Registry:
        registry = Registry(self.config_dir)
        registry.load()
        return registry


class PushPreviewTests(unittest.TestCase):
    def test_preview_counts_and_skips(self):
        fx = Fixture()
        registry = fx.registry()
        syncer = Syncer(registry, fx.data_dir)
        preview = push_preview(syncer, registry.get_repo("work_repo"))
        self.assertIn("Will commit 1 file(s)", preview)  # a.txt (x.log skipped)
        self.assertIn("Skipped by ignore rules: 1", preview)
        self.assertNotIn("Secret scan", preview)  # private repo
        fx.tmp.cleanup()


class DashboardPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_row_with_push_columns(self):
        fx = Fixture()
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one(DataTable)
            self.assertEqual(table.row_count, 1)
            labels = [str(col.label) for col in table.columns.values()]
            self.assertIn("GitHub Push", labels)
            self.assertIn("ghrp Push", labels)
        fx.tmp.cleanup()

    async def test_push_modal_opens_and_escape_closes(self):
        fx = Fixture()
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(app.screen.__class__.__name__, "ConfirmPushScreen")
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotEqual(app.screen.__class__.__name__, "ConfirmPushScreen")
        fx.tmp.cleanup()

    async def test_add_modal_opens_and_escape_closes(self):
        fx = Fixture()
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            self.assertEqual(app.screen.__class__.__name__, "AddRepoScreen")
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotEqual(app.screen.__class__.__name__, "AddRepoScreen")
        fx.tmp.cleanup()

    async def test_in_tui_push_reaches_bare_origin_and_audits(self):
        fx = Fixture()
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            config = app.registry.get_repo("work_repo")
            app._on_push_choice(config, "push")
            await app.workers.wait_for_complete()
            await pilot.pause()
        tree = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "refs/heads/main"],
            cwd=fx.origin, capture_output=True, text=True,
        ).stdout.split()
        self.assertIn("a.txt", tree)
        self.assertNotIn("x.log", tree)  # excluded by ignore rules
        history = (fx.data_dir / "push_history.jsonl").read_text()
        self.assertIn('"status":"success"', history.replace(" ", ""))
        fx.tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
