"""OpenCode and zcode session reader and discovery."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from reader_core import (
    ReaderError,
    WorkIndex,
    _add_warning,
    _finalize_result,
    _iso_from_millis,
    _json_preview,
    _one_line,
    _open_sqlite_readonly,
    _paths_match,
    _safe_text,
    _table_columns,
    _timestamp_to_millis,
    _turn,
    _within,
)


def _opencode_data_dir() -> Path:
    """Return the OpenCode data directory holding the session SQLite store."""
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "opencode"
    local_share = Path.home() / ".local" / "share" / "opencode"
    if local_share.is_dir():
        return local_share
    macos = Path.home() / "Library" / "Application Support" / "opencode"
    return macos if macos.is_dir() else local_share


def _opencode_db_paths() -> list[Path]:
    """Candidate OpenCode session databases (most recent format first)."""
    root = _opencode_data_dir()
    output: list[Path] = []
    for name in ("opencode.db", "opencode-next.db"):
        path = root / name
        if path.is_file() and not path.is_symlink():
            output.append(path)
    return output


def _zcode_config_dir() -> Path:
    """Return the zcode home directory; its CLI store lives under `cli/`."""
    configured = os.environ.get("ZCODE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".zcode"


def _zcode_db_paths() -> list[Path]:
    """Candidate zcode session databases."""
    path = _zcode_config_dir() / "cli" / "db" / "db.sqlite"
    return [path] if path.is_file() and not path.is_symlink() else []


def _opencode_model_id(model: Any) -> str | None:
    if not isinstance(model, str):
        return None
    try:
        value = json.loads(model)
    except json.JSONDecodeError:
        return model
    if isinstance(value, dict):
        model_id = value.get("id")
        if isinstance(model_id, str):
            return model_id
    return model


def read_opencode_session(
    database_path: Path | str, session_id: str, max_tool_chars: int = 300
) -> dict[str, Any]:
    return _read_opencode_schema_session(
        database_path,
        session_id,
        max_tool_chars,
        tool="opencode",
        source="opencode",
        label="OpenCode",
    )


def read_zcode_session(
    database_path: Path | str, session_id: str, max_tool_chars: int = 300
) -> dict[str, Any]:
    return _read_opencode_schema_session(
        database_path,
        session_id,
        max_tool_chars,
        tool="zcode",
        source="zcode-cli",
        label="zcode",
    )


def _read_opencode_schema_session(
    database_path: Path | str,
    session_id: str,
    max_tool_chars: int = 300,
    *,
    tool: str,
    source: str,
    label: str,
) -> dict[str, Any]:
    database_path = Path(database_path).expanduser()
    warnings: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    index = WorkIndex()
    try:
        with _open_sqlite_readonly(database_path) as database:
            columns = _table_columns(database, "session")
            optional = [name for name in ("agent", "model") if name in columns]
            row = database.execute(
                "SELECT directory, title, time_created, time_updated"
                + "".join(f", {name}" for name in optional)
                + " FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ReaderError(f"no {label} session {session_id} in {database_path}")
            directory, title, created_at, updated_at = row[:4]
            extra = dict(zip(optional, row[4:]))
            agent = extra.get("agent")
            model = extra.get("model")
            message_agent: str | None = None
            message_model: str | None = None
            parts_by_message: dict[str, list[dict[str, Any]]] = {}
            part_rows = database.execute(
                "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
            for message_id, raw in part_rows:
                if not isinstance(message_id, str):
                    continue
                try:
                    value = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(value, dict):
                    parts_by_message.setdefault(message_id, []).append(value)
            rows = database.execute(
                "SELECT id, data FROM message WHERE session_id = ? ORDER BY time_created",
                (session_id,),
            ).fetchall()
            skipped = 0
            synthetic = 0
            for message_id, raw in rows:
                if not isinstance(message_id, str):
                    continue
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    skipped += 1
                    continue
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in {"user", "assistant"}:
                    continue
                if role == "assistant":
                    named_agent = message.get("agent")
                    if isinstance(named_agent, str) and named_agent:
                        message_agent = named_agent
                    named_model = message.get("modelID")
                    if isinstance(named_model, str) and named_model:
                        message_model = named_model
                texts: list[str] = []
                calls: list[dict[str, Any]] = []
                results: list[dict[str, Any]] = []
                for part in parts_by_message.get(message_id, []):
                    part_type = part.get("type")
                    if part_type == "text":
                        if role == "user" and part.get("synthetic"):
                            synthetic += 1
                            continue
                        text = part.get("text")
                        if isinstance(text, str) and text.strip():
                            texts.append(_safe_text(text))
                    elif part_type == "tool":
                        state = part.get("state")
                        if not isinstance(state, dict):
                            state = {}
                        call_id = part.get("callID") or part.get("id")
                        index.record(part.get("tool"), state.get("input", {}))
                        calls.append(
                            {
                                "id": call_id,
                                "name": _safe_text(part.get("tool") or "tool"),
                                "input": _json_preview(state.get("input", {}), max_tool_chars),
                                "inert": True,
                            }
                        )
                        output = state.get("output")
                        if output is not None:
                            status = str(state.get("status") or "")
                            results.append(
                                {
                                    "tool_use_id": call_id,
                                    "content": _one_line(str(output), max_tool_chars),
                                    "is_error": status in {"error", "rejected"},
                                    "unavailable": False,
                                    "inert": True,
                                }
                            )
                    elif part_type == "reasoning":
                        skipped += 1
                text = "\n".join(part for part in texts if part.strip())
                if text or calls or results:
                    turns.append(_turn(role, text=text, tool_calls=calls, tool_results=results))
            if skipped:
                _add_warning(
                    warnings,
                    "unsafe_records_skipped",
                    f"Skipped {skipped} {label} reasoning/unknown part(s).",
                )
            if synthetic:
                _add_warning(
                    warnings,
                    "harness_text_dropped",
                    f"Dropped {synthetic} harness-written {label} text part(s) that "
                    "wore the user role.",
                )
    except sqlite3.Error as exc:
        raise ReaderError(f"failed to read {label} database {database_path}: {exc}") from exc

    if not isinstance(agent, str) or not agent:
        agent = message_agent
    if model is None:
        model = message_model

    result = {
        "tool": tool,
        "source": source,
        "session_id": session_id,
        "path": str(database_path),
        "title": _one_line(title, 200) if isinstance(title, str) else None,
        "cwd": directory if isinstance(directory, str) else None,
        "branch": None,
        "created_at": _iso_from_millis(_timestamp_to_millis(created_at)),
        "updated_at": _iso_from_millis(_timestamp_to_millis(updated_at)),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
        "model": _opencode_model_id(model),
    }
    result["agent"] = agent if isinstance(agent, str) else None
    return _finalize_result(result, index)


def _discover_opencode(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    return _discover_opencode_schema(
        _opencode_db_paths(), "opencode", "opencode", cwd, within_min
    )


def _discover_zcode(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    return _discover_opencode_schema(
        _zcode_db_paths(), "zcode", "zcode-cli", cwd, within_min
    )


def _discover_opencode_schema(
    db_paths: list[Path], tool: str, source: str, cwd: str | None, within_min: int
) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for database_path in db_paths:
        try:
            with _open_sqlite_readonly(database_path) as database:
                columns = _table_columns(database, "session")
                optional = [
                    name for name in ("agent", "model", "task_type") if name in columns
                ]
                rows = database.execute(
                    "SELECT id, directory, title, time_created, time_updated"
                    + "".join(f", {name}" for name in optional)
                    + " FROM session WHERE COALESCE(time_archived, 0) = 0 "
                    "ORDER BY time_updated DESC"
                ).fetchall()
        except (ReaderError, sqlite3.Error):
            continue
        for row in rows:
            session_id, directory, title, created_at, updated_at = row[:5]
            extra = dict(zip(optional, row[5:]))
            if not isinstance(session_id, str):
                continue
            task_type = extra.get("task_type")
            if isinstance(task_type, str) and task_type.startswith("subagent"):
                continue
            if cwd is not None:
                if not isinstance(directory, str) or not _paths_match(directory, cwd):
                    continue
            updated = _timestamp_to_millis(updated_at)
            if updated is not None and not _within(updated, within_min):
                continue
            sessions.append(
                {
                    "tool": tool,
                    "source": source,
                    "session_id": session_id,
                    "path": str(database_path),
                    "title": _one_line(title, 200) if isinstance(title, str) else None,
                    "cwd": directory if isinstance(directory, str) else None,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                    "model": _opencode_model_id(extra.get("model")),
                }
            )
    return sessions


def _find_opencode_id(session_id: str, cwd: str) -> dict[str, Any] | None:
    return _find_opencode_schema_id(
        _opencode_db_paths(), "opencode", "opencode", session_id, cwd
    )


def _find_zcode_id(session_id: str, cwd: str) -> dict[str, Any] | None:
    return _find_opencode_schema_id(
        _zcode_db_paths(), "zcode", "zcode-cli", session_id, cwd
    )


def _find_opencode_schema_id(
    db_paths: list[Path], tool: str, source: str, session_id: str, cwd: str
) -> dict[str, Any] | None:
    for database_path in db_paths:
        try:
            with _open_sqlite_readonly(database_path) as database:
                row = database.execute(
                    "SELECT directory, title, time_updated FROM session WHERE id = ?",
                    (session_id,),
                ).fetchone()
            if row is None:
                continue
            directory, title, updated_at = row
            updated = _timestamp_to_millis(updated_at)
            return {
                "tool": tool,
                "source": source,
                "session_id": session_id,
                "path": str(database_path),
                "title": _one_line(title, 200) if isinstance(title, str) else None,
                "cwd": directory if isinstance(directory, str) else cwd,
                "updated_at_ms": updated,
                "updated_at": _iso_from_millis(updated),
                "source_repo_root_path": None,
            }
        except (ReaderError, sqlite3.Error):
            continue
    return None
