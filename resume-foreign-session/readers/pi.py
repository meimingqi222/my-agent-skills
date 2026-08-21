"""pi (pi-coding-agent) session reader and discovery.

pi stores sessions as JSONL files under ``~/.pi/agent/sessions/<encoded-cwd>/``.
The directory name encodes the working directory by replacing ``/`` with ``-``
and wrapping in ``--`` (e.g. ``/Users/foo/proj`` -> ``--Users-foo-proj--``).
Each file is named ``<ISO-timestamp>Z_<uuid>.jsonl``.

Record types in the JSONL:
- ``session``: metadata (id, cwd, version, timestamp)
- ``model_change``: model switch events
- ``thinking_level_change``: thinking level changes
- ``message``: the actual conversation turns, with ``message.role`` being
  ``user``, ``assistant``, or ``toolResult``. Content items use types
  ``text``, ``thinking``, and ``toolCall``.
"""

from __future__ import annotations

import json
import os
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
    _paths_match,
    _safe_text,
    _timestamp_to_millis,
    _turn,
    _within,
)


def _pi_data_dir() -> Path:
    """返回 pi agent 的数据目录。"""
    configured = os.environ.get("PI_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser() / "agent"
    return Path.home() / ".pi" / "agent"


def _pi_sessions_dir() -> Path:
    """返回 pi 会话存储根目录。"""
    return _pi_data_dir() / "sessions"


def _pi_encode_cwd(cwd: str) -> str:
    """将 cwd 编码为 pi 的目录名格式。

    pi 把 ``/Users/foo/proj`` 编码成 ``--Users-foo-proj--``。
    """
    # 去掉末尾的 /
    clean = cwd.rstrip("/")
    return "--" + clean.replace("/", "-") + "--"


def _pi_decode_cwd(dirname: str) -> str | None:
    """将 pi 的目录名解码回 cwd 路径。"""
    if not (dirname.startswith("--") and dirname.endswith("--")):
        return None
    inner = dirname[2:-2]
    if not inner:
        return None
    return "/" + inner.replace("-", "/")


def _pi_session_id_from_path(path: Path) -> str:
    """从文件名提取 session ID（uuid 部分）。"""
    stem = path.stem
    # 文件名格式: 2026-08-21T08-56-34-036Z_01a02389-45f4-7ce3-88f4-a1362237058a
    if "_" in stem:
        return stem.split("_", 1)[1]
    return stem


def _pi_read_session_meta(path: Path) -> dict[str, Any] | None:
    """读取 JSONL 文件的第一条 session 记录，提取元数据。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("type") == "session":
                    return record
                # 如果第一条不是 session 记录，也继续找
    except OSError:
        return None
    return None


def _pi_extract_text(content: list[Any]) -> str:
    """从 content 数组中提取所有 text 类型的文本。"""
    texts: list[str] = []
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict):
            if item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(_safe_text(text))
    return "\n".join(texts)


def read_pi_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    """读取一个 pi 会话 JSONL 文件。"""
    session_path = Path(path).expanduser()
    warnings: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    index = WorkIndex()
    session_id: str | None = None
    cwd: str | None = None
    created_at: str | None = None
    model: str | None = None
    skipped = 0

    try:
        with session_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if not isinstance(record, dict):
                    skipped += 1
                    continue

                rec_type = record.get("type")

                if rec_type == "session":
                    session_id = record.get("id")
                    cwd = record.get("cwd")
                    created_at = record.get("timestamp")
                    continue

                if rec_type == "model_change":
                    model_id = record.get("modelId")
                    if isinstance(model_id, str) and model_id:
                        model = model_id
                    continue

                if rec_type == "thinking_level_change":
                    continue

                if rec_type != "message":
                    skipped += 1
                    continue

                message = record.get("message")
                if not isinstance(message, dict):
                    skipped += 1
                    continue

                role = message.get("role")
                content = message.get("content", [])

                if role == "user":
                    text_str = _pi_extract_text(content)
                    if text_str.strip():
                        turns.append(_turn("user", text=text_str))
                    continue

                if role == "assistant":
                    texts: list[str] = []
                    calls: list[dict[str, Any]] = []
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            item_type = item.get("type")
                            if item_type == "text":
                                t = item.get("text")
                                if isinstance(t, str) and t.strip():
                                    texts.append(_safe_text(t))
                            elif item_type == "toolCall":
                                call_id = item.get("id")
                                tool_name = item.get("name") or "tool"
                                arguments = item.get("arguments", {})
                                index.record(tool_name, arguments)
                                calls.append(
                                    {
                                        "id": call_id,
                                        "name": _safe_text(tool_name),
                                        "input": _json_preview(arguments, max_tool_chars),
                                        "inert": True,
                                    }
                                )
                            elif item_type == "thinking":
                                skipped += 1
                    text_str = "\n".join(t for t in texts if t.strip())
                    if text_str or calls:
                        turns.append(
                            _turn("assistant", text=text_str, tool_calls=calls)
                        )
                    continue

                if role == "toolResult":
                    # toolResult 是独立的 message 记录
                    tool_call_id = message.get("toolCallId")
                    tool_name = message.get("toolName") or "tool"
                    result_text = _pi_extract_text(content)
                    is_error = False
                    # 检查是否有 error 标记
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "error":
                                is_error = True
                                break
                    if result_text or tool_call_id:
                        # 附加到最近的 assistant turn 的 tool_results
                        if turns and turns[-1]["role"] == "assistant" and turns[-1]["tool_calls"]:
                            turns[-1]["tool_results"].append(
                                {
                                    "tool_use_id": tool_call_id,
                                    "content": _one_line(result_text, max_tool_chars * 3),
                                    "is_error": is_error,
                                    "unavailable": False,
                                    "inert": True,
                                }
                            )
                    continue

                skipped += 1

    except OSError as exc:
        raise ReaderError(f"failed to read pi session {session_path}: {exc}") from exc

    if skipped:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {skipped} pi thinking/unknown record(s).",
        )

    if session_id is None:
        session_id = _pi_session_id_from_path(session_path)

    if cwd is None:
        # 尝试从父目录名解码
        decoded = _pi_decode_cwd(session_path.parent.name)
        if decoded:
            cwd = decoded

    updated = _mtime_millis(session_path)

    result = {
        "tool": "pi",
        "source": "pi-coding-agent",
        "session_id": session_id,
        "path": str(session_path),
        "title": None,
        "cwd": cwd,
        "branch": None,
        "created_at": _iso_from_millis(_timestamp_to_millis(created_at)),
        "updated_at": _iso_from_millis(updated),
        "source_repo_root_path": None,
        "turns": turns,
        "warnings": warnings,
        "model": model,
    }
    return _finalize_result(result, index)


def _pi_quick_meta(path: Path) -> dict[str, Any] | None:
    """快速提取 pi 会话的元数据（用于列表展示）。"""
    meta = _pi_read_session_meta(path)
    if meta is None:
        return None
    return {
        "session_id": meta.get("id") or _pi_session_id_from_path(path),
        "cwd": meta.get("cwd"),
        "title": None,
        "updated_at_ms": _mtime_millis(path),
    }


def _discover_pi(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    """发现 pi 会话。"""
    sessions: list[dict[str, Any]] = []
    sessions_root = _pi_sessions_dir()
    if not sessions_root.is_dir():
        return sessions

    for session_dir in sorted(sessions_root.iterdir()):
        if not session_dir.is_dir():
            continue
        for session_file in sorted(session_dir.glob("*.jsonl"), reverse=True):
            meta = _pi_quick_meta(session_file)
            if meta is None:
                continue
            session_cwd = meta.get("cwd")
            if cwd is not None:
                if not isinstance(session_cwd, str) or not _paths_match(session_cwd, cwd):
                    continue
            updated = meta.get("updated_at_ms", 0)
            if updated and not _within(updated, within_min):
                continue
            sessions.append(
                {
                    "tool": "pi",
                    "source": "pi-coding-agent",
                    "session_id": meta["session_id"],
                    "path": str(session_file),
                    "title": None,
                    "cwd": session_cwd,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                }
            )
    return sessions


def _find_pi_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    """按 native session ID 查找 pi 会话。"""
    sessions_root = _pi_sessions_dir()
    if not sessions_root.is_dir():
        return None

    # 尝试在所有会话目录中查找匹配的文件
    candidates: list[Path] = []
    # 先尝试按 cwd 编码的目录
    encoded = _pi_encode_cwd(cwd)
    target_dir = sessions_root / encoded
    if target_dir.is_dir():
        candidates.extend(sorted(target_dir.glob(f"*_{session_id}.jsonl"), reverse=True))
        # 也匹配直接以 session_id 为文件名的情况
        candidates.extend(sorted(target_dir.glob(f"{session_id}.jsonl"), reverse=True))

    # 在所有目录中搜索
    for session_dir in sessions_root.iterdir():
        if not session_dir.is_dir():
            continue
        candidates.extend(sorted(session_dir.glob(f"*_{session_id}.jsonl"), reverse=True))
        candidates.extend(sorted(session_dir.glob(f"{session_id}.jsonl"), reverse=True))

    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        meta = _pi_quick_meta(path)
        if meta is None:
            continue
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("pi", str(path), cwd)
            if candidate is not None:
                return candidate
        else:
            return {
                "tool": "pi",
                "source": "pi-coding-agent",
                "session_id": meta["session_id"],
                "path": str(path),
                "title": None,
                "cwd": meta.get("cwd") or cwd,
                "branch": None,
                "updated_at_ms": meta.get("updated_at_ms", 0),
                "updated_at": _iso_from_millis(meta.get("updated_at_ms", 0)),
                "source_repo_root_path": None,
            }
    return None
