"""Codex session reader and discovery."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Iterable

from reader_core import (
    CODEX_IGNORED_TOP_LEVEL,
    CODEX_ROLLOUT_RE,
    CODEX_SAFE_TOP_LEVEL,
    UUID_RE,
    ReaderError,
    WorkIndex,
    _add_warning,
    _blocks,
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


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _read_codex_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    if path.name.endswith(".jsonl.zst"):
        executable = shutil.which("zstd")
        if executable is None:
            raise ReaderError(
                f"zstd is required to read compressed Codex rollout {path}; install zstd "
                "and ensure it is on PATH."
            )
        try:
            completed = subprocess.run(
                [executable, "-dc", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as exc:
            raise ReaderError(f"failed to run zstd for {path}: {exc}") from exc
        if completed.returncode != 0:
            detail = _one_line(completed.stderr.decode("utf-8", errors="replace"), 300)
            raise ReaderError(f"zstd failed to decompress {path}: {detail or 'unknown error'}")
        text = completed.stdout.decode("utf-8", errors="replace")
        records: list[dict[str, Any]] = []
        malformed = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                malformed += 1
        return records, malformed
    return _read_plain_jsonl(path)


def _codex_message_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in _blocks(item.get("content")):
        block_type = block.get("type")
        if block_type in {"reasoning", "thinking", "encrypted_content"}:
            continue
        if block_type in {"input_text", "output_text", "text"}:
            text = block.get("text")
            if isinstance(text, str) and text.strip() and not _is_generated_meta_text(text):
                parts.append(_safe_text(text))
    return "\n".join(parts)


def _render_codex_item(
    item: Any, max_tool_chars: int, index: WorkIndex | None = None
) -> tuple[dict[str, Any] | None, bool]:
    if not isinstance(item, dict):
        return None, True
    item_type = item.get("type")
    if index is not None and item_type in {
        "function_call",
        "local_shell_call",
        "custom_tool_call",
    }:
        index.record(
            "local_shell" if item_type == "local_shell_call" else item.get("name"),
            item.get("arguments")
            if item_type == "function_call"
            else item.get("action")
            if item_type == "local_shell_call"
            else item.get("input"),
        )
    if item_type == "message":
        role = item.get("role")
        if role not in {"user", "assistant"}:
            return None, role in {"system", "developer"}
        text = _codex_message_text(item)
        if not text:
            return None, True
        return _turn(role, text=text), False
    if item_type == "function_call":
        return (
            _turn(
                "assistant",
                tool_calls=[
                    {
                        "id": item.get("call_id") or item.get("id"),
                        "name": _safe_text(item.get("name") or "function"),
                        "input": _json_preview(item.get("arguments", ""), max_tool_chars),
                        "inert": True,
                    }
                ],
            ),
            False,
        )
    if item_type == "local_shell_call":
        return (
            _turn(
                "assistant",
                tool_calls=[
                    {
                        "id": item.get("call_id") or item.get("id"),
                        "name": "local_shell",
                        "input": _json_preview(item.get("action", {}), max_tool_chars),
                        "inert": True,
                    }
                ],
            ),
            False,
        )
    if item_type == "custom_tool_call":
        return (
            _turn(
                "assistant",
                tool_calls=[
                    {
                        "id": item.get("call_id") or item.get("id"),
                        "name": _safe_text(item.get("name") or "custom_tool"),
                        "input": _json_preview(item.get("input", ""), max_tool_chars),
                        "inert": True,
                    }
                ],
            ),
            False,
        )
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        output = item.get("output")
        if isinstance(output, dict):
            output = output.get("body") or output.get("text") or output
        return (
            _turn(
                "tool",
                tool_results=[
                    {
                        "tool_use_id": item.get("call_id") or item.get("id"),
                        "content": _json_preview(output, max_tool_chars),
                        "is_error": item.get("success") is False,
                        "unavailable": False,
                        "inert": True,
                    }
                ],
            ),
            False,
        )
    if item_type in {
        "reasoning",
        "world_state",
        "environment_context",
        "user_instructions",
        "computer_initialize_state",
    }:
        return None, True
    return None, True


def _drop_last_user_turns(turns: list[dict[str, Any]], number: int) -> None:
    if number <= 0:
        return
    positions = [index for index, turn in enumerate(turns) if turn["role"] == "user"]
    cut = positions[max(0, len(positions) - number)] if positions else 0
    del turns[cut:]


def _codex_id_from_path(path: Path) -> str:
    match = CODEX_ROLLOUT_RE.match(path.name)
    return match.group(1) if match else path.stem.removesuffix(".jsonl")


def read_codex_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    records, malformed = _read_codex_jsonl(session_path)
    warnings: list[dict[str, str]] = []
    if malformed:
        _add_warning(
            warnings,
            "malformed_records_skipped",
            f"Skipped {malformed} malformed Codex rollout record(s).",
        )
    first_meta = next(
        (
            record.get("payload")
            for record in records
            if record.get("type") == "session_meta"
            and isinstance(record.get("payload"), dict)
        ),
        {},
    )
    base_items: list[Any] = []
    start_index = 0
    for position, record in enumerate(records):
        if record.get("type") != "compacted":
            continue
        payload = record.get("payload")
        replacement = payload.get("replacement_history") if isinstance(payload, dict) else None
        if isinstance(replacement, list):
            base_items = replacement
            start_index = position + 1
    turns: list[dict[str, Any]] = []
    unsafe_count = 0
    index = WorkIndex()
    for item in base_items:
        turn, unsafe = _render_codex_item(item, max_tool_chars, index)
        unsafe_count += int(unsafe)
        if turn is not None:
            turns.append(turn)
    for record in records[start_index:]:
        record_type = record.get("type")
        payload = record.get("payload")
        if record_type == "response_item":
            turn, unsafe = _render_codex_item(payload, max_tool_chars, index)
            unsafe_count += int(unsafe)
            if turn is not None:
                turns.append(turn)
        elif (
            record_type == "event_msg"
            and isinstance(payload, dict)
            and payload.get("type") == "thread_rolled_back"
        ):
            number = payload.get("num_turns")
            _drop_last_user_turns(turns, number if isinstance(number, int) else 0)
        elif record_type in {"session_meta", "compacted"}:
            continue
        elif record_type in CODEX_IGNORED_TOP_LEVEL or record_type not in CODEX_SAFE_TOP_LEVEL:
            unsafe_count += 1
    if unsafe_count:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {unsafe_count} foreign instruction, reasoning, context, or unknown Codex item(s).",
        )
    session_id = (
        first_meta.get("id")
        if isinstance(first_meta.get("id"), str)
        else _codex_id_from_path(session_path)
    )
    git = first_meta.get("git") if isinstance(first_meta.get("git"), dict) else {}
    timestamps = [
        record["timestamp"]
        for record in records
        if isinstance(record.get("timestamp"), str)
    ]
    title = next(
        (_one_line(turn["text"], 200) for turn in turns if turn["role"] == "user" and turn["text"]),
        None,
    )
    result = {
        "tool": "codex",
        "source": (
            f"codex-{first_meta.get('source')}"
            if first_meta.get("source") in {"cli", "vscode"}
            else "codex"
        ),
        "session_id": session_id,
        "path": str(session_path),
        "title": title,
        "cwd": first_meta.get("cwd") if isinstance(first_meta.get("cwd"), str) else None,
        "branch": (
            git.get("branch")
            if isinstance(git.get("branch"), str)
            else first_meta.get("git_branch")
            if isinstance(first_meta.get("git_branch"), str)
            else None
        ),
        "created_at": timestamps[0] if timestamps else None,
        "updated_at": timestamps[-1] if timestamps else _iso_from_millis(_mtime_millis(session_path)),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
    }
    if isinstance(git.get("commit_hash"), str):
        result["base_commit"] = git["commit_hash"]
    return _finalize_result(result, index)


def _codex_state_database(home: Path) -> Path | None:
    candidates: list[tuple[int, Path]] = []
    try:
        children = home.iterdir()
    except OSError:
        return None
    for path in children:
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if match and path.is_file() and not path.is_symlink():
            candidates.append((int(match.group(1)), path))
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


def _existing_codex_rollout(home: Path, raw_path: Any, session_id: str) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = home / path
    candidates = [path]
    if path.name.endswith(".jsonl"):
        candidates.append(Path(str(path) + ".zst"))
    for candidate in candidates:
        match = CODEX_ROLLOUT_RE.match(candidate.name)
        if (
            match
            and match.group(1).lower() == session_id.lower()
            and candidate.is_file()
            and not candidate.is_symlink()
        ):
            return candidate
    return None


def _discover_codex_database(
    home: Path, database_path: Path, cwd: str | None, within_min: int
) -> list[dict[str, Any]] | None:
    try:
        with _open_sqlite_readonly(database_path) as database:
            columns = _table_columns(database, "threads")
            required = {"id", "rollout_path", "source", "cwd", "archived"}
            if not required.issubset(columns):
                return None
            updated_column = (
                "updated_at_ms"
                if "updated_at_ms" in columns
                else "updated_at"
                if "updated_at" in columns
                else None
            )
            if updated_column is None:
                return None
            title = "title" if "title" in columns else "''"
            first = "first_user_message" if "first_user_message" in columns else "''"
            branch = "git_branch" if "git_branch" in columns else "NULL"
            rows = database.execute(
                f"SELECT id, rollout_path, {updated_column}, source, cwd, "
                f"{title}, {first}, {branch} FROM threads "
                "WHERE archived = 0 AND source IN ('cli', 'vscode') "
                f"ORDER BY {updated_column} DESC, id ASC",
            )
            sessions: list[dict[str, Any]] = []
            for row in rows:
                session_id, raw_path, raw_updated, source, stored_cwd, raw_title, first_user, git = row
                if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
                    continue
                if cwd is not None and not _paths_match(stored_cwd, cwd):
                    continue
                rollout = _existing_codex_rollout(home, raw_path, session_id)
                if rollout is None:
                    continue
                updated = _timestamp_to_millis(raw_updated) or _mtime_millis(rollout)
                if not _within(updated, within_min):
                    continue
                title_value = raw_title if isinstance(raw_title, str) and raw_title.strip() else first_user
                sessions.append(
                    {
                        "tool": "codex",
                        "source": f"codex-{source}",
                        "session_id": session_id,
                        "path": str(rollout),
                        "title": _one_line(title_value, 200) or "(untitled)",
                        "cwd": stored_cwd,
                        "branch": git if isinstance(git, str) else None,
                        "updated_at_ms": updated,
                        "updated_at": _iso_from_millis(updated),
                        "source_repo_root_path": None,
                    }
                )
            return sessions
    except (ReaderError, sqlite3.Error):
        return None


def _iter_codex_rollouts(home: Path, include_archived: bool) -> Iterable[Path]:
    names = ["sessions", "archived_sessions"] if include_archived else ["sessions"]
    for name in names:
        root = home / name
        if not root.is_dir() or root.is_symlink():
            continue
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if not (Path(directory) / name).is_symlink()
            )
            for filename in sorted(filenames):
                path = Path(directory) / filename
                if CODEX_ROLLOUT_RE.fullmatch(filename) and not path.is_symlink():
                    yield path


def _codex_rollout_head(path: Path) -> dict[str, Any] | None:
    try:
        records, _ = _read_codex_jsonl(path)
    except ReaderError:
        return None
    for record in records[:10]:
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"]
    return None


def _discover_codex_files(home: Path, cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for path in _iter_codex_rollouts(home, include_archived=False):
        updated = _mtime_millis(path)
        if not _within(updated, within_min):
            continue
        metadata = _codex_rollout_head(path)
        if not metadata or metadata.get("source") not in {"cli", "vscode"}:
            continue
        if cwd is not None and not _paths_match(metadata.get("cwd"), cwd):
            continue
        session_id = metadata.get("id") or _codex_id_from_path(path)
        if not isinstance(session_id, str) or not UUID_RE.fullmatch(session_id):
            continue
        sessions.append(
            {
                "tool": "codex",
                "source": f"codex-{metadata.get('source')}",
                "session_id": session_id,
                "path": str(path),
                "title": "(untitled)",
                "cwd": cwd if cwd is not None else metadata.get("cwd"),
                "branch": (
                    metadata.get("git", {}).get("branch")
                    if isinstance(metadata.get("git"), dict)
                    else None
                ),
                "updated_at_ms": updated,
                "updated_at": _iso_from_millis(updated),
                "source_repo_root_path": None,
            }
        )
    return sessions


def _discover_codex(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    home = _codex_home()
    database_path = _codex_state_database(home)
    if database_path is not None:
        sessions = _discover_codex_database(home, database_path, cwd, within_min)
        if sessions is not None:
            return sessions
    return _discover_codex_files(home, cwd, within_min)


def _find_codex_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    for path in _iter_codex_rollouts(_codex_home(), include_archived=True):
        match = CODEX_ROLLOUT_RE.match(path.name)
        if match and match.group(1).lower() == session_id.lower():
            if candidate_from_path_fn is not None:
                candidate = candidate_from_path_fn("codex", str(path), cwd)
                if candidate is not None:
                    return candidate
            else:
                return {
                    "tool": "codex",
                    "source": "codex",
                    "session_id": session_id,
                    "path": str(path),
                    "title": "(untitled)",
                    "cwd": cwd,
                    "updated_at_ms": _mtime_millis(path),
                }
    return None
