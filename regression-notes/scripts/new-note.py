#!/usr/bin/env python3
"""Scaffold a new regression note, or archive an implemented one in one step.

Usage:
    python3 new-note.py --class bug-fix --status proposed|implemented|rejected|archived "short topic slug" \
        [--test tests/test_...] [--reason "why rejected"] \
        [--supersedes <note.md>] [--partly-supersedes <note.md>] [--notes-dir .agents/notes]

    python3 new-note.py --archive <old-note.md> --successor <new-note.md> \
        [--notes-dir .agents/notes] [--repo-root .]

    python3 new-note.py --install [--dry-run] [--notes-dir .agents/notes] [--repo-root .]

    python3 new-note.py --retire <archived-note.md> --reason "why it no longer matters" \
        [--notes-dir .agents/notes]

The class set is read from the verifier, so it is defined in exactly one place
(the verifier's CLASSES constant) and this script cannot drift from the gate.
`--status archived` scaffolds `Status: implemented` plus an `Archived:` line,
per the gate's archived grammar.
`--archive` moves an implemented note to `archived/`, adds `Archived:` plus
`Superseded-by:`, then verifies and seals in the same step.
`--install` wires the gate into a repository in one step: the notes tree, a
pre-commit hook that runs the verifier, and the standing rule in AGENTS.md or
CLAUDE.md; then runs the verifier's `--check-install`. It never overwrites an
existing hook or duplicates an existing rule.
`--retire` deletes an archived note whose subject no longer exists and records
the retirement in the seal manifest, so the tree can shrink without the gate
mistaking a deliberate removal for tampering. Git history keeps the text.
Before scaffolding, the notes already citing the same test files are listed:
updating the note that owns a mechanism beats adding another one beside it.
"""

import argparse
import importlib.util
import os
import json
import re
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE.parent / "templates"


