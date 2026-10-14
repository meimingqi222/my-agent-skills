---
name: regression-notes
description: >
  Record non-obvious bug-fix decisions as Agent Notes bound to regression tests
  so later AI-driven changes cannot silently undo them. Use when fixing a bug
  that a future change could plausibly revert, reviewing such a fix, verifying a
  notes tree, or asking when to write a postmortem. Do not use for routine fixes
  a test and commit message already explain, feature docs, or changelogs.
license: Apache-2.0
metadata:
  author: meimingqi222
  derived-from: >
    DeepSeek Harness `.agents/notes` Agent Note system (bug-fix class, format
    gate, regression binding, postmortem bar); lightweight, dependency-free port.
  tools: any
  # Claude Code v2.1.129+ expands ${CLAUDE_SKILL_DIR} here so the verifier runs
  # without a permission prompt; other harnesses ignore this field.
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/verify-notes.py *), Bash(${CLAUDE_SKILL_DIR}/scripts/new-note.py *)
---

# Regression notes

Version 0.6.0

Use `python` or `py -3` when `python3` is unavailable (stock Windows).

A lightweight port of the DeepSeek Harness Agent Note loop: a fix whose reason a later change could lose ships **one note (the why) + one regression test (the lock)** in the same change. The note records the decision; the test pins the behavior.

## When to write a note

Notes are the exception, tests are the default. A note is read before every edit to the files it cites, so each one costs every future change a little attention; a tree with a note per bug turns a hot file's lookup into hundreds of entries nobody reads. Decide per fix:

| The fix… | Write |
| --- | --- |
| was introduced and fixed inside the same uncommitted change | nothing — it never reached anyone |
| is obvious from the code and its test | a regression test (prefer one more assertion in an existing test over a new file) and a clear commit message |
| has a reason that fits in one line at the fix | that test, plus a code comment at the fix — the comment sits exactly where the tempting edit happens |
| is a decision a later change could plausibly undo: the fixed code looks redundant or wrong, a tempting alternative was rejected, a trade-off was made (e.g. performance for correctness), or the fix spans files or a shared contract/format | a note bound to its regression test |

Before writing a note, find the note that already owns the mechanism (`--find`, or the hint `new-note.py` prints when other notes cite the same tests) and update it instead. One note per mechanism, not one per incident.

## Installing into a repository

