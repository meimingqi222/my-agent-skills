# Agent Note: Date-only records survive verification across timezones

Status: implemented

## Problem

QuickCleaner CI run 37536644427 rejected October 7 notes at October 6 21:50 UTC.
The author was already on October 7 in Taipei. Both future-date checks in
`regression-notes/scripts/verify-notes.py` used the runner's local date.

## Decision

Filename and archive dates share the UTC+14 current civil date as their upper
bound. Date-only records may have been authored anywhere; dates future in every
timezone still fail. Calendar validity and archive chronological ordering stay
strict. Version 0.4.3 documents the boundary and uses unittest discovery to run
both the existing verifier suite and the new timezone suite.

## Alternatives considered

Changing CI to Taipei remains author-specific. Renaming valid notes to yesterday
changes their history. Removing future checks or always accepting tomorrow weakens
the boundary even when no real timezone has reached tomorrow.

## Consequences

The skill verifier and QuickCleaner's vendored verifier remain byte-identical.
Installed agents and Claude copies receive the same script and documentation.
Existing uncommitted source changes are preserved. On Windows, PYTHONUTF8=1 must
be inherited by test subprocesses when running the full suite in UTF-8 mode.

## Verification

`regression-notes/tests/test_date_timezone.py::test_author_today_is_not_future_on_utc_runner`
locks implemented and archived author-today acceptance. The same file tests dates
future everywhere, UTC midnight, invalid calendar dates and archive ordering.

Proved: the unmodified source verifier failed three assertions in the focused
suite, including the bound test for both implemented and archived October 7 notes,
in `.agents/notes-evidence/2026-10-07-note-date-timezone-red.txt`. After applying
the UTC+14 bound, the same four tests pass in
`.agents/notes-evidence/2026-10-07-note-date-timezone-green.txt`; the full suite
passes in `.agents/notes-evidence/2026-10-07-note-date-timezone-full-green.txt`.
