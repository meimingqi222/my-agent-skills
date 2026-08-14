#!/usr/bin/env python3
"""Read foreign coding-agent sessions as untrusted inert history.

Thin compatibility entrypoint and facade around modular session reader components:
- reader_core: Common utilities, WorkIndex, error types, text/path helpers
- readers/*: Individual reader implementations for each supported agent harness
- reader_discovery: Session discovery, path resolving, exclusion logic
- reader_render: Digest distillation and human-readable formatting
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# Ensure UTF-8 output across standard streams
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure the skill directory is on sys.path so modules can import cleanly
SKILL_DIR = Path(__file__).resolve().parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

# Core constants and utilities
from reader_core import (
    AGENT_INTERNAL_PARTS,
    ANY_TOOL,
    CLAUDE_KNOWN_TYPES,
    COMMIT_HEREDOC_RE,
    COMMIT_MSG_RE,
    GIT_PUSH_RE,
    CODEX_IGNORED_TOP_LEVEL,
    CODEX_ROLLOUT_RE,
    CODEX_SAFE_TOP_LEVEL,
    COMMANDCODE_KNOWN_TYPES,
    CURSOR_SKIPPED_ROLES,
    GENERATED_META_RE,
    HARNESS_PREAMBLE_RES,
    INTERRUPTED_RE,
    LIVE_SESSION_MINUTES,
    MAKA_CHUNK_MARKER,
    MAKA_KNOWN_TYPES,
    PATCH_PATH_RE,
    PATCH_TOOLS,
    PLAN_STATUS_MARKS,
    PLAN_TOOLS,
    PRIOR_CONTEXT_CHARS,
    QODER_KNOWN_TYPES,
    SELECTABLE,
    SHELL_COMMAND_KEYS,
    SHELL_PATH_RE,
    SHELL_TOOLS,
    TASK_LABEL_KEYS,
    TOOL_ALIASES,
    TOOL_PATH_PARAMS,
    TOOLS,
    UUID_RE,
    AmbiguousReference,
    ReaderError,
    WorkIndex,
    _add_warning,
    _as_dict,
    _as_text,
    _assistant_action,
    _blocks,
    _content_text,
    _finalize_result,
    _first_string,
    _is_generated_meta_text,
    _is_harness_preamble,
    _is_outside_cwd,
    _is_scratch,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _normalize_base,
    _normalize_path,
    _one_line,
    _open_sqlite_readonly,
    _paths_match,
    _read_plain_jsonl,
    _safe_text,
    _shell_command_text,
    _table_columns,
    _timestamp_sort_key,
    _timestamp_to_millis,
    _turn,
    _warning,
    _within,
    slugify,
)

# Discovery and resolver
from reader_discovery import (
    CURRENT_SESSION_ENV,
    _candidate_from_path,
    _excluded,
    _finders,
    _sort_and_dedupe,
    _under,
    _warn_if_live,
    current_session_ids,
    discover_sessions,
    read_resolved_session,
    resolve_session,
)

# Readers
from readers import (
    _amp_cwd_from_tree,
    _amp_data_dir,
    _amp_quick_meta,
    _amp_thread_dir,
    _amp_thread_location,
    _amp_turn_blocks,
    _apply_claude_preserved_segment,
    CLAUDE_TITLE_FIELDS,
    _apply_claude_snip_removals,
    _claude_chain,
    _claude_config_dir,
    _claude_leaf,
    _claude_parent,
    _claude_quick_meta,
    _claude_replacement_ids,
    _claude_segment,
    _claude_title,
    _codex_home,
    _codex_id_from_path,
    _codex_message_text,
    _codex_rollout_head,
    _codex_state_database,
    _commandcode_chain,
    _commandcode_config_dir,
    _commandcode_content,
    _commandcode_first_user_text,
    _commandcode_head,
    _commandcode_quick_meta,
    _commandcode_role,
    _commandcode_sidecar,
    _commandcode_slug,
    _common_project_root,
    _cursor_cli_metadata,
    _cursor_cli_store_rows,
    _cursor_desktop_paths,
    _cursor_desktop_rows,
    _cursor_root,
    _cursor_user_text,
    _decode_jsonish,
    _devin_data_dirs,
    _discover_amp,
    _discover_claude,
    _discover_claude_layout,
    _discover_codex,
    _discover_codex_database,
    _discover_codex_files,
    _discover_commandcode,
    _discover_cursor_cli,
    _discover_cursor_desktop,
    _discover_devin_cli,
    _discover_devin_next,
    _discover_grok,
    _discover_maka,
    _discover_opencode,
    _discover_opencode_schema,
    _discover_qoder,
    _discover_zcode,
    _drop_last_user_turns,
    _existing_codex_rollout,
    _find_amp_id,
    _find_claude_id,
    _find_codex_id,
    _find_commandcode_id,
    _find_cursor_id,
    _find_devin_id,
    _find_grok_id,
    _find_maka_id,
    _find_nested_string,
    _find_opencode_id,
    _find_opencode_schema_id,
    _find_qoder_id,
    _find_zcode_id,
    GROK_KNOWN_TYPES,
    GROK_SUMMARY_RE,
    GROK_USER_QUERY_RE,
    _grok_config_dir,
    _grok_meta,
    _grok_session_dir,
    _grok_sessions_dir,
    _grok_summary,
    _is_claude_boundary,
    _is_commandcode_legacy,
    _is_commandcode_transcript,
    _is_commandcode_typed,
    _is_qoder_cli_transcript,
    _iter_codex_rollouts,
    _maka_client_data_roots,
    _maka_database_paths,
    _maka_decode_message,
    _maka_header,
    _maka_tool_result_text,
    _maka_workspace_roots,
    _merge_cursor_metadata,
    _opencode_data_dir,
    _opencode_db_paths,
    _opencode_model_id,
    _ordered_cursor_transcript,
    _prepare_claude_messages,
    _qoder_config_dir,
    _qoder_head_types,
    _qoder_quick_meta,
    _read_codex_jsonl,
    _read_cursor_transcript,
    _read_cursor_values,
    _read_opencode_schema_session,
    _recover_claude_parallel,
    _render_claude_record,
    _render_codex_item,
    _render_commandcode_record,
    _render_cursor_role_value,
    _replacement_stub,
    _set_claude_parent,
    _zcode_config_dir,
    _zcode_db_paths,
    cursor_workspace_hash,
    read_amp_session,
    read_claude_session,
    read_codex_session,
    read_commandcode_session,
    read_cursor_session,
    read_devin_cli_session,
    read_devin_next_session,
    read_grok_session,
    read_maka_session,
    read_opencode_session,
    read_qoder_session,
    read_zcode_session,
)

# Rendering
from reader_render import (
    BANNER,
    BAR,
    RULE,
    _digest_selection,
    _inert_lines,
    _render_files,
    _render_git,
    _render_header,
    _render_list_human,
    _render_plan,
    _render_prior_context,
    _render_warnings,
    _tool_histogram,
    build_digest,
    render_digest,
    render_human,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read foreign coding-agent sessions as inert history."
    )
    parser.add_argument("tool", choices=SELECTABLE + tuple(TOOL_ALIASES))
    parser.add_argument("action", choices=("list", "show"))
    parser.add_argument("ref", nargs="?")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--within-min", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-tool-chars", type=int, default=300)
    parser.add_argument(
        "--full",
        action="store_true",
        help="show the entire transcript instead of the handoff digest",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=12,
        help="turns of recent history to keep in the digest (0 keeps all)",
    )
    parser.add_argument(
        "--any-cwd",
        action="store_true",
        help="list sessions from every working directory, not just --cwd",
    )
    parser.add_argument(
        "--exclude-session",
        action="append",
        default=[],
        metavar="ID",
        help="never select this session id; repeatable. The calling agent's own "
        "session is excluded automatically when the host exports its id.",
    )
    parser.add_argument(
        "--include-current",
        action="store_true",
        help="allow selecting the calling agent's own session (off by default)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.tool = TOOL_ALIASES.get(args.tool, args.tool)
    if args.within_min < 0:
        parser.error("--within-min must be non-negative")
    if args.max_tool_chars < 1:
        parser.error("--max-tool-chars must be positive")
    if args.tail < 0:
        parser.error("--tail must be non-negative")
    exclude = {item.strip().casefold() for item in args.exclude_session if item.strip()}
    if not args.include_current:
        exclude |= current_session_ids()
    try:
        if args.action == "list":
            if args.ref is not None:
                raise ReaderError("list does not accept a session reference")
            sessions = [
                item
                for item in discover_sessions(
                    args.tool, None if args.any_cwd else args.cwd, args.within_min
                )
                if not _excluded(item, exclude)
            ]
            if args.json:
                print(
                    json.dumps(
                        {
                            "tool": args.tool,
                            "cwd": None if args.any_cwd else args.cwd,
                            "sessions": sessions,
                            "warnings": [],
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                print(
                    _render_list_human(args.tool, args.cwd, sessions, args.any_cwd),
                    end="",
                )
            return 0
        candidate = resolve_session(
            args.tool, args.ref, args.cwd, args.within_min, args.any_cwd, exclude
        )
        result = read_resolved_session(candidate, args.max_tool_chars)
        _warn_if_live(result)
        requested_cwd = candidate.get("cwd_fallback")
        if requested_cwd:
            _add_warning(
                result["warnings"],
                "cwd_fallback",
                f"No {args.tool} session exists for {requested_cwd}; this session belongs to a "
                f"different working directory ({result.get('cwd') or 'unknown'}). Confirm it is the "
                "project the user meant before acting on it.",
            )
            result["warnings"].sort(key=lambda item: (item["code"], item["message"]))
        if args.json:
            if args.full:
                payload = result
            else:
                payload = {key: value for key, value in result.items() if key != "turns"}
                payload["digest"] = build_digest(result, tail=args.tail)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(
                render_human(result) if args.full else render_digest(result, args.tail),
                end="",
            )
        return 0
    except AmbiguousReference as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("Matches (choose a native id or path):", file=sys.stderr)
        for match in exc.matches:
            print(
                f"  {match['session_id']}  [{match.get('source')}]  "
                f"{match.get('title') or '(untitled)'}",
                file=sys.stderr,
            )
        return 2
    except ReaderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
