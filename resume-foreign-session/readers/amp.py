"""Amp session reader and discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from reader_core import (
    ReaderError,
    WorkIndex,
    _add_warning,
    _finalize_result,
    _iso_from_millis,
    _json_preview,
    _mtime_millis,
    _one_line,
    _paths_match,
    _safe_text,
    _turn,
    _within,
)


def _amp_data_dir() -> Path:
    configured = os.environ.get("AMP_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    local_share = os.environ.get("XDG_DATA_HOME")
    if local_share:
        return Path(local_share).expanduser() / "amp"
    return Path.home() / ".local" / "share" / "amp"


def _amp_thread_dir() -> Path:
    return _amp_data_dir() / "threads"


def _amp_cwd_from_tree(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    uri = value.get("uri")
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return None
    path = uri[len("file://"):]
    path = path.split("#", 1)[0].split("?", 1)[0]
    path = unquote(path)
    if path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    if not path:
        return None
    return os.path.normpath(path)


def _amp_turn_blocks(
    content: Any, index: WorkIndex | None = None
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    if not isinstance(content, list):
        if isinstance(content, str) and content:
            texts.append(content)
        return texts, calls, results
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
        elif block_type == "tool_use":
            if index is not None:
                index.record(block.get("name"), block.get("input"))
            calls.append(
                {
                    "id": block.get("id"),
                    "name": _safe_text(block.get("name") or "tool"),
                    "input": _json_preview(block.get("input"), 300),
                    "inert": True,
                }
            )
        elif block_type == "tool_result":
            run = block.get("run")
            content_value = ""
            if isinstance(run, dict):
                result = run.get("result")
                if isinstance(result, str):
                    content_value = result
                elif isinstance(result, dict):
                    content_value = json.dumps(result, ensure_ascii=False)
            elif isinstance(run, str):
                content_value = run
            results.append(
                {
                    "tool_use_id": block.get("toolUseID"),
                    "content": content_value,
                    "is_error": False,
                    "unavailable": False,
                    "inert": True,
                }
            )
    return texts, calls, results


def _amp_thread_location(thread: dict[str, Any]) -> tuple[str | None, str | None]:
    tree = None
    env = thread.get("env")
    if isinstance(env, dict):
        initial = env.get("initial")
        if isinstance(initial, dict):
            trees = initial.get("trees")
            if isinstance(trees, list) and trees:
                tree = trees[0]
    branch = None
    repository = tree.get("repository") if isinstance(tree, dict) else None
    if isinstance(repository, dict) and isinstance(repository.get("ref"), str):
        ref = repository["ref"]
        if ref.startswith("refs/heads/"):
            branch = ref[len("refs/heads/"):]
    return _amp_cwd_from_tree(tree), branch


def _amp_quick_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            thread = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(thread, dict):
        return None
    cwd, branch = _amp_thread_location(thread)
    title = thread.get("title")
    return {
        "session_id": thread.get("id") or path.stem,
        "cwd": cwd,
        "branch": branch,
        "title": _one_line(title, 200) if isinstance(title, str) else None,
    }


def read_amp_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    warnings: list[dict[str, str]] = []
    try:
        with session_path.open("r", encoding="utf-8") as handle:
            thread = json.load(handle)
    except OSError as exc:
        raise ReaderError(f"failed to read Amp thread {session_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ReaderError(f"failed to parse Amp thread {session_path}: {exc}") from exc
    if not isinstance(thread, dict):
        raise ReaderError(f"malformed Amp thread {session_path}: expected an object")

    messages = thread.get("messages")
    if not isinstance(messages, list):
        raise ReaderError(f"malformed Amp thread {session_path}: missing messages array")

    cwd, branch = _amp_thread_location(thread)
    turns: list[dict[str, Any]] = []
    unsafe_count = 0
    index = WorkIndex()
    for message in messages:
        if not isinstance(message, dict):
            unsafe_count += 1
            continue
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        texts, calls, results = _amp_turn_blocks(message.get("content"), index)
        for text in texts:
            turns.append(_turn(role, text=text))
            for call in calls:
                turns[-1]["tool_calls"].append(call)
            for result in results:
                turns[-1]["tool_results"].append(result)
        if not texts:
            turns.append(
                _turn(
                    role,
                    tool_calls=calls,
                    tool_results=results,
                )
            )
    if unsafe_count:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {unsafe_count} unknown Amp message(s) without interpreting them.",
        )

    timestamps = [
        message.get("meta", {}).get("sentAt")
        for message in messages
        if isinstance(message.get("meta"), dict) and message["meta"].get("sentAt")
    ]
    title = thread.get("title")
    result = {
        "tool": "amp",
        "source": "amp",
        "session_id": thread.get("id") or session_path.stem,
        "path": str(session_path),
        "title": _one_line(title, 200) if isinstance(title, str) else None,
        "cwd": cwd,
        "branch": branch,
        "created_at": _iso_from_millis(thread.get("created")),
        "updated_at": _iso_from_millis(
            timestamps[-1] if timestamps else thread.get("updated")
        ),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
    }
    return _finalize_result(result, index)


def _discover_amp(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    thread_dir = _amp_thread_dir()
    if not thread_dir.is_dir() or thread_dir.is_symlink():
        return []
    sessions: list[dict[str, Any]] = []
    try:
        paths = sorted(thread_dir.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for path in paths:
        if not path.is_file() or path.suffix != ".json" or path.is_symlink():
            continue
        updated = _mtime_millis(path)
        if not _within(updated, within_min):
            continue
        meta = _amp_quick_meta(path)
        if meta is None:
            continue
        thread_cwd = meta.get("cwd")
        if cwd is not None:
            if not isinstance(thread_cwd, str) or not _paths_match(thread_cwd, cwd):
                continue
        sessions.append(
            {
                "tool": "amp",
                "source": "amp",
                "session_id": meta["session_id"],
                "path": str(path),
                "title": meta["title"],
                "cwd": thread_cwd if isinstance(thread_cwd, str) else cwd,
                "branch": meta["branch"],
                "updated_at_ms": updated,
                "updated_at": _iso_from_millis(updated),
                "source_repo_root_path": None,
            }
        )
    return sessions


def _find_amp_id(session_id: str, cwd: str) -> dict[str, Any] | None:
    thread_dir = _amp_thread_dir()
    if not thread_dir.is_dir():
        return None
    for path in (
        thread_dir / f"{session_id}.json",
        thread_dir / session_id,
    ):
        if path.is_file() and not path.is_symlink():
            meta = _amp_quick_meta(path)
            if meta is not None:
                updated = _mtime_millis(path)
                return {
                    "tool": "amp",
                    "source": "amp",
                    "session_id": meta["session_id"],
                    "path": str(path),
                    "title": meta["title"],
                    "cwd": meta["cwd"] or cwd,
                    "branch": meta["branch"],
                    "updated_at_ms": updated,
                }
    return None
