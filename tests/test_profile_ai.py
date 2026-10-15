"""Tests for the AI-drafted profile description feature.

Peter's ask: when a repo is pushed, sometimes it's brand new to the profile
page and needs an introduction, sometimes it's an existing one whose
description has gone stale. He wants a grounded model to draft the one-line
description — reading the repo's own README, never inventing claims, never
publishing without his sign-off. These cover: the prompt stays grounded
(pure-function, no network), the `claude -p` subprocess call is parsed
correctly and fails loudly rather than silently, the assembly logic finds
whether a repo already has an entry, and applying a (possibly edited)
suggestion goes through the same publish path as everything else.
"""

import json
import subprocess
import unittest
from unittest import mock

from github_repo_push.models import ProfileReadmeConfig, ProfileRepoConfig, ProfileSectionConfig, RepoConfig
from github_repo_push.profile_ai import (
    ProfileSuggestion,
    _existing_style_examples,
    _find_current_entry,
    _run_claude,
    apply_suggestion,
    build_prompt,
    suggest_profile_entry,
)


def _repo_config(**overrides) -> RepoConfig:
    defaults = dict(
        name="Widget_Tool", local_path="/tmp/Widget_Tool", remote="Panbear1983/Widget_Tool",
        visibility="public",
    )
    defaults.update(overrides)
    return RepoConfig(**defaults)


def _profile_config(sections=None) -> ProfileReadmeConfig:
    return ProfileReadmeConfig(owner="Panbear1983", profile_repo="Panbear1983", sections=sections or [])


class BuildPromptTests(unittest.TestCase):
    def test_states_the_grounding_rule(self):
        prompt = build_prompt(_repo_config(), "some readme", None, [], None, None, [], [])
        self.assertIn("STRICT GROUNDING RULE", prompt)
        self.assertIn("Never invent", prompt)

    def test_includes_readme_content(self):
        prompt = build_prompt(_repo_config(), "A tool that parses widgets.", None, [], None, None, [], [])
        self.assertIn("A tool that parses widgets.", prompt)

    def test_missing_readme_says_so_rather_than_omitting(self):
        prompt = build_prompt(_repo_config(), None, None, [], None, None, [], [])
        self.assertIn("no README found", prompt)

    def test_readme_is_truncated_not_sent_in_full(self):
        huge = "x" * 20000
        prompt = build_prompt(_repo_config(), huge, None, [], None, None, [], [])
        self.assertLess(len(prompt), 10000)

    def test_new_entry_says_new_introduction(self):
        prompt = build_prompt(_repo_config(), "readme", None, [], None, None, [], [])
        self.assertIn("no profile entry yet", prompt)

    def test_existing_entry_shown_and_told_to_keep_close(self):
        prompt = build_prompt(_repo_config(), "readme", None, [], None, "Old description.", [], [])
        self.assertIn("Old description.", prompt)
        self.assertIn("already has a profile entry", prompt)

    def test_new_section_is_flagged_as_a_real_decision(self):
        prompt = build_prompt(_repo_config(), "readme", None, [], None, None, ["Other Projects"], [])
        self.assertIn("is_new_section=true", prompt)
        self.assertIn("real decision Peter makes", prompt)

    def test_style_examples_labeled_as_other_projects(self):
        prompt = build_prompt(_repo_config(), "readme", None, [], None, None, [], ["- **X** — does Y"])
        self.assertIn("OTHER projects, not this one", prompt)
        self.assertIn("- **X** — does Y", prompt)


class ExistingStyleExamplesTests(unittest.TestCase):
    def test_collects_descriptions_up_to_limit(self):
        sections = [ProfileSectionConfig(name="S", repos=[
            ProfileRepoConfig(name="a", description="does a"),
            ProfileRepoConfig(name="b", description="does b"),
            ProfileRepoConfig(name="c", description="does c"),
        ])]
        examples = _existing_style_examples(_profile_config(sections), limit=2)
        self.assertEqual(len(examples), 2)

    def test_skips_entries_with_no_description(self):
        sections = [ProfileSectionConfig(name="S", repos=[ProfileRepoConfig(name="a", description="")])]
        self.assertEqual(_existing_style_examples(_profile_config(sections)), [])


