r"""The outgoing scan must inspect every commit, not just the endpoints.

Regression for a real leak path found on 2026-08-28: `_secret_scan` compared
`origin/<branch>..HEAD` with a two-dot `git diff`, which reports only the NET
change. A secret added in commit N and removed in commit N+1 therefore looked
clean — while both commits were pushed and the secret sat in the remote's
history permanently. ghrp commits BEFORE it scans, so a blocked push plus a
follow-up fix reproduces this exactly.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "secret_scan.sh"
# Assembled at runtime so this file never contains a scanner-tripping literal.
SECRET = "sk-or-" + "v1-" + "a1b2c3d4e5f6a7b8c9d0e1f2"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


class OutgoingScanRangeTests(unittest.TestCase):
    """Build a repo where a secret is added then removed, and scan it both ways."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")

        (self.repo / "a.txt").write_text("clean baseline\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        # commit 1: introduces the secret (this is what a blocked ghrp push leaves behind)
        (self.repo / "cfg.py").write_text(f'OPENROUTER_API_KEY = "{SECRET}"\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "oops")

        # commit 2: removes it again (the follow-up fix)
        (self.repo / "cfg.py").write_text('OPENROUTER_API_KEY = os.environ["KEY"]\n')
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "fixed")

    def tearDown(self):
        self._tmp.cleanup()

    def _scan(self, *git_args):
        result = subprocess.run(
            ["bash", str(SCRIPT), "git", "-C", str(self.repo), *git_args],
            capture_output=True, text=True,
        )
        return result.returncode

    def test_two_dot_diff_is_blind_to_it(self):
        # Documents the old behaviour — this is precisely why the fix was needed.
        self.assertEqual(self._scan("diff", f"{self.base}..HEAD"), 0)

    def test_per_commit_log_catches_it(self):
        self.assertEqual(self._scan("log", "-p", "--no-color", f"{self.base}..HEAD"), 1)

    def test_the_secret_really_is_in_history(self):
        found = git(self.repo, "log", "--all", "-S", SECRET, "--oneline").stdout
        self.assertTrue(found.strip(), "fixture should have the secret in history")


if __name__ == "__main__":
    unittest.main()
