"""Golden test: the generated profile README must reproduce the live one.

The golden file is the live Panbear1983/Panbear1983 README snapshotted
2026-08-17. If config/profile_readme.yaml is deliberately changed, regenerate
the golden with:  PYTHONPATH=src python3 -m github_repo_push.cli profile-preview
"""

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from github_repo_push.profile_readme import build_profile_badges, generate_profile_readme, render_entry
from github_repo_push.models import ProfileRepoConfig
from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer

REPO_ROOT = Path(__file__).resolve().parents[1]


def _gh_completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


class ProfileGeneratorTests(unittest.TestCase):
    def test_matches_live_golden(self):
        registry = Registry(REPO_ROOT / "config")
        registry.load()
        generated = generate_profile_readme(registry.profile_config)
        golden = (REPO_ROOT / "tests" / "data" / "profile_readme_golden.md").read_text(encoding="utf-8")
        self.assertEqual(generated, golden)

    def test_subpath_file_link_is_encoded_blob(self):
        entry = render_entry(
            "Panbear1983",
            ProfileRepoConfig(
                name="Repo",
                title="Case #1: Study",
                path="Dir/(CTF) Case#1: Study.md",
                featured=True,
            ),
        )
        self.assertEqual(
            entry,
            "- **[Case #1: Study](<https://github.com/Panbear1983/Repo/blob/main/Dir/(CTF)%20Case%231%3A%20Study.md>)**",
        )

    def test_subpath_dir_link_is_tree(self):
        entry = render_entry(
            "Panbear1983",
            ProfileRepoConfig(name="Repo", title="Sub", path="some_dir", featured=False),
        )
        self.assertEqual(
            entry,
            "- [Sub](https://github.com/Panbear1983/Repo/tree/main/some_dir)",
        )

    def test_badge_is_appended_after_description(self):
        entry = render_entry(
            "Panbear1983",
            ProfileRepoConfig(name="Repo", title="Sub", description="A tool.", featured=False),
            badge="pushed 2026-09-06 · 2 open PRs",
        )
        self.assertEqual(
            entry,
            "- [Sub](https://github.com/Panbear1983/Repo) — A tool. · pushed 2026-09-06 · 2 open PRs",
        )

    def test_no_badge_matches_today_exactly(self):
        # generate_profile_readme(config) with no second arg must be byte-identical
        # to today's output — this is what test_matches_live_golden depends on.
        registry = Registry(REPO_ROOT / "config")
        registry.load()
        self.assertEqual(
            generate_profile_readme(registry.profile_config),
            generate_profile_readme(registry.profile_config, badges=None),
        )


class BuildProfileBadgesTests(unittest.TestCase):
    def _registry_with(self, tmp_path: Path) -> Registry:
        import yaml
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "repos.yaml").write_text(yaml.dump({
            "repos": [
                {"name": "PublicRepo", "local_path": "PublicRepo", "remote": "Panbear1983/PublicRepo",
                 "default_branch": "main", "push_branch": "main", "visibility": "public"},
                {"name": "PrivateRepo", "local_path": "PrivateRepo", "remote": "Panbear1983/PrivateRepo",
                 "default_branch": "main", "push_branch": "main", "visibility": "private"},
                # Local registry name deliberately differs from the GitHub remote name,
                # mirroring the real Kash_Realestate_Property -> ..._Database mismatch.
                {"name": "LocalName", "local_path": "LocalName", "remote": "Panbear1983/RemoteName",
                 "default_branch": "main", "push_branch": "main", "visibility": "public"},
            ]
        }))
        (config_dir / "push_rules.yaml").write_text(yaml.dump({
            "defaults": {"owner": "Panbear1983", "commit_message_template": "test", "ignore_patterns": []}
        }))
        registry = Registry(config_dir)
        registry.load()
        return registry

    def test_public_repo_gets_both_badge_parts_private_repo_excluded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry = self._registry_with(tmp_path)
            syncer = Syncer(registry, tmp_path / "data")

            repo_view_payload = (
                '{"name":"PublicRepo","description":null,"isPrivate":false,'
                '"defaultBranchRef":{"name":"main"},"diskUsage":1,'
                '"pushedAt":"2026-09-01T00:00:00Z","url":"https://github.com/Panbear1983/PublicRepo",'
                '"sshUrl":"","stargazerCount":0,"forkCount":0,"repositoryTopics":[],"primaryLanguage":null}'
            )
            pr_list_payload = (
                '[{"number":1,"title":"x","author":{"login":"a"},"url":"u",'
                '"createdAt":"2026-08-01T00:00:00Z","headRefName":"b","isDraft":false},'
                '{"number":2,"title":"y","author":{"login":"a"},"url":"u",'
                '"createdAt":"2026-08-01T00:00:00Z","headRefName":"c","isDraft":false}]'
            )

            def fake_run(cmd, **kwargs):
                if cmd[:3] == ["gh", "repo", "view"]:
                    return _gh_completed(stdout=repo_view_payload)
                if cmd[:3] == ["gh", "pr", "list"]:
                    return _gh_completed(stdout=pr_list_payload)
                return _gh_completed(returncode=1, stderr="unexpected call in this test")

            with mock.patch("github_repo_push.github_api.subprocess.run", side_effect=fake_run):
                badges = build_profile_badges(registry, syncer)

        self.assertIn("PublicRepo", badges)
        self.assertNotIn("PrivateRepo", badges)  # private repos never queried/included
        self.assertIn("pushed 2026-09-01", badges["PublicRepo"])
        self.assertIn("2 open PRs", badges["PublicRepo"])

    def test_badge_keyed_by_github_name_not_local_registry_name(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry = self._registry_with(tmp_path)
            syncer = Syncer(registry, tmp_path / "data")

            def fake_run(cmd, **kwargs):
                if cmd[:3] == ["gh", "repo", "view"] and "Panbear1983/RemoteName" in cmd:
                    return _gh_completed(stdout=(
                        '{"name":"RemoteName","description":null,"isPrivate":false,'
                        '"defaultBranchRef":{"name":"main"},"diskUsage":1,'
                        '"pushedAt":"2026-09-01T00:00:00Z","url":"u","sshUrl":"",'
                        '"stargazerCount":0,"forkCount":0,"repositoryTopics":[],"primaryLanguage":null}'
                    ))
                if cmd[:3] == ["gh", "pr", "list"]:
                    return _gh_completed(stdout="[]")
                return _gh_completed(returncode=1, stderr="unexpected call")

            with mock.patch("github_repo_push.github_api.subprocess.run", side_effect=fake_run):
                badges = build_profile_badges(registry, syncer)

        self.assertIn("RemoteName", badges)      # keyed by GitHub name, matching profile_readme.yaml's repo.name
        self.assertNotIn("LocalName", badges)    # not the local registry name

    def test_gh_failure_on_one_lookup_does_not_suppress_the_other(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            registry = self._registry_with(tmp_path)
            syncer = Syncer(registry, tmp_path / "data")

            def fake_run(cmd, **kwargs):
                if cmd[:3] == ["gh", "repo", "view"]:
                    return _gh_completed(returncode=1, stderr="boom")
                if cmd[:3] == ["gh", "pr", "list"]:
                    return _gh_completed(stdout="[]")
                return _gh_completed(returncode=1)

            with mock.patch("github_repo_push.github_api.subprocess.run", side_effect=fake_run):
                badges = build_profile_badges(registry, syncer)

        # pushed-date lookup failed, PR lookup succeeded but found none -> no badge at all
        self.assertNotIn("PublicRepo", badges)


if __name__ == "__main__":
    unittest.main()
