import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent

GOOD = """# Agent Note: Fix login retry race

Status: implemented

## Problem

Retries overlap and double-submit.

## Decision

Serialize retries behind a latch.

## Alternatives considered

**Unbounded retries.** Wastes quota and still races.

## Consequences

One in-flight retry at a time.

## Verification

Covered by `tests/test_login.py`.

Proved: temporarily disabled the latch → the test above failed as expected, then reverted.
"""

ARCHIVED_GOOD = GOOD.replace(
    "Status: implemented",
    f"Status: implemented\nArchived: {date.today():%Y-%m-%d}",
)


def write_tree(root, notes):
    for rel, content in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


class TestVerifyNotes(unittest.TestCase):
    def run_verifier(self, files, extra=None, repo_files=("tests/test_login.py",)):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for f in repo_files:
                p = root / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, files)
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)] + (extra or []),
                capture_output=True, text=True)
            return r

    def test_good_note_passes(self):
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_missing_alternatives_fails(self):
        bad = GOOD.replace("## Alternatives considered", "## Notes")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Alternatives considered", r.stdout)

    def test_status_mismatch_fails(self):
        bad = GOOD.replace("Status: implemented", "Status: proposed")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Status:", r.stdout)

    def test_missing_verification_target_fails(self):
        bad = GOOD.replace("tests/test_login.py", "tests/test_missing.py")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not found", r.stdout)

    def test_missing_verification_target_warns_with_no_strict(self):
        bad = GOOD.replace("tests/test_login.py", "tests/test_missing.py")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad},
                              extra=["--no-strict"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("WARNING", r.stdout)

    def test_missing_proved_line_fails(self):
        bad = GOOD.replace(
            "Proved: temporarily disabled the latch → the test above failed as expected, then reverted.\n",
            "")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Proved", r.stdout)

    def test_missing_notes_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(root / "nonexistent"), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)

    def test_missing_notes_dir_allow_missing_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(root / "nonexistent"), "--allow-missing"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("allow-missing", r.stdout)

    def test_nested_note_fails(self):
        r = self.run_verifier({"implemented/bug-fix/sub/2026-09-08-login-retry-race.md": GOOD})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no subdirectories", r.stdout)

    def test_new_note_proposed_passes_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "login-retry-race", "--status", "proposed",
                 "--test", "tests/test_login.py",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_new_note_rejected_passes_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "login-retry-race", "--status", "rejected",
                 "--reason", "duplicates the existing retry policy",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_new_note_rejected_requires_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes"
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "login-retry-race", "--status", "rejected",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("reason", r.stderr.lower())

    def test_new_note_rejects_non_bugfix_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "some-idea", "--class", "feature",
                 "--notes-dir", str(Path(tmp) / "notes")],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("bug-fix", r.stderr)

    def test_snapshot_style_lock_passes(self):
        good = GOOD.replace("tests/test_login.py", "snapshots/session/login-retry/session.jsonl")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good},
                              repo_files=("snapshots/session/login-retry/session.jsonl",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_command_not_path_passes(self):
        good = GOOD.replace("Covered by `tests/test_login.py`.",
                            "Covered by `tests/test_login.py`; run via `pnpm run test:e2e`.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_archived_note_passes_when_sealed(self):
        r = self.run_verifier({"archived/bug-fix/2026-09-01-login-retry-race.md": ARCHIVED_GOOD},
                              extra=["--seal"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sealed 1 archived note(s)", r.stdout)

    def test_unsealed_archived_note_fails(self):
        r = self.run_verifier({"archived/bug-fix/2026-09-01-login-retry-race.md": ARCHIVED_GOOD})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not sealed", r.stdout)

    def test_archived_note_missing_date_fails(self):
        r = self.run_verifier({"archived/bug-fix/2026-09-01-login-retry-race.md": GOOD})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Archived", r.stdout)

    def test_archived_date_before_filename_fails(self):
        archived = GOOD.replace(
            "Status: implemented",
            "Status: implemented\nArchived: 2026-08-01"
        )
        r = self.run_verifier({"archived/bug-fix/2026-09-08-login-retry-race.md": archived})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("archived date", r.stdout)

    def test_simplification_class_note_passes(self):
        simple = """# Agent Note: Remove hand-rolled retry queue

Status: implemented

## Problem

The codebase maintains a custom bounded queue that duplicates the vendored one.

## Decision

Use the vendored `Queue` with the existing timeout adapter.

## Alternatives considered

**Keep the custom queue.** It carries extra tests and no upstream maintenance.

## Consequences

Less owned code; the vendored queue's contract now owns timeout behavior.
"""
        r = self.run_verifier({"implemented/simplification/2026-09-01-remove-hand-rolled-queue.md": simple},
                              repo_files=())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_invalid_filename_date_fails(self):
        r = self.run_verifier({"implemented/bug-fix/2026-13-99-login-retry-race.md": GOOD})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("filename date", r.stdout)

    def test_rejected_note_passes(self):
        rejected = """# Agent Note: Retry with exponential backoff

Status: rejected — duplicates the existing retry policy

## Problem

Retries overlap and double-submit.

## Proposal

Add exponential backoff to the login retry path.

## Alternatives considered

**Use the existing retry policy.** The shared policy already handles backoff and jitter; a per-path duplicate would drift.

## Acceptance criteria

<Observable state that means done.>

## Risks

<What could go wrong, and what the change gives up.>
"""
        r = self.run_verifier({"rejected/bug-fix/2026-09-01-retry-backoff.md": rejected},
                              repo_files=())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_rejected_note_missing_reason_fails(self):
        rejected = """# Agent Note: Retry with exponential backoff

Status: rejected —

## Problem

Retries overlap and double-submit.

## Proposal

Add exponential backoff to the login retry path.

## Alternatives considered

**Use the existing retry policy.** The shared policy already handles backoff and jitter.
"""
    def test_rejected_note_missing_reason_fails(self):
        rejected = """# Agent Note: Retry with exponential backoff

Status: rejected —

## Problem

Retries overlap and double-submit.

## Proposal

Add exponential backoff to the login retry path.

## Alternatives considered

**Use the existing retry policy.** The shared policy already handles backoff and jitter.
"""
        r = self.run_verifier({"rejected/bug-fix/2026-09-01-retry-backoff.md": rejected},
                              repo_files=())
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Status:", r.stdout)

    def test_short_note_fails_cleanly(self):
        r = self.run_verifier({"implemented/bug-fix/2026-09-01-login-retry-race.md":
                               "# Agent Note: x\nStatus: implemented\n"})
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)

    def test_short_archived_note_fails_cleanly(self):
        short = "# Agent Note: x\n\nStatus: implemented\n"
        r = self.run_verifier({"archived/bug-fix/2026-09-01-login-retry-race.md": short},
                              repo_files=())
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)

    def test_stray_non_markdown_file_fails(self):
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-01-login-retry-race.md": GOOD,
            "implemented/bug-fix/stray.txt": "x",
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("only `.md`", r.stdout)

    def test_template_placeholder_proved_fails(self):
        bad = GOOD.replace(
            "Proved: temporarily disabled the latch → the test above failed as expected, then reverted.",
            "Proved: <what you re-broke> → the test above failed as expected, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("placeholder", r.stdout)

    def test_new_note_rejects_empty_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "登录重试", "--notes-dir", str(Path(tmp) / "notes")],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("slug", r.stderr.lower())

    def test_seal_roundtrip_and_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {"archived/bug-fix/2026-09-01-login-retry-race.md": ARCHIVED_GOOD})

            def run(*extra):
                return subprocess.run(
                    [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                     "--notes-dir", str(notes), "--repo-root", str(root), *extra],
                    capture_output=True, text=True)

            unsealed = run()
            self.assertNotEqual(unsealed.returncode, 0)
            self.assertIn("not sealed", unsealed.stdout)

            sealed = run("--seal")
            self.assertEqual(sealed.returncode, 0, sealed.stdout + sealed.stderr)
            manifest = notes / "archived" / "manifest.json"
            self.assertTrue(manifest.exists())
            before = manifest.read_bytes()
            self.assertEqual(run().returncode, 0)

            note = notes / "archived" / "bug-fix" / "2026-09-01-login-retry-race.md"
            note.write_text(ARCHIVED_GOOD + "\nTampered.", encoding="utf-8")
            tampered = run()
            self.assertNotEqual(tampered.returncode, 0)
            self.assertIn("modified", tampered.stdout)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertNotEqual(run("--seal").returncode, 0)
            self.assertEqual(manifest.read_bytes(), before)

    def test_sealed_note_deletion_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {"archived/bug-fix/2026-09-01-login-retry-race.md": ARCHIVED_GOOD})
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            run = lambda *extra: subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root), *extra],
                capture_output=True, text=True)
            self.assertEqual(run("--seal").returncode, 0)
            (notes / "archived" / "bug-fix" / "2026-09-01-login-retry-race.md").unlink()
            r = run()
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("missing", r.stdout)

    def test_malformed_manifest_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {"archived/bug-fix/2026-09-01-login-retry-race.md": ARCHIVED_GOOD})
            (notes / "archived" / "manifest.json").write_text("not json", encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("manifest", r.stdout)


if __name__ == "__main__":
    unittest.main()
