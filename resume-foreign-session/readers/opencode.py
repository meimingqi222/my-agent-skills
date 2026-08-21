"""OpenCode, zcode, and OpenCode v2 session reader and discovery."""

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


# ---------------------------------------------------------------------------
# OpenCode v2 (session_v2 + session_message schema)
# ---------------------------------------------------------------------------

def _opencode2_model_id(model: Any) -> str | None:
    """从 session_v2 的 model JSON 字符串中提取模型 ID。"""
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


def _opencode2_tool_output(state: dict[str, Any], max_chars: int) -> str | None:
    """从 v2 tool part 的 state 中提取输出文本。

    v2 的输出在 ``state.content``（一个 ``[{type, text}]`` 数组）或
    ``state.error`` 中，而不是 v1 的 ``state.output`` 字符串。
    """
    content = state.get("content")
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
        if texts:
            return _one_line("\n".join(texts), max_chars)
    error = state.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str):
            return _one_line(message, max_chars)
    elif isinstance(error, str) and error.strip():
        return _one_line(error, max_chars)
    return None


def read_opencode2_session(
    database_path: Path | str, session_id: str, max_tool_chars: int = 300
) -> dict[str, Any]:
    """读取一个 opencode v2 会话（session_v2 + session_message schema）。"""
    database_path = Path(database_path).expanduser()
    warnings: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    index = WorkIndex()
    prior_context: dict[str, Any] | None = None
    try:
        with _open_sqlite_readonly(database_path) as database:
            v2_columns = _table_columns(database, "session_v2")
            if not v2_columns:
                raise ReaderError(
                    f"no session_v2 table in {database_path}; "
                    "this database does not contain opencode v2 sessions"
                )
            optional = [
                name
                for name in ("agent", "model", "parent_id", "version")
                if name in v2_columns
            ]
            row = database.execute(
                "SELECT directory, title, time_created, time_updated"
                + "".join(f", {name}" for name in optional)
                + " FROM session_v2 WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ReaderError(
                    f"no opencode v2 session {session_id} in {database_path}"
                )
            directory, title, created_at, updated_at = row[:4]
            extra = dict(zip(optional, row[4:]))
            agent = extra.get("agent")
            model = extra.get("model")
            parent_id = extra.get("parent_id")
            message_agent: str | None = None
            message_model: str | None = None

            # 读取 session_message 表
            msg_columns = _table_columns(database, "session_message")
            if not msg_columns:
                raise ReaderError(
                    f"no session_message table in {database_path}"
                )
            rows = database.execute(
                "SELECT id, type, data FROM session_message "
                "WHERE session_id = ? ORDER BY seq, time_created",
                (session_id,),
            ).fetchall()
            skipped = 0
            synthetic = 0
            compaction_count = 0
            for message_id, msg_type, raw in rows:
                if not isinstance(msg_type, str):
                    continue
                # compaction 消息：跳过，但记录
                if msg_type == "compaction":
                    compaction_count += 1
                    continue
                # synthetic 消息：跳过（harness 注入文本）
                if msg_type == "synthetic":
                    synthetic += 1
                    continue
                if msg_type not in {"user", "assistant"}:
                    skipped += 1
                    continue
                try:
                    message = json.loads(raw) if isinstance(raw, str) else None
                except (json.JSONDecodeError, TypeError):
                    skipped += 1
                    continue
                if not isinstance(message, dict):
                    continue

                if msg_type == "assistant":
                    named_agent = message.get("agent")
                    if isinstance(named_agent, str) and named_agent:
                        message_agent = named_agent
                    named_model = message.get("model")
                    if isinstance(named_model, dict):
                        model_id = named_model.get("id")
                        if isinstance(model_id, str) and model_id:
                            message_model = model_id
                    elif isinstance(named_model, str) and named_model:
                        message_model = named_model

                texts: list[str] = []
                calls: list[dict[str, Any]] = []
                results: list[dict[str, Any]] = []

                # v2 user 消息：text 直接在顶层
                if msg_type == "user":
                    text = message.get("text")
                    if isinstance(text, str) and text.strip():
                        texts.append(_safe_text(text))
                else:
                    # v2 assistant 消息：content 数组包含 parts
                    content = message.get("content")
                    if isinstance(content, list):
                        for part in content:
                            if not isinstance(part, dict):
                                continue
                            part_type = part.get("type")
                            if part_type == "text":
                                text = part.get("text")
                                if isinstance(text, str) and text.strip():
                                    texts.append(_safe_text(text))
                            elif part_type == "tool":
                                state = part.get("state")
                                if not isinstance(state, dict):
                                    state = {}
                                call_id = part.get("id")
                                tool_name = part.get("name") or part.get("tool") or "tool"
                                index.record(tool_name, state.get("input", {}))
                                calls.append(
                                    {
                                        "id": call_id,
                                        "name": _safe_text(tool_name),
                                        "input": _json_preview(
                                            state.get("input", {}), max_tool_chars
                                        ),
                                        "inert": True,
                                    }
                                )
                                output = _opencode2_tool_output(state, max_tool_chars)
                                if output is not None:
                                    status = str(state.get("status") or "")
                                    results.append(
                                        {
                                            "tool_use_id": call_id,
                                            "content": output,
                                            "is_error": status in {"error", "rejected"}
                                            or "error" in state,
                                            "unavailable": False,
                                            "inert": True,
                                        }
                                    )
                            elif part_type == "reasoning":
                                skipped += 1

                text = "\n".join(part for part in texts if part.strip())
                if text or calls or results:
                    turns.append(
                        _turn(msg_type, text=text, tool_calls=calls, tool_results=results)
                    )

            if compaction_count:
                _add_warning(
                    warnings,
                    "history_compacted",
                    f"Skipped {compaction_count} compaction marker(s); "
                    "earlier turns may have been summarized by the harness.",
                )
            if skipped:
                _add_warning(
                    warnings,
                    "unsafe_records_skipped",
                    f"Skipped {skipped} opencode v2 reasoning/unknown part(s).",
                )
            if synthetic:
                _add_warning(
                    warnings,
                    "harness_text_dropped",
                    f"Dropped {synthetic} harness-written opencode v2 synthetic message(s).",
                )

            # 读取 todo 表（与 v1 共用）
            try:
                todo_rows = database.execute(
                    "SELECT content, status, priority FROM todo "
                    "WHERE session_id = ? ORDER BY position, time_created",
                    (session_id,),
                ).fetchall()
                for todo_content, todo_status, _priority in todo_rows:
                    if isinstance(todo_content, str) and todo_content.strip():
                        index.record(
                            "todowrite",
                            {
                                "todos": [
                                    {
                                        "content": todo_content,
                                        "status": todo_status or "pending",
                                    }
                                ]
                            },
                        )
            except sqlite3.Error:
                pass

    except sqlite3.Error as exc:
        raise ReaderError(
            f"failed to read opencode v2 database {database_path}: {exc}"
        ) from exc

    if not isinstance(agent, str) or not agent:
        agent = message_agent
    if model is None:
        model = message_model

    result = {
        "tool": "opencode2",
        "source": "opencode-v2",
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
        "model": _opencode2_model_id(model),
        "agent": agent if isinstance(agent, str) else None,
        "prior_context": prior_context,
    }
    if parent_id:
        _add_warning(
            warnings,
            "subagent_session",
            f"This opencode v2 session has parent_id {parent_id}; "
            "it may be a subagent transcript rather than user-driven work.",
        )
    return _finalize_result(result, index)


