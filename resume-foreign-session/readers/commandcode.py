"""Command Code session reader and discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from reader_core import (
    COMMANDCODE_KNOWN_TYPES,
    PRIOR_CONTEXT_CHARS,
    ReaderError,
    WorkIndex,
    _add_warning,
    _blocks,
    _content_text,
    _finalize_result,
    _is_generated_meta_text,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _read_plain_jsonl,
    _safe_text,
    _timestamp_sort_key,
    _turn,
    slugify,
)
from readers.claude import _discover_claude_layout


def _commandcode_config_dir() -> Path:
    configured = os.environ.get("COMMANDCODE_CONFIG_DIR") or os.environ.get(
        "COMMAND_CODE_CONFIG_DIR"
    )
    return Path(configured).expanduser() if configured else Path.home() / ".commandcode"


def _commandcode_slug(cwd: str) -> str:
    return slugify(cwd).lower().lstrip("-")


def _is_commandcode_typed(record: dict[str, Any]) -> bool:
    return isinstance(record.get("type"), str) and isinstance(record.get("id"), str)


def _is_commandcode_legacy(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata")
    return (
        record.get("type") is None
        and record.get("role") in {"user", "assistant", "tool"}
        and isinstance(record.get("content"), list)
        and isinstance(metadata, dict)
        and isinstance(metadata.get("source"), str)
    )


def _commandcode_head(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(limit), handle):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _is_commandcode_transcript(path: Path) -> bool:
    for record in _commandcode_head(path, 20):
        if record.get("type") == "session" and isinstance(record.get("id"), str):
            return True
        if record.get("type") == "message" and isinstance(record.get("message"), dict):
            return True
        if _is_commandcode_legacy(record):
            return True
    return False


def _commandcode_sidecar(path: Path) -> dict[str, Any]:
    sidecar = path.with_name(f"{path.stem}.meta.json")
    try:
        with sidecar.open("r", encoding="utf-8", errors="replace") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _commandcode_role(record: dict[str, Any]) -> str | None:
    message = record.get("message")
    if isinstance(message, dict):
        role = message.get("role")
        return role if role in {"user", "assistant"} else None
    role = record.get("role")
    return role if role in {"user", "assistant", "tool"} else None


def _commandcode_content(record: dict[str, Any]) -> list[dict[str, Any]]:
    message = record.get("message")
    if isinstance(message, dict):
        return _blocks(message.get("content"))
    return _blocks(record.get("content"))


def _commandcode_first_user_text(records: list[dict[str, Any]]) -> str | None:
    for record in records:
        if _commandcode_role(record) != "user":
            continue
        metadata = record.get("metadata")
        if isinstance(metadata, dict) and (
            metadata.get("isAutomated") or metadata.get("isSummary")
        ):
            continue
        for block in _commandcode_content(record):
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text.strip() and not _is_generated_meta_text(text):
                return text
    return None


def _commandcode_quick_meta(path: Path) -> dict[str, Any] | None:
    head = _commandcode_head(path)
    if not head:
        return None
    if not any(
        record.get("type") in {"session", "message"} or _is_commandcode_legacy(record)
        for record in head
    ):
        return None
    cwd = next(
        (
            record["cwd"]
            for record in head
            if record.get("type") == "session" and isinstance(record.get("cwd"), str)
        ),
        None,
    )
    branch = next(
        (
            record["gitBranch"]
            for record in reversed(head)
            if isinstance(record.get("gitBranch"), str) and record["gitBranch"].strip()
        ),
        None,
    )
    sidecar = _commandcode_sidecar(path)
    title = sidecar.get("title")
    if not isinstance(title, str) or not title.strip():
        title = _commandcode_first_user_text(head)
    return {
        "cwd": cwd,
        "branch": branch if branch != "-" else None,
        "title": _one_line(title, 200) or None,
    }


def _commandcode_chain(
    records: list[dict[str, Any]], warnings: list[dict[str, str]]
) -> list[dict[str, Any]]:
    graph = {
        str(record["id"]): record
        for record in records
        if isinstance(record.get("id"), str) and record["id"]
    }
    renderable = [
        record
        for record in records
        if _commandcode_role(record) is not None and isinstance(record.get("id"), str)
    ]
    if not renderable:
        return []
    positions = {
        str(record.get("id")): position for position, record in enumerate(records)
    }
    parents = {
        str(record["parentId"])
        for record in records
        if isinstance(record.get("parentId"), str) and record["parentId"]
    }
    leaves = [record for record in renderable if str(record["id"]) not in parents]
    leaf = max(
        leaves or renderable,
        key=lambda record: _timestamp_sort_key(
            record, positions.get(str(record.get("id")), -1)
        ),
    )
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: dict[str, Any] | None = leaf
    while current is not None:
        identifier = str(current.get("id"))
        if identifier in seen:
            _add_warning(
                warnings,
                "parent_cycle",
                "A cycle was detected in the transcript parent chain; only the "
                "recoverable suffix is shown.",
            )
            break
        seen.add(identifier)
        if _commandcode_role(current) is not None:
            chain.append(current)
        parent = current.get("parentId")
        current = graph.get(str(parent)) if isinstance(parent, str) and parent else None
    chain.reverse()
    if not chain:
        return renderable
    dropped = len(renderable) - len(chain)
    if dropped > 0:
        _add_warning(
            warnings,
            "branch_records_skipped",
            f"Skipped {dropped} record(s) on abandoned branches of the transcript "
            "(the session was rewound); only the live thread is shown.",
        )
    return chain


def _render_commandcode_record(
    record: dict[str, Any],
    max_tool_chars: int,
    index: WorkIndex | None,
    counters: dict[str, int],
    summaries: list[str],
) -> dict[str, Any] | None:
    role = _commandcode_role(record)
    if role is None:
        return None
    metadata = record.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("isSummary"):
            counters["summary"] = counters.get("summary", 0) + 1
            text = "\n".join(
                _safe_text(block.get("text"))
                for block in _commandcode_content(record)
                if block.get("type") == "text" and isinstance(block.get("text"), str)
            ).strip()
            if text:
                summaries.append(text)
            return None
        if metadata.get("isAutomated"):
            counters["automated"] = counters.get("automated", 0) + 1
            return None
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    for block in _commandcode_content(record):
        block_type = block.get("type")
        if block_type in {"thinking", "reasoning", "redacted_thinking", "signature"}:
            continue
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip() and not _is_generated_meta_text(text):
                texts.append(_safe_text(text))
        elif block_type == "image":
            texts.append("[image content unavailable]")
        elif block_type == "tool_use":
            if index is not None:
                index.record(block.get("name"), block.get("input", {}))
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "name": _safe_text(block.get("name") or "unknown"),
                    "input": _json_preview(block.get("input", {}), max_tool_chars),
                    "inert": True,
                }
            )
        elif block_type == "tool-call":
            if index is not None:
                index.record(block.get("toolName"), block.get("input", {}))
            tool_calls.append(
                {
                    "id": block.get("toolCallId"),
                    "name": _safe_text(block.get("toolName") or "unknown"),
                    "input": _json_preview(block.get("input", {}), max_tool_chars),
                    "inert": True,
                }
            )
        elif block_type == "tool_result":
            tool_results.append(
                {
                    "tool_use_id": block.get("tool_use_id"),
                    "content": _one_line(_content_text(block.get("content")), max_tool_chars),
                    "is_error": bool(block.get("is_error")),
                    "unavailable": False,
                    "inert": True,
                }
            )
        elif block_type == "tool-result":
            output = block.get("output")
            value = output.get("value") if isinstance(output, dict) else output
            tool_results.append(
                {
                    "tool_use_id": block.get("toolCallId"),
                    "content": _json_preview(value, max_tool_chars),
                    "is_error": isinstance(output, dict)
                    and output.get("type") == "error-text",
                    "unavailable": False,
                    "inert": True,
                }
            )
    text = "\n".join(item for item in texts if item.strip())
    if not text and not tool_calls and not tool_results:
        counters["empty"] = counters.get("empty", 0) + 1
        return None
    return _turn(role, text=text, tool_calls=tool_calls, tool_results=tool_results)


def read_commandcode_session(
    path: Path | str, max_tool_chars: int = 300, *, cwd_hint: str | None = None
) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    records, malformed = _read_plain_jsonl(session_path)
    warnings: list[dict[str, str]] = []
    if malformed:
        _add_warning(
            warnings,
            "malformed_records_skipped",
            f"Skipped {malformed} malformed Command Code transcript record(s).",
        )
    if not any(
        record.get("type") in {"session", "message"} or _is_commandcode_legacy(record)
        for record in records
    ):
        raise ReaderError(
            f"{session_path} is not a Command Code transcript this reader can render "
            "(no session, message, or legacy role records)."
        )
    unknown = sum(
        1
        for record in records
        if isinstance(record.get("type"), str)
        and record["type"] not in COMMANDCODE_KNOWN_TYPES
    )
    if unknown:
        _add_warning(
            warnings,
            "unknown_records_skipped",
            f"Skipped {unknown} unknown Command Code record(s) without interpreting "
            "their payloads.",
        )
    chain = _commandcode_chain(records, warnings)
    index = WorkIndex()
    counters: dict[str, int] = {}
    summaries: list[str] = []
    turns = [
        turn
        for record in chain
        for turn in [
            _render_commandcode_record(record, max_tool_chars, index, counters, summaries)
        ]
        if turn is not None
    ]
    prior_context: dict[str, Any] | None = None
    if summaries:
        _add_warning(
            warnings,
            "history_compacted",
            f"This transcript opens after an auto-compaction: {counters['summary']} "
            "summary record(s) stand in for earlier turns that are not in this file. "
            "The summary is reported as prior context and is the previous agent's "
            "account, not verified state.",
        )
        text = "\n\n".join(summaries)
        prior_context = {
            "source": "auto-compaction summary",
            "text": _safe_text(text[:PRIOR_CONTEXT_CHARS]),
            "truncated": len(text) > PRIOR_CONTEXT_CHARS,
        }
    if counters.get("automated"):
        _add_warning(
            warnings,
            "harness_text_dropped",
            f"Dropped {counters['automated']} harness-written record(s) that wore the "
            "user role (slash-command expansions, interruption markers).",
        )
    cwd = next(
        (
            record["cwd"]
            for record in records
            if record.get("type") == "session" and isinstance(record.get("cwd"), str)
        ),
        None,
    )
    if cwd is None and cwd_hint and _commandcode_slug(cwd_hint) == session_path.parent.name:
        cwd = cwd_hint
    branch = next(
        (
            record["gitBranch"]
            for record in reversed(chain)
            if isinstance(record.get("gitBranch"), str) and record["gitBranch"].strip()
        ),
        None,
    )
    timestamps = [
        record["timestamp"] for record in chain if isinstance(record.get("timestamp"), str)
    ]
    created = next(
        (
            record["timestamp"]
            for record in records
            if record.get("type") == "session" and isinstance(record.get("timestamp"), str)
        ),
        timestamps[0] if timestamps else None,
    )
    sidecar = _commandcode_sidecar(session_path)
    title = sidecar.get("title")
    if not isinstance(title, str) or not title.strip():
        title = next(
            (turn["text"] for turn in turns if turn["role"] == "user" and turn["text"]),
            None,
        )
    result = {
        "tool": "commandcode",
        "source": "command-code",
        "session_id": session_path.stem,
        "path": str(session_path),
        "title": _one_line(title, 200) or None,
        "cwd": cwd,
        "branch": branch if branch != "-" else None,
        "created_at": created,
        "updated_at": timestamps[-1]
        if timestamps
        else _iso_from_millis(_mtime_millis(session_path)),
        "source_repo_root_path": None,
        "prior_context": prior_context,
        "turns": turns,
        "warnings": warnings,
    }
    model = sidecar.get("model")
    if isinstance(model, str) and model:
        result["model"] = model
    return _finalize_result(result, index)


def _discover_commandcode(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    return _discover_claude_layout(
        _commandcode_config_dir() / "projects",
        "commandcode",
        "command-code",
        _commandcode_quick_meta,
        cwd,
        within_min,
        slug=_commandcode_slug,
    )


def _find_commandcode_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    projects = _commandcode_config_dir() / "projects"
    if not projects.is_dir():
        return None
    candidates = [projects / _commandcode_slug(cwd) / f"{session_id}.jsonl"]
    candidates.extend(sorted(projects.glob(f"*/{session_id}.jsonl"), key=str))
    for path in candidates:
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("commandcode", str(path), cwd)
            if candidate is not None:
                return candidate
        else:
            if path.is_file() and not path.is_symlink():
                meta = _commandcode_quick_meta(path)
                return {
                    "tool": "commandcode",
                    "source": "command-code",
                    "session_id": session_id,
                    "path": str(path),
                    "title": (meta.get("title") if meta else None) or "(untitled)",
                    "cwd": (meta.get("cwd") if meta else None) or cwd,
                    "branch": meta.get("branch") if meta else None,
                    "updated_at_ms": _mtime_millis(path),
                }
    return None
