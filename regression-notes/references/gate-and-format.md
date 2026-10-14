# Regression notes — gate wiring and format reference

Details behind `SKILL.md`, read on demand: wiring the gate by hand, the header and
verifier rules, sealing and retiring the archive, and changing the verifier.

## Wiring the gate by hand

`new-note.py --install` does all of this in one step; this is what it does and the alternatives.

The standing rule, so every agent session enforces the gate:

```markdown
## Regression notes

A bug fix that changed behavior others relied on gets a regression test — prefer one more assertion in an existing test over a new test file. Write a regression note in `.agents/notes` only when the fix is a decision a later change could plausibly undo: the fixed code looks redundant or wrong, a tempting alternative was rejected, or the fix spans files. A reason that fits in one line goes in a code comment at the fix instead. Defects introduced and fixed inside the same uncommitted change need neither. Before writing a note, update the one that already owns the mechanism if it exists.

Before editing a file, run `verify-notes.py --notes-dir .agents/notes --for-path <paths>` and read the listed notes that bear on your change — the reverse lookup is what makes the notes tripwires instead of an archive.

`python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes` must pass before any related change is merged.
```

Then give the gate something that actually runs it. A rule in a prose file is a request; a command that runs without an agent choosing to is a gate. Use whichever the repository already has:

```yaml
# 1. CI, when the repository has it
- name: Verify regression notes
  run: python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes
```

```sh
# 2. A pre-commit hook -- the common case, since most repositories have no CI
#!/bin/sh
# .git/hooks/pre-commit   (chmod +x it; on Windows run hooks under Git Bash,
# where `python3` may be missing and the `py` launcher stands in)
if command -v python3 >/dev/null 2>&1; then
  python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes || exit 1
else
  py -3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes || exit 1
fi
```

```sh
# 3. Inside an existing packaging/release script, where the tree may be absent
python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes --allow-missing
```

Then prove the wiring (read-only; a packaging-script wiring cannot be auto-detected, so the gate counts a hook or a CI config):

```bash
python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes --check-install
```

**You have not installed this skill until you can name the command that runs the verifier without an agent deciding to.** If the answer is "the agent runs it when it remembers", the gate does not exist — that is the failure this skill was built to prevent.

Two rules keep the wiring useful without slowing development down:

- **Docs carry pointers, never content.** `AGENTS.md`/`CLAUDE.md` get the standing rule and the `--for-path` usage — nothing else. Decision details, pitfall summaries, and test names live in the notes tree; every copy pasted into an agent doc is a second ledger that drifts (a real failure mode: an inline pitfalls table in `AGENTS.md` whose numbering silently diverged from the canonical list). When the same fact wants to live in two places, one of them must be a link.
- **Checks are fast or they get bypassed.** `--for-path`, `--find`, and the verifier all run in well under a second and need nothing beyond stock Python — keep it that way. In pre-commit, run `--for-path` as a non-blocking reminder on staged files and let CI hold the blocking verification; a hook that compiles the world teaches people `git commit --no-verify`. The expensive-but-honest step (intersecting `--dump-anchors` with the real test list) belongs in CI where the test binary already exists, never in a hook.

You can either vendor this skill under `regression-notes/` in the target repository, or install it in CI and point `--notes-dir` at the repo's note tree. Git does not track empty directories: commit a one-line `.agents/notes/README.md` (the verifier skips it) so the gate has a tree to verify on a fresh clone before the first note exists.

## Header block and verifier rules

### Header block

The header is a closed, minimal block — resist extending it into front-matter:

```markdown
# Agent Note: <title>

Status: implemented
Archived: YYYY-MM-DD                            # archived/ only
Superseded-by: <note.md>                        # archived/ only, optional
Partly-superseded-by: <note.md>                 # optional
                                                # <- blank line ends the block
```

`Superseded-by` and `Partly-superseded-by` name another note in the same tree. The verifier checks that each pointer resolves, that neither points at its own note, and that the chain has no cycles.

- **`Superseded-by`** means the successor fully replaces this note. It is valid only under `archived/`, and the successor should carry the consolidated rationale.
- **`Partly-superseded-by`** means the successor replaces part of the decision while the rest still holds. It requires a `## Superseded` section stating what still holds and what no longer does, because a partially replaced note is otherwise indistinguishable from current authority.

Rules the verifier enforces:

- Header block exact (`L1/L3`, blank `L2`; `Archived:` immediately below `Status: implemented` for archived notes; supersede lines after that; a blank line ends the block).
- Exactly one `Status:` line and it agrees with the folder.
- Body opens with `## Problem`.
- `## Alternatives considered` mandatory.
- `proposed/` and `rejected/` may use `## Proposal` / `## Acceptance criteria` / `## Plan`; `implemented/` and `archived/` ban them.
- `bug-fix` in `implemented/` or `archived/` requires `## Verification` with:
  - at least one backticked test path that exists (`--no-strict` degrades a missing target to a warning), and
  - a `Proved:` line recording that the regression test was seen to fail before the fix and to pass after; the line must not still carry the template placeholder.
