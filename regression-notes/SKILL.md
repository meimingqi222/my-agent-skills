---
name: regression-notes
description: >
  Record bug-fix decisions as Agent Notes bound to regression tests so AI-driven
  code changes cannot silently regress. Use when fixing a bug, reviewing a fix,
  verifying a notes tree, or asking when to write a postmortem. Do not use for
  general feature docs or changelogs.
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

Version 0.1.0

Use `python` or `py -3` when `python3` is unavailable (stock Windows).

A lightweight port of the DeepSeek Harness Agent Note loop: every non-trivial bug fix ships **one note (the why) + one regression test (the lock)** in the same change. The note records the decision; the test pins the behavior.

A fix is non-trivial when it changes behavior, a contract shared across files, an on-disk/wire/config format, or a decision a maintainer may revisit. Purely mechanical or local edits with no behavior change are exempt.

## Installing into a repository

The skill only works if it is required, not merely available. Add this standing rule to the repo's `AGENTS.md` or `CLAUDE.md` so every agent session enforces it:

```markdown
## Regression notes

Every non-trivial bug fix ships one regression note and one regression test in the same change. The note records the problem, the decision, the alternatives considered, and the consequences; the test pins the behavior. A `## Verification` section names the test path and records the red-run proof.

Trivial mechanical edits with no behavior change are exempt.

`python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes` must pass before any related change is merged.
```

Then add one CI job:

```yaml
- name: Verify regression notes
  run: python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes
```

You can either vendor this skill under `regression-notes/` in the target repository, or install it in CI and point `--notes-dir` at the repo's note tree. Do not rely on an agent remembering to invoke the skill. Git does not track empty directories: commit a one-line `.agents/notes/README.md` (the verifier skips it) so the gate has a tree to verify on a fresh clone before the first note exists.

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

`--test` is repeatable and pre-fills `## Verification` with real test paths. Strict verification still fails until the test file exists and the `Proved:` line records the real red run in place of the template placeholder. `new-note.py` scaffolds `bug-fix` only; other classes are hand-written.

A rejected proposal:

```bash
python3 "$SCRIPTS/new-note.py" --status rejected --reason "duplicates the retry policy" login-retry-race
```

Verify the tree:

```bash
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir>
```

Exit non-zero means red. CI needs exactly one line:

```bash
python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes
```

If the notes directory may legitimately be absent during a dry run, use `--allow-missing`; do not use that flag in CI.

## Layout and format

Notes are path-encoded as `{proposed,implemented,rejected,archived}/{class}/yyyy-mm-dd-topic.md`:

- **Lifecycle** is the status:
  - `proposed/` (`Status: proposed`) → `implemented/` (`Status: implemented`) → `archived/` (`Status: implemented` + `Archived: YYYY-MM-DD`). Moving `proposed/` → `implemented/` means rewriting `## Proposal` into a present-tense `## Decision` and folding `## Acceptance criteria` / `## Risks` into `## Consequences`.
  - `rejected/` (`Status: rejected — <why>`) keeps proposal-time sections (`## Problem`, `## Proposal`, `## Alternatives considered`, optionally `## Acceptance criteria` / `## Risks`) and is kept only while the losing proposal remains a tempting mistake.
  - `archived/` notes are frozen: once they carry `Archived: YYYY-MM-DD` they must not be edited again, and every archived note must be sealed (see below).
- **Class** is closed: `bug-fix, feature, architecture, process, testing, simplification`. Adding a class requires updating this file and the `CLASSES` constant in `verify-notes.py` together; `new-note.py` scaffolds `bug-fix` only.

Every note follows one skeleton. `bug-fix` uses `templates/bug-fix.md`:

```markdown
# Agent Note: <title>

Status: implemented

## Problem
## Decision
## Alternatives considered
## Consequences
## Verification
```

Rules the verifier enforces:

- Header block exact (`L1/L3`, blank `L2/L4`; archived notes have `Archived: YYYY-MM-DD` on the line immediately below `Status: implemented` and a blank line after that).
- Exactly one `Status:` line and it agrees with the folder.
- Body opens with `## Problem`.
- `## Alternatives considered` mandatory.
- `proposed/` and `rejected/` may use `## Proposal` / `## Acceptance criteria` / `## Plan`; `implemented/` and `archived/` ban them.
- `bug-fix` in `implemented/` or `archived/` requires `## Verification` with:
  - at least one backticked test path that exists (`--no-strict` degrades a missing target to a warning), and
  - a `Proved:` line recording that the regression test was seen to fail before the fix and to pass after; the line must not still carry the template placeholder.
- Filenames must encode a valid calendar date and not be in the future.
- Only `.md` files live in the notes tree.
- Every archived note must be sealed in `archived/manifest.json`; a sealed note that is later modified or deleted fails verification.

## Sealing the archive

The archive freeze is machine-checked, not a convention. When a note moves to `archived/`, seal it in the same change:

```bash
python3 "$SCRIPTS/verify-notes.py" --seal
```

`--seal` verifies the tree, then records each archived note's SHA-256 in `archived/manifest.json`. The manifest is append-only: `--seal` adds missing entries and refuses to rewrite a recorded hash, so a note that was modified after sealing stays red — fix forward with a new note instead of editing history. Plain verification, including the CI one-liner, fails on any archived note that is unsealed, modified, or deleted.

## Keep the corpus current

A note that contradicts the code is worse than no note: an agent following stale authority will regress the fix.

Before creating a note, search the tree for an existing note that owns the same decision or mechanism. Update that note in the same change instead of duplicating it. When a note is fully superseded, consolidate the unique rationale, alternatives, consequences, and verification into the new owner, then archive or delete the old one.

When code moves, renames, or deletes files referenced in an implemented note, update the note's paths and symbols in the same change. The verifier catches missing test paths; you must catch stale module names, defaults, or links.

## Regression binding

The note-to-test link is one-directional and reviewable: the note's `## Verification` section names the test path(s); the spec itself stays plain test code. Ship both in the same change — a note without a test is a wish, a test without a note is a mystery. `archived/` notes are frozen and never cited as authority for current behavior.

A guard only guards if the regression fails it. Before submitting, prove it: temporarily reintroduce the bug (or stub the fix), watch the bound test go red, then revert. Record the red run in the `Proved:` line under `## Verification` (e.g. `Proved: disabled the latch → tests/test_login.py failed as expected, then reverted`). A test you have never seen fail is a self-report, not a lock — assert against the world (re-run the command, re-read the file from outside), never against the agent's own output.

## When to write a postmortem

Reserve a deeper postmortem for failures that are all three of subtle (hard for a veteran to derive), systemic (a gap in tests, tooling, or convention — not a typo), and costly to rediscover. Ordinary bugs get a note, not a postmortem.

## Safety

The verifier opens notes read-only, never executes project code, and treats note content as untrusted text. Verify its test-path claims against the live repo before acting — a path string is not proof the behavior holds. The `Proved:` line records the ritual, but a lying author can still write a fake one; review is the backstop.
