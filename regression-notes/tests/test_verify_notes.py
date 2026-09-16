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

    def test_new_note_scaffolds_every_class(self):
        """Every class in the verifier's closed set scaffolds and verifies. A
        `proposed` scaffold is used because an `implemented` bug-fix deliberately
        fails until its `Proved:` placeholder is replaced."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_notes", SKILL_DIR / "scripts" / "verify-notes.py")
        verifier = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verifier)
        for cls in verifier.CLASSES:
            with self.subTest(cls=cls):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    (root / "tests").mkdir()
                    (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
                    notes = root / "notes"
                    r = subprocess.run(
                        [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                         "some-topic", "--class", cls, "--status", "proposed",
                         "--notes-dir", str(notes)],
                        capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    r = subprocess.run(
                        [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                         "--notes-dir", str(notes), "--repo-root", str(root)],
                        capture_output=True, text=True)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_new_note_rejects_unknown_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "some-idea", "--class", "not-a-class",
                 "--notes-dir", str(Path(tmp) / "notes")],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("invalid choice", r.stderr)

    def test_new_note_partly_supersedes_adds_section_and_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {"implemented/bug-fix/2026-09-01-old-decision.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "new-decision", "--class", "bug-fix", "--status", "implemented",
                 "--partly-supersedes", "2026-09-01-old-decision.md",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            created = notes / "implemented" / "bug-fix" / f"{date.today():%Y-%m-%d}-new-decision.md"
            body = created.read_text(encoding="utf-8")
            self.assertIn("Partly-superseded-by: 2026-09-01-old-decision.md", body)
            self.assertIn("## Superseded", body)

    def test_new_note_supersedes_requires_archived_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "new-decision", "--status", "implemented",
                 "--supersedes", "2026-09-01-old-decision.md",
                 "--notes-dir", str(Path(tmp) / "notes")],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("archived", r.stderr)

    def test_new_note_archived_with_supersedes_emits_one_header_line_each(self):
        """The archived and supersede lines must each appear exactly once, and the
        scaffold must verify once sealed."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {"implemented/bug-fix/2026-09-01-old-decision.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "new-decision", "--class", "bug-fix", "--status", "archived",
                 "--supersedes", "2026-09-01-old-decision.md",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            created = notes / "archived" / "bug-fix" / f"{date.today():%Y-%m-%d}-new-decision.md"
            body = created.read_text(encoding="utf-8")
            self.assertEqual(body.count("Archived: "), 1, body[:200])
            self.assertEqual(body.count("Superseded-by: "), 1, body[:200])

            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root), "--seal"],
                capture_output=True, text=True)
            # The scaffold's Proved: placeholder is still there, so the note is
            # expected to fail on that alone -- not on a duplicated header line.
            self.assertIn("placeholder", r.stdout)
            self.assertNotIn("must be blank", r.stdout)

    def test_new_note_supersedes_and_partly_are_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "new-decision", "--partly-supersedes", "a.md", "--supersedes", "b.md",
                 "--status", "archived", "--notes-dir", str(Path(tmp) / "notes")],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("mutually exclusive", r.stderr)

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


