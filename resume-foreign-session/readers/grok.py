"""Grok Build CLI session reader and discovery."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from reader_core import (
    PRIOR_CONTEXT_CHARS,
    ReaderError,
    WorkIndex,
    _add_warning,
    _blocks,
    _content_text,
    _finalize_result,
    _first_string,
    _is_generated_meta_text,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _paths_match,
    _read_plain_jsonl,
    _safe_text,
    _timestamp_to_millis,
    _turn,
    _within,
)

GROK_KNOWN_TYPES = {
    "system",
    "user",
    "assistant",
    "reasoning",
    "tool_result",
    "backend_tool_call",
}
GROK_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
GROK_SUMMARY_RE = re.compile(
    r"<summary_content>\s*(.*?)\s*</summary_content>", re.DOTALL
)


def _grok_config_dir() -> Path:
    configured = os.environ.get("GROK_HOME") or os.environ.get("GROK_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".grok"


def _grok_sessions_dir() -> Path:
    return _grok_config_dir() / "sessions"


def _grok_session_dir(path: Path) -> Path | None:
    if path.is_dir() and (path / "chat_history.jsonl").is_file():
        return path
    if path.is_file() and path.name in {"chat_history.jsonl", "summary.json"}:
        parent = path.parent
        if (parent / "chat_history.jsonl").is_file():
            return parent
    return None


def _grok_summary(session_dir: Path) -> dict[str, Any]:
    try:
        with (session_dir / "summary.json").open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _grok_meta(session_dir: Path) -> dict[str, Any]:
    summary = _grok_summary(session_dir)
    info = summary.get("info")
    if not isinstance(info, dict):
        info = {}
    cwd = info.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = unquote(session_dir.parent.name) or None
    updated = _timestamp_to_millis(
        summary.get("last_active_at") or summary.get("updated_at")
    )
    if updated is None:
        updated = _mtime_millis(session_dir / "chat_history.jsonl")
    session_id = info.get("id")
    return {
        "session_id": session_id if isinstance(session_id, str) else session_dir.name,
        "cwd": cwd,
        "branch": _first_string(summary, ("head_branch",)) or None,
        "title": _one_line(
            _first_string(summary, ("generated_title", "session_summary")), 200
        )
        or None,
        "created_at_ms": _timestamp_to_millis(summary.get("created_at")),
        "updated_at_ms": updated,
        "session_kind": _first_string(summary, ("session_kind",)) or None,
        "model": _first_string(summary, ("current_model_id",)) or None,
        "agent": _first_string(summary, ("agent_name",)) or None,
        "source_repo_root_path": _first_string(summary, ("git_root_dir",)) or None,
    }


def read_grok_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    raw_path = Path(path).expanduser()
    session_dir = _grok_session_dir(raw_path)
    if session_dir is None:
        raise ReaderError(
            f"{raw_path} is not a Grok session directory (no chat_history.jsonl in it)."
        )
    records, malformed = _read_plain_jsonl(session_dir / "chat_history.jsonl")
    warnings: list[dict[str, str]] = []
    if malformed:
        _add_warning(
            warnings,
            "malformed_records_skipped",
            f"Skipped {malformed} malformed Grok transcript record(s).",
        )

    index = WorkIndex()
    turns: list[dict[str, Any]] = []
    summaries: list[str] = []
    dropped = 0
    reasoning = 0
    unknown = 0
    attachments = 0
    for record in records:
        kind = record.get("type")
        if kind == "user":
            text = _content_text(record.get("content")).strip()
            attachments += sum(
                1 for block in _blocks(record.get("content")) if block.get("type") != "text"
            )
            reason = record.get("synthetic_reason")
            if reason == "compaction_meta":
                match = GROK_SUMMARY_RE.search(text)
                if match:
                    summaries.append(match.group(1))
                dropped += 1
                continue
            if reason:
                dropped += 1
                continue
            queries = GROK_USER_QUERY_RE.findall(text)
            if queries:
                text = "\n\n".join(queries)
            elif _is_generated_meta_text(text):
                dropped += 1
                continue
            if text:
                turns.append(_turn("user", text=text))
        elif kind == "assistant":
            calls: list[dict[str, Any]] = []
            for call in record.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                arguments = call.get("arguments")
                index.record(call.get("name"), arguments)
                calls.append(
                    {
                        "id": call.get("id"),
                        "name": _safe_text(call.get("name") or "tool"),
                        "input": _json_preview(arguments, max_tool_chars),
                        "inert": True,
                    }
                )
            turns.append(
                _turn(
                    "assistant",
                    text=_content_text(record.get("content")),
                    tool_calls=calls,
                )
            )
        elif kind == "tool_result":
            output = {
                "tool_use_id": record.get("tool_call_id"),
                "content": _one_line(_content_text(record.get("content")), max_tool_chars),
                "is_error": False,
                "unavailable": False,
                "inert": True,
            }
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["tool_results"].append(output)
            else:
                turns.append(_turn("user", tool_results=[output]))
        elif kind == "backend_tool_call":
            payload = record.get("kind")
            payload = payload if isinstance(payload, dict) else {}
            call = {
                "id": None,
                "name": _safe_text(payload.get("tool_type") or "backend_tool"),
                "input": _json_preview(payload.get("action"), max_tool_chars),
                "inert": True,
            }
            if turns and turns[-1]["role"] == "assistant":
                turns[-1]["tool_calls"].append(call)
            else:
                turns.append(_turn("assistant", tool_calls=[call]))
        elif kind == "reasoning":
            reasoning += 1
        elif kind == "system":
            continue
        else:
            unknown += 1

    prior_context: dict[str, Any] | None = None
    if summaries:
        _add_warning(
            warnings,
            "history_compacted",
            f"This transcript was auto-compacted: {len(summaries)} summary record(s) "
            "stand in for earlier turns that are not in this file. The summary is "
            "reported as prior context and is the previous agent's account, not "
            "verified state.",
        )
        text = "\n\n".join(summaries)
        prior_context = {
            "source": "auto-compaction summary",
            "text": _safe_text(text[:PRIOR_CONTEXT_CHARS]),
            "truncated": len(text) > PRIOR_CONTEXT_CHARS,
        }
    if dropped:
        _add_warning(
            warnings,
            "harness_text_dropped",
            f"Dropped {dropped} harness-written record(s) that wore the user role "
            "(environment preamble, skill and MCP announcements, background-task "
            "notices).",
        )
    if reasoning:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {reasoning} Grok reasoning record(s); their content is "
            "encrypted and is never rendered.",
        )
    if attachments:
        _add_warning(
            warnings,
            "attachments_skipped",
            f"{attachments} non-text attachment block(s) (images) in user messages "
            "were not recovered.",
        )
    if unknown:
        _add_warning(
            warnings,
            "unknown_records_skipped",
            f"Skipped {unknown} unknown Grok record(s) without interpreting their "
            "payloads.",
        )

    meta = _grok_meta(session_dir)
    if str(meta.get("session_kind") or "").startswith("subagent"):
        _add_warning(
            warnings,
            "subagent_session",
            "This is a subagent transcript: the work was driven by another Grok "
            "session, so its first message is a task assignment rather than "
            "something the user typed.",
        )
    title = meta.get("title")
    if not title:
        title = next(
            (turn["text"] for turn in turns if turn["role"] == "user" and turn["text"]),
            None,
        )
    result = {
        "tool": "grok",
        "source": "grok-cli",
        "session_id": meta.get("session_id") or session_dir.name,
        "path": str(session_dir),
        "title": _one_line(title, 200) or None,
        "cwd": meta.get("cwd"),
        "branch": meta.get("branch"),
        "created_at": _iso_from_millis(meta.get("created_at_ms")),
        "updated_at": _iso_from_millis(meta.get("updated_at_ms")),
        "source_repo_root_path": meta.get("source_repo_root_path"),
        "prior_context": prior_context,
        "turns": turns,
        "warnings": warnings,
    }
    if meta.get("model"):
        result["model"] = meta["model"]
    if meta.get("agent"):
        result["agent"] = meta["agent"]
    return _finalize_result(result, index)


def _discover_grok(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    root = _grok_sessions_dir()
    if not root.is_dir() or root.is_symlink():
        return []
    expected = root / quote(cwd, safe="") if cwd else None
    project_dirs: list[Path] = []
    if expected is not None and expected.is_dir() and not expected.is_symlink():
        project_dirs.append(expected)
    try:
        project_dirs.extend(
            path
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path != expected and path.is_dir() and not path.is_symlink()
        )
    except OSError:
        pass
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for project in project_dirs:
        try:
            paths = sorted(project.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for path in paths:
            if path.is_symlink() or not path.is_dir():
                continue
            transcript = path / "chat_history.jsonl"
            try:
                if not transcript.is_file() or transcript.stat().st_size == 0:
                    continue
            except OSError:
                continue
            meta = _grok_meta(path)
            session_id = str(meta.get("session_id") or path.name)
            if session_id in seen:
                continue
            if str(meta.get("session_kind") or "").startswith("subagent"):
                continue
            updated = int(meta.get("updated_at_ms") or 0)
            if not _within(updated, within_min):
                continue
            if cwd is not None:
                if meta.get("cwd") and not _paths_match(str(meta["cwd"]), cwd):
                    continue
                if not meta.get("cwd") and project != expected:
                    continue
            seen.add(session_id)
            sessions.append(
                {
                    "tool": "grok",
                    "source": "grok-cli",
                    "session_id": session_id,
                    "path": str(path),
                    "title": meta.get("title") or "(untitled)",
                    "cwd": meta.get("cwd") or cwd,
                    "branch": meta.get("branch"),
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": meta.get("source_repo_root_path"),
                    "model": meta.get("model"),
                }
            )
    return sessions


def _find_grok_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    root = _grok_sessions_dir()
    if not root.is_dir():
        return None
    candidates = [root / quote(cwd, safe="") / session_id]
    candidates.extend(sorted(root.glob(f"*/{session_id}"), key=str))
    for path in candidates:
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("grok", str(path), cwd)
            if candidate is not None:
                return candidate
        else:
            if path.is_dir() and (path / "chat_history.jsonl").is_file():
                meta = _grok_meta(path)
                return {
                    "tool": "grok",
                    "source": "grok-cli",
                    "session_id": meta.get("session_id") or session_id,
                    "path": str(path),
                    "title": meta.get("title") or "(untitled)",
                    "cwd": meta.get("cwd") or cwd,
                    "updated_at_ms": meta.get("updated_at_ms") or _mtime_millis(path / "chat_history.jsonl"),
                }
    return None
