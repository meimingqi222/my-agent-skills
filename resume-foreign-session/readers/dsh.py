"""DeepSeek Harness (DSH) session reader and discovery.

DSH stores sessions as zstd-compressed JSONL files under
``~/.dsh/sessions/<encoded-cwd>/<session-id>/session.jsonl.zstd``.
The directory name encodes the working directory by replacing ``/`` with ``-``
and wrapping in ``--`` (same scheme as pi).

Record types in the JSONL:
- ``session``: metadata (id, cwd, createdAt, version, agentPreset)
- ``session/title``: title records (latest wins)
- ``user/message``: user messages (``data.content``, ``data.source.kind``
  distinguishes real user input from plugin/system injections)
- ``assistant/message``: assistant messages (``data.message.content`` with
  reasoning/text parts)
- ``tool/call``: tool invocation (``data.name``, ``data.arguments`` JSON string)
- ``tool/result``: tool result (``data.message.content``, ``data.error``)
- ``assistant/chunk``, ``text-chunks``, ``reasoning-chunks``: streaming chunks
  (ignored; the final ``assistant/message`` has the complete content)
- Other metadata records (turn/start, step/start, permission/preset, etc.)
"""

from __future__ import annotations

import json
import os
import subprocess
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


def _dsh_data_dir() -> Path:
    """返回 DSH 的数据目录。"""
    configured = os.environ.get("DSH_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".dsh"


def _dsh_sessions_dir() -> Path:
    """返回 DSH 会话存储根目录。"""
    return _dsh_data_dir() / "sessions"


def _dsh_encode_cwd(cwd: str) -> str:
    """将 cwd 编码为 DSH 的目录名格式（与 pi 相同的编码方式）。"""
    clean = cwd.rstrip("/")
    return "--" + clean.replace("/", "-") + "--"


def _dsh_decode_cwd(dirname: str) -> str | None:
    """将 DSH 的目录名解码回 cwd 路径。"""
    if not (dirname.startswith("--") and dirname.endswith("--")):
        return None
    inner = dirname[2:-2]
    if not inner:
        return None
    return "/" + inner.replace("-", "/")


def _dsh_decompress_zstd(path: Path) -> str:
    """解压 zstd 压缩的 JSONL 文件，返回文本内容。

    优先使用 python zstandard 库，回退到 zstd 命令行工具。
    """
    # 优先使用 python 库
    try:
        import zstandard  # type: ignore[import-not-found]

        with path.open("rb") as f:
            dctx = zstandard.ZstdDecompressor()
            # 使用流式解压避免 "could not determine content size" 错误
            with dctx.stream_reader(f) as reader:
                return reader.read().decode("utf-8", errors="replace")
    except ImportError:
        pass

    # 回退到 zstd 命令行工具
    try:
        result = subprocess.run(
            ["zstd", "-d", str(path), "--stdout"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise ReaderError(
                f"zstd decompression failed for {path}: {result.stderr.decode('utf-8', errors='replace')}"
            )
        return result.stdout.decode("utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise ReaderError(
            f"Cannot decompress {path}: neither python zstandard nor zstd CLI is available"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ReaderError(f"zstd decompression timed out for {path}") from exc


def _dsh_read_records(path: Path) -> list[dict[str, Any]]:
    """读取并解压 DSH 会话文件，返回记录列表。"""
    text = _dsh_decompress_zstd(path)
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _dsh_extract_text(content: list[Any]) -> str:
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


def _dsh_quick_meta_from_records(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """从已读取的记录中提取会话元数据。"""
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    for record in records:
        rec_type = record.get("type")
        if rec_type == "session":
            session_id = record.get("id")
            cwd = record.get("cwd")
        elif rec_type == "session/title":
            data = record.get("data")
            if isinstance(data, dict):
                t = data.get("title")
                if isinstance(t, str) and t.strip():
                    title = t
    if session_id is None:
        return None
    return {"session_id": session_id, "cwd": cwd, "title": title}


def _dsh_quick_meta(path: Path) -> dict[str, Any] | None:
    """快速提取 DSH 会话的元数据（用于列表展示）。"""
    try:
        records = _dsh_read_records(path)
    except (ReaderError, OSError):
        return None
    meta = _dsh_quick_meta_from_records(records)
    if meta is None:
        return None
    meta["updated_at_ms"] = _mtime_millis(path)
    return meta


def read_dsh_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    """读取一个 DSH 会话（zstd 压缩的 JSONL）。"""
    session_path = Path(path).expanduser()
    warnings: list[dict[str, str]] = []
    turns: list[dict[str, Any]] = []
    index = WorkIndex()
    session_id: str | None = None
    cwd: str | None = None
    created_at: Any = None
    title: str | None = None
    model: str | None = None
    skipped = 0
    plugin_messages = 0

    records = _dsh_read_records(session_path)

    # 第一遍：提取元数据
    for record in records:
        rec_type = record.get("type")
        if rec_type == "session":
            session_id = record.get("id")
            cwd = record.get("cwd")
            created_at = record.get("createdAt")
        elif rec_type == "session/title":
            data = record.get("data")
            if isinstance(data, dict):
                t = data.get("title")
                if isinstance(t, str) and t.strip():
                    title = t

    # 第二遍：构建 turns
    # 用 tool_call_id 关联 tool/call 和 tool/result
    pending_tool_results: dict[str, dict[str, Any]] = {}
    for record in records:
        rec_type = record.get("type")

        if rec_type == "user/message":
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            source = data.get("source")
            source_kind = source.get("kind") if isinstance(source, dict) else None
            # 跳过 plugin/system/skill-catalog 注入的消息
            if source_kind in ("plugin", "system", "skill-catalog"):
                plugin_messages += 1
                continue
            content = data.get("content", [])
            text = _dsh_extract_text(content)
            if text.strip():
                turns.append(_turn("user", text=text))
            continue

        if rec_type == "assistant/message":
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            # 提取模型信息
            msg_source = message.get("source")
            if isinstance(msg_source, dict):
                model_id = msg_source.get("model")
                if isinstance(model_id, str) and model_id:
                    model = model_id

            content = message.get("content", [])
            texts: list[str] = []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "text":
                        t = item.get("text")
                        if isinstance(t, str) and t.strip():
                            texts.append(_safe_text(t))
                    elif item_type == "reasoning":
                        skipped += 1
            text = "\n".join(t for t in texts if t.strip())
            if text:
                turns.append(_turn("assistant", text=text))
            continue

        if rec_type == "tool/call":
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            call_id = data.get("callId")
            tool_name = data.get("name") or "tool"
            arguments_raw = data.get("arguments", "{}")
            # arguments 是 JSON 字符串，需要解析
            if isinstance(arguments_raw, str):
                try:
                    arguments = json.loads(arguments_raw)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments_raw}
            elif isinstance(arguments_raw, dict):
                arguments = arguments_raw
            else:
                arguments = {}
            index.record(tool_name, arguments)
            calls = [
                {
                    "id": call_id,
                    "name": _safe_text(tool_name),
                    "input": _json_preview(arguments, max_tool_chars),
                    "inert": True,
                }
            ]
            # 查找是否有对应的 tool/result
            results: list[dict[str, Any]] = []
            turns.append(
                _turn("assistant", text="", tool_calls=calls, tool_results=results)
            )
            continue

        if rec_type == "tool/result":
            data = record.get("data")
            if not isinstance(data, dict):
                continue
            message = data.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            result_text = ""
            is_error = bool(data.get("error"))
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "tool-result":
                            inner = item.get("content", [])
                            if isinstance(inner, list):
                                result_text = _dsh_extract_text(inner)
                            if item.get("isError"):
                                is_error = True
                        elif item.get("type") == "text":
                            t = item.get("text")
                            if isinstance(t, str) and t.strip():
                                result_text = t
            # 附加到最近的 tool/call turn
            if turns and turns[-1]["role"] == "assistant" and turns[-1]["tool_calls"]:
                turns[-1]["tool_results"].append(
                    {
                        "tool_use_id": None,
                        "content": _one_line(result_text, max_tool_chars * 3),
                        "is_error": is_error,
                        "unavailable": False,
                        "inert": True,
                    }
                )
            continue

        # 忽略其他记录类型（chunk、metadata 等）
        if rec_type not in (
            "session",
            "session/title",
            "permission/preset",
            "sandbox/mode",
            "approval/policy",
            "agent/inbox/spliced",
            "turn/start",
            "turn/end",
            "step/start",
            "step/end",
            "request/header",
            "request/context",
            "session/title-llm-request",
            "assistant/chunk",
            "text-chunks",
            "reasoning-chunks",
        ):
            skipped += 1

    if skipped:
        _add_warning(
            warnings,
            "unsafe_records_skipped",
            f"Skipped {skipped} DSH reasoning/unknown record(s).",
        )
    if plugin_messages:
        _add_warning(
            warnings,
            "harness_text_dropped",
            f"Dropped {plugin_messages} DSH plugin/system-injected message(s).",
        )

    if session_id is None:
        # 从路径推断
        session_id = session_path.parent.name

    if cwd is None:
        decoded = _dsh_decode_cwd(session_path.parent.parent.name)
        if decoded:
            cwd = decoded

    updated = _mtime_millis(session_path)

    result = {
        "tool": "dsh",
        "source": "deepseek-harness",
        "session_id": session_id,
        "path": str(session_path),
        "title": _one_line(title, 200) if title else None,
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


def _discover_dsh(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    """发现 DSH 会话。"""
    sessions: list[dict[str, Any]] = []
    sessions_root = _dsh_sessions_dir()
    if not sessions_root.is_dir():
        return sessions

    for session_dir in sorted(sessions_root.iterdir()):
        if not session_dir.is_dir():
            continue
        for session_subdir in sorted(session_dir.iterdir(), reverse=True):
            if not session_subdir.is_dir():
                continue
            session_file = session_subdir / "session.jsonl.zstd"
            if not session_file.is_file():
                continue
            try:
                meta = _dsh_quick_meta(session_file)
            except (ReaderError, OSError, Exception):
                continue
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
                    "tool": "dsh",
                    "source": "deepseek-harness",
                    "session_id": meta["session_id"],
                    "path": str(session_file),
                    "title": _one_line(meta.get("title"), 200) if meta.get("title") else None,
                    "cwd": session_cwd,
                    "branch": None,
                    "updated_at_ms": updated,
                    "updated_at": _iso_from_millis(updated),
                    "source_repo_root_path": None,
                }
            )
    return sessions


def _find_dsh_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    """按 native session ID 查找 DSH 会话。"""
    sessions_root = _dsh_sessions_dir()
    if not sessions_root.is_dir():
        return None

    # session_id 可能带 "session-" 前缀
    clean_id = session_id.removeprefix("session-")

    candidates: list[Path] = []
    # 先尝试按 cwd 编码的目录
    encoded = _dsh_encode_cwd(cwd)
    target_dir = sessions_root / encoded
    if target_dir.is_dir():
        # 目录名可能是 session-<uuid> 或 <uuid>
        candidates.append(target_dir / f"session-{clean_id}" / "session.jsonl.zstd")
        candidates.append(target_dir / clean_id / "session.jsonl.zstd")

    # 在所有目录中搜索
    for session_dir in sessions_root.iterdir():
        if not session_dir.is_dir():
            continue
        candidates.append(session_dir / f"session-{clean_id}" / "session.jsonl.zstd")
        candidates.append(session_dir / clean_id / "session.jsonl.zstd")

    for path in candidates:
        if not path.is_file():
            continue
        meta = _dsh_quick_meta(path)
        if meta is None:
            continue
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("dsh", str(path), cwd)
            if candidate is not None:
                return candidate
        else:
            return {
                "tool": "dsh",
                "source": "deepseek-harness",
                "session_id": meta["session_id"],
                "path": str(path),
                "title": _one_line(meta.get("title"), 200) if meta.get("title") else None,
                "cwd": meta.get("cwd") or cwd,
                "branch": None,
                "updated_at_ms": meta.get("updated_at_ms", 0),
                "updated_at": _iso_from_millis(meta.get("updated_at_ms", 0)),
                "source_repo_root_path": None,
            }
    return None
