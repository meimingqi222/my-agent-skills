"""Devin CLI and Devin Next session readers and discovery."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from reader_core import (
    ReaderError,
    WorkIndex,
    _add_warning,
    _finalize_result,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _open_sqlite_readonly,
    _paths_match,
    _safe_text,
    _timestamp_to_millis,
    _turn,
    _within,
)


def _devin_data_dirs() -> list[Path]:
    """Return Devin CLI data dirs (cli and cli-next), most specific first."""
    base = os.environ.get("DEVIN_CONFIG_DIR")
    if base:
        return [Path(base).expanduser() / name for name in ("cli-next", "cli")]

    candidates: list[Path] = []
    home = Path.home()
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(Path(local_appdata).expanduser() / "devin")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata).expanduser() / "devin")
        candidates.append(home / ".devin")
    elif sys.platform == "darwin":
        candidates.append(home / ".local" / "share" / "devin")
        candidates.append(home / ".devin")
        candidates.append(home / "Library" / "Application Support" / "devin")
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME")
        if xdg_data:
            candidates.append(Path(xdg_data).expanduser() / "devin")
        candidates.append(home / ".local" / "share" / "devin")
        candidates.append(home / ".devin")

    roots: list[Path] = []
    seen: set[Path] = set()
    for root in candidates:
        if root in seen:
            continue
        seen.add(root)
        if root.is_dir():
            roots.append(root)
    return [root / name for root in roots for name in ("cli-next", "cli")]


def _common_project_root(paths: list[str]) -> str | None:
    cleaned: list[list[str]] = []
    for path in paths:
        path = re.sub(r"[\"'`)\]}]+$", "", path.strip())
        normalized = os.path.normpath(path)
        if not normalized or not os.path.isabs(normalized):
            continue
        parts = normalized.split(os.sep)
        if parts and ":" in parts[0]:
            parts[0] = parts[0].rstrip(":")
        cleaned.append(parts)
    if not cleaned:
        return None
    prefix = cleaned[0]
    for parts in cleaned[1:]:
        max_len = min(len(prefix), len(parts))
        i = 0
        while i < max_len and prefix[i].lower() == parts[i].lower():
            i += 1
        prefix = prefix[:i]
    if not prefix:
        return None
    root = os.sep.join(prefix)
    if not root.endswith(os.sep):
        root += os.sep
    return root


def read_devin_cli_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    warnings: list[dict[str, str]] = []
    try:
        with session_path.open("r", encoding="utf-8") as handle:
            transcript = json.load(handle)
    except OSError as exc:
        raise ReaderError(f"failed to read Devin transcript {session_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReaderError(f"failed to parse Devin transcript {session_path}: {exc}") from exc
    if not isinstance(transcript, dict):
        raise ReaderError(f"malformed Devin transcript {session_path}: expected an object")

    steps = transcript.get("steps")
    if not isinstance(steps, list):
        raise ReaderError(f"malformed Devin transcript {session_path}: missing steps array")

    cwd = None
    path_hints: list[str] = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        for tool_call in step.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            arguments = tool_call.get("arguments")
            if not isinstance(arguments, dict):
                continue
            for value in arguments.values():
                if isinstance(value, str):
                    matches = re.findall(
                        r"(?i)[a-z]:[\\/][^\s\"'\n`|<>]+", value
                    )
                    path_hints.extend(matches)
    if path_hints:
        cwd = _common_project_root(path_hints)

    model_name = None
    agent = transcript.get("agent")
    if isinstance(agent, dict) and isinstance(agent.get("model_name"), str):
        model_name = agent["model_name"]

    turns: list[dict[str, Any]] = []
    unsafe_count = 0
    index = WorkIndex()
    for step in steps:
        if not isinstance(step, dict):
            unsafe_count += 1
            continue
        source = step.get("source")
        if source == "system":
            continue
        if source == "user":
            message = step.get("message")
            if isinstance(message, str) and message:
                turns.append(_turn("user", text=message))
        elif source == "agent":
            calls: list[dict[str, Any]] = []
            for tool_call in step.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                index.record(tool_call.get("function_name"), tool_call.get("arguments"))
                calls.append(
                    {
                        "id": tool_call.get("tool_call_id"),
                        "name": _safe_text(tool_call.get("function_name") or "tool"),
                        "input": _json_preview(tool_call.get("arguments"), max_tool_chars),
                        "inert": True,
                    }
                )
            results: list[dict[str, Any]] = []
            observation = step.get("observation")
            if isinstance(observation, dict):
                for item in observation.get("results") or []:
                    if not isinstance(item, dict):
                        continue
                    content_value = item.get("content")
                    if isinstance(content_value, dict):
                        content_value = json.dumps(content_value, ensure_ascii=False)
                    results.append(
                        {
                            "tool_use_id": item.get("source_call_id"),
                            "content": _one_line(str(content_value or ""), max_tool_chars),
                            "is_error": False,
                            "unavailable": False,
                            "inert": True,
                        }
                    )
            message = step.get("message")
            text = message if isinstance(message, str) else ""
            reasoning = step.get("reasoning_content")
            if reasoning:
                unsafe_count += 1
            if text or calls or results:
                turns.append(_turn("assistant", text=text, tool_calls=calls, tool_results=results))
    if unsafe_count:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {unsafe_count} Devin reasoning/system/unknown step(s).",
        )

    timestamps = [
        step["timestamp"]
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("timestamp"), str)
    ]
    result = {
        "tool": "devin",
        "source": "devin-cli",
        "session_id": transcript.get("session_id") or session_path.stem,
        "path": str(session_path),
        "title": None,
        "cwd": cwd,
        "branch": None,
        "created_at": timestamps[0] if timestamps else None,
        "updated_at": timestamps[-1] if timestamps else _iso_from_millis(_mtime_millis(session_path)),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
    }
    result["model"] = model_name
    return _finalize_result(result, index)


def read_devin_next_session(
    database_path: Path | str,
    session_id: str,
    max_tool_chars: int = 300,
) -> dict[str, Any]:
    database_path = Path(database_path).expanduser()
    warnings: list[dict[str, str]] = []
    index = WorkIndex()
    try:
        with _open_sqlite_readonly(database_path) as database:
            row = database.execute(
                "SELECT working_directory, backend_type, model, agent_mode, created_at, "
                "last_activity_at, title, main_chain_id FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ReaderError(f"no Devin session {session_id} in {database_path}")
            (
                working_directory,
                backend_type,
                model,
                agent_mode,
                created_at,
                last_activity_at,
                title,
                main_chain_id,
            ) = row
            turns: list[dict[str, Any]] = []
            main_chain_end = main_chain_id if isinstance(main_chain_id, int) else None
            if main_chain_end is not None:
                nodes_by_id: dict[int, tuple[int | None, str]] = {}
                rows = database.execute(
                    "SELECT node_id, parent_node_id, chat_message FROM message_nodes "
                    "WHERE session_id = ? AND node_id <= ?",
                    (session_id, main_chain_end),
                ).fetchall()
                for node_id, parent_node_id, chat_message in rows:
                    nodes_by_id[node_id] = (parent_node_id, chat_message)
                chain_ids: list[int] = []
                cursor_id: int | None = main_chain_end
                seen_ids: set[int] = set()
                while cursor_id is not None and cursor_id not in seen_ids:
                    seen_ids.add(cursor_id)
                    chain_ids.append(cursor_id)
                    node = nodes_by_id.get(cursor_id)
                    if node is None:
                        break
                    parent = node[0]
                    cursor_id = parent if parent is not None else None
                chain_ids.reverse()
                for node_id in chain_ids:
                    _, chat_message = nodes_by_id[node_id]
                    try:
                        message = json.loads(chat_message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(message, dict):
                        continue
                    role = message.get("role")
                    if role not in {"user", "assistant", "tool"}:
                        continue
                    content = message.get("content")
                    text = ""
                    tool_calls: list[dict[str, Any]] = []
                    tool_results: list[dict[str, Any]] = []
                    if isinstance(content, str):
                        if role == "tool":
                            text = _one_line(content, max_tool_chars)
                        else:
                            text = content
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type == "text" and isinstance(block.get("text"), str):
                                text += block["text"]
                            elif block_type == "tool_use":
                                index.record(block.get("name"), block.get("input"))
                                tool_calls.append(
                                    {
                                        "id": block.get("id"),
                                        "name": _safe_text(block.get("name") or "tool"),
                                        "input": _json_preview(block.get("input"), max_tool_chars),
                                        "inert": True,
                                    }
                                )
                            elif block_type == "tool_result":
                                tool_results.append(
                                    {
                                        "tool_use_id": block.get("tool_use_id") or block.get("id"),
                                        "content": _one_line(
                                            str(block.get("content") or ""), max_tool_chars
                                        ),
                                        "is_error": False,
                                        "unavailable": False,
                                        "inert": True,
                                    }
                                )
                    if text or tool_calls or tool_results:
                        turns.append(
                            _turn(role, text=text, tool_calls=tool_calls, tool_results=tool_results)
                        )
    except sqlite3.Error as exc:
        raise ReaderError(f"failed to read Devin database {database_path}: {exc}") from exc

    branch = None
    result = {
        "tool": "devin",
        "source": "devin-next",
        "session_id": session_id,
        "path": str(database_path),
        "title": _one_line(title, 200) if isinstance(title, str) else None,
        "cwd": working_directory if isinstance(working_directory, str) else None,
        "branch": branch,
        "created_at": _iso_from_millis(_timestamp_to_millis(created_at)),
        "updated_at": _iso_from_millis(_timestamp_to_millis(last_activity_at)),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
    }
    result["model"] = model if isinstance(model, str) else None
    return _finalize_result(result, index)


def _discover_devin_cli(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for data_dir in _devin_data_dirs():
        transcript_dir = data_dir / "transcripts"
        if not transcript_dir.is_dir() or transcript_dir.is_symlink():
            continue
        try:
            paths = sorted(transcript_dir.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for path in paths:
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                continue
            updated = _mtime_millis(path)
            if not _within(updated, within_min):
                continue
            try:
                result = read_devin_cli_session(path, max_tool_chars=80)
            except ReaderError:
                continue
            session_cwd = result.get("cwd")
            if cwd is not None:
                if not session_cwd:
                    continue
                if not _paths_match(session_cwd, cwd):
                    continue
            sessions.append(
                {
                    "tool": "devin",
                    "source": "devin-cli",
                    "session_id": result.get("session_id") or path.stem,
                    "path": str(path),
                    "title": result.get("title") or "(untitled)",
                    "cwd": session_cwd or cwd,
                    "branch": result.get("branch"),
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                }
            )
    return sessions


def _discover_devin_next(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for data_dir in _devin_data_dirs():
        database_path = data_dir / "sessions.db"
        if not database_path.is_file() or database_path.is_symlink():
            continue
        try:
            with _open_sqlite_readonly(database_path) as database:
                rows = database.execute(
                    "SELECT id, working_directory, backend_type, model, created_at, "
                    "last_activity_at, title FROM sessions "
                    "WHERE COALESCE(hidden, 0) = 0 ORDER BY last_activity_at DESC"
                ).fetchall()
        except (ReaderError, sqlite3.Error):
            continue
        for (
            session_id,
            working_directory,
            backend_type,
            model,
            created_at,
            last_activity_at,
            title,
        ) in rows:
            if not isinstance(working_directory, str):
                continue
            if cwd is not None and not _paths_match(working_directory, cwd):
                continue
            updated = _timestamp_to_millis(last_activity_at)
            if updated is not None and not _within(updated, within_min):
                continue
            sessions.append(
                {
                    "tool": "devin",
                    "source": "devin-next",
                    "session_id": session_id,
                    "path": str(database_path),
                    "title": _one_line(title, 200) if isinstance(title, str) else None,
                    "cwd": working_directory,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                    "model": model if isinstance(model, str) else None,
                }
            )
    return sessions


def _find_devin_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    for data_dir in _devin_data_dirs():
        transcript = data_dir / "transcripts" / f"{session_id}.json"
        if transcript.is_file():
            if candidate_from_path_fn is not None:
                candidate = candidate_from_path_fn("devin", str(transcript), cwd)
                if candidate is not None:
                    return candidate
            else:
                try:
                    meta = read_devin_cli_session(transcript, max_tool_chars=80)
                    return {
                        "tool": "devin",
                        "source": "devin-cli",
                        "session_id": meta.get("session_id") or session_id,
                        "path": str(transcript),
                        "title": meta.get("title") or "(untitled)",
                        "cwd": meta.get("cwd") or cwd,
                        "updated_at_ms": _mtime_millis(transcript),
                    }
                except ReaderError:
                    pass
        database_path = data_dir / "sessions.db"
        if database_path.is_file():
            try:
                with _open_sqlite_readonly(database_path) as database:
                    row = database.execute(
                        "SELECT working_directory, title, last_activity_at FROM sessions "
                        "WHERE id = ?",
                        (session_id,),
                    ).fetchone()
                if row:
                    return {
                        "tool": "devin",
                        "source": "devin-next",
                        "session_id": session_id,
                        "path": str(database_path),
                        "title": (
                            _one_line(row[1], 200) if isinstance(row[1], str) else None
                        ),
                        "cwd": row[0] if isinstance(row[0], str) else cwd,
                        "updated_at_ms": _timestamp_to_millis(row[2]),
                    }
            except (ReaderError, sqlite3.Error):
                continue
    return None
