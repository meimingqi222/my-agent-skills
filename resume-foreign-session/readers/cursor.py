"""Cursor (Desktop and CLI) session reader and discovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from reader_core import (
    CURSOR_SKIPPED_ROLES,
    UUID_RE,
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
    _open_sqlite_readonly,
    _paths_match,
    _read_plain_jsonl,
    _safe_text,
    _table_columns,
    _timestamp_to_millis,
    _turn,
    _within,
)


def cursor_workspace_hash(cwd: str) -> str:
    return hashlib.md5(cwd.encode("utf-8", errors="replace")).hexdigest()


def _cursor_root() -> Path:
    return Path.home() / ".cursor"


def _cursor_desktop_paths() -> list[Path]:
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
        home / ".config/Cursor/User/globalStorage/state.vscdb",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "Cursor/User/globalStorage/state.vscdb")
    output: list[Path] = []
    for path in candidates:
        if path not in output:
            output.append(path)
    return output


def _decode_jsonish(raw: Any) -> Any:
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(raw, str):
        text = raw
    else:
        return raw if isinstance(raw, (dict, list)) else None
    stripped = text.strip()
    if stripped and len(stripped) % 2 == 0 and all(
        char in "0123456789abcdefABCDEF" for char in stripped
    ):
        try:
            decoded = bytes.fromhex(stripped).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _merge_cursor_metadata(target: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict):
        return
    for target_key, source_keys in (
        ("title", ("title", "name")),
        ("cwd", ("cwd", "workspacePath")),
        ("source_repo_root_path", ("sourceRepoRootPath",)),
    ):
        if target.get(target_key):
            continue
        for key in source_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                target[target_key] = candidate
                break
    if not target.get("updated_at_ms"):
        for key in ("updatedAtMs", "lastUpdatedAt", "updated_at_ms"):
            candidate = _timestamp_to_millis(value.get(key))
            if candidate is not None:
                target["updated_at_ms"] = candidate
                break
    workspace = value.get("workspaceIdentifier")
    if not target.get("cwd") and isinstance(workspace, dict):
        uri = workspace.get("uri")
        if isinstance(uri, dict):
            candidate = uri.get("fsPath") or uri.get("path")
            if isinstance(candidate, str):
                target["cwd"] = candidate
        candidate = workspace.get("fsPath")
        if not target.get("cwd") and isinstance(candidate, str):
            target["cwd"] = candidate


def _cursor_cli_metadata(session_dir: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "title": None,
        "cwd": None,
        "updated_at_ms": 0,
        "source_repo_root_path": None,
    }
    meta_path = session_dir / "meta.json"
    if meta_path.is_file() and not meta_path.is_symlink():
        try:
            _merge_cursor_metadata(
                metadata,
                json.loads(meta_path.read_text(encoding="utf-8", errors="replace")),
            )
        except (OSError, json.JSONDecodeError):
            pass
    store_path = session_dir / "store.db"
    if store_path.is_file() and not store_path.is_symlink():
        try:
            with _open_sqlite_readonly(store_path) as database:
                columns = _table_columns(database, "meta")
                if {"key", "value"}.issubset(columns):
                    rows = database.execute(
                        "SELECT key, value FROM meta ORDER BY CASE key "
                        "WHEN '0' THEN 0 WHEN 'metadata' THEN 1 WHEN 'updatedAtMs' THEN 2 "
                        "WHEN 'title' THEN 3 WHEN 'name' THEN 4 WHEN 'cwd' THEN 5 ELSE 6 END, key"
                    )
                    for key, raw in rows:
                        value = _decode_jsonish(raw)
                        _merge_cursor_metadata(metadata, value)
                        if key in {"title", "name", "cwd", "updatedAtMs"}:
                            _merge_cursor_metadata(metadata, {str(key): value})
        except (ReaderError, sqlite3.Error):
            pass
    metadata["updated_at_ms"] = metadata.get("updated_at_ms") or max(
        _mtime_millis(meta_path), _mtime_millis(store_path), _mtime_millis(session_dir)
    )
    return metadata


def _discover_cursor_cli(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    chats = _cursor_root() / "chats"
    if not chats.is_dir() or chats.is_symlink():
        return []
    try:
        children = sorted(chats.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    if cwd is None:
        workspaces = [
            child
            for child in children
            if child.is_dir() and not child.is_symlink()
        ]
    else:
        workspace = chats / cursor_workspace_hash(cwd)
        workspaces = [workspace] if workspace.is_dir() and not workspace.is_symlink() else []
    sessions: list[dict[str, Any]] = []
    for workspace in workspaces:
        try:
            session_dirs = sorted(workspace.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for child in session_dirs:
            if not UUID_RE.fullmatch(child.name) or not child.is_dir() or child.is_symlink():
                continue
            metadata = _cursor_cli_metadata(child)
            stored_cwd = metadata.get("cwd")
            if cwd is not None and stored_cwd and not _paths_match(stored_cwd, cwd):
                continue
            updated = int(metadata.get("updated_at_ms") or 0)
            if not _within(updated, within_min):
                continue
            store = child / "store.db"
            meta = child / "meta.json"
            path = store if store.is_file() else meta
            if not path.is_file():
                continue
            sessions.append(
                {
                    "tool": "cursor",
                    "source": "cursor-cli",
                    "session_id": child.name,
                    "path": str(path),
                    "title": metadata.get("title") or "(untitled)",
                    "cwd": stored_cwd or cwd,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": metadata.get("source_repo_root_path"),
                }
            )
    return sessions


def _discover_cursor_desktop(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for path in _cursor_desktop_paths():
        if not path.is_file() or path.is_symlink():
            continue
        try:
            with _open_sqlite_readonly(path) as database:
                columns = _table_columns(database, "composerHeaders")
                required = {
                    "composerId",
                    "lastUpdatedAt",
                    "isArchived",
                    "isSubagent",
                    "value",
                }
                if not required.issubset(columns):
                    continue
                order = "recency" if "recency" in columns else "lastUpdatedAt"
                rows = database.execute(
                    "SELECT composerId, lastUpdatedAt, value FROM composerHeaders "
                    "WHERE COALESCE(isArchived, 0) = 0 AND COALESCE(isSubagent, 0) = 0 "
                    f"ORDER BY {order} DESC, composerId ASC"
                )
                for session_id, raw_updated, raw_value in rows:
                    if not isinstance(session_id, str):
                        continue
                    value = _decode_jsonish(raw_value)
                    metadata: dict[str, Any] = {
                        "title": None,
                        "cwd": None,
                        "updated_at_ms": _timestamp_to_millis(raw_updated) or 0,
                        "source_repo_root_path": None,
                    }
                    _merge_cursor_metadata(metadata, value)
                    if cwd is not None and not _paths_match(metadata.get("cwd"), cwd):
                        continue
                    updated = int(metadata["updated_at_ms"])
                    if not _within(updated, within_min):
                        continue
                    sessions.append(
                        {
                            "tool": "cursor",
                            "source": "cursor-desktop",
                            "session_id": session_id,
                            "path": str(path),
                            "title": metadata.get("title") or "(untitled)",
                            "cwd": metadata.get("cwd"),
                            "branch": (
                                value.get("gitBranch")
                                if isinstance(value, dict)
                                and isinstance(value.get("gitBranch"), str)
                                else None
                            ),
                            "updated_at_ms": updated,
                            "updated_at": _iso_from_millis(updated),
                            "source_repo_root_path": metadata.get("source_repo_root_path"),
                        }
                    )
        except (ReaderError, sqlite3.Error):
            continue
    return sessions


def _find_nested_string(value: Any, key: str, depth: int = 0) -> str | None:
    if depth > 8:
        return None
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
        for nested in value.values():
            found = _find_nested_string(nested, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_nested_string(nested, key, depth + 1)
            if found is not None:
                return found
    return None


def _cursor_user_text(text: str) -> str | None:
    matches = re.findall(r"<user_query>\s*(.*?)\s*</user_query>", text, flags=re.DOTALL)
    if matches:
        return "\n".join(_safe_text(match) for match in matches if match.strip()) or None
    stripped = text.lstrip()
    blocked_wrappers = (
        "<environment_context",
        "<user_instructions",
        "<system_reminder",
        "<manually_attached_skills",
        "<timestamp",
    )
    if stripped.startswith(blocked_wrappers):
        return None
    return _safe_text(text)


def _render_cursor_role_value(
    value: Any, max_tool_chars: int, index: WorkIndex | None = None
) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(value, dict):
        return [], False
    value_type = value.get("type")
    if value_type in {"thinking", "reasoning", "redacted_thinking"}:
        return [], True
    role = value.get("role")
    if isinstance(role, str):
        normalized_role = role.lower()
        if normalized_role in CURSOR_SKIPPED_ROLES:
            return [], True
        if normalized_role not in {"user", "assistant", "tool"}:
            return [], True
        message = value.get("message")
        content = (
            message.get("content")
            if isinstance(message, dict) and "content" in message
            else value.get("content")
        )
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for block in _blocks(content):
            block_type = block.get("type")
            if block_type in {"thinking", "reasoning", "redacted_thinking", "signature"}:
                continue
            if block_type in {"text", "input_text", "output_text"}:
                text = block.get("text")
                if isinstance(text, str):
                    rendered = (
                        _cursor_user_text(text)
                        if normalized_role == "user"
                        else None
                        if _is_generated_meta_text(text)
                        else _safe_text(text)
                    )
                    if rendered:
                        texts.append(rendered)
            elif block_type in {"tool_use", "tool_call"}:
                if index is not None:
                    index.record(
                        block.get("name"), block.get("input", block.get("arguments", {}))
                    )
                calls.append(
                    {
                        "id": block.get("id") or block.get("call_id"),
                        "name": _safe_text(block.get("name") or "unknown"),
                        "input": _json_preview(
                            block.get("input", block.get("arguments", {})), max_tool_chars
                        ),
                        "inert": True,
                    }
                )
            elif block_type in {"tool_result", "tool_output"}:
                results.append(
                    {
                        "tool_use_id": block.get("tool_use_id") or block.get("call_id"),
                        "content": _one_line(_content_text(block.get("content")), max_tool_chars),
                        "is_error": bool(block.get("is_error")),
                        "unavailable": False,
                        "inert": True,
                    }
                )
        top_calls = value.get("tool_calls")
        if isinstance(top_calls, list):
            for call in top_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") if isinstance(call.get("function"), dict) else call
                if index is not None:
                    index.record(
                        function.get("name"),
                        function.get("arguments", function.get("input", {})),
                    )
                calls.append(
                    {
                        "id": call.get("id") or function.get("call_id"),
                        "name": _safe_text(function.get("name") or "unknown"),
                        "input": _json_preview(
                            function.get("arguments", function.get("input", {})), max_tool_chars
                        ),
                        "inert": True,
                    }
                )
        if normalized_role == "tool" and not results:
            results.append(
                {
                    "tool_use_id": value.get("tool_call_id") or value.get("call_id"),
                    "content": _one_line(_content_text(content), max_tool_chars),
                    "is_error": bool(value.get("is_error")),
                    "unavailable": False,
                    "inert": True,
                }
            )
            texts = []
        text = "\n".join(part for part in texts if part.strip())
        if text or calls or results:
            return [
                _turn(
                    normalized_role,
                    text=text,
                    tool_calls=calls,
                    tool_results=results,
                )
            ], False
        return [], False
    turns: list[dict[str, Any]] = []
    for key in ("messages", "turns", "conversation", "bubbles"):
        nested = value.get(key)
        if isinstance(nested, list):
            skipped = False
            for item in nested:
                item_turns, item_skipped = _render_cursor_role_value(
                    item, max_tool_chars, index
                )
                turns.extend(item_turns)
                skipped |= item_skipped
            return turns, skipped
    return [], False


def _ordered_cursor_transcript(session_id: str) -> Path | None:
    projects = _cursor_root() / "projects"
    if not projects.is_dir():
        return None
    candidates = sorted(
        projects.glob(f"*/agent-transcripts/{session_id}/{session_id}.jsonl"),
        key=lambda path: (-_mtime_millis(path), str(path)),
    )
    return next((path for path in candidates if path.is_file() and not path.is_symlink()), None)


def _read_cursor_values(
    rows: Iterable[tuple[Any, Any]],
    *,
    max_tool_chars: int,
    warnings: list[dict[str, str]],
    index: WorkIndex | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    turns: list[dict[str, Any]] = []
    source_root: str | None = None
    unavailable = 0
    unsafe = 0
    row_count = 0
    for _, raw in rows:
        row_count += 1
        value = _decode_jsonish(raw)
        if value is None:
            unavailable += 1
            continue
        source_root = source_root or _find_nested_string(value, "sourceRepoRootPath")
        value_turns, skipped = _render_cursor_role_value(value, max_tool_chars, index)
        turns.extend(value_turns)
        unsafe += int(skipped)
    if unavailable:
        _add_warning(
            warnings,
            "binary_content_unavailable",
            f"{unavailable} Cursor blob(s) were binary, protobuf, or non-JSON and are unavailable.",
        )
    if unsafe:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {unsafe} Cursor system, preamble, instruction, or reasoning payload(s).",
        )
    if row_count and not turns:
        _add_warning(
            warnings,
            "transcript_content_unavailable",
            "No role-tagged UTF-8 JSON turns were recoverable; binary/protobuf content was not fabricated.",
        )
    return turns, source_root


def _read_cursor_transcript(
    path: Path,
    max_tool_chars: int,
    warnings: list[dict[str, str]],
    index: WorkIndex | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    records, malformed = _read_plain_jsonl(path)
    if malformed:
        _add_warning(
            warnings,
            "malformed_records_skipped",
            f"Skipped {malformed} malformed Cursor transcript record(s).",
        )
    return _read_cursor_values(
        enumerate(records),
        max_tool_chars=max_tool_chars,
        warnings=warnings,
        index=index,
    )


def _cursor_cli_store_rows(database: sqlite3.Connection) -> list[tuple[Any, Any]]:
    columns = _table_columns(database, "blobs")
    key_column = next((name for name in ("id", "key", "hash") if name in columns), None)
    value_column = next((name for name in ("data", "value", "blob") if name in columns), None)
    if key_column is None or value_column is None:
        return []
    try:
        return list(
            database.execute(
                f'SELECT "{key_column}", "{value_column}" FROM blobs ORDER BY "{key_column}"'
            )
        )
    except sqlite3.Error:
        return []


def _cursor_desktop_rows(
    database: sqlite3.Connection, session_id: str
) -> list[tuple[Any, Any]]:
    columns = _table_columns(database, "cursorDiskKV")
    if not {"key", "value"}.issubset(columns):
        return []
    try:
        return list(
            database.execute(
                "SELECT key, value FROM cursorDiskKV "
                "WHERE key = ? OR key LIKE ? ORDER BY key",
                (f"composerData:{session_id}", f"bubbleId:{session_id}:%"),
            )
        )
    except sqlite3.Error:
        return []


def read_cursor_session(
    candidate: dict[str, Any], max_tool_chars: int = 300
) -> dict[str, Any]:
    warnings: list[dict[str, str]] = []
    index = WorkIndex()
    session_id = str(candidate.get("session_id") or "")
    source = str(candidate.get("source") or "cursor")
    path = Path(str(candidate.get("path") or "")).expanduser()
    metadata = {
        "title": candidate.get("title"),
        "cwd": candidate.get("cwd"),
        "updated_at_ms": candidate.get("updated_at_ms") or _mtime_millis(path),
        "source_repo_root_path": candidate.get("source_repo_root_path"),
    }
    transcript = (
        path
        if source == "cursor-transcript" or path.name.endswith(".jsonl")
        else _ordered_cursor_transcript(session_id)
    )
    if transcript is not None:
        turns, source_root = _read_cursor_transcript(
            transcript, max_tool_chars, warnings, index
        )
        selected_path = transcript
    elif source == "cursor-desktop" or path.name == "state.vscdb":
        selected_path = path
        try:
            with _open_sqlite_readonly(path) as database:
                turns, source_root = _read_cursor_values(
                    _cursor_desktop_rows(database, session_id),
                    max_tool_chars=max_tool_chars,
                    warnings=warnings,
                    index=index,
                )
                try:
                    row = database.execute(
                        "SELECT lastUpdatedAt, value FROM composerHeaders WHERE composerId = ? "
                        "ORDER BY lastUpdatedAt DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                except sqlite3.Error:
                    row = None
                if row:
                    metadata["updated_at_ms"] = _timestamp_to_millis(row[0])
                    _merge_cursor_metadata(metadata, _decode_jsonish(row[1]))
        except ReaderError as exc:
            raise ReaderError(str(exc)) from exc
    else:
        session_dir = path.parent if path.name in {"store.db", "meta.json"} else path
        cli_metadata = _cursor_cli_metadata(session_dir)
        for key, value in cli_metadata.items():
            if value and not metadata.get(key):
                metadata[key] = value
        store_path = session_dir / "store.db"
        selected_path = store_path if store_path.is_file() else path
        if store_path.is_file():
            with _open_sqlite_readonly(store_path) as database:
                turns, source_root = _read_cursor_values(
                    _cursor_cli_store_rows(database),
                    max_tool_chars=max_tool_chars,
                    warnings=warnings,
                    index=index,
                )
        else:
            turns, source_root = [], None
            _add_warning(
                warnings,
                "transcript_content_unavailable",
                "Cursor CLI store.db is absent; no transcript content was fabricated.",
            )
    source_root = source_root or metadata.get("source_repo_root_path")
    updated_ms = _timestamp_to_millis(metadata.get("updated_at_ms"))
    title = metadata.get("title")
    if title == "(untitled)":
        title = None
    title = title or next(
        (_one_line(turn["text"], 200) for turn in turns if turn["role"] == "user" and turn["text"]),
        None,
    )
    result = {
        "tool": "cursor",
        "source": source,
        "session_id": session_id or path.stem,
        "path": str(selected_path),
        "title": title,
        "cwd": metadata.get("cwd"),
        "branch": candidate.get("branch"),
        "created_at": None,
        "updated_at": _iso_from_millis(updated_ms),
        "source_repo_root_path": source_root,
        "turns": turns,
        "warnings": warnings,
    }
    return _finalize_result(result, index)


def _find_cursor_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    transcript = _ordered_cursor_transcript(session_id)
    if transcript is not None:
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("cursor", str(transcript), cwd)
            if candidate is not None:
                return candidate
        else:
            return {
                "tool": "cursor",
                "source": "cursor-transcript",
                "session_id": session_id,
                "path": str(transcript),
                "title": None,
                "cwd": cwd,
                "updated_at_ms": _mtime_millis(transcript),
            }
    chats = _cursor_root() / "chats"
    if chats.is_dir():
        for path in sorted(chats.glob(f"*/{session_id}/store.db"), key=str):
            if candidate_from_path_fn is not None:
                candidate = candidate_from_path_fn("cursor", str(path), cwd)
                if candidate is not None:
                    return candidate
            else:
                return {
                    "tool": "cursor",
                    "source": "cursor-cli",
                    "session_id": session_id,
                    "path": str(path),
                    "title": None,
                    "cwd": cwd,
                    "updated_at_ms": _mtime_millis(path),
                }
    for database_path in _cursor_desktop_paths():
        if not database_path.is_file():
            continue
        try:
            with _open_sqlite_readonly(database_path) as database:
                row = database.execute(
                    "SELECT lastUpdatedAt, value FROM composerHeaders WHERE composerId = ? "
                    "AND COALESCE(isArchived, 0) = 0 AND COALESCE(isSubagent, 0) = 0 "
                    "ORDER BY lastUpdatedAt DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
            if row:
                value = _decode_jsonish(row[1])
                metadata: dict[str, Any] = {
                    "title": None,
                    "cwd": cwd,
                    "updated_at_ms": _timestamp_to_millis(row[0]) or 0,
                    "source_repo_root_path": None,
                }
                _merge_cursor_metadata(metadata, value)
                return {
                    "tool": "cursor",
                    "source": "cursor-desktop",
                    "session_id": session_id,
                    "path": str(database_path),
                    **metadata,
                }
        except (ReaderError, sqlite3.Error):
            continue
    return None
