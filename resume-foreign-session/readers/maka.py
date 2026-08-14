"""Maka desktop session reader and discovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from reader_core import (
    MAKA_CHUNK_MARKER,
    MAKA_KNOWN_TYPES,
    ReaderError,
    WorkIndex,
    _add_warning,
    _as_dict,
    _as_text,
    _finalize_result,
    _is_generated_meta_text,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _open_sqlite_readonly,
    _paths_match,
    _safe_text,
    _turn,
    _within,
)


def _maka_client_data_roots() -> list[Path]:
    """Candidate Maka client data roots (``<profile>/workspaces`` parents)."""
    roots: list[Path] = []
    for profile in ("Maka Dev", "Maka"):
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA")
            base = Path(appdata).expanduser() if appdata else None
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            xdg = os.environ.get("XDG_CONFIG_HOME")
            base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        if base is not None:
            roots.append(base / profile)
    return roots


def _maka_workspace_roots() -> list[Path]:
    """Every Maka workspace root, i.e. the directories holding runtime.sqlite."""
    output: list[Path] = []
    for data_root in _maka_client_data_roots():
        workspaces = data_root / "workspaces"
        if not workspaces.is_dir() or workspaces.is_symlink():
            continue
        try:
            children = sorted(workspaces.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for child in children:
            if child.is_dir() and not child.is_symlink() and child not in output:
                output.append(child)
    return output


def _maka_database_paths() -> list[Path]:
    """Candidate Maka session databases, newest format/profile first."""
    output: list[Path] = []
    for root in _maka_workspace_roots():
        path = root / "runtime.sqlite"
        if path.is_file() and not path.is_symlink():
            output.append(path)
    return output


def _maka_header(payload_json: Any) -> dict[str, Any]:
    header = _as_dict(payload_json)
    return header if isinstance(header, dict) else {}


def _maka_decode_message(
    database: sqlite3.Connection,
    session_id: str,
    sequence: int,
    raw_json: Any,
) -> dict[str, Any] | None:
    """Decode one Maka message row, reassembling chunked records when present."""
    if not isinstance(raw_json, str) or raw_json != MAKA_CHUNK_MARKER:
        return _as_dict(raw_json)
    try:
        row = database.execute(
            "SELECT sha256 FROM session_message_payloads "
            "WHERE session_id = ? AND sequence = ?",
            (session_id, sequence),
        ).fetchone()
        expected = str(row[0]) if row else None
        chunks = database.execute(
            "SELECT chunk_index, data FROM session_message_chunks "
            "WHERE session_id = ? AND sequence = ? ORDER BY chunk_index",
            (session_id, sequence),
        ).fetchall()
        if not chunks:
            return None
        blob = b"".join(bytes(chunk[1]) for chunk in chunks)
        if expected and hashlib.sha256(blob).hexdigest().casefold() != expected.casefold():
            return None
        return _as_dict(blob.decode("utf-8", errors="replace"))
    except (sqlite3.Error, UnicodeDecodeError, ReaderError):
        return None


def _maka_tool_result_text(content: Any) -> str:
    """Recover the readable payload of a Maka tool_result content object."""
    if not isinstance(content, dict):
        return _as_text(content)
    kind = content.get("kind")
    if kind == "text":
        return str(content.get("text") or "")
    if kind == "terminal":
        output = content.get("output") if isinstance(content.get("output"), dict) else {}
        return "\n".join(
            part for part in (output.get("stdout") or "", output.get("stderr") or "") if part
        )
    if kind == "json":
        value = content.get("value")
        if value is not None:
            return json.dumps(value, ensure_ascii=False)
        return ""
    if kind == "file_diff":
        return str(content.get("diff") or "")
    if kind == "shell_run":
        output = content.get("output") if isinstance(content.get("output"), dict) else {}
        combined = "\n".join(
            part
            for part in (output.get("stdout") or "", output.get("stderr") or "")
            if part
        )
        if combined:
            return combined
        return str(content.get("failureMessage") or "")
    if kind == "explore_agent":
        return str(content.get("objective") or "")
    return json.dumps(content, ensure_ascii=False)


def read_maka_session(
    database_path: Path | str, session_id: str, max_tool_chars: int = 300
) -> dict[str, Any]:
    database_path = Path(database_path).expanduser()
    warnings: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    index = WorkIndex()

    def flush(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is not None and (
            current["text"] or current["tool_calls"] or current["tool_results"]
        ):
            turns.append(current)
        return None

    try:
        with _open_sqlite_readonly(database_path) as database:
            if not session_id:
                row = database.execute(
                    "SELECT session_id FROM session_metadata "
                    "WHERE session_id NOT IN (SELECT session_id FROM session_metadata_tombstones) "
                    "ORDER BY COALESCE(last_message_at, last_used_at, created_at) DESC LIMIT 1"
                ).fetchone()
                if row is None or not isinstance(row[0], str):
                    raise ReaderError(f"no Maka session in {database_path}")
                session_id = row[0]
            row = database.execute(
                "SELECT payload_json, created_at, last_used_at, last_message_at "
                "FROM session_metadata WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ReaderError(f"no Maka session {session_id} in {database_path}")
            payload_json, created_at, last_used_at, last_message_at = row
            header = _maka_header(payload_json)
            current: dict[str, Any] | None = None
            unknown = 0
            unavailable = 0
            dropped = 0
            for seq, _, _, raw in database.execute(
                "SELECT sequence, message_type, message_ts, record_json "
                "FROM session_messages WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ):
                message = _maka_decode_message(database, session_id, int(seq), raw)
                if not isinstance(message, dict):
                    unavailable += 1
                    continue
                kind = message.get("type")
                if kind not in MAKA_KNOWN_TYPES:
                    unknown += 1
                    continue
                if kind == "user":
                    current = flush(current)
                    text = _safe_text(message.get("text") or "")
                    if text.strip() and not _is_generated_meta_text(text):
                        current = _turn("user", text=text)
                    else:
                        dropped += 1
                elif kind == "assistant":
                    current = flush(current)
                    text = _safe_text(message.get("text") or "")
                    if text.strip() and not _is_generated_meta_text(text):
                        current = _turn("assistant", text=text)
                    else:
                        current = _turn("assistant")
                elif kind == "tool_call":
                    if current is None or current["role"] != "assistant":
                        current = flush(current)
                        current = _turn("assistant")
                    index.record(message.get("toolName"), message.get("args"))
                    current["tool_calls"].append(
                        {
                            "id": message.get("id"),
                            "name": _safe_text(message.get("toolName") or "tool"),
                            "input": _json_preview(message.get("args", {}), max_tool_chars),
                            "inert": True,
                        }
                    )
                elif kind == "tool_result":
                    if current is None or current["role"] != "assistant":
                        current = flush(current)
                        current = _turn("assistant")
                    current["tool_results"].append(
                        {
                            "tool_use_id": message.get("toolUseId"),
                            "content": _one_line(
                                _maka_tool_result_text(message.get("content")), max_tool_chars
                            ),
                            "is_error": bool(message.get("isError")),
                            "unavailable": False,
                            "inert": True,
                        }
                    )
            current = flush(current)
    except sqlite3.Error as exc:
        raise ReaderError(f"failed to read Maka database {database_path}: {exc}") from exc

    if unavailable:
        _add_warning(
            warnings,
            "binary_content_unavailable",
            f"{unavailable} Maka message(s) were chunked, unreadable, or non-JSON and are unavailable.",
        )
    if unknown:
        _add_warning(
            warnings,
            "unknown_records_skipped",
            f"Skipped {unknown} unknown Maka message(s) without interpreting their payloads.",
        )
    if dropped:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {dropped} Maka user/assistant message(s) whose text was a "
            "harness preamble or interruption marker.",
        )

    cwd = header.get("cwd")
    name = header.get("name")
    model = header.get("model")
    result = {
        "tool": "maka",
        "source": "maka",
        "session_id": session_id,
        "path": str(database_path),
        "title": _one_line(name, 200) if isinstance(name, str) else None,
        "cwd": cwd if isinstance(cwd, str) else None,
        "branch": None,
        "created_at": _iso_from_millis(created_at if isinstance(created_at, int) else None),
        "updated_at": _iso_from_millis(
            last_message_at
            if isinstance(last_message_at, int)
            else last_used_at
            if isinstance(last_used_at, int)
            else None
        ),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
        "model": model if isinstance(model, str) else None,
    }
    return _finalize_result(result, index)


def _discover_maka(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for database_path in _maka_database_paths():
        rows: list[Any] | None = None
        try:
            with _open_sqlite_readonly(database_path) as database:
                try:
                    rows = database.execute(
                        "SELECT metadata.session_id, metadata.payload_json, projection.activity_at "
                        "FROM session_catalog_projection projection "
                        "JOIN session_metadata metadata ON metadata.session_id = projection.session_id "
                        "WHERE COALESCE(projection.is_archived, 0) = 0 "
                        "AND COALESCE(json_extract(metadata.payload_json, '$.conversationCopy.state'), '') <> 'preparing' "
                        "AND COALESCE(json_extract(metadata.payload_json, '$.transcriptLedgerVersion'), 1) <> 0 "
                        "AND COALESCE(projection.subagent_parent_session_id, '') = '' "
                        "ORDER BY projection.activity_at DESC, projection.session_id ASC"
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = database.execute(
                        "SELECT session_id, payload_json, "
                        "COALESCE(last_message_at, last_used_at, created_at) "
                        "FROM session_metadata "
                        "WHERE COALESCE(is_archived, 0) = 0 "
                        "AND COALESCE(subagent_parent_session_id, '') = '' "
                        "ORDER BY COALESCE(last_message_at, last_used_at, created_at) DESC"
                    ).fetchall()
        except (ReaderError, sqlite3.Error):
            continue
        if not rows:
            continue
        for session_id, payload_json, activity in rows:
            if not isinstance(session_id, str) or not session_id:
                continue
            header = _maka_header(payload_json)
            stored_cwd = header.get("cwd")
            if cwd is not None:
                if not isinstance(stored_cwd, str) or not _paths_match(stored_cwd, cwd):
                    continue
            updated = activity if isinstance(activity, int) else _mtime_millis(database_path)
            if not _within(updated, within_min):
                continue
            name = header.get("name")
            model = header.get("model")
            sessions.append(
                {
                    "tool": "maka",
                    "source": "maka",
                    "session_id": session_id,
                    "path": str(database_path),
                    "title": _one_line(name, 200) if isinstance(name, str) else None,
                    "cwd": stored_cwd if isinstance(stored_cwd, str) else cwd,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                    "model": model if isinstance(model, str) else None,
                }
            )
    return sessions


def _find_maka_id(session_id: str, cwd: str) -> dict[str, Any] | None:
    for database_path in _maka_database_paths():
        try:
            with _open_sqlite_readonly(database_path) as database:
                row = database.execute(
                    "SELECT payload_json, COALESCE(last_message_at, last_used_at, created_at) "
                    "FROM session_metadata WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            if row is None:
                continue
            header = _maka_header(row[0])
            updated = row[1] if isinstance(row[1], int) else _mtime_millis(database_path)
            name = header.get("name")
            return {
                "tool": "maka",
                "source": "maka",
                "session_id": session_id,
                "path": str(database_path),
                "title": _one_line(name, 200) if isinstance(name, str) else None,
                "cwd": header.get("cwd") if isinstance(header.get("cwd"), str) else cwd,
                "updated_at_ms": updated,
            }
        except (ReaderError, sqlite3.Error):
            continue
    return None
