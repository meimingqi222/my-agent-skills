# Per-tool listing quirks

Sessions that `list` and `show latest` leave out, per tool, and how to still reach them.

| Tool | Left out of listings | Still reachable |
| --- | --- | --- |
| Devin CLI | transcripts whose project root cannot be inferred are left out of per-directory listings | by native session ID |
| Qoder | transcripts from pre-1.1 builds and its IDE-side stores use schemas this reader does not render; they are skipped rather than shown as empty sessions | no |
| Command Code | transcripts older than its `version: 3` schema record no working directory | under their own project directory, or with `--any-cwd` |
| Grok, zcode | the separate transcript written for each subagent (it holds the parent session's own tool traffic, not work the user drove) | by native session ID; Grok adds a `subagent_session` warning |
| OpenCode v2 (`opencode2`) | sessions with a non-null `parent_id` (subagent transcripts) | by native session ID, with a `subagent_session` warning |
| WorkBuddy | teammate-agent transcripts (`agent-<hex>.jsonl`, written when a session runs an agent team); they hold work the lead session directed | by native session ID, with a `subagent_session` warning |