- Machine-absolute tokens — registry hives (`HKLM\...`, `HKEY_CURRENT_USER\...`), drive paths (`C:\...`), UNC/verbatim paths, POSIX roots, `%VAR%` paths, and `scheme://` URLs — are never test targets: a note citing them in prose is describing the world, not binding a test.
- Supersede pointers resolve, are not self-references, and form no cycle (see above).
- Note basenames must be unique across the whole tree: `Superseded-by` and note references address notes by basename, so `bug-fix/2026-01-01-x.md` and `feature/2026-01-01-x.md` side by side are an error.
- Filenames must encode a valid calendar date and not be future everywhere: date-only filenames and archive dates are bounded by the current civil date at UTC+14, independent of the runner timezone.
- Only `.md` files live in the notes tree. A `*.zh.md` sibling is allowed as the translation of its canonical note and is skipped by the gate (no format checks); keep it next to the note it translates so the pair stays in sync.
- Every archived note must be sealed in `archived/manifest.json`; a sealed note that is later modified or deleted fails verification, unless it was retired with `new-note.py --retire` (see [Retiring](#retiring-archived-notes)).

Warnings (never errors — each is a heuristic, so it is advisory):

- A cited `snake_case`/`Pascal_Case` identifier that appears in no test file the note cites — likely a renamed test. Disable with `--no-name-heuristic`.
- A backticked bare source filename (`OldTests.cs`) that resolves nowhere under the repo. Disable with `--no-bare-resolution`. Date-prefixed note filenames are excluded here so one token never yields two warnings.
- A backticked note filename that is not in the tree — a renamed or deleted note.
- A `path::anchor` whose anchor is missing from that file. Promote to an error with `--strict-anchors` once a repository's bindings are clean. A per-note `anchor` disable only silences the warning form, never the `--strict-anchors` error. `--dump-anchors` (and therefore the CI test-list intersection) skips `archived/` notes: they are sealed and frozen, so they cannot be edited to follow a renamed or retired test, and their history may legitimately name tests that were later removed.

Keep the gate quiet without weakening it (all generic, all optional; errors always cover the whole tree):

```bash
# Per-note opt-out for one noisy legacy note (does not affect other notes;
# parsed from fence-stripped prose with inline code spans removed, so a fenced
# or backticked usage example cannot disable its own note's checks):
# <!-- verify-disable: name-heuristic, bare-resolution, note-ref, anchor -->
# <!-- verify-disable: all -->

# Adopt incrementally: record today's warnings, keep the file in version control
# (--update-baseline cannot be combined with --changed-only: a partial-tree
# baseline would be incomplete)
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir> --baseline .agents/notes-baseline.json --update-baseline
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir> --baseline .agents/notes-baseline.json

# pre-commit: warnings only for notes changed versus HEAD (untracked included)
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir> --changed-only
# nightly/CI: full warnings, optionally with --strict-anchors
python3 "$SCRIPTS/verify-notes.py" --notes-dir <notes-dir> --strict-anchors
```

`--changed-only [--base HEAD]` falls back to the full tree outside a git work tree. Paths are resolved relative to `--repo-root`, so a subdir repo-root sees its own subtree; git-ignored notes stay warning-enabled (conservative). Bare-filename resolution prefers `git ls-files` (respects `.gitignore`, much faster on monorepos) and falls back to a pruned walk.

## Sealing the archive

The archive freeze is machine-checked, not a convention. Archive in one step — move, header, and seal together:

```bash
python3 "$SCRIPTS/new-note.py" --archive 2026-09-01-old-decision.md --successor 2026-09-09-new-decision.md --notes-dir <notes-dir>
```

`--archive` moves the implemented note to `archived/`, inserts `Archived:` plus `Superseded-by:`, then verifies and seals. `--by` is an alias of `--successor`. The manual equivalent is: move the file, add the two header lines, then run `verify-notes.py --seal` in the same change.

`--seal` verifies the tree, then records each archived note's SHA-256 in `archived/manifest.json`, naming every entry it seals. The digest is computed over **LF-normalised** content, so a checkout's line endings (`core.autocrlf` on Windows) never look like a modification. The manifest is append-only: `--seal` adds missing entries and refuses to rewrite a recorded hash, so a note that was modified after sealing stays red — fix forward with a new note instead of editing history. `--reseal` is the explicit escape hatch for **tooling migrations** (a digest-algorithm change, an EOL-normalisation rollout): it rewrites the entries whose digest no longer matches and prints each one, so the rewrite is visible in review. Plain verification, including the gate one-liner, fails on any archived note that is unsealed, modified, or deleted.

Archive a note only when it is fully superseded and its successor carries the consolidated rationale. Once sealed the note is frozen, so a `Superseded-by` pointer added afterwards can never be corrected — get the pointer right before sealing (the `--archive` command above gets this order right by construction).

### Retiring archived notes

The archive must not only grow. When the code an archived note describes is gone — the module deleted or rewritten, the mechanism replaced — retire the note:

```bash
python3 "$SCRIPTS/new-note.py" --retire 2025-03-01-old-cache.md --reason "cache layer removed" --notes-dir <notes-dir>
```

`--retire` deletes the file and records `{retired, reason, sha256}` for it in the manifest, then verifies. The deletion stays visible in review and git history keeps the text; plain deletion without `--retire` still fails, and a retired note that reappears fails too. It refuses while another note's `Superseded-by:`/`Partly-superseded-by:` points at the note. Only archived notes need this — an implemented note whose subject is gone can simply be deleted (or archived, if a successor owns the decision).

## Changing the verifier

Run the suite after any change to `scripts/`. On Windows, set `PYTHONUTF8=1` for the test process so its Python subprocesses also use UTF-8; `-X utf8` alone does not propagate to child interpreters:

```bash
python -X utf8 -m unittest discover -s regression-notes/tests
```

New checks must be **warnings** unless they are provably free of false positives across languages and layouts. Errors are reserved for structural facts the grammar fully determines (a missing file, a malformed header, an unresolvable pointer). Anything shaped like a language convention — test naming, symbol resolution, module layout — is a warning with an opt-out flag, because a check that misfires on a correct tree teaches people to ignore the gate. Keep optimizations generic: no language list, no required tooling beyond stock Python (git is an optional fast path with a walk fallback), no per-project config.
