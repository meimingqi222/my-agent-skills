#!/usr/bin/env python3
"""Verify regression-notes tree: layout, format, status consistency, test binding.

Usage:
    python3 verify-notes.py [--notes-dir .agents/notes] [--repo-root .] [--no-strict] [--allow-missing] [--seal]

Exit non-zero on any error. With --no-strict, a missing regression-test
path degrades to a warning; with --allow-missing, a missing notes directory
does not fail. --seal verifies the tree, then records every archived note's
SHA-256 in `archived/manifest.json`; once sealed, any later modification or
deletion of an archived note fails verification.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

LIFECYCLES = ("proposed", "implemented", "rejected", "archived")
CLASSES = ("bug-fix", "feature", "architecture", "process", "testing", "simplification")
FILENAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-.+\.md$")
SKIP_NAMES = {"AGENTS.md", "CLAUDE.md", "README.md", "manifest.json"}
MANIFEST_NAME = "manifest.json"

# Per-lifecycle status line grammar.
STATUS_RE = {
    "proposed": re.compile(r"^Status: proposed$"),
    "implemented": re.compile(r"^Status: implemented$"),
    "rejected": re.compile(r"^Status: rejected — .+$"),
    "archived": re.compile(r"^Status: implemented$"),
}

# Required ## headings beyond the universal opener `## Problem`.
REQUIRED = {
    "proposed": ["Proposal", "Acceptance criteria", "Risks"],
    "implemented": ["Decision", "Consequences"],
    "rejected": ["Proposal", "Alternatives considered"],
    "archived": ["Decision", "Consequences"],
}

BANNED_IMPLEMENTED_HEADINGS = re.compile(
    r"^## (?:Proposal\b|Plan\b|Migration plan\b|Acceptance criteria\b)", re.I
)

# Backticked path-like token that contains a directory separator.
TEST_RE = re.compile(r"`([^`\s]*[/\\][^`\s]*)`")
PROVED_RE = re.compile(r"^Proved:\s*\S", re.M)


def valid_date(year, month, day):
    try:
        return datetime.date(int(year), int(month), int(day))
    except ValueError:
        return None


def strip_fences(text):
    out, in_fence = [], False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def parse_header(lines, lifecycle, path):
    """Validate the header block; returns a list of errors."""
    errors = []

    if not re.match(r"^# Agent Note: \S", lines[0]):
        errors.append(f"{path}: L1 must be `# Agent Note: <title>`")
    if lines[1].strip() != "":
        errors.append(f"{path}: L2 must be blank")

    want = STATUS_RE[lifecycle]
    if not want.match(lines[2].strip()):
        errors.append(
            f"{path}: L3 must match the `{lifecycle}/` status grammar ({want.pattern})"
        )

    if lifecycle == "archived":
        if len(lines) < 5:
            return [f"{path}: archived notes need `Archived: YYYY-MM-DD` after `Status:`"]
        archived_match = re.match(r"^Archived: (\d{4})-(\d{2})-(\d{2})$", lines[3].strip())
        if not archived_match:
            errors.append(f"{path}: L4 must be `Archived: YYYY-MM-DD`")
        else:
            archive_date = valid_date(*archived_match.groups())
            today = datetime.date.today()
            if not archive_date:
                errors.append(f"{path}: archived date is not a valid calendar date")
            elif archive_date > today:
                errors.append(f"{path}: archived date cannot be in the future")
        if lines[4].strip() != "":
            errors.append(f"{path}: L5 must be blank for archived notes")
    else:
        if lines[3].strip() != "":
            errors.append(f"{path}: L4 must be blank")

    return errors


def note_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(notes):
    """The archived-freeze manifest, or None when the file is not a JSON object."""
    path = notes / "archived" / MANIFEST_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def check_file(path, lifecycle, cls, repo_root, strict):
    errors, warnings = [], []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        return [f"{path}: cannot read: {e}"], []

    lines = text.splitlines()
    if len(lines) < 4:
        return [f"{path}: header block too short"], []
    errors.extend(parse_header(lines, lifecycle, path))

    # Exactly one Status: line in the entire prose, and it matches the header.
    prose = strip_fences(text)
    status_lines = [ln for ln in prose if ln.startswith("Status:")]
    if len(status_lines) != 1:
        errors.append(f"{path}: exactly one `Status:` line required")
    elif lines and status_lines[0] != lines[2]:
        errors.append(f"{path}: the only `Status:` line must be L3")

    h2s = [ln.strip()[3:].strip() for ln in prose if ln.startswith("## ")]
    if not h2s or h2s[0] != "Problem":
        errors.append(f"{path}: body must open with `## Problem`")

    for sec in REQUIRED[lifecycle]:
        if sec not in h2s:
            errors.append(f"{path}: missing `## {sec}` for `{lifecycle}/`")

    if "Alternatives considered" not in h2s:
        errors.append(f"{path}: missing mandatory `## Alternatives considered`")

    if lifecycle in ("implemented", "archived"):
        for h2 in h2s:
            if BANNED_IMPLEMENTED_HEADINGS.match(f"## {h2}"):
                errors.append(
                    f"{path}: `{h2}` is a proposal-era heading; implemented/archived notes state what is"
                )

    if cls == "bug-fix" and lifecycle in ("implemented", "archived"):
        if "Verification" not in h2s:
            errors.append(f"{path}: bug-fix notes require `## Verification`")
        else:
            body = "\n".join(prose)
            ver = body.split("## Verification", 1)[1]
            ver = re.split(r"^## ", ver, maxsplit=1, flags=re.M)[0]

            if not PROVED_RE.search(ver):
                errors.append(
                    f"{path}: `## Verification` must contain a `Proved:` line recording the red-run proof"
                )
            if "<what you re-broke>" in ver:
                errors.append(f"{path}: `Proved:` line still carries the template placeholder")

            targets = [t.strip() for t in TEST_RE.findall(ver)]
            if not targets:
                errors.append(
                    f"{path}: `## Verification` must reference at least one test path in backticks"
                )
            else:
                missing = [t for t in targets if not (repo_root / t).exists()]
                if missing:
                    msg = f"{path}: verification target(s) not found: {', '.join(missing)}"
                    if strict:
                        errors.append("ERROR: " + msg)
                    else:
                        warnings.append("WARNING: " + msg)

    # Filename date must be a real calendar date and not in the future.
    filename_match = FILENAME_RE.match(path.name)
    if filename_match:
        file_date = valid_date(*filename_match.groups())
        today = datetime.date.today()
        if not file_date:
            errors.append(f"{path}: filename date is not a valid calendar date")
        elif file_date > today:
            errors.append(f"{path}: filename date cannot be in the future")
        elif lifecycle == "archived":
            archive_match = re.match(r"^Archived: (\d{4})-(\d{2})-(\d{2})$", lines[3].strip() or "")
            if archive_match:
                archive_date = valid_date(*archive_match.groups())
                if archive_date and file_date and archive_date < file_date:
                    errors.append(
                        f"{path}: archived date `{lines[3].strip()}` is before filename date `{path.name[:10]}`"
                    )

    return errors, warnings


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes-dir", default=os.environ.get("NOTES_DIR", ".agents/notes"))
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--no-strict", action="store_true")
    ap.add_argument(
        "--allow-missing",
        action="store_true",
        help="treat a missing notes directory as OK (not recommended for CI)",
    )
    ap.add_argument(
        "--seal",
        action="store_true",
        help="record the SHA-256 of every archived note in archived/manifest.json",
    )
    args = ap.parse_args(argv)

    notes = Path(args.notes_dir)
    repo = Path(args.repo_root)
    strict = not args.no_strict
    errors, warnings = [], []
    archived = []

    if not notes.is_dir():
        msg = f"no notes dir at {notes}"
        if args.allow_missing:
            print(f"{msg}, nothing to verify (allow-missing)")
            return 0
        print(f"{msg}", file=sys.stderr)
        return 1

    if (notes / "INDEX.md").exists():
        errors.append(f"{notes}/INDEX.md: no centralized index, the tree is the index")

    for top in sorted(p for p in notes.iterdir() if p.name not in SKIP_NAMES):
        if not top.is_dir():
            errors.append(f"{notes}/{top.name}: unexpected file at notes root")
            continue
        if top.name not in LIFECYCLES:
            errors.append(f"{notes}/{top.name}: unknown lifecycle folder")
            continue
        for second in sorted(top.iterdir()):
            if second.name in SKIP_NAMES:
                continue
            if second.is_file():
                errors.append(f"{second}: notes must live at `{top.name}/<class>/yyyy-mm-dd-topic.md`")
                continue
            if second.name not in CLASSES:
                errors.append(f"{second}: unknown class folder (allowed: {', '.join(CLASSES)})")
                continue
            for stray in sorted(second.rglob("*")):
                if stray.is_file() and stray.suffix != ".md" and stray.name not in SKIP_NAMES:
                    errors.append(f"{stray}: only `.md` notes live under `{top.name}/{second.name}/`")
            for md in sorted(second.rglob("*.md")):
                if md.name.endswith(".zh.md"):
                    continue
                rel = md.relative_to(second)
                if len(rel.parts) > 1:
                    errors.append(f"{md}: notes must live directly in `{top.name}/{second.name}/`, no subdirectories")
                    continue
                if not FILENAME_RE.match(md.name):
                    errors.append(f"{md}: filename must be `yyyy-mm-dd-topic.md`")
                    continue
                if top.name == "archived":
                    archived.append((md, md.relative_to(notes).as_posix()))
                e, w = check_file(md, top.name, second.name, repo, strict)
                errors.extend(e)
                warnings.extend(w)

    manifest = load_manifest(notes)
    if manifest is None:
        errors.append(f"{notes}/archived/{MANIFEST_NAME}: manifest must be a JSON object")
    else:
        if args.seal and not errors:
            new = [(md, rel) for md, rel in archived if rel not in manifest]
            for md, rel in new:
                manifest[rel] = note_digest(md)
            if new:
                (notes / "archived" / MANIFEST_NAME).write_text(
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print(f"sealed {len(new)} archived note(s)")
        live = {rel for _, rel in archived}
        for rel in sorted(set(manifest) - live):
            errors.append(f"{notes}/{rel}: sealed archived note is missing")
        for md, rel in archived:
            if rel not in manifest:
                errors.append(f"{notes}/{rel}: archived note is not sealed (run with --seal)")
            elif manifest[rel] != note_digest(md):
                errors.append(f"{notes}/{rel}: archived note was modified after sealing")

    for w in warnings:
        print(w)
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(f"  {e}")
        return 1
    print(f"OK: regression notes under {notes} verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
