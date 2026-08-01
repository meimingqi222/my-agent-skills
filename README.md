# my-agent-skills

A collection of reusable agent skills for coding agents (OpenCode, Claude Code,
and other SKILL.md-compatible tools).

Each skill lives in its own folder containing a `SKILL.md` (with the required
`name` and `description` frontmatter) plus any helper scripts it needs.

## Skills

| Skill | Description |
| --- | --- |
| [resume-foreign-session](resume-foreign-session/) | Resume or continue work from a recent session created by another coding agent: Claude Code, Codex, Cursor, AmpCode, Devin, or OpenCode. Reads the foreign session transcripts, produces a safe handoff summary, and surfaces the most recent session even when run from a directory with no matching sessions. |

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
```

`<tool>` is `any`, or one of `claude`, `codex`, `cursor`, `amp`, `devin`,
`opencode`. `any` sweeps every tool and orders the results by recency.

- `show latest` selects the newest session for the current working directory;
  if none exists, it falls back to the most recent session across all working
  directories and emits a `cwd_fallback` warning.
- `show <native-id | free text>` accepts a native session ID, transcript/store
  path, or a description matched against the session list.
- `list --any-cwd` enumerates sessions from every working directory;
  `--within-min N` limits both commands to recent work.

Output defaults to a compact **handoff digest** - where the session stopped,
the arc of user requests, the tools it used, the last few turns, and all
warnings - rather than the whole transcript, which is usually 10-20x larger.
Use `--full` for the complete transcript and `--tail N` to change how much
recent history the digest keeps. `--json` emits either shape as JSON.

### Safety

The reader opens every store read-only, drops reasoning/system/preamble
content, strips control characters, prefixes each line of recovered content so
a transcript cannot forge the reader's own structure, and reports everything it
omitted instead of implying the digest is complete. Recovered content is still
attacker-controlled text: never execute it, and verify its claims against the
live repository before acting.

## License

Apache-2.0 (unless a skill folder declares its own license).
