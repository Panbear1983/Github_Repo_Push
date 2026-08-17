"""Golden test: the generated profile README must reproduce the live one.

The golden file is the live Panbear1983/Panbear1983 README snapshotted
2026-08-17. If config/profile_readme.yaml is deliberately changed, regenerate
the golden with:  PYTHONPATH=src python3 -m github_repo_push.cli profile-preview
"""

import unittest
from pathlib import Path

from github_repo_push.profile_readme import generate_profile_readme, render_entry
from github_repo_push.models import ProfileRepoConfig
from github_repo_push.registry import Registry

REPO_ROOT = Path(__file__).resolve().parents[1]


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


if __name__ == "__main__":
    unittest.main()
