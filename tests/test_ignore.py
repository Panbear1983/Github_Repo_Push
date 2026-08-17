"""Tests for ignore-pattern staging filter."""

import unittest

from github_repo_push.ignore import filter_paths

PATTERNS = ["*.log", "__pycache__/", ".env", ".env.*", "data/*.db", "*.sqlite3", ".DS_Store"]


class FilterPathsTests(unittest.TestCase):
    def test_blocks_env_files_anywhere(self):
        allowed, skipped = filter_paths([".env", "sub/dir/.env", "conf/.env.local"], PATTERNS)
        self.assertEqual(allowed, [])
        self.assertEqual(skipped, [".env", "sub/dir/.env", "conf/.env.local"])

    def test_blocks_directory_patterns_at_any_depth(self):
        allowed, skipped = filter_paths(["a/__pycache__/x.pyc", "__pycache__/y.pyc", "src/ok.py"], PATTERNS)
        self.assertEqual(allowed, ["src/ok.py"])

    def test_path_pattern_matches_full_relative_path(self):
        allowed, skipped = filter_paths(["data/prod.db", "other/prod.db", "notes.sqlite3"], PATTERNS)
        self.assertEqual(allowed, ["other/prod.db"])
        self.assertIn("data/prod.db", skipped)
        self.assertIn("notes.sqlite3", skipped)

    def test_allows_normal_source_files(self):
        allowed, skipped = filter_paths(["src/app.py", "README.md", "config/x.yaml"], PATTERNS)
        self.assertEqual(skipped, [])
        self.assertEqual(len(allowed), 3)

    def test_no_patterns_allows_everything(self):
        allowed, skipped = filter_paths(["a", "b/.env"], [])
        self.assertEqual(allowed, ["a", "b/.env"])
        self.assertEqual(skipped, [])


if __name__ == "__main__":
    unittest.main()