class FindCurrentEntryTests(unittest.TestCase):
    def test_finds_existing_description(self):
        sections = [ProfileSectionConfig(name="S", repos=[ProfileRepoConfig(name="Widget_Tool", description="does X")])]
        self.assertEqual(_find_current_entry(_profile_config(sections), "Widget_Tool"), "does X")

    def test_returns_none_when_repo_has_no_entry(self):
        self.assertIsNone(_find_current_entry(_profile_config(), "Widget_Tool"))


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr="")


class RunClaudeTests(unittest.TestCase):
    def test_parses_structured_output(self):
        envelope = json.dumps({"structured_output": {
            "description": "A tool.", "section": "Other Projects",
            "is_new_section": False, "grounding_note": "",
        }})
        with mock.patch("github_repo_push.profile_ai.subprocess.run", return_value=_completed(stdout=envelope)):
            result = _run_claude("prompt")
        self.assertEqual(result["description"], "A tool.")

    def test_nonzero_exit_raises(self):
        with mock.patch("github_repo_push.profile_ai.subprocess.run", return_value=_completed(returncode=1)):
            with self.assertRaises(RuntimeError):
                _run_claude("prompt")

    def test_non_json_output_raises(self):
        with mock.patch("github_repo_push.profile_ai.subprocess.run", return_value=_completed(stdout="not json")):
            with self.assertRaises(RuntimeError):
                _run_claude("prompt")

    def test_missing_structured_output_raises(self):
        with mock.patch("github_repo_push.profile_ai.subprocess.run", return_value=_completed(stdout="{}")):
            with self.assertRaises(RuntimeError):
                _run_claude("prompt")

    def test_timeout_raises(self):
        with mock.patch(
            "github_repo_push.profile_ai.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=90),
        ):
            with self.assertRaises(RuntimeError):
                _run_claude("prompt")

    def test_never_grants_tool_access(self):
        # The whole point is grounding in ONLY what we hand it — no filesystem/web reach.
        with mock.patch(
            "github_repo_push.profile_ai.subprocess.run", return_value=_completed(returncode=1)
        ) as run:
            with self.assertRaises(RuntimeError):
                _run_claude("prompt")
        args = run.call_args[0][0]
        self.assertIn("--disallowedTools", args)
        disallowed = args[args.index("--disallowedTools") + 1]
        for tool in ("Bash", "Edit", "Write", "WebFetch", "WebSearch"):
            self.assertIn(tool, disallowed)


class SuggestProfileEntryTests(unittest.TestCase):
    def _fake_github_api(self, readme="A tool that parses widgets.", description=None, topics=None, language=None):
        api = mock.Mock()
        api.get_readme.return_value = readme
        remote = mock.Mock(description=description, topics=topics or [], primary_language=language)
        api.get_repo.return_value = remote
        return api

    def test_new_entry_is_flagged_new(self):
        github_api = self._fake_github_api()
        structured = {"description": "Parses widgets.", "section": "Other Projects",
                      "is_new_section": False, "grounding_note": ""}
        with mock.patch("github_repo_push.profile_ai._run_claude", return_value=structured):
            suggestion = suggest_profile_entry(_repo_config(), _profile_config(), github_api)
        self.assertTrue(suggestion.is_new_entry)
        self.assertIsNone(suggestion.current_description)
        self.assertEqual(suggestion.description, "Parses widgets.")

    def test_existing_entry_is_not_flagged_new(self):
        sections = [ProfileSectionConfig(name="S", repos=[ProfileRepoConfig(name="Widget_Tool", description="old")])]
        github_api = self._fake_github_api()
        structured = {"description": "Parses widgets, updated.", "section": "S",
                      "is_new_section": False, "grounding_note": ""}
        with mock.patch("github_repo_push.profile_ai._run_claude", return_value=structured):
            suggestion = suggest_profile_entry(_repo_config(), _profile_config(sections), github_api)
        self.assertFalse(suggestion.is_new_entry)
        self.assertEqual(suggestion.current_description, "old")


