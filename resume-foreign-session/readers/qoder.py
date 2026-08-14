"""Qoder CLI session reader and discovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from reader_core import (
    QODER_KNOWN_TYPES,
    ReaderError,
    _mtime_millis,
    slugify,
)
from readers.claude import (
    _claude_quick_meta,
    _discover_claude_layout,
    read_claude_session,
)


def _qoder_config_dir() -> Path:
    configured = os.environ.get("QODER_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".qoder"


def _qoder_head_types(path: Path) -> set[str]:
    found: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _, line in zip(range(200), handle):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(record, dict)
                    and record.get("type") in {"user", "assistant"}
                    and isinstance(record.get("uuid"), str)
                    and isinstance(record.get("message"), dict)
                ):
                    found.add(str(record["type"]))
    except OSError:
        return set()
    return found


def _is_qoder_cli_transcript(path: Path) -> bool:
    return bool(_qoder_head_types(path))


def _qoder_quick_meta(path: Path) -> dict[str, Any] | None:
    if "assistant" not in _qoder_head_types(path):
        return None
    return _claude_quick_meta(path)


def read_qoder_session(path: Path | str, max_tool_chars: int = 300) -> dict[str, Any]:
    session_path = Path(path).expanduser()
    if not _is_qoder_cli_transcript(session_path):
        raise ReaderError(
            f"{session_path} is not a Qoder CLI transcript this reader can render "
            "(no typed user/assistant records; likely a legacy `parts` transcript)."
        )
    return read_claude_session(
        session_path,
        max_tool_chars,
        tool="qoder",
        source="qoder-cli",
        label="Qoder",
        known_types=QODER_KNOWN_TYPES,
    )


def _discover_qoder(cwd: str | None, within_min: int) -> list[dict[str, Any]]:
    return _discover_claude_layout(
        _qoder_config_dir() / "projects",
        "qoder",
        "qoder-cli",
        _qoder_quick_meta,
        cwd,
        within_min,
    )


def _find_qoder_id(
    session_id: str,
    cwd: str,
    candidate_from_path_fn: Any = None,
) -> dict[str, Any] | None:
    projects = _qoder_config_dir() / "projects"
    if not projects.is_dir():
        return None
    candidates = [projects / slugify(cwd) / f"{session_id}.jsonl"]
    candidates.extend(sorted(projects.glob(f"*/{session_id}.jsonl"), key=str))
    for path in candidates:
        if candidate_from_path_fn is not None:
            candidate = candidate_from_path_fn("qoder", str(path), cwd)
            if candidate is not None:
                return candidate
        else:
            if path.is_file() and not path.is_symlink():
                meta = _qoder_quick_meta(path)
                return {
                    "tool": "qoder",
                    "source": "qoder-cli",
                    "session_id": session_id,
                    "path": str(path),
                    "title": (meta.get("title") if meta else None) or "(untitled)",
                    "cwd": (meta.get("cwd") if meta else None) or cwd,
                    "branch": meta.get("branch") if meta else None,
                    "updated_at_ms": _mtime_millis(path),
                }
    return None
