#!/usr/bin/env python3
"""Scaffold a new regression note, or archive an implemented one in one step.

Usage:
    python3 new-note.py --class bug-fix --status proposed|implemented|rejected|archived "short topic slug" \
        [--test tests/test_...] [--reason "why rejected"] \
        [--supersedes <note.md>] [--partly-supersedes <note.md>] [--notes-dir .agents/notes]

    python3 new-note.py --archive <old-note.md> --successor <new-note.md> \
        [--notes-dir .agents/notes] [--repo-root .]

The class set is read from the verifier, so it is defined in exactly one place
(the verifier's CLASSES constant) and this script cannot drift from the gate.
`--status archived` scaffolds `Status: implemented` plus an `Archived:` line,
per the gate's archived grammar.
`--archive` moves an implemented note to `archived/`, adds `Archived:` plus
`Superseded-by:`, then verifies and seals in the same step.
"""

import argparse
import importlib.util
import os
import re
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
    ap.add_argument("--repo-root", default=".",
                    help="repo root for the verify-and-seal step of --archive "
                         "(default: .)")
    ap.add_argument("--notes-dir", default=os.environ.get("NOTES_DIR", ".agents/notes"))
    args = ap.parse_args()

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
