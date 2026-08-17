"""Tests for the surgical profile-README editor and README generation.

Ported from Github_Push_Automator/tests/test_profile_markdown.py during the
2026-08-17 consolidation (imports adapted; git-root test replaced by a
readme_gen creation test since ensure_git_repo lives in GitRepo now).
"""

import tempfile
import unittest
from pathlib import Path

from github_repo_push.profile_markdown import update_profile_markdown
from github_repo_push.readme_gen import ensure_readme, readme_description


class ProfileMarkdownTests(unittest.TestCase):
    def test_adds_missing_section(self):
        original = "# Panbear1983\n"
        updated = update_profile_markdown(
            original,
            "Automation, Analytics & Applied AI",
            "ExampleRepo",
            "Panbear1983",
            "Example description.",
        )

        self.assertIn("## Automation, Analytics & Applied AI", updated)
        self.assertIn("- [ExampleRepo](https://github.com/Panbear1983/ExampleRepo) - Example description.", updated)

    def test_inserts_into_existing_section_before_next_heading(self):
        original = """# Panbear1983

## Automation, Analytics & Applied AI

- Existing - Old.

## Other

Text.
"""
        updated = update_profile_markdown(
            original,
            "Automation, Analytics & Applied AI",
            "ExampleRepo",
            "Panbear1983",
            "Example description.",
        )

        self.assertLess(updated.index("ExampleRepo"), updated.index("## Other"))

    def test_updates_existing_entry(self):
        original = """# Panbear1983

## Automation, Analytics & Applied AI

- [ExampleRepo](https://github.com/Panbear1983/ExampleRepo) - Old description.
"""
        updated = update_profile_markdown(
            original,
            "Automation, Analytics & Applied AI",
            "ExampleRepo",
            "Panbear1983",
            "New description.",
        )

        self.assertNotIn("Old description", updated)
        self.assertIn("New description.", updated)

    def test_readme_description_skips_repo_title(self):
        text = """# Github_Push_Automator

Github_Push_Automator automates publishing local repositories to GitHub.
"""

        self.assertEqual(
            readme_description(text, "Github_Push_Automator"),
            "Github_Push_Automator automates publishing local repositories to GitHub.",
        )


class ReadmeGenTests(unittest.TestCase):
    def test_creates_readme_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ExampleRepo"
            repo.mkdir()
            (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")

            readme, description, created = ensure_readme(repo, "ExampleRepo")

            self.assertTrue(created)
            self.assertTrue(readme.exists())
            self.assertIn("Python project", description)
            self.assertIn("`main.py`", readme.read_text(encoding="utf-8"))

    def test_keeps_existing_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "ExampleRepo"
            repo.mkdir()
            (repo / "README.md").write_text("# ExampleRepo\n\nHand-written intro.\n", encoding="utf-8")

            readme, description, created = ensure_readme(repo, "ExampleRepo")

            self.assertFalse(created)
            self.assertEqual(description, "Hand-written intro.")
            self.assertEqual(readme.read_text(encoding="utf-8"), "# ExampleRepo\n\nHand-written intro.\n")


if __name__ == "__main__":
    unittest.main()
