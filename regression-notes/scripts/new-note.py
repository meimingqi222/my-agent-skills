#!/usr/bin/env python3
"""Scaffold a new regression note from the bug-fix template.

Usage:
    python3 new-note.py --class bug-fix --status proposed|implemented|rejected "short topic slug" \
        [--test tests/test_...] [--reason "why rejected"] [--notes-dir .agents/notes]
"""

import argparse
import os
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE.parent / "templates" / "bug-fix.md"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug", help="topic slug, e.g. login-retry-race")
    ap.add_argument("--class", dest="cls", default="bug-fix", choices=("bug-fix",),
                    help="only bug-fix is scaffolded; the other classes are hand-written")
    ap.add_argument("--status", default="proposed", choices=("proposed", "implemented", "rejected"))
    ap.add_argument("--test", dest="tests", action="append", default=[],
                    help="regression test path for ## Verification, repeatable; "
                         "defaults to `tests/...` placeholder (fails strict verification "
                         "until replaced with a real path)")
    ap.add_argument("--reason", default="",
                    help="required for --status rejected: why the proposal was declined")
    ap.add_argument("--notes-dir", default=os.environ.get("NOTES_DIR", ".agents/notes"))
    args = ap.parse_args()

    if args.cls != "bug-fix":
        ap.error("only --class bug-fix is scaffolded so far; write other classes by hand")

    if args.status == "rejected":
        if not args.reason or not args.reason.strip():
            ap.error("--reason is required when --status rejected")
        if args.tests:
            ap.error("--test is not used for rejected notes")
    elif args.reason:
        ap.error("--reason is only valid with --status rejected")

    slug = re.sub(r"[^a-z0-9]+", "-", args.slug.lower()).strip("-")
    if not slug:
        ap.error("slug must contain ASCII letters or digits")
    target = Path(args.notes_dir) / args.status / args.cls / f"{date.today():%Y-%m-%d}-{slug}.md"
    if target.exists():
        ap.error(f"already exists: {target}")

    text = TEMPLATE.read_text(encoding="utf-8-sig")

    # Status line.
    if args.status == "rejected":
        text = text.replace("Status: implemented", f"Status: rejected — {args.reason.strip()}")
    else:
        text = text.replace("Status: implemented", f"Status: {args.status}")

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


if __name__ == "__main__":
    main()
