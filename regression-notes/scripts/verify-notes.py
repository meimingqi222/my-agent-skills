#!/usr/bin/env python3
"""Verify regression-notes tree: layout, format, status consistency, test binding.

Usage:
    python3 verify-notes.py [--notes-dir .agents/notes] [--repo-root .]
                            [--no-strict] [--allow-missing] [--seal]
                            [--strict-anchors] [--no-name-heuristic]
                            [--no-bare-resolution] [--find REGEX]

Exit non-zero on any error. With --no-strict, a missing regression-test
path degrades to a warning; with --allow-missing, a missing notes directory
does not fail. --seal verifies the tree, then records every archived note's
SHA-256 in `archived/manifest.json`; once sealed, any later modification or
deletion of an archived note fails verification.

--find REGEX prints the notes matching REGEX and exits without verifying; use it
instead of an index file to locate the note that owns a decision.

Binding checks. `## Verification` may bind a note to one exact test by writing
`path::anchor`, e.g. `tests/test_login.py::test_retry_after_lockout`; the anchor
is checked as a plain substring of that file, so the grammar stays language
neutral. A token containing `::` is only treated as a binding when the part
before the first `::` resolves to an existing file, which keeps C++ scope
resolution (`std::filesystem`, `Class::Method`) out of the grammar without a
language list. Two further checks are heuristic and therefore warnings, never
errors: a backticked bare filename that resolves nowhere under the repo, and a
backticked `snake_case`/`Pascal_Case` identifier that is absent from every test
file the note cites (likely a renamed test).
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

# Optional header lines naming a successor note. Both are validated for
# resolution, self-reference, and cycles; `Partly-superseded-by` additionally
# requires a `## Superseded` section so a partially replaced decision cannot be
# read as still-current authority.
SUPERSEDE_RE = re.compile(r"^(Partly-superseded-by|Superseded-by): (\S.*)$")

# `path::anchor`, split on the first `::`.
ANCHOR_SEP = "::"

# Backticked token shaped like a bare filename: a lowercase extension, which is
# what separates `SnapshotGeometry.cs` from a dotted code identifier or a rule ID
# (`Assert.False`, `rule.Execute`, `CheckTool.Engine.Tests`, `R01.G.01` all have a
# non-lowercase or numeric suffix and are therefore never candidates).
BARE_FILE_RE = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_.+-]*\.([a-z][a-z0-9]{1,7}))`")

# Extensions that make a bare token a *file citation* rather than an API member.
# This is a list of file extensions, not of languages: `Polygon.area` (an arcpy
# attribute) and `obj.value` are excluded because `area`/`value` are not here,
# while `SnapshotGeometry.cs` is included. Extend it when a project cites a file
# type that is missing, rather than weakening the check.
SOURCE_EXTS = frozenset({
    "c", "cc", "cfg", "clj", "conf", "cpp", "cs", "css", "csv", "cxx", "dart",
    "ex", "exs", "fs", "go", "h", "hpp", "hs", "htm", "html", "ini", "java",
    "jl", "js", "json", "jsonl", "jsx", "kt", "kts", "lua", "m", "md", "ml",
    "mm", "php", "pl", "properties", "ps1", "py", "r", "rb", "rs", "sass",
    "scala", "scss", "sh", "sql", "svelte", "swift", "toml", "ts", "tsx",
    "txt", "vue", "xml", "yaml", "yml",
})

# Backticked identifier shaped like a test name in the common `snake_case` /
# `Pascal_Case` convention. Heuristic only: it feeds a warning, never an error.
NAME_RE = re.compile(r"`([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)`")

# Backticked note filename, i.e. a date-prefixed `.md`. Scanning the whole note
# (not just `## Verification`) catches a note citing another note that has since
# been renamed or deleted.
NOTE_REF_RE = re.compile(r"`(\d{4}-\d{2}-\d{2}-[A-Za-z0-9._-]+\.md)`")

# Directories never walked when resolving a bare filename.
PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "bin", "obj", "target", "dist",
    "build", ".idea", ".vs", ".next", ".cache",
})
BARE_INDEX_CAP = 200_000


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


def read_text_safe(path):
    try:
        return path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return ""


def looks_like_test_name(token):
    """`test_...` or a token carrying an uppercase letter, so prose identifiers
    like `read_only` or `or_default` do not trip the heuristic."""
    if "_" not in token:
        return False
    return token.startswith("test_") or any(c.isupper() for c in token)


class BareIndex:
    """Lazily built basename index for bare-filename resolution. Building it walks
    the repo, so it is only built once a bare candidate actually appears, and it
    reports None past the file cap instead of guessing."""

    def __init__(self, repo_root):
        self.repo_root = repo_root
        self._index = None
        self._exhausted = False

    def get(self):
        if self._index is None and not self._exhausted:
            index, count = {}, 0
            for root, dirs, files in os.walk(self.repo_root):
                dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
                for name in files:
                    index[name] = True
                    count += 1
                    if count > BARE_INDEX_CAP:
                        self._exhausted = True
                        return None
            self._index = index
        return self._index


def parse_header(lines, lifecycle, path):
    """Validate the header block; returns (errors, supersedes).

    Header grammar: title, blank, `Status:`, optional `Archived:` (archived only),
    zero or more supersede lines, blank.
    """
    errors = []
    supersedes = []

    if not re.match(r"^# Agent Note: \S", lines[0]):
        errors.append(f"{path}: L1 must be `# Agent Note: <title>`")
    if lines[1].strip() != "":
        errors.append(f"{path}: L2 must be blank")

    want = STATUS_RE[lifecycle]
    if not want.match(lines[2].strip()):
        errors.append(
            f"{path}: L3 must match the `{lifecycle}/` status grammar ({want.pattern})"
        )

    i = 3
    if lifecycle == "archived":
        if len(lines) <= i:
            errors.append(f"{path}: archived notes need `Archived: YYYY-MM-DD` after `Status:`")
            return errors, supersedes
        archived_match = re.match(r"^Archived: (\d{4})-(\d{2})-(\d{2})$", lines[i].strip())
        if not archived_match:
            errors.append(f"{path}: L4 must be `Archived: YYYY-MM-DD`")
        else:
            archive_date = valid_date(*archived_match.groups())
            today = datetime.date.today()
            if not archive_date:
                errors.append(f"{path}: archived date is not a valid calendar date")
            elif archive_date > today:
                errors.append(f"{path}: archived date cannot be in the future")
        i += 1

    while i < len(lines):
        match = SUPERSEDE_RE.match(lines[i].strip())
        if not match:
            break
        supersedes.append((match.group(1), match.group(2).strip()))
        i += 1

    if i >= len(lines):
        errors.append(f"{path}: the header block must end with a blank line")
    elif lines[i].strip() != "":
        errors.append(
            f"{path}: L{i + 1} must be blank (the header block ends with a blank line)"
        )

    return errors, supersedes


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


def check_verification(path, ver, repo_root, strict, opts, bare_index):
    """Validate a `## Verification` section. Returns (errors, warnings)."""
    errors, warnings = [], []

    if not PROVED_RE.search(ver):
        errors.append(
            f"{path}: `## Verification` must contain a `Proved:` line recording the red-run proof"
        )
    if "<what you re-broke>" in ver:
        errors.append(f"{path}: `Proved:` line still carries the template placeholder")

    raw = [t.strip() for t in TEST_RE.findall(ver)]
    targets, anchors = [], []
    for token in raw:
        left, sep, right = token.partition(ANCHOR_SEP)
        if sep and right and (repo_root / left).is_file():
            targets.append(left)
            anchors.append((left, right))
        else:
            targets.append(token)

    if not targets:
        errors.append(
            f"{path}: `## Verification` must reference at least one test path in backticks"
        )
        return errors, warnings

    missing = [t for t in targets if not (repo_root / t).exists()]
    if missing:
        msg = f"{path}: verification target(s) not found: {', '.join(missing)}"
        if strict:
            errors.append("ERROR: " + msg)
        else:
            warnings.append("WARNING: " + msg)

    for left, right in anchors:
        if right not in read_text_safe(repo_root / left):
            msg = f"{path}: anchor `{right}` not found in `{left}`"
            if opts.strict_anchors:
                errors.append("ERROR: " + msg)
            else:
                warnings.append("WARNING: " + msg)

    if not opts.no_name_heuristic:
        cited = [(t, repo_root / t) for t in targets if (repo_root / t).is_file()]
        if cited:
            blob = "\n".join(read_text_safe(f) for _, f in cited)
            for token in sorted(set(NAME_RE.findall(ver))):
                if looks_like_test_name(token) and token not in blob:
                    warnings.append(
                        f"WARNING: {path}: `{token}` is cited in `## Verification` but absent "
                        f"from {', '.join(t for t, _ in cited)}; if it is a test name, cite "
                        f"`{cited[0][0]}::{token}` so the binding is checkable"
                    )

    if not opts.no_bare_resolution:
        bare = sorted({tok for tok, ext in BARE_FILE_RE.findall(ver) if ext in SOURCE_EXTS})
        if bare:
            index = bare_index.get()
            if index is None:
                warnings.append(
                    f"WARNING: {path}: bare-filename resolution skipped (repo exceeds "
                    f"{BARE_INDEX_CAP} files)"
                )
            else:
                for token in bare:
                    if token not in index:
                        warnings.append(
                            f"WARNING: {path}: `{token}` is a bare filename that resolves "
                            f"nowhere under {repo_root}; cite a path"
                        )

    return errors, warnings


def check_file(path, lifecycle, cls, repo_root, strict, opts, bare_index, note_names=None):
    """Returns (errors, warnings, supersedes)."""
    errors, warnings = [], []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        return [f"{path}: cannot read: {e}"], [], []

    lines = text.splitlines()
    if len(lines) < 4:
        return [f"{path}: header block too short"], [], []
    header_errors, supersedes = parse_header(lines, lifecycle, path)
    errors.extend(header_errors)

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

    if any(kind == "Partly-superseded-by" for kind, _ in supersedes) and "Superseded" not in h2s:
        errors.append(
            f"{path}: `Partly-superseded-by` requires a `## Superseded` section stating "
            f"what still holds and what no longer does"
        )

    if cls == "bug-fix" and lifecycle in ("implemented", "archived"):
        if "Verification" not in h2s:
            errors.append(f"{path}: bug-fix notes require `## Verification`")
        else:
            body = "\n".join(prose)
            ver = body.split("## Verification", 1)[1]
            ver = re.split(r"^## ", ver, maxsplit=1, flags=re.M)[0]
            e, w = check_verification(path, ver, repo_root, strict, opts, bare_index)
            errors.extend(e)
            warnings.extend(w)

    if note_names is not None:
        for ref in sorted(set(NOTE_REF_RE.findall(text))):
            if ref != path.name and ref not in note_names:
                warnings.append(
                    f"WARNING: {path}: references note `{ref}`, which is not in this tree "
                    f"(renamed or deleted?)"
                )

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

    return errors, warnings, supersedes


def check_supersede_graph(notes, edges):
    """Validate every supersede pointer: resolution, self-reference, cycles, and
    the archived-folder expectation for a fully superseded note."""
    errors = []
    by_name = {}
    for md, lifecycle, _cls in notes:
        by_name.setdefault(md.name, (md, lifecycle))

    graph = {}
    for src_name, kind, target, src_lifecycle in edges:
        if target == src_name:
            errors.append(f"{src_name}: `{kind}: {target}` points at itself")
            continue
        if target not in by_name:
            errors.append(
                f"{src_name}: `{kind}: {target}` does not resolve to a note in this tree"
            )
            continue
        if kind == "Superseded-by" and src_lifecycle != "archived":
            errors.append(
                f"{src_name}: `Superseded-by` is set but the note is not under `archived/`; "
                f"a fully superseded note is consolidated into its successor and archived"
            )
        graph.setdefault(src_name, []).append(target)

    # Cycle detection over the supersede graph.
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {}

    def visit(node, stack):
        colour[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if colour.get(nxt, WHITE) == GREY:
                loop = stack[stack.index(nxt):] + [nxt]
                errors.append("supersede cycle: " + " -> ".join(loop))
            elif colour.get(nxt, WHITE) == WHITE:
                visit(nxt, stack)
        stack.pop()
        colour[node] = BLACK

    for node in sorted(graph):
        if colour.get(node, WHITE) == WHITE:
            visit(node, [])

    return errors


def find_notes(notes, pattern):
    """Print notes matching `pattern` (title, body, or filename)."""
    rx = re.compile(pattern, re.I)
    hits = 0
    for top in sorted(p for p in notes.iterdir() if p.is_dir() and p.name in LIFECYCLES):
        for second in sorted(p for p in top.iterdir() if p.is_dir() and p.name in CLASSES):
            for md in sorted(second.glob("*.md")):
                text = read_text_safe(md)
                if not (rx.search(md.name) or rx.search(text)):
                    continue
                hits += 1
                lines = text.splitlines()
                title = lines[0][len("# Agent Note: "):] if lines and lines[0].startswith("# Agent Note: ") else md.name
                decision = ""
                if "## Decision" in lines:
                    rest = lines[lines.index("## Decision") + 1:]
                    decision = next((l.strip() for l in rest if l.strip()), "")
                print(md.relative_to(notes).as_posix())
                print(f"    title:    {title}")
                if decision:
                    print(f"    decision: {decision[:160]}")
                for target in TEST_RE.findall(text):
                    print(f"    test:     {target.strip()}")
                print()
    print(f"{hits} note(s) matched `{pattern}`")
    return 0


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
    ap.add_argument(
        "--strict-anchors",
        action="store_true",
        help="make a `path::anchor` whose anchor is missing from that file an error",
    )
    ap.add_argument(
        "--no-name-heuristic",
        action="store_true",
        help="skip the renamed-test-name warning (it is a language-shaped heuristic)",
    )
    ap.add_argument(
        "--no-bare-resolution",
        action="store_true",
        help="skip resolving backticked bare filenames (avoids the repo walk)",
    )
    ap.add_argument(
        "--find",
        metavar="REGEX",
        help="print notes matching REGEX (title, body, or filename) and exit",
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

    if args.find:
        return find_notes(notes, args.find)

    if (notes / "INDEX.md").exists():
        errors.append(f"{notes}/INDEX.md: no centralized index, the tree is the index")

    collected, edges = [], []
    bare_index = BareIndex(repo)
    note_names = {p.name for p in notes.rglob("*.md")}
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
                e, w, supersedes = check_file(md, top.name, second.name, repo, strict, args,
                                              bare_index, note_names)
                errors.extend(e)
                warnings.extend(w)
                collected.append((md, top.name, second.name))
                for kind, target in supersedes:
                    edges.append((md.name, kind, target, top.name))

    errors.extend(check_supersede_graph(collected, edges))

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
