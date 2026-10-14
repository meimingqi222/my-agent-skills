"""WorkBuddy (Tencent, CodeBuddy-based) session reader and discovery.

WorkBuddy keeps one JSONL transcript per session under
``~/.workbuddy/projects/<encoded-cwd>/<session-id>.jsonl``; the "WorkBuddy AI"
build uses ``~/.workbuddy-ai`` with the same layout. A sibling
``<session-id>.meta.json`` may record ``cwd`` (sessions migrated from the old
agent history), and a sibling ``<session-id>/tool-results/`` directory holds
tool output offloaded from the transcript.

Records are CodeBuddy items with a ``type``:

- ``message`` with ``role`` ``user`` (``input_text`` / ``image_blob_ref``
  content, or a bare string) or ``assistant`` (``output_text``);
- ``function_call`` (``name``, JSON-string ``arguments``, ``callId``) and
  ``function_call_result`` (``output`` text, ``status`` ``completed`` or
  ``incomplete`` for a failed call);
- ``reasoning``, never rendered;
- metadata: ``ai-title``, ``custom-title``, ``summary``, ``session-meta``,
  ``file-history-snapshot``.

Several kinds of harness-written text wear the user role and are not user
requests: the ``<system-reminder data-role="user-context">`` environment block
(the real request sits inside ``<user_query>`` in the same text), error-recovery
reminders, ``<teammate-message>`` inbox deliveries between team agents, and the
``/compact`` command and prompt (``providerData.agent == "compact"``). A
``<cb_summary>`` message is the compaction summary standing in for earlier turns.

Records are rendered in file order: a parent with several children is a batch of
parallel tool calls, not a rewind.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from reader_core import (
    PRIOR_CONTEXT_CHARS,
    ReaderError,
    WorkIndex,
    _add_warning,
    _finalize_result,
    _is_harness_preamble,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _paths_match,
    _safe_text,
    _timestamp_to_millis,
    _turn,
    _within,
)

# `source` label per data directory name.
WORKBUDDY_SOURCES = {".workbuddy": "workbuddy", ".workbuddy-ai": "workbuddy-ai"}
# providerData.agent values that mean the main agent drove the session; any
# other value names a teammate agent, whose transcript is a subagent session.
MAIN_AGENTS = {None, "cli", "compact"}

USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
SYSTEM_REMINDER_RE = re.compile(r"<system-reminder\b[^>]*>.*?</system-reminder>", re.DOTALL)
CB_SUMMARY_RE = re.compile(r"^\s*<cb_summary>\s*(.*?)\s*(?:</cb_summary>\s*)?$", re.DOTALL)
TITLE_LINE_RE = re.compile(r'"type":"(ai-title|custom-title|summary)"')
USER_LINE_RE = re.compile(r'"role":"user"')
# Native ids besides UUIDs: 32-hex ids of sessions migrated from the old agent
# history, and `agent-<hex>` ids of teammate (subagent) transcripts.
WORKBUDDY_ID_RE = re.compile(r"[0-9a-fA-F]{32}|agent-[0-9a-fA-F]{6,}")


def _workbuddy_data_dirs() -> list[Path]:
    configured = os.environ.get("WORKBUDDY_CONFIG_DIR")
    if configured:
        return [Path(configured).expanduser()]
    return [Path.home() / name for name in WORKBUDDY_SOURCES]


def _workbuddy_source(path: Path) -> str:
    for parent in path.parents:
        if parent.name in WORKBUDDY_SOURCES:
            return WORKBUDDY_SOURCES[parent.name]
    return "workbuddy"


def _workbuddy_sidecar_cwd(path: Path) -> str | None:
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cwd = meta.get("cwd") if isinstance(meta, dict) else None
    return cwd if isinstance(cwd, str) and cwd else None


def _message_text(content: Any) -> tuple[str, int]:
    """(joined text, number of image attachments) of a message's content."""
    if isinstance(content, str):
        return content, 0
    texts: list[str] = []
    images = 0
    for item in content if isinstance(content, list) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in {"input_text", "output_text", "text"} and isinstance(item.get("text"), str):
            texts.append(item["text"])
        elif kind in {"image_blob_ref", "input_image", "image"}:
            images += 1
    return "\n".join(texts), images


def _classify_user_text(text: str) -> tuple[str, str]:
    """Split a user-role text into (kind, text): ``request`` (what the user
    typed), ``summary`` (compaction summary), or ``harness`` (injected)."""
    summary = CB_SUMMARY_RE.match(text)
    if summary:
        return "summary", summary.group(1)
    queries = USER_QUERY_RE.findall(text)
    if queries:
        return "request", "\n\n".join(q for q in queries if q.strip())
    stripped = SYSTEM_REMINDER_RE.sub("", text).strip()
    if not stripped or stripped.startswith("<teammate-message") or _is_harness_preamble(stripped):
        return "harness", ""
    return "request", stripped


