"""Tests for the protected, hand-editable profile-page intro zone.

Peter's core worry, after the "hand-edit the whole page" idea surfaced a real
clobbering problem: automatic project-listing updates must never be able to
silently erase hand-written text. These prove the actual guarantee — not
just that the markers parse, but that a real publish cycle (real git clone/
commit/push against a local bare origin, not mocked) preserves whatever is
live unless the intro-edit path itself is the one calling.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from github_repo_push.profile_readme import (
    DEFAULT_MANUAL_SECTION,
    MANUAL_SECTION_END,
    MANUAL_SECTION_START,
    extract_manual_section,
    fetch_current_manual_section,
    generate_profile_readme,
    set_manual_section,
    update_profile_readme,
)
from github_repo_push.registry import Registry


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout.strip()


class ExtractManualSectionTests(unittest.TestCase):
    def test_finds_text_between_markers(self):
        content = f"{MANUAL_SECTION_START}\nHello there.\n{MANUAL_SECTION_END}\n## Auto stuff"
        self.assertEqual(extract_manual_section(content), "Hello there.")

    def test_none_when_markers_absent(self):
        self.assertIsNone(extract_manual_section("# Just a plain README\n\nNo markers here."))

    def test_none_when_content_is_none_or_empty(self):
        self.assertIsNone(extract_manual_section(None))
        self.assertIsNone(extract_manual_section(""))

    def test_none_when_markers_are_reversed(self):
        content = f"{MANUAL_SECTION_END}\ngarbage\n{MANUAL_SECTION_START}"
        self.assertIsNone(extract_manual_section(content))


class GenerateWithManualSectionTests(unittest.TestCase):
    def _profile_config(self):
        from github_repo_push.models import ProfileReadmeConfig
        return ProfileReadmeConfig(owner="Panbear1983", profile_repo="Panbear1983", sections=[])

    def test_explicit_manual_section_appears_verbatim(self):
        content = generate_profile_readme(self._profile_config(), manual_section="My custom bio text.")
        self.assertIn("My custom bio text.", content)
        self.assertIn(MANUAL_SECTION_START, content)
        self.assertIn(MANUAL_SECTION_END, content)
        self.assertNotIn(DEFAULT_MANUAL_SECTION, content)

    def test_none_falls_back_to_default(self):
        content = generate_profile_readme(self._profile_config(), manual_section=None)
        self.assertIn(DEFAULT_MANUAL_SECTION, content)

    def test_manual_section_is_never_mixed_with_auto_sections(self):
        # The whole point: text between the markers must be exactly what was
        # handed in, nothing generated leaking into it.
        content = generate_profile_readme(self._profile_config(), manual_section="X")
        between = extract_manual_section(content)
        self.assertEqual(between, "X")


class ProfileRepoFixture:
    """A real bare git origin standing in for the Panbear1983/Panbear1983
    profile repo, plus a fake GitHubAPI whose clone_repo/get_readme talk to
    it for real — so the publish tests exercise actual git commit/push
    behavior, not a mocked stand-in for it."""

    def __init__(self, initial_readme: str):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.origin = root / "Panbear1983.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)

        seed = root / "seed"
        seed.mkdir()
        _git(seed, "init", "-qb", "main")
        _git(seed, "config", "user.name", "Test")
        _git(seed, "config", "user.email", "test@example.com")
        (seed / "README.md").write_text(initial_readme)
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "seed")
        _git(seed, "remote", "add", "origin", f"file://{self.origin}")
        _git(seed, "push", "-q", "-u", "origin", "main")

        self.config_dir = root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "repos.yaml").write_text(yaml.dump({"repos": []}))
        (self.config_dir / "profile_readme.yaml").write_text(yaml.dump({
            "owner": "Panbear1983", "profile_repo": "Panbear1983", "sections": [],
        }))
        (self.config_dir / "push_rules.yaml").write_text(yaml.dump({"defaults": {"owner": "Panbear1983"}}))

    def registry(self) -> Registry:
        registry = Registry(self.config_dir)
        registry.load()
        return registry

    def fake_github_api(self):
        origin = self.origin

        api = mock.Mock()

        def clone_repo(name, target):
            subprocess.run(["git", "clone", "-q", f"file://{origin}", str(target)], check=True)
            return True

        def get_readme(name):
            result = subprocess.run(
                ["git", "show", "main:README.md"], cwd=str(origin), capture_output=True, text=True,
            )
            return result.stdout if result.returncode == 0 else None

        api.clone_repo.side_effect = clone_repo
        api.get_readme.side_effect = get_readme
        return api

    def live_readme(self) -> str:
        result = subprocess.run(["git", "show", "main:README.md"], cwd=str(self.origin),
                                capture_output=True, text=True)
        return result.stdout

    def cleanup(self):
        self.tmp.cleanup()


class UpdateProfileReadmePreservesManualSectionTests(unittest.TestCase):
    def test_project_listing_update_preserves_custom_live_intro(self):
        live = f"{MANUAL_SECTION_START}\nA hand-written bio Peter wrote himself.\n{MANUAL_SECTION_END}\n\n"
        fx = ProfileRepoFixture(live)
        registry = fx.registry()
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            ok, message = update_profile_readme(registry)  # no manual_override — a normal listing update
        self.assertTrue(ok)
        self.assertIn("A hand-written bio Peter wrote himself.", fx.live_readme())
        fx.cleanup()

    def test_no_syncer_does_not_crash_and_skips_badges(self):
        # Regression: update_profile_for_repo used to call this with no syncer
        # at all, which crashed because syncer was a required positional arg.
        fx = ProfileRepoFixture(f"{MANUAL_SECTION_START}\nHi.\n{MANUAL_SECTION_END}\n")
        registry = fx.registry()
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            ok, message = update_profile_readme(registry, dry_run=False)
        self.assertTrue(ok)
        fx.cleanup()

    def test_first_ever_publish_with_no_markers_yet_uses_default(self):
        fx = ProfileRepoFixture("# Some old plain README with no markers\n")
        registry = fx.registry()
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            update_profile_readme(registry)
        self.assertIn(DEFAULT_MANUAL_SECTION, fx.live_readme())
        fx.cleanup()


class SetManualSectionTests(unittest.TestCase):
    def test_overrides_whatever_was_live(self):
        fx = ProfileRepoFixture(f"{MANUAL_SECTION_START}\nOld bio.\n{MANUAL_SECTION_END}\n")
        registry = fx.registry()
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            ok, message = set_manual_section(registry, "Brand new bio, hand-typed just now.")
        self.assertTrue(ok)
        live = fx.live_readme()
        self.assertIn("Brand new bio, hand-typed just now.", live)
        self.assertNotIn("Old bio.", live)
        fx.cleanup()

    def test_dry_run_previews_without_publishing(self):
        fx = ProfileRepoFixture(f"{MANUAL_SECTION_START}\nOld bio.\n{MANUAL_SECTION_END}\n")
        registry = fx.registry()
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            ok, message = set_manual_section(registry, "Would-be new bio.", dry_run=True)
        self.assertTrue(ok)
        self.assertIn("Would-be new bio.", message)
        self.assertIn("Old bio.", fx.live_readme())  # untouched
        fx.cleanup()


class FetchCurrentManualSectionTests(unittest.TestCase):
    def test_returns_live_text_when_present(self):
        fx = ProfileRepoFixture(f"{MANUAL_SECTION_START}\nWhat's live now.\n{MANUAL_SECTION_END}\n")
        registry = fx.registry()
        text = fetch_current_manual_section(registry, github_api=fx.fake_github_api())
        self.assertEqual(text, "What's live now.")
        fx.cleanup()

    def test_returns_default_when_nothing_live_yet(self):
        fx = ProfileRepoFixture("# No markers at all\n")
        registry = fx.registry()
        text = fetch_current_manual_section(registry, github_api=fx.fake_github_api())
        self.assertEqual(text, DEFAULT_MANUAL_SECTION)
        fx.cleanup()


class DashboardEditIntroPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_e_key_edits_and_publishes(self):
        from textual.widgets import Log

        from github_repo_push.tui_app import make_app

        fx = ProfileRepoFixture(f"{MANUAL_SECTION_START}\nOriginal live bio.\n{MANUAL_SECTION_END}\n")
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.config_dir.parent / "data", local_base=Path(fx.tmp.name))
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()

                await pilot.press("e")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.screen.__class__.__name__, "ProfileIntroScreen")

                text_area = app.screen.query_one("#intro-text")
                self.assertIn("Original live bio.", text_area.text)
                text_area.text = "Edited from the dashboard."

                await pilot.click("#save")
                await app.workers.wait_for_complete()
                await pilot.pause()

                self.assertIn("Edited from the dashboard.", fx.live_readme())
                log_text = "\n".join(app.query_one(Log).lines)
                self.assertIn("✓", log_text)
        fx.cleanup()

    async def test_escape_discards_the_edit(self):
        from github_repo_push.tui_app import make_app

        fx = ProfileRepoFixture(f"{MANUAL_SECTION_START}\nOriginal.\n{MANUAL_SECTION_END}\n")
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.config_dir.parent / "data", local_base=Path(fx.tmp.name))
        with mock.patch("github_repo_push.profile_readme.get_github_api", return_value=fx.fake_github_api()):
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("e")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertIn("Original.", fx.live_readme())
        fx.cleanup()


if __name__ == "__main__":
    unittest.main()
