"""Tests for the dashboard's folder tree (expand/collapse) and cross-repo search.

Peter's actual complaint: Multi-Funtion_SOC_Agent_Research shows as one row on
the dashboard but really contains several separate write-ups nested inside
it, with no way to reach them without leaving the TUI for Finder. These cover
the two pieces built for that: list_children/search_repos (the pure logic)
and a Pilot test exercising expand/collapse through the live table.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from github_repo_push.registry import Registry
from github_repo_push.syncer import Syncer
from github_repo_push.tui_app import list_children, repo_name_for, search_repos


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class TreeFixture:
    """A repo with the same shape as the real problem: a subfolder full of
    write-ups, plus vendor clutter that must never show up in the tree."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.work = (root / "SOC_Research").resolve()
        self.work.mkdir()
        (self.work / "README.md").write_text("root\n")

        hunts = self.work / "Threat_Hunting_Projects"
        hunts.mkdir()
        (hunts / "SAGA1_Port_of_Entry.md").write_text("hunt 1\n")
        (hunts / "SAGA3_Bridge_Takeover.md").write_text("hunt 3\n")

        vendor = self.work / ".venv" / "lib"
        vendor.mkdir(parents=True)
        (vendor / "noise.md").write_text("should never surface\n")

        (self.work / "empty_dir").mkdir()

        _git(self.work, "init", "-qb", "main")
        _git(self.work, "config", "user.name", "Test")
        _git(self.work, "config", "user.email", "test@example.com")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", "init")

        self.config_dir = root / "config"
        self.config_dir.mkdir()
        (self.config_dir / "repos.yaml").write_text(yaml.dump({
            "repos": [{
                "name": "SOC_Research",
                "local_path": str(self.work),
                "remote": "Panbear1983/SOC_Research",
                "default_branch": "main",
                "push_branch": "main",
                "visibility": "private",
            }]
        }))
        (self.config_dir / "push_rules.yaml").write_text(yaml.dump({
            "defaults": {"owner": "Panbear1983"}
        }))
        self.data_dir = root / "data"

    def registry(self) -> Registry:
        registry = Registry(self.config_dir)
        registry.load()
        return registry

    def cleanup(self):
        self.tmp.cleanup()


class ListChildrenTests(unittest.TestCase):
    def test_dirs_before_files_alphabetical(self):
        fx = TreeFixture()
        names = [name for name, _path, _is_dir in list_children(fx.work)]
        # dirs first (alphabetical among themselves), then files
        self.assertEqual(names, ["empty_dir", "Threat_Hunting_Projects", "README.md"])
        fx.cleanup()

    def test_vendor_and_dotdirs_excluded(self):
        fx = TreeFixture()
        names = {name for name, _path, _is_dir in list_children(fx.work)}
        self.assertNotIn(".venv", names)
        self.assertNotIn(".git", names)
        fx.cleanup()

    def test_the_actual_writeups_are_visible_one_level_down(self):
        fx = TreeFixture()
        hunts = fx.work / "Threat_Hunting_Projects"
        names = {name for name, _path, is_dir in list_children(hunts) if not is_dir}
        self.assertIn("SAGA3_Bridge_Takeover.md", names)
        fx.cleanup()

    def test_missing_path_returns_empty_not_an_error(self):
        self.assertEqual(list_children(Path("/nonexistent/wherever")), [])


class SearchReposTests(unittest.TestCase):
    def test_finds_a_buried_writeup_by_partial_name(self):
        fx = TreeFixture()
        results = search_repos(fx.registry(), "bridge")
        self.assertEqual(len(results), 1)
        repo, path = results[0]
        self.assertEqual(repo, "SOC_Research")
        self.assertEqual(path.name, "SAGA3_Bridge_Takeover.md")
        fx.cleanup()

    def test_case_insensitive(self):
        fx = TreeFixture()
        results = search_repos(fx.registry(), "BRIDGE")
        self.assertEqual(len(results), 1)
        fx.cleanup()

    def test_never_searches_inside_vendored_clutter(self):
        fx = TreeFixture()
        results = search_repos(fx.registry(), "noise")
        self.assertEqual(results, [])
        fx.cleanup()

    def test_blank_query_returns_nothing(self):
        fx = TreeFixture()
        self.assertEqual(search_repos(fx.registry(), "   "), [])
        fx.cleanup()

    def test_repo_name_for_maps_path_back_to_its_repo(self):
        fx = TreeFixture()
        registry = fx.registry()
        path = fx.work / "Threat_Hunting_Projects" / "SAGA3_Bridge_Takeover.md"
        self.assertEqual(repo_name_for(registry, path), "SOC_Research")
        fx.cleanup()

    def test_unowned_path_returns_question_mark(self):
        fx = TreeFixture()
        registry = fx.registry()
        self.assertEqual(repo_name_for(registry, Path("/elsewhere/file.txt")), "?")
        fx.cleanup()


class DashboardTreePilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_expand_reveals_children_collapse_removes_them(self):
        from textual.widgets import DataTable

        from github_repo_push.tui_app import make_app

        fx = TreeFixture()
        app_cls = make_app()
        app = app_cls(fx.config_dir, fx.data_dir, local_base=Path(fx.tmp.name))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            table = app.query_one(DataTable)
            before = table.row_count
            self.assertEqual(before, 1)  # just the repo row, nothing expanded yet

            # Cursor starts on the only row (the repo); Enter expands it.
            await pilot.press("enter")
            await pilot.pause()
            after_expand = table.row_count
            # repo row + Threat_Hunting_Projects/ + README.md + empty_dir/
            self.assertEqual(after_expand, 4)

            keys = {str(row.value) for row in table.rows}
            self.assertIn("SOC_Research", keys)
            self.assertTrue(any(k.endswith("Threat_Hunting_Projects") for k in keys))
            self.assertFalse(any(".venv" in k for k in keys))  # never expanded, never shown

            # Collapse it back.
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(table.row_count, before)
        fx.cleanup()


if __name__ == "__main__":
    unittest.main()