def _note_title(record: dict[str, Any], titles: dict[str, str]) -> None:
    """Record a title-bearing record into `titles`, keyed by its kind; the
    initial-message summary keeps its first value, the others their last."""
    kind = record.get("type")
    field = {"ai-title": "aiTitle", "custom-title": "customTitle", "summary": "summary"}.get(kind)
    value = record.get(field) if field else None
    if not isinstance(value, str) or not value.strip():
        return
    if kind == "summary":
        titles.setdefault(kind, value)
    else:
        titles[kind] = value


def _pick_title(titles: dict[str, str]) -> str | None:
    """A user-set title beats the AI-generated one, which beats the first-message summary."""
    return titles.get("custom-title") or titles.get("ai-title") or titles.get("summary")


def _call_output(output: Any) -> str:
    if isinstance(output, dict):
        return output.get("text") if isinstance(output.get("text"), str) else _json_preview(output, 4000)
    if isinstance(output, list):
        return "\n".join(
            item.get("text", "") for item in output
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return "" if output is None else str(output)


def read_workbuddy_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    warnings: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    index = WorkIndex()
    summaries: list[str] = []
    titles: dict[str, str] = {}
    session_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    first_ts: int | None = None
    first_agent: Any = "unset"
    harness = reasoning = images = unknown = malformed = 0

    def assistant_turn() -> dict[str, Any]:
        if not turns or turns[-1]["role"] != "assistant":
            turns.append(_turn("assistant"))
        return turns[-1]

    try:
        handle = session_path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ReaderError(f"failed to read WorkBuddy transcript {session_path}: {exc}") from exc
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(record, dict):
                malformed += 1
                continue
            kind = record.get("type")
            if first_ts is None:
                first_ts = _timestamp_to_millis(record.get("timestamp"))
            if session_id is None and isinstance(record.get("sessionId"), str):
                session_id = record["sessionId"]
            if cwd is None and isinstance(record.get("cwd"), str) and record["cwd"]:
                cwd = record["cwd"]
            provider = record.get("providerData") if isinstance(record.get("providerData"), dict) else {}
            if isinstance(provider.get("model"), str):
                model = provider["model"]

            if kind == "message":
                text, attached = _message_text(record.get("content"))
                images += attached
                if record.get("role") == "user":
                    agent = provider.get("agent")
                    if first_agent == "unset":
                        first_agent = agent
                    category, body = _classify_user_text(text)
                    # The compaction summary itself is flagged isCompactInternal,
                    # so recognise it before dropping the /compact command and prompt.
                    if category == "summary":
                        summaries.append(body)
                    elif agent == "compact" or provider.get("isCompactInternal"):
                        harness += 1
                    elif category == "harness":
                        harness += 1
                    elif body.strip():
                        turns.append(_turn("user", text=body))
                elif record.get("role") == "assistant":
                    if text.strip():
                        turns.append(_turn("assistant", text=text))
                else:
                    unknown += 1
            elif kind == "function_call":
                name = record.get("name") or "tool"
                raw = record.get("arguments")
                try:
                    arguments = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    arguments = raw
                index.record(name, arguments)
                assistant_turn()["tool_calls"].append({
                    "id": record.get("callId"),
                    "name": _safe_text(name),
                    "input": _json_preview(arguments, max_tool_chars),
                    "inert": True,
                })
            elif kind == "function_call_result":
                assistant_turn()["tool_results"].append({
                    "tool_use_id": record.get("callId"),
                    "content": _one_line(_call_output(record.get("output")), max_tool_chars * 3),
                    "is_error": record.get("status") not in (None, "completed"),
                    "unavailable": False,
                    "inert": True,
                })
            elif kind == "reasoning":
                reasoning += 1
            elif kind in {"ai-title", "custom-title", "summary"}:
                _note_title(record, titles)
            elif kind in {"session-meta", "file-history-snapshot"}:
                continue
            else:
                unknown += 1

    if summaries:
        _add_warning(
            warnings,
            "history_compacted",
            f"This transcript was compacted: {len(summaries)} summary record(s) stand in "
            "for earlier turns that are not in this file. The summary is reported as prior "
            "context and is the previous agent's account, not verified state.",
        )
    if harness:
        _add_warning(
            warnings,
            "harness_text_dropped",
            f"Dropped {harness} harness-written record(s) that wore the user role "
            "(environment reminders, teammate inbox messages, /compact prompts).",
        )
    if reasoning:
        _add_warning(warnings, "unsafe_records_skipped",
                     f"Skipped {reasoning} WorkBuddy reasoning record(s); reasoning is never rendered.")
    if images:
        _add_warning(warnings, "attachments_skipped",
                     f"Skipped {images} image attachment(s); their content is not recovered.")
    if unknown or malformed:
        _add_warning(warnings, "malformed_records_skipped",
                     f"Skipped {malformed} malformed and {unknown} unrecognised WorkBuddy record(s).")
    if first_agent not in MAIN_AGENTS and first_agent != "unset":
        _add_warning(
            warnings,
            "subagent_session",
            f"This is the transcript of teammate agent {_one_line(first_agent, 60)!r}: its work "
            "was directed by another WorkBuddy session, not by the user.",
        )

    summary_text = "\n\n".join(summaries)
    result = {
        "tool": "workbuddy",
        "source": _workbuddy_source(session_path),
        "session_id": session_id or session_path.stem,
        "path": str(session_path),
        "title": _pick_title(titles),
        "cwd": cwd or _workbuddy_sidecar_cwd(session_path),
        "branch": None,
        "created_at": _iso_from_millis(first_ts),
        "updated_at": _iso_from_millis(_mtime_millis(session_path)),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
        "model": model,
        "prior_context": {
            "source": "compaction summary",
            "text": _safe_text(summary_text[:PRIOR_CONTEXT_CHARS]),
            "truncated": len(summary_text) > PRIOR_CONTEXT_CHARS,
        } if summaries else None,
    }
    return _finalize_result(result, index)


def _workbuddy_quick_meta(path: Path) -> dict[str, Any] | None:
    """Listing metadata without rendering: id, cwd, title, and whether the
    session belongs to a teammate agent. Only title lines and the first record
    carrying each field are parsed."""
    titles: dict[str, str] = {}
    session_id = cwd = first_request = None
    first_agent: Any = "unset"
    saw_message = False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                title_kind = TITLE_LINE_RE.search(line)
                needs_head = session_id is None or cwd is None or first_agent == "unset"
                needs_request = first_request is None and USER_LINE_RE.search(line)
                if not title_kind and not needs_head and not needs_request:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                saw_message = saw_message or kind == "message"
                if session_id is None and isinstance(record.get("sessionId"), str):
                    session_id = record["sessionId"]
                if cwd is None and isinstance(record.get("cwd"), str) and record["cwd"]:
                    cwd = record["cwd"]
                if kind == "message" and record.get("role") == "user":
                    provider = record.get("providerData")
                    provider = provider if isinstance(provider, dict) else {}
                    if first_agent == "unset":
                        first_agent = provider.get("agent")
                    if first_request is None and provider.get("agent") != "compact":
                        category, body = _classify_user_text(_message_text(record.get("content"))[0])
                        if category == "request" and body.strip():
                            first_request = _one_line(body, 60)
                _note_title(record, titles)
    except OSError:
        return None
    if not saw_message:
        return None
    return {
        "session_id": session_id or path.stem,
        "cwd": cwd or _workbuddy_sidecar_cwd(path),
        "title": _pick_title(titles) or first_request,
        "subagent": first_agent not in MAIN_AGENTS and first_agent != "unset",
        "updated_at_ms": _mtime_millis(path),
    }


def _workbuddy_transcripts() -> list[Path]:
    paths: list[Path] = []
    for data_dir in _workbuddy_data_dirs():
        projects = data_dir / "projects"
        if not projects.is_dir() or projects.is_symlink():
            continue
        for path in sorted(projects.glob("*/*.jsonl")):
            if path.is_file() and not path.is_symlink():
                paths.append(path)
    return paths


def _workbuddy_candidate(path: Path, meta: dict[str, Any], fallback_cwd: str | None) -> dict[str, Any]:
    updated = meta.get("updated_at_ms", 0)
    return {
        "tool": "workbuddy",
        "source": _workbuddy_source(path),
        "session_id": meta["session_id"],
        "path": str(path),
        "title": meta.get("title") or "(untitled)",
        "cwd": meta.get("cwd") or fallback_cwd,
        "branch": None,
        "updated_at_ms": updated,
        "updated_at": _iso_from_millis(updated),
        "source_repo_root_path": None,
    }


def _discover_workbuddy(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for path in _workbuddy_transcripts():
        if not _within(_mtime_millis(path), within_min):
            continue
        meta = _workbuddy_quick_meta(path)
        # Teammate transcripts hold work another session directed; they stay
        # reachable by native session ID but are kept out of listings.
        if meta is None or meta["subagent"]:
            continue
        if cwd is not None and not (meta.get("cwd") and _paths_match(meta["cwd"], cwd)):
            continue
        sessions.append(_workbuddy_candidate(path, meta, cwd))
    return sessions


def _find_workbuddy_id(session_id: str, cwd: str) -> dict[str, Any] | None:
    for path in _workbuddy_transcripts():
        if path.stem != session_id:
            continue
        meta = _workbuddy_quick_meta(path)
        if meta is not None:
            return _workbuddy_candidate(path, meta, cwd)
    return None
