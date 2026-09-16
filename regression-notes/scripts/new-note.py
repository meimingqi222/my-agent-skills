#!/usr/bin/env python3
"""Scaffold a new regression note.

Usage:
    python3 new-note.py --class bug-fix --status proposed|implemented|rejected "short topic slug" \
        [--test tests/test_...] [--reason "why rejected"] \
        [--supersedes <note.md>] [--partly-supersedes <note.md>] [--notes-dir .agents/notes]

The class set is read from the verifier, so it is defined in exactly one place
(the verifier's CLASSES constant) and this script cannot drift from the gate.
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="topic slug, e.g. login-retry-race")
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
                    help="note filename this note fully replaces (archived notes only)")
    ap.add_argument("--partly-supersedes", dest="partly", default="",
                    help="note filename this note partly replaces; adds a `## Superseded` "
                         "section to fill in")
    ap.add_argument("--notes-dir", default=os.environ.get("NOTES_DIR", ".agents/notes"))
    args = ap.parse_args()

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

    slug = re.sub(r"[^a-z0-9]+", "-", args.slug.lower()).strip("-")
    if not slug:
        ap.error("slug must contain ASCII letters or digits")
    target = Path(args.notes_dir) / args.status / args.cls / f"{date.today():%Y-%m-%d}-{slug}.md"
    if target.exists():
        ap.error(f"already exists: {target}")

    text = template_for(args.cls).read_text(encoding="utf-8-sig")

    # Status line, plus the optional archived/supersede lines directly beneath it.
    header = []
    if args.status == "rejected":
        header.append(f"Status: rejected — {args.reason.strip()}")
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


if __name__ == "__main__":
    main()
