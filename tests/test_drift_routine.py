"""Tests for the daily drift sweep and its launchd routine installer."""

import plistlib
import unittest
from pathlib import Path
from unittest import mock

from github_repo_push import routine
from github_repo_push.models import SyncStatus
from github_repo_push.notify import _load_env_file, format_report, resolve_credentials
from github_repo_push.syncer import DriftEntry


class DriftEntrySummaryTests(unittest.TestCase):
    def test_pushed_entry_reports_commit_count(self):
        e = DriftEntry("r", SyncStatus.AHEAD, pushed=True, commits_pushed=3)
        self.assertEqual(e.summary(), "PUSHED 3 commits")

    def test_singular_commit(self):
        e = DriftEntry("r", SyncStatus.AHEAD, pushed=True, commits_pushed=1)
        self.assertEqual(e.summary(), "PUSHED 1 commit")

    def test_dirty_is_reported_alongside_a_push(self):
        # The whole point of drift: committed work ships, dirt is only reported.
        e = DriftEntry("r", SyncStatus.AHEAD, dirty_files=2, pushed=True, commits_pushed=4)
        self.assertEqual(e.summary(), "PUSHED 4 commits, DIRTY 2")

    def test_behind_and_diverged_are_never_silent(self):
        self.assertIn("BEHIND", DriftEntry("r", SyncStatus.BEHIND).summary())
        self.assertIn("DIVERGED", DriftEntry("r", SyncStatus.DIVERGED).summary())

    def test_clean_repo_needs_no_attention(self):
        self.assertFalse(DriftEntry("r", SyncStatus.SYNCED).needs_attention)

    def test_dirty_or_behind_or_failed_needs_attention(self):
        self.assertTrue(DriftEntry("r", SyncStatus.SYNCED, dirty_files=1).needs_attention)
        self.assertTrue(DriftEntry("r", SyncStatus.BEHIND).needs_attention)
        self.assertTrue(DriftEntry("r", SyncStatus.SYNCED, error="boom").needs_attention)

    def test_error_wins_over_everything(self):
        self.assertEqual(DriftEntry("r", SyncStatus.AHEAD, error="no remote").summary(),
                         "FAILED — no remote")


class ReportFormattingTests(unittest.TestCase):
    def test_all_clean_says_so(self):
        report = format_report([DriftEntry("a", SyncStatus.SYNCED)])
        self.assertIn("All clean", report)

    def test_separates_pushed_from_needs_you(self):
        report = format_report([
            DriftEntry("pushed_one", SyncStatus.AHEAD, pushed=True, commits_pushed=1),
            DriftEntry("dirty_one", SyncStatus.SYNCED, dirty_files=5),
        ])
        self.assertIn("Pushed:", report)
        self.assertIn("Needs you:", report)
        self.assertIn("1 pushed", report)

    def test_a_pushed_but_dirty_repo_is_not_double_counted(self):
        report = format_report([DriftEntry("r", SyncStatus.AHEAD, dirty_files=2,
                                           pushed=True, commits_pushed=1)])
        self.assertIn("1 pushed · 0 need attention", report)

    def test_empty_fleet(self):
        self.assertIn("no repos registered", format_report([]))


class CredentialTests(unittest.TestCase):
    def test_env_file_parsing_skips_comments_and_strips_quotes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# comment\nA=1\nB="two"\nBAD_LINE\n')
            self.assertEqual(_load_env_file(p), {"A": "1", "B": "two"})

    def test_missing_env_file_is_not_an_error(self):
        self.assertEqual(_load_env_file(Path("/nonexistent/.env")), {})

    def test_environment_overrides_the_env_file(self):
        with mock.patch.dict("os.environ", {"GHRP_TELEGRAM_BOT_TOKEN": "from-env",
                                            "GHRP_TELEGRAM_CHAT_ID": "42"}):
            token, chat = resolve_credentials(Path("/nonexistent/data"))
        self.assertEqual((token, chat), ("from-env", "42"))


class RoutineInstallerTests(unittest.TestCase):
    def test_plist_pins_an_absolute_interpreter(self):
        # launchd does not inherit a login PATH; a bare `python3` can resolve to
        # an interpreter without the dependencies.
        data = routine.build_plist(Path("/repo"), python="/abs/python3")
        command = data["ProgramArguments"][2]
        self.assertIn("/abs/python3", command)
        self.assertIn("cd /repo", command)
        self.assertIn("drift", command)

    def test_schedule_is_written(self):
        data = routine.build_plist(Path("/repo"), hour=7, minute=30)
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 7, "Minute": 30})

    def test_does_not_run_at_load(self):
        self.assertFalse(routine.build_plist(Path("/repo"))["RunAtLoad"])

    def test_status_reports_off_when_absent(self):
        st = routine.status(plist_path=Path("/nonexistent/x.plist"))
        self.assertFalse(st.installed)
        self.assertIn("off", st.describe())

    def test_install_and_uninstall_roundtrip(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            plist = Path(d) / "test.plist"
            with mock.patch("github_repo_push.routine.subprocess.run"):
                routine.install(Path("/repo"), hour=6, minute=15, plist_path=plist)
                self.assertTrue(plist.exists())
                with open(plist, "rb") as f:
                    self.assertEqual(plistlib.load(f)["StartCalendarInterval"],
                                     {"Hour": 6, "Minute": 15})
                self.assertTrue(routine.uninstall(plist_path=plist))
                self.assertFalse(plist.exists())
                self.assertFalse(routine.uninstall(plist_path=plist))


if __name__ == "__main__":
    unittest.main()


class RoutinePathTests(unittest.TestCase):
    """Regression: launchd does not inherit a login PATH.

    Without an explicit PATH the sweep failed on every repo with
    "No such file or directory: 'gh'" — and only under launchd, never when the
    same command was run by hand. See routine.JOB_PATH.
    """

    def test_plist_exports_a_path_that_includes_homebrew(self):
        env = routine.build_plist(Path("/repo"))["EnvironmentVariables"]
        self.assertIn("/opt/homebrew/bin", env["PATH"])
        self.assertIn("/usr/bin", env["PATH"])

    def test_plist_exports_home(self):
        env = routine.build_plist(Path("/repo"))["EnvironmentVariables"]
        self.assertTrue(env["HOME"])