class TestBindingChecks(unittest.TestCase):
    """`path::anchor` binding, and the two heuristic warnings."""

    def run_verifier(self, files, extra=None, repo_files=("tests/test_login.py",)):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for f in repo_files:
                p = root / f
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, files)
            return subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)] + (extra or []),
                capture_output=True, text=True)

    def note_with_verification(self, verification):
        return GOOD.replace(
            "Covered by `tests/test_login.py`.\n\n"
            "Proved: temporarily disabled the latch → the test above failed as expected, then reverted.",
            verification)

    def test_anchor_present_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text(
                "def test_retry_after_lockout():\n    pass\n", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {"implemented/bug-fix/2026-09-08-login-retry-race.md":
                               self.note_with_verification(
                                   "Bound to `tests/test_login.py::test_retry_after_lockout`.\n\n"
                                   "Proved: disabled the latch → that test failed, then reverted.")})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--strict-anchors"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_anchor_absent_warns(self):
        bad = self.note_with_verification(
            "Bound to `tests/test_login.py::test_renamed_away`.\n\n"
            "Proved: disabled the latch → that test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("anchor", r.stdout)
        self.assertIn("test_renamed_away", r.stdout)

    def test_anchor_absent_is_error_with_strict_anchors(self):
        bad = self.note_with_verification(
            "Bound to `tests/test_login.py::test_renamed_away`.\n\n"
            "Proved: disabled the latch → that test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": bad},
                              extra=["--strict-anchors"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("anchor", r.stdout)

    def test_cpp_scope_operator_is_not_an_anchor(self):
        """`std::filesystem` has no `std` file, so the token stays a plain path
        token and the anchor check never sees it."""
        good = self.note_with_verification(
            "Covered by `tests/test_login.py`; the failure is in `std::filesystem::path`.\n\n"
            "Proved: disabled the latch → the test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("anchor", r.stdout)

    def test_dotted_api_member_is_not_a_bare_file(self):
        """`Polygon.area` must not be reported: `area` is not a source extension."""
        good = self.note_with_verification(
            "Covered by `tests/test_login.py`; cross-checked via `Polygon.area` and `obj.value`.\n\n"
            "Proved: disabled the latch → the test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("bare filename", r.stdout)

    def test_dotted_class_name_is_not_a_bare_file(self):
        good = self.note_with_verification(
            "Covered by `tests/test_login.py`; see `CheckTool.Engine.Tests` and `R01.G.01`.\n\n"
            "Proved: disabled the latch → the test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("bare filename", r.stdout)

    def test_unresolvable_bare_source_file_warns(self):
        good = self.note_with_verification(
            "Covered by `tests/test_login.py`, renamed from `OldLoginTests.cs`.\n\n"
            "Proved: disabled the latch → the test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("OldLoginTests.cs", r.stdout)

    def test_bare_resolution_can_be_disabled(self):
        good = self.note_with_verification(
            "Covered by `tests/test_login.py`, renamed from `OldLoginTests.cs`.\n\n"
            "Proved: disabled the latch → the test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good},
                              extra=["--no-bare-resolution"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("OldLoginTests.cs", r.stdout)

    def test_stale_test_name_warns(self):
        good = self.note_with_verification(
            "`tests/test_login.py` — `test_retry_after_lockout`.\n\n"
            "Proved: disabled the latch → that test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_retry_after_lockout", r.stdout)

    def test_present_test_name_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text(
                "def test_retry_after_lockout():\n    pass\n", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {"implemented/bug-fix/2026-09-08-login-retry-race.md":
                               self.note_with_verification(
                                   "`tests/test_login.py` — `test_retry_after_lockout`.\n\n"
                                   "Proved: disabled the latch → that test failed, then reverted.")})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("test_retry_after_lockout", r.stdout)

    def test_lowercase_prose_identifier_does_not_warn(self):
        """`read_only` / `or_default` are prose, not test names."""
        good = self.note_with_verification(
            "Covered by `tests/test_login.py`; the guard is `read_only` and `or_default`.\n\n"
            "Proved: disabled the latch → the test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("read_only", r.stdout)

    def test_name_heuristic_can_be_disabled(self):
        good = self.note_with_verification(
            "`tests/test_login.py` — `test_retry_after_lockout`.\n\n"
            "Proved: disabled the latch → that test failed, then reverted.")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good},
                              extra=["--no-name-heuristic"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("test_retry_after_lockout", r.stdout)

    def test_dangling_note_reference_warns(self):
        good = GOOD + "\nSee `2026-09-01-renamed-away.md` for the earlier decision.\n"
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2026-09-01-renamed-away.md", r.stdout)

    def test_resolving_note_reference_does_not_warn(self):
        good = GOOD + "\nSee `2026-09-01-login-retry-race.md` for the earlier decision.\n"
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-08-login-retry-race.md": good,
            "implemented/bug-fix/2026-09-01-login-retry-race.md": GOOD,
        })
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("2026-09-01-login-retry-race.md", r.stdout)


class TestSupersedeChain(unittest.TestCase):
    """`Superseded-by` / `Partly-superseded-by` header lines."""

    def run_verifier(self, files, extra=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, files)
            return subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)] + (extra or []),
                capture_output=True, text=True)

    @staticmethod
    def with_header(note, line):
        return note.replace("Status: implemented", f"Status: implemented\n{line}", 1)

    def test_partly_superseded_with_section_passes(self):
        note = self.with_header(
            GOOD, "Partly-superseded-by: 2026-09-09-login-retry-race.md").replace(
            "## Verification", "## Superseded\n\nThe retry cap no longer applies.\n\n## Verification")
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-08-login-retry-race.md": note,
            "implemented/bug-fix/2026-09-09-login-retry-race.md": GOOD,
        })
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_partly_superseded_without_section_fails(self):
        note = self.with_header(GOOD, "Partly-superseded-by: 2026-09-09-login-retry-race.md")
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-08-login-retry-race.md": note,
            "implemented/bug-fix/2026-09-09-login-retry-race.md": GOOD,
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Superseded", r.stdout)

    def test_unresolvable_supersede_target_fails(self):
        note = self.with_header(
            GOOD, "Partly-superseded-by: 2026-09-09-gone-away.md").replace(
            "## Verification", "## Superseded\n\nGone.\n\n## Verification")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": note})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not resolve", r.stdout)

    def test_supersede_self_reference_fails(self):
        note = self.with_header(
            GOOD, "Partly-superseded-by: 2026-09-08-login-retry-race.md").replace(
            "## Verification", "## Superseded\n\nSelf.\n\n## Verification")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": note})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("itself", r.stdout)

    def test_supersede_cycle_fails(self):
        a = self.with_header(GOOD, "Partly-superseded-by: 2026-09-09-login-retry-race.md").replace(
            "## Verification", "## Superseded\n\nA.\n\n## Verification")
        b = self.with_header(GOOD, "Partly-superseded-by: 2026-09-08-login-retry-race.md").replace(
            "## Verification", "## Superseded\n\nB.\n\n## Verification")
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-08-login-retry-race.md": a,
            "implemented/bug-fix/2026-09-09-login-retry-race.md": b,
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cycle", r.stdout)

    def test_superseded_by_outside_archive_fails(self):
        note = self.with_header(
            GOOD, "Superseded-by: 2026-09-09-login-retry-race.md").replace(
            "## Verification", "## Superseded\n\nGone.\n\n## Verification")
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-08-login-retry-race.md": note,
            "implemented/bug-fix/2026-09-09-login-retry-race.md": GOOD,
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("archived", r.stdout)

    def test_superseded_by_in_archive_passes(self):
        archived = GOOD.replace(
            "Status: implemented",
            f"Status: implemented\nArchived: {date.today():%Y-%m-%d}\n"
            "Superseded-by: 2026-09-09-login-retry-race.md", 1)
        r = self.run_verifier({
            "archived/bug-fix/2026-09-01-login-retry-race.md": archived,
            "implemented/bug-fix/2026-09-09-login-retry-race.md": GOOD,
        }, extra=["--seal"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unknown_supersede_kind_is_a_header_error(self):
        note = self.with_header(GOOD, "Supersedes: 2026-09-09-login-retry-race.md")
        r = self.run_verifier({
            "implemented/bug-fix/2026-09-08-login-retry-race.md": note,
            "implemented/bug-fix/2026-09-09-login-retry-race.md": GOOD,
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("blank", r.stdout)


class TestFindNotes(unittest.TestCase):
    def test_find_matches_body_and_prints_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD,
                "implemented/bug-fix/2026-09-09-unrelated-topic.md":
                    GOOD.replace("Fix login retry race", "Something else entirely")
                        .replace("Retries overlap and double-submit.", "Unrelated problem.")
                        .replace("Serialize retries behind a latch.", "Unrelated decision.")
                        .replace("disabled the latch", "disabled the unrelated guard"),
            })
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--find", "latch"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("implemented/bug-fix/2026-09-08-login-retry-race.md", r.stdout)
            self.assertIn("tests/test_login.py", r.stdout)
            self.assertNotIn("unrelated-topic", r.stdout)

    def test_find_with_no_match_reports_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes"
            write_tree(notes, {"implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--find", "nothing-matches-this"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("0 note(s) matched", r.stdout)


class TestGenericOptimizations(unittest.TestCase):
    """Dedupe, per-note disable, baseline, changed-only, archived scaffold."""

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

    def test_archived_scaffold_uses_implemented_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "arch-topic", "--class", "bug-fix", "--status", "archived",
                 "--test", "tests/test_login.py",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            created = list(notes.rglob("*.md"))
            self.assertEqual(len(created), 1)
            lines = created[0].read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[2], "Status: implemented")
            self.assertTrue(lines[3].startswith("Archived: "))

    def test_dangling_note_warns_once(self):
        good = GOOD + "\nSee `2026-09-01-renamed-away.md` for the earlier decision.\n"
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("2026-09-01-renamed-away.md"), 1)
        self.assertIn("references note", r.stdout)
        self.assertNotIn("bare filename", r.stdout)

    def test_per_note_disable(self):
        good = (GOOD + "\nSee `2026-09-01-renamed-away.md`.\n\n"
                "<!-- verify-disable: note-ref, bare-resolution -->\n")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("2026-09-01-renamed-away.md", r.stdout)

    def test_baseline_update_and_suppress(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md":
                    GOOD + "\nSee `2026-09-01-renamed-away.md`.\n"})
            base = root / "baseline.json"
            r1 = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--baseline", str(base), "--update-baseline"],
                capture_output=True, text=True)
            self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
            data = json.loads(base.read_text(encoding="utf-8"))
            self.assertIn("warnings", data)
            self.assertTrue(data["warnings"])
            # Portable keys: no absolute temp path leaks into the baseline.
            self.assertNotIn(str(root), json.dumps(data))
            r2 = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--baseline", str(base)],
                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertNotIn("WARNING", r2.stdout)

    def test_changed_only_limits_warnings_but_not_errors(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True,
                           capture_output=True)
            subprocess.run(["git", "config", "user.email", "t@t"],
                           cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "t"],
                           cwd=root, check=True, capture_output=True)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-a.md":
                    GOOD + "\nSee `2026-09-01-gone-a.md`.\n",
                "implemented/bug-fix/2026-09-08-b.md": GOOD,
            })
            subprocess.run(["git", "add", "."], cwd=root, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True,
                           capture_output=True)
            (notes / "implemented" / "bug-fix" / "2026-09-08-b.md").write_text(
                GOOD + "\nSee `2026-09-02-gone-b.md`.\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True,
                           capture_output=True)
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--changed-only", "--base", "HEAD"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("2026-09-01-gone-a.md", r.stdout)
            self.assertIn("2026-09-02-gone-b.md", r.stdout)

    def test_changed_only_subdir_repo_root(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp)
            sub = top / "sub"
            notes = sub / "notes"
            (sub / "tests").mkdir(parents=True)
            (sub / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-a.md": GOOD,
                "implemented/bug-fix/2026-09-08-b.md": GOOD,
            })
            for args in (["git", "init", "-q"],
                         ["git", "config", "user.email", "t@t"],
                         ["git", "config", "user.name", "t"]):
                subprocess.run(args, cwd=top, check=True, capture_output=True)
            subprocess.run(["git", "add", "."], cwd=top, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=top, check=True,
                           capture_output=True)
            # Tracked change inside the subdir repo-root: must stay visible.
            (notes / "implemented" / "bug-fix" / "2026-09-08-a.md").write_text(
                GOOD + "\nSee `2026-09-01-gone-sub.md`.\n", encoding="utf-8")
            # Untracked note in the same subdir: must stay visible too.
            (notes / "implemented" / "bug-fix" / "2026-09-08-c.md").write_text(
                GOOD + "\nSee `2026-09-02-gone-new.md`.\n", encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(sub),
                 "--changed-only", "--base", "HEAD"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("2026-09-01-gone-sub.md", r.stdout)
            self.assertIn("2026-09-02-gone-new.md", r.stdout)

    def test_changed_only_gitignored_notes_still_warn(self):
        import shutil
        if shutil.which("git") is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for args in (["git", "init", "-q"],
                         ["git", "config", "user.email", "t@t"],
                         ["git", "config", "user.name", "t"]):
                subprocess.run(args, cwd=root, check=True, capture_output=True)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            (root / ".gitignore").write_text("notes-ignored/\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True,
                           capture_output=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True,
                           capture_output=True)
            notes = root / "notes-ignored"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-a.md":
                    GOOD + "\nSee `2026-09-01-gone-ignored.md`.\n"})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--changed-only", "--base", "HEAD"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("2026-09-01-gone-ignored.md", r.stdout)

    def test_anchor_disable_is_warning_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text(
                "def test_present():\n    pass\n", encoding="utf-8")
            notes = root / "notes"
            body = GOOD.replace(
                "Covered by `tests/test_login.py`.",
                "Bound to `tests/test_login.py::test_missing`.")
            body += "\n<!-- verify-disable: anchor -->\n"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": body})
            plain = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
            self.assertNotIn("anchor", plain.stdout)
            strict = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--strict-anchors"],
                capture_output=True, text=True)
            self.assertNotEqual(strict.returncode, 0)
            self.assertIn("anchor", strict.stdout)

    def test_fenced_disable_comment_is_ignored(self):
        good = (GOOD + "\nSee `2026-09-01-gone-fenced.md`.\n\n"
                "```\n<!-- verify-disable: all -->\n```\n")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2026-09-01-gone-fenced.md", r.stdout)

    def test_update_baseline_with_changed_only_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--baseline", str(root / "b.json"), "--update-baseline",
                 "--changed-only"],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("changed-only", r.stderr)

    def test_baseline_version_check(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD})
            bad = root / "bad.json"
            bad.write_text(json.dumps({"version": 999, "warnings": []}),
                           encoding="utf-8")
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--baseline", str(bad)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("baseline", r.stdout)
            legacy = root / "legacy.json"
            legacy.write_text(json.dumps({"warnings": []}), encoding="utf-8")
            r2 = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--baseline", str(legacy)],
                capture_output=True, text=True)
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)

    def test_supersedes_help_names_successor(self):
        r = subprocess.run(
            [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"), "--help"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("successor", r.stdout)
        self.assertIn("Superseded-by", r.stdout)

    def test_inline_code_span_disable_is_ignored(self):
        good = (GOOD + "\nSee `2026-09-01-gone-inline.md`.\n\n"
                "`<!-- verify-disable: all -->`\n")
        r = self.run_verifier({"implemented/bug-fix/2026-09-08-login-retry-race.md": good})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2026-09-01-gone-inline.md", r.stdout)

    def test_warning_key_short_prefix(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_notes", SKILL_DIR / "scripts" / "verify-notes.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        key = module.warning_key(
            "WARNING: notes/implemented/bug-fix/2026-09-08-a.md: "
            "see `tests/test_notes.py`",
            Path("notes"), Path("."))
        self.assertTrue(key.startswith("WARNING: $NOTES/"), key)
        self.assertIn("test_notes.py", key)


class TestArchiveFlow(unittest.TestCase):
    """One-step archive, duplicate basenames, translations, install check."""

    def test_archive_moves_adds_header_and_seals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-01-old-decision.md": GOOD,
                "implemented/bug-fix/2026-09-09-new-decision.md": GOOD,
            })
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "--archive", "2026-09-01-old-decision.md",
                 "--by", "2026-09-09-new-decision.md",
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            dst = notes / "archived" / "bug-fix" / "2026-09-01-old-decision.md"
            self.assertFalse(
                (notes / "implemented" / "bug-fix" / "2026-09-01-old-decision.md").exists())
            lines = dst.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[2], "Status: implemented")
            self.assertTrue(lines[3].startswith("Archived: "))
            self.assertEqual(lines[4], "Superseded-by: 2026-09-09-new-decision.md")
            self.assertTrue((notes / "archived" / "manifest.json").exists())
            v = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(v.returncode, 0, v.stdout + v.stderr)

    def test_archive_missing_old_fails_without_touching_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-09-new-decision.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "--archive", "2026-09-01-nope.md",
                 "--successor", "2026-09-09-new-decision.md",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertTrue(
                (notes / "implemented" / "bug-fix" / "2026-09-09-new-decision.md").exists())
            self.assertFalse((notes / "archived").exists())

    def test_archive_missing_successor_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-01-old-decision.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "new-note.py"),
                 "--archive", "2026-09-01-old-decision.md",
                 "--successor", "2026-09-09-gone.md",
                 "--notes-dir", str(notes)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("successor", r.stderr)
            self.assertTrue(
                (notes / "implemented" / "bug-fix" / "2026-09-01-old-decision.md").exists())

    def test_duplicate_basename_fails(self):
        feature = GOOD.replace(
            "Covered by `tests/test_login.py`.\n\n"
            "Proved: temporarily disabled the latch \u2192 the test above failed as expected, then reverted.",
            "No test binding needed for non-bug-fix classes.")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-same-name.md": GOOD,
                "implemented/feature/2026-09-08-same-name.md": feature,
            })
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("duplicate", r.stdout)

    def test_zh_translation_sibling_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD,
                "implemented/bug-fix/2026-09-08-login-retry-race.zh.md":
                    "garbage, not a note at all\n",
            })
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_check_install_ok_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD})
            hook = root / ".git" / "hooks" / "pre-commit"
            hook.parent.mkdir(parents=True)
            hook.write_text(
                "#!/bin/sh\npython3 regression-notes/scripts/verify-notes.py "
                "--notes-dir .agents/notes || exit 1\n", encoding="utf-8")
            run = lambda: subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--check-install"],
                capture_output=True, text=True)
            ok = run()
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("INSTALLED", ok.stdout)
            hook.unlink()
            missing = run()
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("NOT INSTALLED", missing.stdout)

    def test_base_without_changed_only_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": GOOD})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root),
                 "--base", "HEAD"],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("ignoring --base", r.stderr)

    def test_nospace_disable_comment_works(self):
        good = (GOOD + "\nSee `2026-09-01-gone-nospace.md`.\n\n"
                "<!-- verify-disable:note-ref-->")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_login.py").write_text("x", encoding="utf-8")
            notes = root / "notes"
            write_tree(notes, {
                "implemented/bug-fix/2026-09-08-login-retry-race.md": good})
            r = subprocess.run(
                [sys.executable, str(SKILL_DIR / "scripts" / "verify-notes.py"),
                 "--notes-dir", str(notes), "--repo-root", str(root)],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("2026-09-01-gone-nospace.md", r.stdout)


if __name__ == "__main__":
    unittest.main()
