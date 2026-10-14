"""new-note.py --install: wires notes tree, pre-commit hook, and agent-doc rule in
one step, is idempotent, never edits a foreign hook, and leaves a hook that runs."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
NEW_NOTE = SKILL_DIR / "scripts" / "new-note.py"


def git_repo(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


class TestInstall(unittest.TestCase):
    def install(self, repo, *extra):
        return subprocess.run(
            [sys.executable, str(NEW_NOTE), "--install", "--repo-root", str(repo),
             "--notes-dir", str(repo / ".agents" / "notes"), *extra],
            capture_output=True, text=True, encoding="utf-8")

    def test_fresh_repo_is_installed_and_hook_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp))
            r = self.install(repo)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("INSTALLED", r.stdout)
            self.assertTrue((repo / ".agents" / "notes" / "README.md").is_file())
            hook = repo / ".git" / "hooks" / "pre-commit"
            self.assertTrue(os.access(hook, os.X_OK))
            self.assertIn('--notes-dir ".agents/notes"', hook.read_text())
            rule = (repo / "AGENTS.md").read_text()
            self.assertIn("## Regression notes", rule)
            self.assertIn("same uncommitted change", rule)
            # The installed hook is a real gate: it runs and passes on the empty tree.
            run = subprocess.run(["sh", str(hook)], cwd=repo, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp))
            self.install(repo)
            r = self.install(repo)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual((repo / "AGENTS.md").read_text().count("## Regression notes"), 1)
            self.assertIn("already", r.stdout)

    def test_existing_claude_md_gets_rule_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp))
            (repo / "CLAUDE.md").write_text("# Project\n\nExisting rules.\n", encoding="utf-8")
            self.install(repo)
            text = (repo / "CLAUDE.md").read_text()
            self.assertTrue(text.startswith("# Project\n\nExisting rules.\n\n## Regression notes"))
            self.assertFalse((repo / "AGENTS.md").exists())

    def test_foreign_hook_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp))
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\nnpm run lint\n", encoding="utf-8")
            r = self.install(repo)
            self.assertNotEqual(r.returncode, 0)
            self.assertEqual(hook.read_text(), "#!/bin/sh\nnpm run lint\n")
            self.assertIn("add this line to it yourself", r.stdout)

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = git_repo(Path(tmp))
            r = self.install(repo, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("would create", r.stdout)
            self.assertFalse((repo / ".agents").exists())
            self.assertFalse((repo / "AGENTS.md").exists())
            self.assertFalse((repo / ".git" / "hooks" / "pre-commit").exists())

    def test_dry_run_requires_install(self):
        r = subprocess.run([sys.executable, str(NEW_NOTE), "--dry-run", "x"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--dry-run is only valid with --install", r.stderr)


if __name__ == "__main__":
    unittest.main()