def _discover_opencode2(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    """发现 opencode v2 会话（从 session_v2 表）。"""
    sessions: list[dict[str, Any]] = []
    for database_path in _opencode_db_paths():
        try:
            with _open_sqlite_readonly(database_path) as database:
                v2_columns = _table_columns(database, "session_v2")
                if not v2_columns:
                    continue
                optional = [
                    name
                    for name in ("agent", "model", "parent_id")
                    if name in v2_columns
                ]
                # 过滤已归档的会话和子代理会话
                where = "COALESCE(time_archived, 0) = 0"
                if "parent_id" in v2_columns:
                    where += " AND parent_id IS NULL"
                rows = database.execute(
                    "SELECT id, directory, title, time_created, time_updated"
                    + "".join(f", {name}" for name in optional)
                    + f" FROM session_v2 WHERE {where} "
                    "ORDER BY time_updated DESC"
                ).fetchall()
        except (ReaderError, sqlite3.Error):
            continue
        for row in rows:
            session_id, directory, title, created_at, updated_at = row[:5]
            extra = dict(zip(optional, row[5:]))
            if not isinstance(session_id, str):
                continue
            if cwd is not None:
                if not isinstance(directory, str) or not _paths_match(directory, cwd):
                    continue
            updated = _timestamp_to_millis(updated_at)
            if updated is not None and not _within(updated, within_min):
                continue
            sessions.append(
                {
                    "tool": "opencode2",
                    "source": "opencode-v2",
                    "session_id": session_id,
                    "path": str(database_path),
                    "title": _one_line(title, 200) if isinstance(title, str) else None,
                    "cwd": directory if isinstance(directory, str) else None,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                    "model": _opencode2_model_id(extra.get("model")),
                }
            )
    return sessions


def _find_opencode2_id(session_id: str, cwd: str) -> dict[str, Any] | None:
    """按 native session ID 查找 opencode v2 会话。"""
    for database_path in _opencode_db_paths():
        try:
            with _open_sqlite_readonly(database_path) as database:
                v2_columns = _table_columns(database, "session_v2")
                if not v2_columns:
                    continue
                row = database.execute(
                    "SELECT directory, title, time_updated FROM session_v2 WHERE id = ?",
                    (session_id,),
                ).fetchone()
            if row is None:
                continue
            directory, title, updated_at = row
            updated = _timestamp_to_millis(updated_at)
            return {
                "tool": "opencode2",
                "source": "opencode-v2",
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
