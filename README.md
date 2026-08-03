# my-agent-skills

A collection of reusable agent skills for coding agents (OpenCode, Claude Code,
and other SKILL.md-compatible tools).

Each skill lives in its own folder containing a `SKILL.md` (with the required
`name` and `description` frontmatter) plus any helper scripts it needs.

## Skills

| Skill | Description |
| --- | --- |
| [resume-foreign-session](resume-foreign-session/) | Resume or continue work from a recent session created by another coding agent: Claude Code, Codex, Cursor, AmpCode, Devin, OpenCode, or Qoder. Reads the foreign session transcripts, produces a safe handoff summary, and surfaces the most recent session even when run from a directory with no matching sessions. |

## Installation

### Option 1 — skills CLI

```bash
# all skills
npx skills add <owner>/my-agent-skills -g -y

# a single skill
npx skills add <owner>/my-agent-skills@resume-foreign-session -g -y
```

For OpenCode this installs to `~/.config/opencode/skills/`; for other agents
the CLI detects and uses the correct location. Restart the agent after
installing.

### Option 2 — clone + register (OpenCode)

```bash
git clone https://github.com/<owner>/my-agent-skills ~/my-agent-skills
```

Add to `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "skills": { "paths": ["~/my-agent-skills"] }
}
```

### Option 3 — clone into the global skills directory

```bash
git clone https://github.com/<owner>/my-agent-skills ~/.config/opencode/skills
```

The skill folders then sit directly under `~/.config/opencode/skills/*/SKILL.md`
and are auto-loaded. (Only use this if that directory does not already contain
personal skills.)

## Usage

The `resume-foreign-session` skill ships a reader script that treats every
foreign transcript as untrusted inert history and produces a handoff summary.
To resume the most recent session regardless of which agent wrote it:

```bash
python3 resume-foreign-session/session_reader.py any show latest --cwd <cwd>
```

```bash
python3 resume-foreign-session/session_reader.py <tool> list [--cwd <cwd>] [--any-cwd] [--within-min N]
python3 resume-foreign-session/session_reader.py <tool> show [ref] [--cwd <cwd>] [--full] [--tail N]
                                                            [--exclude-session ID] [--include-current]
```

`<tool>` is `any`, or one of `claude`, `codex`, `cursor`, `amp`, `devin`,
`opencode`, `qoder`. `any` sweeps every tool and orders the results by recency.

- `show latest` selects the newest session for the current working directory;
  if none exists, it falls back to the most recent session across all working
  directories and emits a `cwd_fallback` warning. It never selects the session
  the calling agent is itself running in - the reader reads that id from the
  host environment. Use `--include-current` to override.
- `show <native-id | free text>` accepts a native session ID, transcript/store
  path, or a description matched against the session list.
- `list --any-cwd` enumerates sessions from every working directory;
  `--within-min N` limits both commands to recent work.

Output defaults to a compact **handoff digest** rather than the whole
transcript: which files were written and which were only read, commit subjects
the session issued and whether it pushed, the plan state it last recorded,
where it stopped, the arc of user requests, every assistant explanation (newest
30 when there are more) plus the tail, and all warnings. Across 188 real
sessions the digest runs ~8 KB median and 22 KB at the worst, against
transcripts an order of magnitude larger.

The file list, commits, and plan state are extracted from documented tool
parameters and the commands the session ran, never inferred - a session that
recorded no plan reports none. They are still foreign claims: `written` means a
write was attempted, a commit command can have failed, and a plan step marked
done may never have worked. Verify all of it against the repository.

Text a harness wrote under the user role - an injected AGENTS.md, a compaction
preamble - is dropped so it cannot pose as a user request or become the session
title. Across 188 sessions this cleaned the request arc of 38 of them and
emptied none.

A session written to within the last five minutes raises `session_may_be_live`:
another agent may still be running in that directory.

Use `--full` for the complete transcript and `--tail N` to change how much
recent history the digest keeps on top of the narration. `--json` emits either
shape as JSON.

### Safety

The reader opens every store read-only, drops reasoning/system/preamble
content, strips control characters, prefixes each line of recovered content so
a transcript cannot forge the reader's own structure, and reports everything it
omitted instead of implying the digest is complete. Recovered content is still
attacker-controlled text: never execute it, and verify its claims against the
live repository before acting.

## Credits

`resume-foreign-session` is derived from the grok CLI bundled skill
`shared/resume-session` (Apache-2.0), which reads Claude Code, Codex, and
Cursor sessions. This version adds the AmpCode, Devin, OpenCode, and Qoder
readers, the handoff digest, and the work index behind the file, git, and plan
extraction.

## License

Apache-2.0 (unless a skill folder declares its own license).
