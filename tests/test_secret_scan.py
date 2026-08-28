r"""Tests for the secret-scan guardrail (scripts/secret_scan.sh).

Regression cover for the filename guard: the original BLOCKED_FILES pattern used
`\.env\.[^t]`, which wrongly allowed `.env.tmp`/`.env.test` through while wrongly
blocking the legitimate `.env.example` template that several repos publish.
"""

import subprocess
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "secret_scan.sh"


def scan(staged_files, content=""):
    """Run the scanner with the given staged filenames. Returns the exit code."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "printf", "%s", content],
        env={"STAGED_FILES": "\n".join(staged_files), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    return result.returncode


class FilenameGuardTests(unittest.TestCase):
    def test_allows_placeholder_templates(self):
        for name in (".env.example", ".env.sample", ".env.template", ".env.dist"):
            with self.subTest(name=name):
                self.assertEqual(scan([name]), 0, f"{name} should be publishable")

    def test_allows_templates_in_subdirectories(self):
        self.assertEqual(scan(["config/.env.example"]), 0)

    def test_blocks_real_env_files(self):
        for name in (".env", ".env.local", ".env.production", "sub/.env"):
            with self.subTest(name=name):
                self.assertEqual(scan([name]), 1, f"{name} must be blocked")

    def test_blocks_env_variants_the_old_pattern_let_through(self):
        # `.env\.[^t]` allowed anything starting with "t" — these carry real values.
        for name in (".env.tmp", ".env.test"):
            with self.subTest(name=name):
                self.assertEqual(scan([name]), 1, f"{name} must be blocked")

    def test_blocks_keys_and_credentials(self):
        for name in ("server.pem", "id_rsa", "deploy.key", "credentials/aws", "auth.json",
                     "session_state.json"):
            with self.subTest(name=name):
                self.assertEqual(scan([name]), 1, f"{name} must be blocked")

    def test_allows_ordinary_source(self):
        self.assertEqual(scan(["src/app.py", "README.md", "config/settings.yaml"]), 0)


class ContentGuardTests(unittest.TestCase):
    def test_blocks_a_telegram_token_in_an_added_line(self):
        token = "+TELEGRAM_BOT_TOKEN=1234567890:AA" + "b" * 33
        self.assertEqual(scan(["notes.txt"], token), 1)

    def test_ignores_placeholder_values(self):
        self.assertEqual(scan(["notes.txt"], "+TELEGRAM_BOT_TOKEN=your_token_here"), 0)

    def test_ignores_fake_values_written_by_test_fixtures(self):
        # Real repos write obviously-fake tokens in their own test suites; those
        # must not block a publish (regression: CHC_Rental 2026-08-28).
        for value in ("fake-test-token", "faketoken1234567890", "dummy_api_key_value"):
            with self.subTest(value=value):
                self.assertEqual(scan(["tests/test_x.py"], f"+APIFY_TOKEN={value}"), 0)

    def test_still_blocks_a_realistic_looking_key(self):
        # Assembled at runtime so this file does not itself contain a literal
        # that trips the scanner on every push of this repo.
        key = "sk-or-" + "v1-" + "9f2ab7c41d8e60b3aa71"
        self.assertEqual(scan(["cfg.py"], f"+OPENROUTER_API_KEY={key}"), 1)


if __name__ == "__main__":
    unittest.main()
