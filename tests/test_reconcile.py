"""Tests for Syncer.reconcile() — the registry-vs-live-GitHub diff.

This is the check that would have caught Family_Budget_Agent sitting in
repos.yaml as UNTRACKED after it was deleted outside ghrp: an entry whose
GitHub remote is gone (orphaned), or a real GitHub repo nobody ever
registered (unregistered).
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from github_repo_push.github_api import RemoteRepoInfo
from github_repo_push.notify import format_reconcile
from github_repo_push.registry import Registry
from github_repo_push.syncer import ReconcileReport, Syncer


def _remote(name: str) -> RemoteRepoInfo:
    return RemoteRepoInfo(
        name=name, full_name=f"Panbear1983/{name}", description=None, is_private=True,
        default_branch="main", size_kb=1, pushed_at=None, url="", clone_url="", ssh_url="",
        stargazers_count=0, forks_count=0, topics=[], primary_language=None,
    )


class ReconcileFixture:
    def __init__(self, repos: list[dict]):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config_dir = root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "repos.yaml").write_text(yaml.dump({"repos": repos}))
        (self.config_dir / "push_rules.yaml").write_text(
            yaml.dump({"defaults": {"owner": "Panbear1983"}})
        )
        self.data_dir = root / "data"

    def syncer(self) -> Syncer:
        registry = Registry(self.config_dir)
        registry.load()
        return Syncer(registry, self.data_dir)

    def cleanup(self):
        self.tmp.cleanup()


def _repo(name: str, remote: str | None = None) -> dict:
    return {
        "name": name,
        "local_path": f"/tmp/{name}",
        "remote": remote or f"Panbear1983/{name}",
        "default_branch": "main",
        "push_branch": "main",
        "visibility": "private",
    }


class ReconcileTests(unittest.TestCase):
    def test_clean_when_registry_matches_github(self):
        fx = ReconcileFixture([_repo("a"), _repo("b")])
        syncer = fx.syncer()
        with mock.patch.object(syncer.github_api, "list_repos", return_value=[_remote("a"), _remote("b")]):
            report = syncer.reconcile()
        self.assertTrue(report.is_clean)
        fx.cleanup()

    def test_deleted_on_github_is_orphaned(self):
        # Exactly the Family_Budget_Agent case: still in repos.yaml, gone from GitHub.
        fx = ReconcileFixture([_repo("Family_Budget_Agent"), _repo("Github_Repo_Push")])
        syncer = fx.syncer()
        with mock.patch.object(syncer.github_api, "list_repos", return_value=[_remote("Github_Repo_Push")]):
            report = syncer.reconcile()
        self.assertEqual(report.orphaned, ["Family_Budget_Agent"])
        self.assertEqual(report.unregistered, [])
        fx.cleanup()

    def test_real_repo_never_registered_is_unregistered(self):
        fx = ReconcileFixture([_repo("Github_Repo_Push")])
        syncer = fx.syncer()
        with mock.patch.object(
            syncer.github_api, "list_repos",
            return_value=[_remote("Github_Repo_Push"), _remote("Family_Budget_Dashboard")],
        ):
            report = syncer.reconcile()
        self.assertEqual(report.orphaned, [])
        self.assertEqual(report.unregistered, ["Family_Budget_Dashboard"])
        fx.cleanup()

    def test_local_name_can_differ_from_github_name(self):
        # Kash_Realestate_Property's remote is .../Kash_Realestate_Property_Database —
        # must diff by repo_name (from `remote:`), not the registry's `name:`.
        fx = ReconcileFixture([_repo("Kash_Realestate_Property", remote="Panbear1983/Kash_Realestate_Property_Database")])
        syncer = fx.syncer()
        with mock.patch.object(syncer.github_api, "list_repos", return_value=[_remote("Kash_Realestate_Property_Database")]):
            report = syncer.reconcile()
        self.assertTrue(report.is_clean)
        fx.cleanup()

    def test_disabled_repo_is_never_reported_orphaned(self):
        repos = [_repo("Archived_Thing")]
        repos[0]["enabled"] = False
        fx = ReconcileFixture(repos)
        syncer = fx.syncer()
        with mock.patch.object(syncer.github_api, "list_repos", return_value=[]):
            report = syncer.reconcile()
        # list_repos() == [] is treated as "gh call failed", so this also
        # covers the empty-response guard below — orphaned must stay empty.
        self.assertEqual(report.orphaned, [])
        fx.cleanup()

    def test_empty_github_response_is_inconclusive_not_mass_orphaning(self):
        # A transient `gh` failure returns [] from list_repos(). Peter's
        # account is never actually empty, so this must NOT report every
        # registered repo as deleted.
        fx = ReconcileFixture([_repo("a"), _repo("b"), _repo("c")])
        syncer = fx.syncer()
        with mock.patch.object(syncer.github_api, "list_repos", return_value=[]):
            report = syncer.reconcile()
        self.assertEqual(report.orphaned, [])
        self.assertIsNotNone(report.error)
        self.assertFalse(report.is_clean)
        fx.cleanup()


class FormatReconcileTests(unittest.TestCase):
    def test_clean_report_says_so(self):
        self.assertIn("matches", format_reconcile(ReconcileReport()))

    def test_error_is_surfaced_plainly(self):
        text = format_reconcile(ReconcileReport(error="boom"))
        self.assertIn("boom", text)

    def test_both_categories_render(self):
        text = format_reconcile(ReconcileReport(orphaned=["Old_Repo"], unregistered=["New_Repo"]))
        self.assertIn("Old_Repo", text)
        self.assertIn("New_Repo", text)
        self.assertIn("Gone from GitHub", text)
        self.assertIn("not in ghrp's list", text)


if __name__ == "__main__":
    unittest.main()
