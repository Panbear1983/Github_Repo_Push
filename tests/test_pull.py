"""Tests for Syncer.pull_repo() — fast-forward-only pull.

Peter, after seeing the new folder-tree dashboard: "however, there is no
poll [pull] request, only push request." These exercise the guarantee that
matters most for a pull button: it only ever fast-forwards a clean ancestor
relationship, and refuses outright rather than risk a dirty worktree or a
diverged history.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip()


class PullFixture:
    """A local clone plus a second clone standing in for "someone edited on
    GitHub.com" — both point at the same bare origin, like the real
    Multi-Funtion_SOC_Agent_Research web-edit case earlier this session."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()

        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)

        self.work = root / "work_repo"
        self.work.mkdir()
        _git(self.work, "init", "-qb", "main")
        _git(self.work, "config", "user.name", "Test")
        _git(self.work, "config", "user.email", "test@example.com")
        (self.work / "a.txt").write_text("hello\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", "init")
        _git(self.work, "remote", "add", "origin", f"file://{self.origin}")
        _git(self.work, "push", "-q", "-u", "origin", "main")

        self.other = root / "other_clone"
        _git(root, "clone", "-q", str(self.origin), str(self.other))
        _git(self.other, "config", "user.name", "Test")
        _git(self.other, "config", "user.email", "test@example.com")

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
            "defaults": {"owner": "Panbear1983"}
        }))
        self.data_dir = root / "data"

    def push_from_other(self, filename: str, content: str, message: str) -> None:
        """Simulate a commit made outside ghrp (e.g. GitHub's web editor)."""
        (self.other / filename).write_text(content)
        _git(self.other, "add", "-A")
        _git(self.other, "commit", "-qm", message)
        _git(self.other, "push", "-q", "origin", "main")

    def syncer(self) -> Syncer:
        registry = Registry(self.config_dir)
        registry.load()
        return Syncer(registry, self.data_dir)

    def cleanup(self):
        self.tmp.cleanup()


class PullRepoTests(unittest.TestCase):
    def test_already_synced(self):
        fx = PullFixture()
        syncer = fx.syncer()
        result = syncer.pull_repo(syncer.registry.get_repo("work_repo"))
        self.assertTrue(result.success)
        self.assertIn("up to date", result.message)
        fx.cleanup()

    def test_local_ahead_is_a_noop_not_a_failure(self):
        fx = PullFixture()
        (fx.work / "b.txt").write_text("new\n")
        _git(fx.work, "add", "-A")
        _git(fx.work, "commit", "-qm", "local only")
        syncer = fx.syncer()
        result = syncer.pull_repo(syncer.registry.get_repo("work_repo"))
        self.assertTrue(result.success)
        self.assertIn("ahead", result.message)
        fx.cleanup()

    def test_clean_fast_forward_pulls_the_new_commit(self):
        fx = PullFixture()
        fx.push_from_other("web_edit.txt", "edited on github\n", "web edit")
        syncer = fx.syncer()
        before = _git(fx.work, "rev-parse", "HEAD")
        result = syncer.pull_repo(syncer.registry.get_repo("work_repo"))
        self.assertTrue(result.success)
        self.assertEqual(result.commits_pulled, 1)
        after = _git(fx.work, "rev-parse", "HEAD")
        self.assertNotEqual(before, after)
        self.assertTrue((fx.work / "web_edit.txt").exists())
        fx.cleanup()

    def test_two_commits_reports_plural_correctly(self):
        fx = PullFixture()
        fx.push_from_other("f1.txt", "1\n", "first")
        fx.push_from_other("f2.txt", "2\n", "second")
        syncer = fx.syncer()
        result = syncer.pull_repo(syncer.registry.get_repo("work_repo"))
        self.assertEqual(result.commits_pulled, 2)
        self.assertIn("2 commits", result.message)
        fx.cleanup()

    def test_dirty_worktree_refuses_and_leaves_head_untouched(self):
        fx = PullFixture()
        fx.push_from_other("web_edit.txt", "edited on github\n", "web edit")
        (fx.work / "a.txt").write_text("locally modified, not committed\n")
        syncer = fx.syncer()
        before = _git(fx.work, "rev-parse", "HEAD")
        result = syncer.pull_repo(syncer.registry.get_repo("work_repo"))
        self.assertFalse(result.success)
        self.assertIn("uncommitted", result.message)
        after = _git(fx.work, "rev-parse", "HEAD")
        self.assertEqual(before, after)  # refused before ever touching HEAD
        self.assertEqual((fx.work / "a.txt").read_text(), "locally modified, not committed\n")
        fx.cleanup()

    def test_diverged_history_refuses_rather_than_merge_commit(self):
        fx = PullFixture()
        fx.push_from_other("web_edit.txt", "edited on github\n", "web edit")
        (fx.work / "local_only.txt").write_text("local work\n")
        _git(fx.work, "add", "-A")
        _git(fx.work, "commit", "-qm", "local commit not pushed")
        syncer = fx.syncer()
        before = _git(fx.work, "rev-parse", "HEAD")
        result = syncer.pull_repo(syncer.registry.get_repo("work_repo"))
        self.assertFalse(result.success)
        self.assertIn("diverged", result.message.lower())
        after = _git(fx.work, "rev-parse", "HEAD")
        self.assertEqual(before, after)  # no merge commit was created
        fx.cleanup()

    def test_missing_local_path_reports_clearly(self):
        fx = PullFixture()
        syncer = fx.syncer()
        config = syncer.registry.get_repo("work_repo")
        config.local_path = str(Path(fx.tmp.name) / "does_not_exist")
        result = syncer.pull_repo(config)
        self.assertFalse(result.success)
        self.assertIn("does not exist", result.message)
        fx.cleanup()


class DashboardPullPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_u_key_pulls_and_logs_result(self):
        from textual.widgets import DataTable, Log

        from github_repo_push.tui_app import make_app

        fx = PullFixture()
        fx.push_from_other("web_edit.txt", "edited on github\n", "web edit")
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one(DataTable)
            row_index = table.get_row_index("work_repo")
            self.assertEqual(table.get_row_at(row_index)[1], "behind")

            await pilot.press("u")
            await app.workers.wait_for_complete()
            await pilot.pause()

            log_text = "\n".join(app.query_one(Log).lines)
            self.assertIn("Pulled 1 commit", log_text)
            row_index = table.get_row_index("work_repo")
            self.assertEqual(table.get_row_at(row_index)[1], "synced")
        fx.cleanup()


if __name__ == "__main__":
    unittest.main()
