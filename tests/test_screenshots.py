"""Tests for the README screenshot engine.

Peter's rules (2026-10-15) these pin down: pictures come from demo data,
new pictures WAIT for his approval ("3 wait for my approval first"), the
daily job never publishes them, and approving touches only README.md and
docs/screenshots/ — never anything else in the repo.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from github_repo_push import screenshots as shots
from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout.strip()


class BlockTests(unittest.TestCase):
    SHOTS = [shots.Shot("1_a", "First view.", "/x/1_a.png"), shots.Shot("2_b", "", "/x/2_b.png")]

    def test_inserted_before_first_section_after_intro(self):
        readme = "# Title\n\nIntro paragraph.\n\n## Features\n\n- one\n"
        out = shots.apply_block(readme, shots.render_block(self.SHOTS))
        self.assertLess(out.index("Intro paragraph."), out.index(shots.BLOCK_START))
        self.assertLess(out.index(shots.BLOCK_END), out.index("## Features"))

    def test_replacing_keeps_everything_outside_the_markers(self):
        readme = "# T\n\nMine.\n\n" + shots.render_block(self.SHOTS[:1]) + "\n\n## Usage\n\nAlso mine.\n"
        out = shots.apply_block(readme, shots.render_block(self.SHOTS))
        self.assertIn("Mine.", out)
        self.assertIn("Also mine.", out)
        self.assertEqual(out.count(shots.BLOCK_START), 1)
        self.assertIn("docs/screenshots/2_b.png", out)

    def test_appended_when_readme_has_no_sections(self):
        out = shots.apply_block("# T\n\nJust text.\n", shots.render_block(self.SHOTS))
        self.assertTrue(out.rstrip().endswith(shots.BLOCK_END))

    def test_caption_rendered_under_picture(self):
        block = shots.render_block(self.SHOTS)
        self.assertIn("![First view.](docs/screenshots/1_a.png)", block)
        self.assertIn("*First view.*", block)


class Fixture:
    """A real repo with a bare origin, a scene script stub, and a ghrp registry."""

    def __init__(self, visibility="private"):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.origin = root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        self.work = root / "proj"
        self.work.mkdir()
        _git(self.work, "init", "-qb", "main")
        _git(self.work, "config", "user.name", "T")
        _git(self.work, "config", "user.email", "t@example.com")
        (self.work / "README.md").write_text("# proj\n\nWhat it does.\n\n## Usage\n\nRun it.\n")
        (self.work / ".ghrp").mkdir()
        (self.work / ".ghrp" / "screenshots.py").write_text("# stub\n")
        (self.work / "app.py").write_text("print('v1')\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", "init")
        _git(self.work, "remote", "add", "origin", f"file://{self.origin}")
        _git(self.work, "push", "-q", "-u", "origin", "main")

        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "repos.yaml").write_text(yaml.dump({"repos": [{
            "name": "proj", "local_path": str(self.work), "remote": "Panbear1983/proj",
            "default_branch": "main", "push_branch": "main", "visibility": visibility,
        }]}))
        (config_dir / "push_rules.yaml").write_text(yaml.dump({"defaults": {"owner": "Panbear1983"}}))
        self.registry = Registry(config_dir)
        self.registry.load()
        self.config = self.registry.get_repo("proj")
        self.data_dir = root / "data"
        self.pixels = {"1_main": b"PNG-main-v1", "2_detail": b"PNG-detail-v1"}

    def fake_capture(self, config, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        result = []
        for name, data in self.pixels.items():
            png = out_dir / f"{name}.png"
            png.write_bytes(data)
            result.append(shots.Shot(name, f"Caption for {name}.", str(png)))
        return result

    def refresh(self, **kw):
        with mock.patch.object(shots, "capture", self.fake_capture):
            return shots.refresh(self.config, self.data_dir, **kw)

    def syncer(self):
        return Syncer(self.registry, self.data_dir)

    def cleanup(self):
        self.tmp.cleanup()


class RefreshTests(unittest.TestCase):
    def test_new_pictures_become_drafts_outside_the_repo(self):
        fx = Fixture()
        result = fx.refresh()
        self.assertEqual(result.status, "drafted")
        drafts = shots.load_drafts(fx.data_dir, "proj")
        self.assertEqual([s.name for s in drafts.shots], ["1_main", "2_detail"])
        for shot in drafts.shots:
            self.assertFalse(Path(shot.png).is_relative_to(fx.work))
        self.assertEqual(_git(fx.work, "status", "--porcelain"), "")  # repo untouched
        fx.cleanup()

    def test_nothing_changed_skips_the_capture_entirely(self):
        fx = Fixture()
        fx.refresh()
        with mock.patch.object(shots, "capture", side_effect=AssertionError("should not capture")):
            result = shots.refresh(fx.config, fx.data_dir)
        self.assertEqual(result.status, "unchanged")
        fx.cleanup()

    def test_a_new_commit_triggers_a_retake(self):
        fx = Fixture()
        fx.refresh()
        (fx.work / "app.py").write_text("print('v2')\n")
        _git(fx.work, "commit", "-qam", "change")
        fx.pixels["1_main"] = b"PNG-main-v2"
        self.assertEqual(fx.refresh().status, "drafted")
        fx.cleanup()

    def test_pictures_matching_what_is_published_make_no_draft(self):
        fx = Fixture()
        fx.refresh()
        ok, _ = shots.approve(fx.config, fx.data_dir, fx.syncer())
        self.assertTrue(ok)
        result = fx.refresh(force=True)
        self.assertEqual(result.status, "identical")
        self.assertEqual(shots.pending(fx.data_dir), {})
        fx.cleanup()

    def test_repo_without_scene_script_is_left_alone(self):
        fx = Fixture()
        (fx.work / ".ghrp" / "screenshots.py").unlink()
        self.assertEqual(fx.refresh().status, "no-scenes")
        fx.cleanup()

    def test_capture_failure_reports_error_and_retries_next_time(self):
        fx = Fixture()
        with mock.patch.object(shots, "capture", side_effect=RuntimeError("boom")):
            self.assertEqual(shots.refresh(fx.config, fx.data_dir).status, "error")
        self.assertEqual(fx.refresh().status, "drafted")  # state wasn't marked as checked
        fx.cleanup()


class ApproveTests(unittest.TestCase):
    def test_publishes_only_readme_and_pictures(self):
        fx = Fixture()
        (fx.work / "unrelated.txt").write_text("work in progress\n")  # must NOT be committed
        fx.refresh()
        ok, message = shots.approve(fx.config, fx.data_dir, fx.syncer())
        self.assertTrue(ok, message)

        changed = _git(fx.work, "show", "--name-only", "--pretty=format:", "HEAD").splitlines()
        self.assertEqual(sorted(changed), ["README.md", "docs/screenshots/1_main.png",
                                           "docs/screenshots/2_detail.png"])
        self.assertIn("?? unrelated.txt", _git(fx.work, "status", "--porcelain"))
        self.assertEqual(_git(fx.work, "rev-parse", "HEAD"), _git(fx.origin, "rev-parse", "main"))
        readme = (fx.work / "README.md").read_text()
        self.assertIn("What it does.", readme)
        self.assertIn("Run it.", readme)
        self.assertIn("docs/screenshots/1_main.png", readme)
        self.assertEqual(shots.pending(fx.data_dir), {})
        fx.cleanup()

    def test_refuses_when_readme_has_unsaved_edits(self):
        fx = Fixture()
        fx.refresh()
        (fx.work / "README.md").write_text("# proj\n\nHalf-written edit\n")
        ok, message = shots.approve(fx.config, fx.data_dir, fx.syncer())
        self.assertFalse(ok)
        self.assertIn("unsaved edits", message)
        self.assertEqual((fx.work / "README.md").read_text(), "# proj\n\nHalf-written edit\n")
        self.assertTrue(shots.pending(fx.data_dir))  # drafts kept for later
        fx.cleanup()

    def test_refuses_when_behind_github(self):
        fx = Fixture()
        other = Path(fx.tmp.name) / "other"
        _git(Path(fx.tmp.name), "clone", "-q", str(fx.origin), str(other))
        _git(other, "config", "user.name", "T")
        _git(other, "config", "user.email", "t@example.com")
        (other / "web.md").write_text("edited on github\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "web")
        _git(other, "push", "-q", "origin", "main")
        fx.refresh()
        ok, message = shots.approve(fx.config, fx.data_dir, fx.syncer())
        self.assertFalse(ok)
        self.assertIn("pull first", message)
        fx.cleanup()

    def test_dropped_scene_removes_only_its_old_picture(self):
        fx = Fixture()
        fx.refresh()
        shots.approve(fx.config, fx.data_dir, fx.syncer())
        (fx.work / "docs" / "screenshots" / "hand_made.png").write_bytes(b"mine")
        _git(fx.work, "add", "-A")
        _git(fx.work, "commit", "-qm", "my own picture")
        del fx.pixels["2_detail"]
        fx.refresh(force=True)
        ok, _ = shots.approve(fx.config, fx.data_dir, fx.syncer())
        self.assertTrue(ok)
        self.assertFalse((fx.work / "docs" / "screenshots" / "2_detail.png").exists())
        self.assertTrue((fx.work / "docs" / "screenshots" / "hand_made.png").exists())
        fx.cleanup()

    def test_nothing_waiting(self):
        fx = Fixture()
        ok, message = shots.approve(fx.config, fx.data_dir, fx.syncer())
        self.assertFalse(ok)
        self.assertIn("No pictures", message)
        fx.cleanup()


class ReadmeStatsTests(unittest.TestCase):
    def test_reports_words_pictures_and_placeholder(self):
        fx = Fixture()
        (fx.work / "README.md").write_text("# p\n\nIf this README was generated automatically, update it.\n")
        _git(fx.work, "commit", "-qam", "placeholder")
        fx.refresh(force=True)
        st = shots.readme_stats(fx.config, fx.data_dir)
        self.assertTrue(st.placeholder)
        self.assertEqual(st.pending_pictures, 2)
        self.assertEqual(st.published_pictures, 0)
        self.assertTrue(st.has_scenes)
        fx.cleanup()


class DriftNeverPublishesTests(unittest.TestCase):
    def test_daily_job_only_drafts_and_reports(self):
        from github_repo_push import cli

        fx = Fixture()
        with mock.patch.object(cli, "DATA_DIR", fx.data_dir), \
             mock.patch.object(shots, "capture", fx.fake_capture), \
             mock.patch.object(shots, "approve", side_effect=AssertionError("drift must never approve")):
            line = cli._refresh_screenshot_drafts(fx.registry, capture=True)
        self.assertIn("waiting for your approval", line)
        self.assertIn("proj", line)
        self.assertEqual(_git(fx.work, "status", "--porcelain"), "")
        fx.cleanup()

    def test_dry_run_takes_no_pictures(self):
        from github_repo_push import cli

        fx = Fixture()
        with mock.patch.object(cli, "DATA_DIR", fx.data_dir), \
             mock.patch.object(shots, "refresh", side_effect=AssertionError("no capture in dry-run")):
            self.assertEqual(cli._refresh_screenshot_drafts(fx.registry, capture=False), "")
        fx.cleanup()


class DashboardScreenshotsPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_s_key_shows_waiting_pictures_and_approve_publishes(self):
        from github_repo_push.tui_app import make_app

        fx = Fixture()
        fx.refresh()
        app = make_app()(fx.registry.config_dir, fx.data_dir, local_base=fx.work.parent)
        with mock.patch("github_repo_push.github_api.GitHubAPI.get_repo", return_value=None), \
             mock.patch.object(shots, "approve", return_value=(True, "Published 2 picture(s) to GitHub.")) as approve:
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertIn("1 with pictures to approve", app.sub_title)
                await pilot.press("s")
                await pilot.pause()
                self.assertEqual(app.screen.__class__.__name__, "ScreenshotsScreen")
                await pilot.click("#approve")
                await app.workers.wait_for_complete()
                await pilot.pause()
                approve.assert_called_once()
        fx.cleanup()


class RealCaptureTests(unittest.TestCase):
    """Runs this repo's own scene script end to end (Textual + Chromium)."""

    def test_own_dashboard_scenes_render(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        registry = Registry(REPO_ROOT / "config")
        registry.load()
        config = registry.get_repo("Github_Repo_Push").model_copy(update={"local_path": str(REPO_ROOT)})
        with tempfile.TemporaryDirectory() as tmp:
            result = shots.capture(config, Path(tmp))
            self.assertEqual([s.name for s in result], ["1_overview", "2_folder_tree", "3_search"])
            for shot in result:
                self.assertEqual(Path(shot.png).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            scenes = json.loads((Path(tmp) / "scenes.json").read_text())
            self.assertTrue(all(s["caption"] for s in scenes))


if __name__ == "__main__":
    unittest.main()
