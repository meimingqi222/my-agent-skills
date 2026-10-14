"""Keeping the corpus usable as it grows: compact capped --for-path, the
duplicate hint when scaffolding, and deliberate retirement of archived notes."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
VERIFY = SKILL_DIR / "scripts" / "verify-notes.py"
NEW_NOTE = SKILL_DIR / "scripts" / "new-note.py"


def note(title, cited, decision="Serialize retries behind a latch.", test="tests/test_login.py"):
    return f"""# Agent Note: {title}

Status: implemented

## Problem

Wrong output in `{cited}`.

## Decision

{decision}

## Alternatives considered

**Unbounded retries.** Still races.

## Consequences

One in-flight retry at a time.

## Verification

- `{test}::test_retry`

Proved: pre-fix run failed at the latch assertion; output saved inline above.
"""


def run(script, *args, cwd):
    return subprocess.run([sys.executable, str(script), *args], cwd=cwd,
                          capture_output=True, text=True, encoding="utf-8")


class Repo:
    def __init__(self, tmp):
        self.root = Path(tmp)
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_login.py").write_text("def test_retry(): pass\n")
        (self.root / "src").mkdir()
        (self.root / "src" / "core.py").write_text("x\n")
        self.notes = self.root / ".agents" / "notes"

    def write(self, rel, text):
        p = self.notes / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def verify(self, *args):
        return run(VERIFY, "--notes-dir", str(self.notes), *args, cwd=self.root)

    def new_note(self, *args):
        return run(NEW_NOTE, "--notes-dir", str(self.notes), "--repo-root", str(self.root),
                   *args, cwd=self.root)


class TestForPath(unittest.TestCase):
    def test_compact_newest_first_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            for day in range(1, 16):
                repo.write(f"implemented/bug-fix/2026-01-{day:02d}-fix-{day}.md",
                           note(f"Fix {day}", "src/core.py", decision=f"Decision {day}."))
            r = repo.verify("--for-path", "src/core.py")
            self.assertEqual(r.returncode, 0, r.stderr)
            entries = [ln for ln in r.stdout.splitlines() if ln.startswith("[locks]")]
            self.assertEqual(len(entries), 10)
            self.assertIn("2026-01-15-fix-15.md: Fix 15", entries[0])
            self.assertIn("    Decision 15.", r.stdout)
            self.assertIn("... and 5 more", r.stdout)
            self.assertIn("15 note(s) cite 1 queried path(s)", r.stdout)
            r_all = repo.verify("--for-path", "src/core.py", "--limit", "0")
            self.assertEqual(r_all.stdout.count("[locks]"), 15)
            self.assertNotIn("more;", r_all.stdout)

    def test_archived_counted_not_listed_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            repo.write("implemented/bug-fix/2026-01-02-new.md", note("New", "src/core.py"))
            old = note("Old", "src/core.py").replace(
                "Status: implemented",
                f"Status: implemented\nArchived: {date.today():%Y-%m-%d}\n"
                f"Superseded-by: 2026-01-02-new.md")
            repo.write("archived/bug-fix/2026-01-01-old.md", old)
            r = repo.verify("--for-path", "src/core.py")
            self.assertNotIn("[history]", r.stdout)
            self.assertIn("1 archived note(s) also cite", r.stdout)
            r2 = repo.verify("--for-path", "src/core.py", "--include-archived")
            self.assertIn("[history] archived/bug-fix/2026-01-01-old.md: Old", r2.stdout)


class TestDuplicateHint(unittest.TestCase):
    def test_existing_owner_is_suggested(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            repo.write("implemented/bug-fix/2026-01-01-retry.md", note("Retry latch", "src/core.py"))
            r = repo.new_note("--status", "implemented",
                              "--test", "tests/test_login.py::test_other", "another-fix")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("1 existing note(s) already cite tests/test_login.py", r.stdout)
            self.assertIn("2026-01-01-retry.md: Retry latch", r.stdout)
            self.assertIn("created", r.stdout)

    def test_no_hint_without_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            r = repo.new_note("--status", "implemented", "--test", "tests/test_login.py", "first")
            self.assertNotIn("existing note", r.stdout)


class TestRetire(unittest.TestCase):
    def archived_tree(self, repo):
        repo.write("implemented/bug-fix/2026-01-02-new.md", note("New", "src/core.py"))
        repo.write("implemented/bug-fix/2026-01-01-old.md", note("Old", "src/core.py"))
        r = repo.new_note("--archive", "2026-01-01-old.md", "--successor", "2026-01-02-new.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return repo.notes / "archived" / "bug-fix" / "2026-01-01-old.md"

    def test_plain_deletion_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            self.archived_tree(repo).unlink()
            r = repo.verify()
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("sealed archived note is missing", r.stdout + r.stderr)

    def test_retire_removes_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            path = self.archived_tree(repo)
            r = repo.new_note("--retire", path.name, "--reason", "core.py rewritten")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertFalse(path.exists())
            manifest = json.loads((repo.notes / "archived" / "manifest.json").read_text())
            entry = manifest["archived/bug-fix/2026-01-01-old.md"]
            self.assertEqual(entry["reason"], "core.py rewritten")
            self.assertEqual(len(entry["sha256"]), 64)
            self.assertEqual(repo.verify().returncode, 0)
            # Re-sealing must not resurrect or rewrite the retirement.
            self.assertEqual(repo.verify("--seal").returncode, 0)
            self.assertTrue(json.loads((repo.notes / "archived" / "manifest.json")
                                       .read_text())["archived/bug-fix/2026-01-01-old.md"]["retired"])

    def test_reappearing_retired_note_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            path = self.archived_tree(repo)
            text = path.read_text()
            repo.new_note("--retire", path.name, "--reason", "gone")
            path.write_text(text)
            r = repo.verify()
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("has reappeared", r.stdout + r.stderr)

    def test_refuses_when_pointed_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            path = self.archived_tree(repo)
            older = note("Older", "src/core.py").replace(
                "Status: implemented",
                f"Status: implemented\nArchived: {date.today():%Y-%m-%d}\n"
                f"Superseded-by: {path.name}")
            repo.write("archived/bug-fix/2025-12-01-older.md", older)
            r = repo.new_note("--retire", path.name, "--reason", "gone")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("points at", r.stderr)
            self.assertTrue(path.exists())

    def test_only_archived_notes_retire(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(tmp)
            repo.write("implemented/bug-fix/2026-01-01-live.md", note("Live", "src/core.py"))
            r = repo.new_note("--retire", "2026-01-01-live.md", "--reason", "x")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("only archived notes", r.stderr)


if __name__ == "__main__":
    unittest.main()
