"""Tests for GitHubAPI.list_open_prs."""

import subprocess
import unittest
from unittest import mock

from github_repo_push.github_api import GitHubAPI, PullRequestInfo


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class ListOpenPRsTests(unittest.TestCase):
    def test_parses_open_prs(self):
        payload = (
            '[{"number":12,"title":"Fix typo","author":{"login":"someoneelse"},'
            '"url":"https://github.com/Panbear1983/foo/pull/12",'
            '"createdAt":"2026-08-01T00:00:00Z","headRefName":"patch-1","isDraft":false}]'
        )
        with mock.patch("github_repo_push.github_api.subprocess.run", return_value=_completed(stdout=payload)):
            prs = GitHubAPI("Panbear1983").list_open_prs("foo")
        self.assertEqual(len(prs), 1)
        self.assertIsInstance(prs[0], PullRequestInfo)
        self.assertEqual(prs[0].number, 12)
        self.assertEqual(prs[0].author, "someoneelse")
        self.assertEqual(prs[0].head_branch, "patch-1")
        self.assertFalse(prs[0].is_draft)

    def test_no_open_prs_returns_empty_list(self):
        with mock.patch("github_repo_push.github_api.subprocess.run", return_value=_completed(stdout="[]")):
            self.assertEqual(GitHubAPI("Panbear1983").list_open_prs("foo"), [])

    def test_gh_error_raises(self):
        with mock.patch(
            "github_repo_push.github_api.subprocess.run",
            return_value=_completed(returncode=1, stderr="repo not found"),
        ):
            with self.assertRaises(RuntimeError):
                GitHubAPI("Panbear1983").list_open_prs("foo")

    def test_timeout_raises(self):
        with mock.patch(
            "github_repo_push.github_api.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=10),
        ):
            with self.assertRaises(RuntimeError):
                GitHubAPI("Panbear1983").list_open_prs("foo")

    def test_existing_calls_unaffected_by_timeout_param(self):
        # get_repo/list_repos never pass timeout= — confirm _run_gh still defaults to None
        # (i.e. subprocess.run is called without blocking on a timeout kwarg mismatch).
        with mock.patch(
            "github_repo_push.github_api.subprocess.run", return_value=_completed(returncode=1)
        ) as run:
            GitHubAPI("Panbear1983").get_repo("foo")
        _, kwargs = run.call_args
        self.assertIsNone(kwargs.get("timeout"))


if __name__ == "__main__":
    unittest.main()