def load_verifier():
    """Import the sibling verifier so CLASSES has a single definition."""
    path = HERE / "verify-notes.py"
    spec = importlib.util.spec_from_file_location("verify_notes", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFIER = load_verifier()


def template_for(cls):
    """The skeleton for `cls`, falling back to the generic one."""
    specific = TEMPLATES / f"{cls}.md"
    return specific if specific.exists() else TEMPLATES / "generic.md"


def do_archive(ap, args):
    """Move an implemented note to `archived/` with its supersede pointer,
    then verify and seal in the same step. Returns a process exit code."""
    notes = Path(args.notes_dir)
    old_name = Path(args.archive).name
    succ_name = Path(args.successor).name
    if old_name == succ_name:
        ap.error("--archive and --successor name the same note")

    impl = notes / "implemented"
    matches = []
    if impl.is_dir():
        for cls_dir in sorted(impl.iterdir()):
            if cls_dir.is_dir() and cls_dir.name in VERIFIER.CLASSES:
                cand = cls_dir / old_name
                if cand.is_file():
                    matches.append((cls_dir.name, cand))
    if not matches:
        ap.error(f"no implemented note named {old_name} under {impl}")
    if len(matches) > 1:
        ap.error(f"ambiguous {old_name} (duplicate basenames -- fix first)")

    cls, src = matches[0]
    succ_files = [p for p in notes.rglob(succ_name) if p.is_file()]
    if not succ_files:
        ap.error(f"successor {succ_name} does not resolve to a note in this tree")
    if any(p.resolve() == src.resolve() for p in succ_files):
        ap.error(f"successor {succ_name} is the note being archived")
    live = [p for p in succ_files
            if "archived" not in p.relative_to(notes).parts]
    if not live:
        ap.error(f"successor {succ_name} is archived; it cannot own "
                 f"current authority")

    text = src.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if (len(lines) < 4 or not lines[0].startswith("# Agent Note:")
            or lines[2].strip() != "Status: implemented"):
        ap.error(f"{src} is not an implemented note "
                 f"(expected `# Agent Note:` / blank / `Status: implemented`)")
    if any(ln.strip().startswith("Superseded-by:") for ln in lines[3:10]):
        ap.error(f"{src} already has a `Superseded-by:` line; edit it by hand")

    lines.insert(3, f"Archived: {date.today():%Y-%m-%d}")
    lines.insert(4, f"Superseded-by: {succ_name}")
    dst_dir = notes / "archived" / cls
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / old_name
    if dst.exists():
        ap.error(f"already exists: {dst}")
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    src.unlink()
    print(f"archived {src} -> {dst}")

    rc = VERIFIER.main(["--notes-dir", str(notes),
                        "--repo-root", str(args.repo_root), "--seal"])
    if rc != 0:
        print(f"archive done, seal deferred (exit {rc}): fix the errors "
              f"above, then re-run `verify-notes.py --seal`",
              file=sys.stderr)
    return rc


def do_retire(ap, args):
    """Delete an archived note and record a retirement entry in the manifest,
    then verify. Returns a process exit code."""
    notes = Path(args.notes_dir)
    name = Path(args.retire).name
    if not args.reason.strip():
        ap.error("--retire requires --reason")
    archived = notes / "archived"
    found = [p for p in archived.rglob(name) if p.is_file()] if archived.is_dir() else []
    if not found:
        ap.error(f"no archived note named {name} under {archived}; only archived notes "
                 f"are retired (an implemented note can simply be deleted or archived)")
    if len(found) > 1:
        ap.error(f"ambiguous {name} (duplicate basenames -- fix first)")
    target = found[0]
    # A header pointer at the note would dangle; a prose mention only warns later.
    for md in notes.rglob("*.md"):
        if md == target:
            continue
        for line in md.read_text(encoding="utf-8-sig", errors="ignore").splitlines()[:12]:
            m = VERIFIER.SUPERSEDE_RE.match(line.strip())
            if m and Path(m.group(2).strip()).name == name:
                ap.error(f"{md.relative_to(notes).as_posix()} points at {name} "
                         f"(`{m.group(1)}:`); retire or re-point that note first")
    manifest = VERIFIER.load_manifest(notes)
    if manifest is None:
        ap.error(f"{archived}/{VERIFIER.MANIFEST_NAME} is not a JSON object")
    rel = target.relative_to(notes).as_posix()
    manifest[rel] = {"retired": f"{date.today():%Y-%m-%d}", "reason": args.reason.strip(),
                     "sha256": VERIFIER.note_digest(target)}
    (archived / VERIFIER.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    target.unlink()
    print(f"retired {rel} (recorded in {VERIFIER.MANIFEST_NAME}; git history keeps the text)")
    return VERIFIER.main(["--notes-dir", str(notes), "--repo-root", str(args.repo_root)])


def warn_existing(notes, tests):
    """List live notes already citing the same test files, so the author can
    update the owner instead of adding a sibling note. Advisory only."""
    files = [t.partition("::")[0] for t in tests]
    hits = VERIFIER.notes_citing(notes, files, include_archived=False)
    if not hits:
        return
    print(f"note: {len(hits)} existing note(s) already cite {', '.join(sorted(set(files)))} -- "
          f"if one owns this mechanism, update it instead of adding another:")
    for h in hits[:5]:
        print(f"  {h['rel']}: {h['title']}")
    if len(hits) > 5:
        print(f"  ... and {len(hits) - 5} more (verify-notes.py --for-path)")


# The standing rule `--install` writes into the repo's agent doc. SKILL.md shows
# the same text under "Installing into a repository"; keep the two in step.
STANDING_RULE = """
## Regression notes

A bug fix that changed behavior others relied on gets a regression test — prefer one more assertion in an existing test over a new test file. Write a regression note in `{notes}` only when the fix is a decision a later change could plausibly undo: the fixed code looks redundant or wrong, a tempting alternative was rejected, or the fix spans files. A reason that fits in one line goes in a code comment at the fix instead. Defects introduced and fixed inside the same uncommitted change need neither. Before writing a note, update the one that already owns the mechanism if it exists.

Before editing a file, run `{verifier} --notes-dir {notes} --for-path <paths>` and read the listed notes that bear on your change.

`{verifier} --notes-dir {notes}` must pass before any related change is merged.
"""

HOOK = """#!/bin/sh
# regression-notes gate (installed by new-note.py --install)
if command -v python3 >/dev/null 2>&1; then PY=python3; else PY="py -3"; fi
$PY "{verifier}" --notes-dir "{notes}" || exit 1
"""


def _rel(path, repo):
    """`path` relative to `repo` in POSIX form when inside it, else absolute."""
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _hooks_dir(repo):
    """The pre-commit hook directory git actually uses (honours core.hooksPath),
    or None when `repo` is not a git work tree."""
    if not (repo / ".git").exists():
        return None
    try:
        out = subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath"],
                             capture_output=True, text=True).stdout.strip()
    except OSError:
        out = ""
    if out:
        hooks = Path(out)
        return hooks if hooks.is_absolute() else repo / hooks
    return repo / ".git" / "hooks"


def do_install(args):
    """Wire the gate into --repo-root, then report via the verifier's
    --check-install. Returns a process exit code."""
    repo = Path(args.repo_root)
    notes = Path(args.notes_dir)
    verifier = _rel(HERE / "verify-notes.py", repo)
    notes_rel = _rel(notes, repo)
    dry = args.dry_run
    act = "would " if dry else ""

    readme = notes / "README.md"
    if notes.is_dir() and any(notes.iterdir()):
        print(f"notes tree: present ({notes_rel})")
    else:
        print(f"notes tree: {act}create {notes_rel}/README.md")
        if not dry:
            notes.mkdir(parents=True, exist_ok=True)
            readme.write_text("Regression notes; see the regression-notes skill. "
                              "Verified by verify-notes.py.\n", encoding="utf-8")

    hooks = _hooks_dir(repo)
    hook_text = HOOK.format(verifier=verifier, notes=notes_rel)
    if hooks is None:
        print("pre-commit hook: skipped -- not a git work tree; wire the verifier into CI instead")
    else:
        hook = hooks / "pre-commit"
        existing = hook.read_text(encoding="utf-8", errors="ignore") if hook.exists() else None
        if existing is None:
            print(f"pre-commit hook: {act}create {_rel(hook, repo)}")
            if not dry:
                hooks.mkdir(parents=True, exist_ok=True)
                hook.write_text(hook_text, encoding="utf-8")
                hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        elif "verify-notes" in existing:
            print(f"pre-commit hook: already runs the verifier ({_rel(hook, repo)})")
        else:
            # Never edit someone else's hook: its exit semantics are theirs.
            print(f"pre-commit hook: {_rel(hook, repo)} exists and does not run the "
                  f"verifier; add this line to it yourself:\n"
                  f"  python3 \"{verifier}\" --notes-dir \"{notes_rel}\" || exit 1")

    docs = [repo / name for name in ("AGENTS.md", "CLAUDE.md")]
    doc = next((d for d in docs if d.exists()), docs[0])
    text = doc.read_text(encoding="utf-8", errors="ignore") if doc.exists() else ""
    if "verify-notes" in text or notes_rel in text:
        print(f"agent doc: {doc.name} already points at the notes")
    else:
        print(f"agent doc: {act}{'append to' if doc.exists() else 'create'} {doc.name} "
              f"(standing rule)")
        if not dry:
            body = STANDING_RULE.format(verifier=verifier, notes=notes_rel).lstrip("\n")
            if text:
                body = text.rstrip("\n") + "\n\n" + body
            doc.write_text(body, encoding="utf-8")

    if dry:
        return 0
    print()
    return VERIFIER.check_install(notes, repo)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", nargs="?", default=None,
                    help="topic slug, e.g. login-retry-race (not used with --archive)")
    ap.add_argument("--class", dest="cls", default="bug-fix", choices=VERIFIER.CLASSES)
    ap.add_argument("--status", default="proposed",
                    choices=("proposed", "implemented", "rejected", "archived"))
    ap.add_argument("--test", dest="tests", action="append", default=[],
                    help="regression test path for ## Verification, repeatable; "
                         "defaults to a `tests/...` placeholder (fails strict verification "
                         "until replaced with a real path). Write `path::anchor` to bind "
                         "one exact test.")
    ap.add_argument("--reason", default="",
                    help="required for --status rejected: why the proposal was declined")
    ap.add_argument("--supersedes", default="",
                    help="successor note filename that fully replaces this note "
                         "(writes `Superseded-by:`, archived notes only)")
    ap.add_argument("--partly-supersedes", dest="partly", default="",
                    help="successor note filename that partly replaces this note "
                         "(writes `Partly-superseded-by:` and adds a `## Superseded` "
                         "section to fill in)")
    ap.add_argument("--archive", metavar="OLD.md", default="",
                    help="archive the implemented note OLD.md in one step: move it to "
                         "`archived/`, add `Archived:` plus `Superseded-by:`, then "
                         "verify and seal (requires --successor)")
    ap.add_argument("--successor", "--by", dest="successor", default="", metavar="NEW.md",
                    help="successor note filename for --archive "
                         "(written as `Superseded-by:` into the archived note)")
    ap.add_argument("--retire", metavar="ARCHIVED.md", default="",
                    help="delete an archived note whose subject no longer exists, "
                         "recording the retirement in the seal manifest (needs --reason)")
    ap.add_argument("--install", action="store_true",
                    help="wire the gate into --repo-root: notes tree, pre-commit hook, "
                         "standing rule in AGENTS.md/CLAUDE.md; then run --check-install")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --install: print what would change, write nothing")
    ap.add_argument("--repo-root", default=".",
                    help="repo root for --install and for the verify-and-seal step of "
                         "--archive (default: .)")
    ap.add_argument("--notes-dir", default=os.environ.get("NOTES_DIR", ".agents/notes"))
    args = ap.parse_args()

    if args.dry_run and not args.install:
        ap.error("--dry-run is only valid with --install")
    if args.retire:
        if args.slug or args.archive or args.tests or args.install:
            ap.error("--retire cannot be combined with a slug, --archive, --test, or --install")
        return do_retire(ap, args)
    if args.install:
        if args.slug or args.archive or args.tests or args.reason:
            ap.error("--install cannot be combined with a slug, --archive, --test, or --reason")
        return do_install(args)

    if args.archive:
        if args.slug or args.tests or args.reason or args.partly or args.supersedes:
            ap.error("--archive cannot be combined with a slug, --test, --reason, "
                     "--supersedes, or --partly-supersedes")
        if args.status != "proposed":
            ap.error("--archive manages status itself; do not pass --status")
        if not args.successor:
            ap.error("--archive requires --successor NEW.md")
        return do_archive(ap, args)

    if args.status == "rejected":
        if not args.reason or not args.reason.strip():
            ap.error("--reason is required when --status rejected")
        if args.tests:
            ap.error("--test is not used for rejected notes")
    elif args.reason:
        ap.error("--reason is only valid with --status rejected")

    if args.supersedes and args.partly:
        ap.error("--supersedes and --partly-supersedes are mutually exclusive")
    if args.supersedes and args.status != "archived":
        ap.error("--supersedes writes `Superseded-by`, which the gate requires to live "
                 "under archived/; use --partly-supersedes, or scaffold into archived/ "
                 "by hand")

    if not args.slug:
        ap.error("slug is required (or use --archive OLD.md --successor NEW.md)")
    slug = re.sub(r"[^a-z0-9]+", "-", args.slug.lower()).strip("-")
    if not slug:
        ap.error("slug must contain ASCII letters or digits")
    target = Path(args.notes_dir) / args.status / args.cls / f"{date.today():%Y-%m-%d}-{slug}.md"
    if target.exists():
        ap.error(f"already exists: {target}")
    if args.tests:
        warn_existing(Path(args.notes_dir), args.tests)

    text = template_for(args.cls).read_text(encoding="utf-8-sig")

    # Status line, plus the optional archived/supersede lines directly beneath it.
    # Archived notes keep `Status: implemented` (the gate's archived grammar);
    # only the folder and the `Archived:` line mark them as archived.
    header = []
    if args.status == "rejected":
        header.append(f"Status: rejected — {args.reason.strip()}")
    elif args.status == "archived":
        header.append("Status: implemented")
    else:
        header.append(f"Status: {args.status}")
    if args.status == "archived":
        header.append(f"Archived: {date.today():%Y-%m-%d}")
    if args.supersedes:
        header.append(f"Superseded-by: {args.supersedes}")
    elif args.partly:
        header.append(f"Partly-superseded-by: {args.partly}")
    text = text.replace("Status: implemented", "\n".join(header), 1)

    if args.partly:
        section = ("## Superseded\n\n<What still holds, and what `" + args.partly +
                   "` replaces.>\n\n")
        text = text.replace("## Alternatives considered", section + "## Alternatives considered", 1)

    # Proposal-era skeleton for proposed and rejected notes.
    if args.status in ("proposed", "rejected"):
        text = text.replace("## Decision\n\n<What shipped, present tense.>",
                            "## Proposal\n\n<What you plan to change, future tense allowed.>")
        text = text.replace("## Consequences\n\n<What the trade-off cost and bought.>",
                            "## Acceptance criteria\n\n<Observable state that means done.>\n\n"
                            "## Risks\n\n<What could go wrong, and what the change gives up.>")

    # A rejected proposal never shipped, so it carries no Verification section.
    if args.status == "rejected":
        text = text.split("## Verification", 1)[0].rstrip() + "\n"

    if args.tests:
        bullets = "\n".join(f"- `{t}`" for t in args.tests)
        text = text.replace("- `tests/...`", bullets)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"created {target}")

    if args.cls == "bug-fix":
        print("reminder: `## Verification` must name a test that exists, and the `Proved:` "
              "line must record the red run you actually observed.")
    if args.partly or args.supersedes:
        print("reminder: verify the chain with "
              f"`verify-notes.py --notes-dir {args.notes_dir}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
