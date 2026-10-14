# my-agent-skills

A collection of reusable agent skills for coding agents (OpenCode, Claude Code,
and other SKILL.md-compatible tools).

Each skill lives in its own folder containing a `SKILL.md` (with the required
`name` and `description` frontmatter) plus any helper scripts it needs.

## Skills

Each skill's `SKILL.md` is the source of truth for what it supports and how to
use it; this README only introduces them.

| Skill | What it does |
| --- | --- |
| [resume-foreign-session](resume-foreign-session/) | Resume work from a recent session created by another coding agent (Claude Code, Codex, Cursor, OpenCode, and others — the current list is in its `SKILL.md`). Produces a safe handoff digest instead of replaying the transcript. |
| [regression-notes](regression-notes/) | Record the non-obvious bug-fix decisions a later change could undo as Agent Notes bound to regression tests; routine fixes get a test and a commit message. Ships a note scaffolder, a one-step repo installer, and a dependency-free verifier for a commit gate or CI. |
| [windows-window-ops](windows-window-ops/) | Find, inspect, foreground, move, close, screenshot, and send keys to native Win32 desktop windows, with DPI-correct coordinates and verified state restoration. |

## Installation

### Option 1 — skills CLI

```bash
# all skills
npx skills add meimingqi222/my-agent-skills -g -y

# a single skill
npx skills add meimingqi222/my-agent-skills@resume-foreign-session -g -y
npx skills add meimingqi222/my-agent-skills@regression-notes -g -y
npx skills add meimingqi222/my-agent-skills@windows-window-ops -g -y
```

For OpenCode this installs to `~/.config/opencode/skills/`; for other agents
the CLI detects and uses the correct location. Restart the agent after
installing.

### Option 2 — clone + register (OpenCode)

```bash
git clone https://github.com/meimingqi222/my-agent-skills ~/my-agent-skills
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
git clone https://github.com/meimingqi222/my-agent-skills ~/.config/opencode/skills
```

The skill folders then sit directly under `~/.config/opencode/skills/*/SKILL.md`
and are auto-loaded. (Only use this if that directory does not already contain
personal skills.)

## Usage

Inside an agent, each `SKILL.md` resolves its own script path (Claude Code
expands `${CLAUDE_SKILL_DIR}`; other harnesses fall back to the standard install
locations), so no manual lookup is needed. From a shell in this repository:

### resume-foreign-session

```bash
python3 resume-foreign-session/session_reader.py any show latest --cwd <cwd>
python3 resume-foreign-session/session_reader.py <tool> list --any-cwd --within-min 120
```

The default output is a compact handoff digest — files written vs. read, commits,
plan state, where the session stopped, and every reader warning. Across 188 real
sessions it ran ~8 KB median and 22 KB at worst, an order of magnitude smaller
than the transcripts. Everything it recovers is untrusted foreign text: verify it
against the repository before acting. Supported tools, options, and the safety
boundary are in [`SKILL.md`](resume-foreign-session/SKILL.md).

### regression-notes

```bash
python3 regression-notes/scripts/new-note.py --install                 # wire the gate into a repo
python3 regression-notes/scripts/new-note.py --class bug-fix --status implemented --test tests/test_x.py my-fix
python3 regression-notes/scripts/verify-notes.py --notes-dir .agents/notes
```

Tests are the default; a note (the why) is the exception, written only when a
fix is a decision a later change could plausibly undo. `--for-path` lists the
notes guarding a file before you edit it, capped so a hot file stays readable.
When to write a note, binding grammar, archiving, and the gate are in
[`SKILL.md`](regression-notes/SKILL.md).

### windows-window-ops

Dot-source `windows-window-ops/scripts/WindowOps.ps1` from PowerShell, then use
helpers such as `Get-AppWindow`, `Show-AppWindow`, and
`Save-AppWindowScreenshot`. Recipes and known Win32 pitfalls are in
[`SKILL.md`](windows-window-ops/SKILL.md).

## Development

Run a skill's tests after changing its scripts:

```bash
python3 -m unittest discover -s resume-foreign-session/tests
python3 -X utf8 -m unittest discover -s regression-notes/tests
```

`windows-window-ops/scripts/Test-WindowOps.ps1` needs Windows: it launches
disposable WinForms fixtures against a live desktop.

## Credits

`resume-foreign-session` is derived from the Grok CLI bundled skill
`shared/resume-session` (Apache-2.0), which reads Claude Code, Codex, and
Cursor sessions; this version adds readers for the other agents listed in its
`SKILL.md`, the handoff digest, and the work index behind the file, git, and plan
extraction. `regression-notes` is a lightweight port of the DeepSeek Harness
`.agents/notes` system.

## License

Apache-2.0 (unless a skill folder declares its own license).