The skill only works if it is required, not merely available. Install it in one step (after resolving `$SCRIPTS`, see [Locate the scripts](#locate-the-scripts)), from the repository root:

```bash
python3 "$SCRIPTS/new-note.py" --install --dry-run   # show what would change
python3 "$SCRIPTS/new-note.py" --install             # notes tree + pre-commit hook + standing rule, then --check-install
```

`--install` creates the notes tree, writes a pre-commit hook that runs the verifier (honouring `core.hooksPath`), and appends a standing rule to `AGENTS.md` (or an existing `CLAUDE.md`). It never edits an existing hook — it prints the line to add instead — and never duplicates a rule already present.

The standing rule it writes, the hook, CI and packaging-script alternatives, and the two rules that keep the wiring cheap are in [references/gate-and-format.md](references/gate-and-format.md#wiring-the-gate-by-hand).

**You have not installed this skill until you can name the command that runs the verifier without an agent deciding to.** If the answer is "the agent runs it when it remembers", the gate does not exist — that is the failure this skill was built to prevent.

## Locate the scripts

The scripts live next to this file. Never glob for them first — resolve once, cheapest source first:

1. **Claude Code v2.1.64+** — the harness already expanded `${CLAUDE_SKILL_DIR}` to this skill's directory when it loaded this file. Use it directly and skip the rest:

   ```bash
   SCRIPTS="${CLAUDE_SKILL_DIR}/scripts"
   ```

2. **Other harnesses** — `~/.agents/skills` is the Agent Skills open-standard location that `npx skills` installs to for most agents; a few agents use their own directories instead. Test the standard locations in one command (sh/bash; on Windows use Git Bash, or the PowerShell equivalent below); it exits at the first hit, and globs only as a last resort:

   ```bash
   for d in "${CLAUDE_SKILL_DIR:-}" "$HOME/.agents/skills" \
            "$HOME/.claude/skills" "$HOME/.config/opencode/skills" \
            "$HOME/.codex/skills" "$HOME/.cursor/skills" \
            ".agents/skills" ".claude/skills"; do
     [ -f "$d/regression-notes/scripts/verify-notes.py" ] \
       && SCRIPTS="$d/regression-notes/scripts" && break
   done
   if [ -z "${SCRIPTS:-}" ]; then
     path=$(find "$HOME" . -maxdepth 6 -path '*/regression-notes/scripts/verify-notes.py' -print -quit 2>/dev/null)
     [ -n "$path" ] && SCRIPTS=$(dirname "$path")
   fi
   ```

   PowerShell equivalent:

   ```powershell
   $roots = @(
       $env:CLAUDE_SKILL_DIR,
       (Join-Path $env:USERPROFILE ".agents\skills"),
       (Join-Path $env:USERPROFILE ".claude\skills"),
       (Join-Path $env:USERPROFILE ".config\opencode\skills"),
       (Join-Path $env:USERPROFILE ".codex\skills"),
       (Join-Path (Get-Location) ".agents\skills")
   ) | Where-Object { -not [string]::IsNullOrEmpty($_) }
   $hit = $roots | ForEach-Object { Join-Path $_ "regression-notes\scripts\verify-notes.py" } |
       Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
   if (-not $hit) { throw "Could not locate regression-notes/scripts/verify-notes.py" }
   $SCRIPTS = Split-Path -Parent $hit
   ```

The notes root defaults to `.agents/notes/` and is overridable per call with `--notes-dir` or globally with the `NOTES_DIR` environment variable.

## Start here

When fixing a bug, scaffold a note and bind it to a regression test:

```bash
python3 "$SCRIPTS/new-note.py" --status implemented --test tests/test_login.py login-retry-race
```

`--test` is repeatable and pre-fills `## Verification` with real test paths. Write `--test 'tests/test_login.py::test_retry_after_lockout'` to bind one exact test (see [Regression binding](#regression-binding)). Strict verification still fails until the test file exists and the `Proved:` line records the real red run in place of the template placeholder.

`--class` accepts any class in the closed set below; non-`bug-fix` classes use `templates/generic.md` and carry no `## Verification` requirement.

A rejected proposal:

```bash
python3 "$SCRIPTS/new-note.py" --status rejected --reason "duplicates the retry policy" login-retry-race
```

Find the note that already owns a decision before writing a new one:

```bash
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir> --find "retry policy"
```

`--find` matches a regex against note filenames and bodies, and prints each hit's path, title, decision, and cited tests. There is no index file to maintain — the tree is the index, and this is how you search it.

Verify the tree:

```bash
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir>
```

Exit non-zero means red. The gate needs exactly one line:

```bash
python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes
```

If the notes directory may legitimately be absent during a dry run, use `--allow-missing`; do not use that flag in the commit gate.

## Layout and format

Notes are path-encoded as `{proposed,implemented,rejected,archived}/{class}/yyyy-mm-dd-topic.md`:

- **Lifecycle** is the status:
  - `proposed/` (`Status: proposed`) → `implemented/` (`Status: implemented`) → `archived/` (`Status: implemented` + `Archived: YYYY-MM-DD`). Moving `proposed/` → `implemented/` means rewriting `## Proposal` into a present-tense `## Decision` and folding `## Acceptance criteria` / `## Risks` into `## Consequences`.
  - `rejected/` (`Status: rejected — <why>`) keeps proposal-time sections (`## Problem`, `## Proposal`, `## Alternatives considered`, optionally `## Acceptance criteria` / `## Risks`) and is kept only while the losing proposal remains a tempting mistake.
  - `archived/` notes are frozen: once they carry `Archived: YYYY-MM-DD` they must not be edited again, and every archived note must be sealed (see below).
- **Class** is closed: `bug-fix, feature, architecture, process, testing, simplification`. Adding a class requires updating this file and the `CLASSES` constant in `verify-notes.py` together; `new-note.py` reads the set from the verifier, so it cannot drift.

Every note follows one skeleton. `bug-fix` uses `templates/bug-fix.md`; the other classes use `templates/generic.md`, which is the same skeleton minus `## Verification` (only `bug-fix` binds a test):

```markdown
# Agent Note: <title>

Status: implemented

## Problem
## Decision
## Alternatives considered
## Consequences
## Verification        # bug-fix only
```

The header block, every rule the verifier enforces, its advisory warnings, and the flags that keep a large tree quiet (`--baseline`, `--changed-only`, per-note `verify-disable`) are in [references/gate-and-format.md](references/gate-and-format.md#header-block-and-verifier-rules).

## Archiving and retiring

Archive a fully superseded note in one step — move, header, and seal together:

```bash
python3 "$SCRIPTS/new-note.py" --archive 2026-09-01-old-decision.md --successor 2026-09-09-new-decision.md --notes-dir <notes-dir>
```

Archived notes are sealed in `archived/manifest.json` and frozen. When the code an archived note describes is gone, retire it so the archive does not only grow:

```bash
python3 "$SCRIPTS/new-note.py" --retire 2025-03-01-old-cache.md --reason "cache layer removed" --notes-dir <notes-dir>
```

Sealing, resealing, and retirement rules are in [references/gate-and-format.md](references/gate-and-format.md#sealing-the-archive).

## Keep the corpus current

A note that contradicts the code is worse than no note: an agent following stale authority will regress the fix.

Before creating a note, search the tree for an existing note that owns the same decision or mechanism (`--find` above). Update that note in the same change instead of duplicating it.

When a later decision replaces an earlier one, record the link in the header so the old note cannot be read as current authority:

- **Fully superseded** — consolidate the unique rationale, alternatives, consequences, and verification into the new owner, then run `new-note.py --archive <old>.md --successor <new>.md` (moves to `archived/`, adds `Superseded-by:`, seals). The old note keeps its history; the pointer says who owns the decision now.
- **Partly superseded** — the successor covers one branch, and the rest still holds. Add `Partly-superseded-by: <successor>.md` and a `## Superseded` section saying which part is dead. Leave the note in `implemented/`: it is still live authority for the rest.

Do not leave a partly replaced decision recorded only as an inline remark in the body — that is how a superseded `## Consequences` section keeps asserting behavior the code no longer has. The header pointer and the `## Superseded` section are what make the stale part legible.

When code moves, renames, or deletes files referenced in an implemented note, update the note's paths and symbols in the same change — or delete the note when its subject is gone (git history keeps it). The verifier warns on missing test paths, unresolvable bare filenames, and dangling note references; you must catch stale module names, defaults, and prose claims it cannot see.

## Regression binding

The note-to-test link is one-directional and reviewable: the note's `## Verification` section names the test path(s); the spec itself stays plain test code. Ship both in the same change — a note without a test is a wish, a test without a note is a mystery. `archived/` notes are frozen and never cited as authority for current behavior.

To bind one exact test, write the path and an anchor separated by `::`:

```markdown
- `tests/test_login.py::test_retry_after_lockout`
```

The anchor is checked as a plain substring of that file. That is the whole grammar: the verifier never parses the test language, so it works the same for `test_foo` (pytest), `TestFoo` (Go), `should foo` (JS), or a snapshot directory name. A token containing `::` is treated as a binding only when the part before the first `::` is an existing file, so C++ scope resolution (`std::filesystem`, `Class::Method`) is never mistaken for one — no language list required.

Prefer the anchored form: a bare `tests/test_login.py` proves the file exists, not that the test does. If you cite a test by name anywhere in `## Verification` without an anchor, the verifier warns, because it cannot tell a renamed test from prose.

A guard only guards if the regression fails it, and the failure must land on the assertion the binding names: a fixture, environment, or compile error does not prove anything, so fix the setup and re-run until the bound assertion fails for the expected reason.

Two kinds of evidence count, and they share one record:

- **Natural red** — a failure you actually observed: the test written before the fix (TDD), or the suite's real failure before the patch landed. This satisfies the proof outright; you do not owe a second manufactured run. The `Proved:` line must cite something checkable, e.g. `Proved: pre-fix run failed at <assertion>; output saved to <evidence-log>` — "it was red earlier" without a log or output is the self-report this line exists to kill.
- **Manufactured red** — needed only when the test was written after the fix and has never been observed to fail. Keep it cheap and reversible: disable **one** guard, run only the bound test (focused, e.g. `cargo test --lib <name>`), save the failing output, restore the guard, then re-run the same focused test green — the restore-and-regreen is part of the ritual, not an option. One manufactured run may cover every test bound to the same guard; tests bound to different guards each need their own.

Record the red run in the `Proved:` line under `## Verification` (e.g. `Proved: disabled the latch → tests/test_login.py::test_retry_after_lockout failed as expected, then reverted; output saved to .agents/notes-evidence/login-retry-red.log`). Put evidence under a committed directory — a sibling of the notes tree (convention: `<notes>-evidence/`), since the notes tree itself accepts only `.md` — rather than a build output dir like `target/` that gets cleaned or gitignored. Backticked paths on `Proved:` lines are treated as evidence, not test targets: they never fail verification, but a missing evidence file warns, so a `Proved:` line whose proof evaporates on `cargo clean` stops passing silently. Assert against the world (re-run the command, re-read the file from outside), never against the agent's own output.

Notes earn nothing sitting unread. `--for-path` is the reverse lookup: pass the files you are about to change and get back the notes citing them — one entry per note (title and the first line of its decision), newest first, capped at 10 (`--limit N`, `0` for all). Archived notes are counted but listed only with `--include-archived`, since they are history rather than current locks. Wire it where edits start — a repo rule ("run `--for-path` on changed files before editing"), a pre-commit hook printing citing notes, or both — because a reminder that runs without anyone remembering it is worth more than a ritual that depends on recall. For repos whose tests list themselves (`cargo test -- --list`, `pytest --collect-only`), a CI step can intersect `--dump-anchors` output with the real list to catch a renamed test that survives as an orphan substring.

The gate verifies notes that exist; it cannot see fixes that never got one. `--audit-commits [N]` closes that gap from the history side: it flags recent commits whose message looks like a fix and that touched code but added neither a note nor a test file. It is a report, not a gate — run it on a schedule or after a bug-fixing session and treat flagged commits as candidates for retroactive notes.

## When to write a postmortem

Reserve a deeper postmortem for failures that are all three of subtle (hard for a veteran to derive), systemic (a gap in tests, tooling, or convention — not a typo), and costly to rediscover. Ordinary bugs get a note, not a postmortem.

## Safety

The verifier opens notes read-only, never executes project code, and treats note content as untrusted text. Verify its test-path claims against the live repo before acting — a path string is not proof the behavior holds. The `Proved:` line records the ritual, but a lying author can still write a fake one; review is the backstop.

The binding and staleness checks are existence checks, not proofs. An anchor that appears in a file does not mean the test passes, and a `Proved:` line cannot be verified without running the suite, which this skill deliberately does not do. Treat a green run as "the tree is internally consistent", not "the fixes work".

## Changing the verifier

Run `python3 -X utf8 -m unittest discover -s regression-notes/tests` after any change to `scripts/`; the rules for adding checks are in [references/gate-and-format.md](references/gate-and-format.md#changing-the-verifier).