class ApplySuggestionTests(unittest.TestCase):
    def test_writes_config_and_publishes(self):
        registry = mock.Mock()
        config = _repo_config()
        suggestion = ProfileSuggestion(
            description="Parses widgets.", section="Other Projects", is_new_section=False,
            grounding_note="", current_description=None, is_new_entry=True,
        )
        with mock.patch("github_repo_push.profile_readme.update_profile_for_repo", return_value=True) as apply:
            ok, message = apply_suggestion(registry, config, suggestion)
        self.assertTrue(ok)
        self.assertEqual(config.profile_description, "Parses widgets.")
        self.assertEqual(config.profile_section, "Other Projects")
        registry.add_repo.assert_called_once_with(config)
        apply.assert_called_once_with(registry, config)

    def test_reports_failure_from_the_publish_step(self):
        registry = mock.Mock()
        config = _repo_config()
        suggestion = ProfileSuggestion("d", "s", False, "", None, True)
        with mock.patch("github_repo_push.profile_readme.update_profile_for_repo", return_value=False):
            ok, message = apply_suggestion(registry, config, suggestion)
        self.assertFalse(ok)
        self.assertIn("failed", message.lower())


class TuiFixture:
    """A minimal registry with one PUBLIC repo — no real git/network needed
    since both suggest_profile_entry and apply_suggestion are mocked in the
    Pilot tests below."""

    def __init__(self):
        import tempfile
        from pathlib import Path

        import yaml

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name).resolve()
        self.work = root / "work_repo"
        self.work.mkdir()
        self.config_dir = root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "repos.yaml").write_text(yaml.dump({
            "repos": [{
                "name": "work_repo",
                "local_path": str(self.work),
                "remote": "Panbear1983/work_repo",
                "default_branch": "main",
                "push_branch": "main",
                "visibility": "public",
            }]
        }))
        (self.config_dir / "push_rules.yaml").write_text(yaml.dump({"defaults": {"owner": "Panbear1983"}}))
        self.data_dir = root / "data"

    def cleanup(self):
        self.tmp.cleanup()


class DashboardProfileSuggestPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_i_key_shows_suggestion_and_apply_publishes(self):
        from pathlib import Path

        from github_repo_push.tui_app import make_app

        fx = TuiFixture()
        suggestion = ProfileSuggestion(
            description="A widget-parsing tool.", section="Other Projects",
            is_new_section=False, grounding_note="README was sparse.",
            current_description=None, is_new_entry=True,
        )
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        with mock.patch("github_repo_push.profile_ai.suggest_profile_entry", return_value=suggestion), \
             mock.patch("github_repo_push.profile_ai.apply_suggestion", return_value=(True, "Profile updated and pushed.")) as apply_mock:
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()

                await pilot.press("i")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertEqual(app.screen.__class__.__name__, "ProfileSuggestionScreen")

                await pilot.click("#apply")
                await app.workers.wait_for_complete()
                await pilot.pause()

                apply_mock.assert_called_once()
                from textual.widgets import Log
                log_text = "\n".join(app.query_one(Log).lines)
                self.assertIn("Profile updated and pushed.", log_text)
        fx.cleanup()

    async def test_escape_discards_without_applying(self):
        from pathlib import Path

        from github_repo_push.tui_app import make_app

        fx = TuiFixture()
        suggestion = ProfileSuggestion("d", "Other Projects", False, "", None, True)
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        with mock.patch("github_repo_push.profile_ai.suggest_profile_entry", return_value=suggestion), \
             mock.patch("github_repo_push.profile_ai.apply_suggestion") as apply_mock:
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("i")
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                apply_mock.assert_not_called()
        fx.cleanup()

    async def test_private_repo_refuses_without_opening_a_screen(self):
        import tempfile
        from pathlib import Path

        import yaml

        from github_repo_push.tui_app import make_app

        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name).resolve()
        work = root / "priv_repo"
        work.mkdir()
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / "repos.yaml").write_text(yaml.dump({
            "repos": [{
                "name": "priv_repo", "local_path": str(work),
                "remote": "Panbear1983/priv_repo", "default_branch": "main",
                "push_branch": "main", "visibility": "private",
            }]
        }))
        (config_dir / "push_rules.yaml").write_text(yaml.dump({"defaults": {"owner": "Panbear1983"}}))
        data_dir = root / "data"

        app_cls = make_app()
        app = app_cls(config_dir, data_dir, local_base=root)
        with mock.patch("github_repo_push.profile_ai.suggest_profile_entry") as suggest_mock:
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("i")
                await pilot.pause()
                self.assertNotEqual(app.screen.__class__.__name__, "ProfileSuggestionScreen")
                suggest_mock.assert_not_called()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
