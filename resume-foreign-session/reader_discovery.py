"""Session discovery, resolution, and candidate building."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from reader_core import (
    ANY_TOOL,
    LIVE_SESSION_MINUTES,
    TOOLS,
    UUID_RE,
    AmbiguousReference,
    ReaderError,
    _add_warning,
    _mtime_millis,
    _timestamp_to_millis,
)
from readers import (
    _commandcode_config_dir,
    _discover_amp,
    _discover_claude,
    _discover_codex,
    _discover_commandcode,
    _discover_cursor_cli,
    _discover_cursor_desktop,
    _discover_devin_cli,
    _discover_devin_next,
    _discover_grok,
    _discover_maka,
    _discover_opencode,
    _discover_qoder,
    _discover_zcode,
    _find_amp_id,
    _find_claude_id,
    _find_codex_id,
    _find_commandcode_id,
    _find_cursor_id,
    _find_devin_id,
    _find_grok_id,
    _find_maka_id,
    _find_opencode_id,
    _find_qoder_id,
    _find_zcode_id,
    _grok_config_dir,
    _grok_meta,
    _grok_session_dir,
    _is_commandcode_transcript,
    _qoder_config_dir,
    read_amp_session,
    read_claude_session,
    read_codex_session,
    read_commandcode_session,
    read_cursor_session,
    read_devin_cli_session,
    read_devin_next_session,
    read_grok_session,
    read_maka_session,
    read_opencode_session,
    read_qoder_session,
    read_zcode_session,
)
from readers.codex import CODEX_ROLLOUT_RE, _codex_id_from_path


def _sort_and_dedupe(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_priority = {
        "cursor-cli": 0,
        "cursor-desktop": 1,
        "claude-code": 0,
        "codex-cli": 0,
        "codex-vscode": 1,
        "maka": 0,
    }
    ordered = sorted(
        sessions,
        key=lambda item: (
            -int(item.get("updated_at_ms") or 0),
            source_priority.get(str(item.get("source")), 9),
            str(item.get("session_id")),
            str(item.get("path")),
        ),
    )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session in ordered:
        key = (str(session.get("tool")), str(session.get("session_id")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(session)
    return deduped


CURRENT_SESSION_ENV = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
    "CURSOR_SESSION_ID",
    "AMP_THREAD_ID",
    "OPENCODE_SESSION_ID",
    "DEVIN_SESSION_ID",
    "QODER_SESSION_ID",
    "COMMANDCODE_SESSION_ID",
    "COMMAND_CODE_SESSION_ID",
    "GROK_SESSION_ID",
    "ZCODE_SESSION_ID",
)


def _warn_if_live(result: dict[str, Any], now_ms: int | None = None) -> None:
    updated = _timestamp_to_millis(result.get("updated_at"))
    if updated is None:
        return
    now = int(time.time() * 1000) if now_ms is None else now_ms
    age_ms = now - updated
    if not 0 <= age_ms <= LIVE_SESSION_MINUTES * 60 * 1000:
        return
    _add_warning(
        result.setdefault("warnings", []),
        "session_may_be_live",
        f"This session was written to {max(1, age_ms // 60000)} minute(s) ago, so "
        f"another agent may still be running in {result.get('cwd') or 'its working directory'}. "
        "Confirm it has stopped before editing the same files.",
    )
    result["warnings"].sort(key=lambda item: (item["code"], item["message"]))


def current_session_ids() -> set[str]:
    ids: set[str] = set()
    for name in CURRENT_SESSION_ENV:
        value = os.environ.get(name)
        if value and value.strip():
            ids.add(value.strip().casefold())
    return ids


def _excluded(session: dict[str, Any], exclude: set[str]) -> bool:
    if not exclude:
        return False
    identifier = str(session.get("session_id") or "").casefold()
    return bool(identifier) and identifier in exclude


def discover_sessions(
    tool: str, cwd: str | None, within_min: int = 0
) -> list[dict[str, Any]]:
    if tool == ANY_TOOL:
        combined: list[dict[str, Any]] = []
        for name in TOOLS:
            try:
                combined.extend(discover_sessions(name, cwd, within_min))
            except ReaderError:
                continue
        return _sort_and_dedupe(combined)
    if tool not in TOOLS:
        raise ReaderError(f"unsupported tool: {tool}")
    requested_cwd = str(Path(cwd).expanduser()) if cwd else None
    if tool == "claude":
        sessions = _discover_claude(requested_cwd, within_min)
    elif tool == "codex":
        sessions = _discover_codex(requested_cwd, within_min)
    elif tool == "amp":
        sessions = _discover_amp(requested_cwd, within_min)
    elif tool == "devin":
        sessions = _discover_devin_cli(requested_cwd, within_min)
        sessions.extend(_discover_devin_next(requested_cwd, within_min))
    elif tool == "opencode":
        sessions = _discover_opencode(requested_cwd, within_min)
    elif tool == "qoder":
        sessions = _discover_qoder(requested_cwd, within_min)
    elif tool == "commandcode":
        sessions = _discover_commandcode(requested_cwd, within_min)
    elif tool == "grok":
        sessions = _discover_grok(requested_cwd, within_min)
    elif tool == "zcode":
        sessions = _discover_zcode(requested_cwd, within_min)
    elif tool == "maka":
        sessions = _discover_maka(requested_cwd, within_min)
    else:
        sessions = _discover_cursor_cli(requested_cwd, within_min)
        sessions.extend(_discover_cursor_desktop(requested_cwd, within_min))
    return _sort_and_dedupe(sessions)


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _candidate_from_path(tool: str, raw_path: str, cwd: str) -> dict[str, Any] | None:
    path = Path(raw_path).expanduser()
    if not path.exists() or path.is_symlink():
        return None
    updated = _mtime_millis(path)
    if tool != "qoder" and _under(path, _qoder_config_dir()):
        return None
    if tool != "commandcode" and _under(path, _commandcode_config_dir()):
        return None
    if tool != "grok" and _under(path, _grok_config_dir()):
        return None
    if tool == "grok":
        session_dir = _grok_session_dir(path)
        if session_dir is None:
            return None
        meta = _grok_meta(session_dir)
        return {
            "tool": tool,
            "source": "grok-cli",
            "session_id": meta.get("session_id") or session_dir.name,
            "path": str(session_dir),
            "title": meta.get("title"),
            "cwd": meta.get("cwd") or cwd,
            "updated_at_ms": int(meta.get("updated_at_ms") or updated),
        }
    if (
        tool == "commandcode"
        and path.is_file()
        and path.suffix == ".jsonl"
        and _is_commandcode_transcript(path)
    ):
        return {
            "tool": tool,
            "source": "command-code",
            "session_id": path.stem,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "claude" and path.is_file() and path.suffix == ".jsonl":
        return {
            "tool": tool,
            "source": "claude-code",
            "session_id": path.stem,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "qoder" and path.is_file() and path.suffix == ".jsonl":
        return {
            "tool": tool,
            "source": "qoder-cli",
            "session_id": path.stem,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "codex" and path.is_file() and CODEX_ROLLOUT_RE.fullmatch(path.name):
        return {
            "tool": tool,
            "source": "codex",
            "session_id": _codex_id_from_path(path),
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "cursor" and path.is_file() and path.suffix == ".jsonl":
        return {
            "tool": tool,
            "source": "cursor-transcript",
            "session_id": path.stem,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "cursor" and path.name in {"store.db", "meta.json"}:
        return {
            "tool": tool,
            "source": "cursor-cli",
            "session_id": path.parent.name,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "amp" and path.is_file() and path.suffix == ".json":
        return {
            "tool": tool,
            "source": "amp",
            "session_id": path.stem,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "devin" and path.is_file() and path.name == "sessions.db":
        return {
            "tool": tool,
            "source": "devin-next",
            "session_id": None,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "maka" and path.is_file() and path.name == "runtime.sqlite":
        return {
            "tool": tool,
            "source": "maka",
            "session_id": None,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    if tool == "devin" and path.is_file() and path.suffix == ".json":
        return {
            "tool": tool,
            "source": "devin-cli",
            "session_id": path.stem,
            "path": str(path),
            "title": None,
            "cwd": cwd,
            "updated_at_ms": updated,
        }
    return None


def _finders() -> dict[str, Any]:
    return {
        "claude": lambda sid, cwd: _find_claude_id(sid, cwd, _candidate_from_path),
        "codex": lambda sid, cwd: _find_codex_id(sid, cwd, _candidate_from_path),
        "cursor": lambda sid, cwd: _find_cursor_id(sid, cwd, _candidate_from_path),
        "amp": lambda sid, cwd: _find_amp_id(sid, cwd),
        "devin": lambda sid, cwd: _find_devin_id(sid, cwd, _candidate_from_path),
        "opencode": _find_opencode_id,
        "qoder": lambda sid, cwd: _find_qoder_id(sid, cwd, _candidate_from_path),
        "commandcode": lambda sid, cwd: _find_commandcode_id(sid, cwd, _candidate_from_path),
        "grok": lambda sid, cwd: _find_grok_id(sid, cwd, _candidate_from_path),
        "zcode": _find_zcode_id,
        "maka": _find_maka_id,
    }


def resolve_session(
    tool: str,
    reference: str | None,
    cwd: str,
    within_min: int = 0,
    any_cwd: bool = False,
    exclude: set[str] | None = None,
) -> dict[str, Any]:
    ref = (reference or "").strip()
    if not ref or ref.casefold() == "latest":
        ref = "latest"
    for name in TOOLS if tool == ANY_TOOL else (tool,):
        path_candidate = _candidate_from_path(name, ref, cwd)
        if path_candidate is not None:
            return path_candidate
    skip = exclude or set() if ref == "latest" else set()
    sessions = [
        item
        for item in discover_sessions(tool, None if any_cwd else cwd, within_min)
        if not _excluded(item, skip)
    ]
    if ref == "latest":
        if not sessions and not any_cwd:
            sessions = [
                item
                for item in discover_sessions(tool, None, within_min)
                if not _excluded(item, skip)
            ]
            if sessions:
                sessions[0] = {**sessions[0], "cwd_fallback": cwd}
        if not sessions:
            where = "any working directory" if any_cwd else f"cwd {cwd}"
            raise ReaderError(f"no {tool} session found for {where}")
        return sessions[0]
    exact = [item for item in sessions if str(item["session_id"]).lower() == ref.lower()]
    if len(exact) == 1:
        return exact[0]
    uuid_ref = ref[2:] if ref.startswith("T-") else ref
    if (
        UUID_RE.fullmatch(ref)
        or UUID_RE.fullmatch(uuid_ref)
        or ref.startswith(("ses_", "sess_"))
    ):
        finders = _finders()
        for name in TOOLS if tool == ANY_TOOL else (tool,):
            try:
                found = finders[name](ref, cwd)
            except ReaderError:
                continue
            if found is not None:
                return found
        raise ReaderError(f"no {tool} session found for native id {ref}")
    query = " ".join(ref.casefold().split())
    matches = [
        item
        for item in sessions
        if query in " ".join(str(item.get("title") or "").casefold().split())
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousReference(ref, matches)
    raise ReaderError(f"no {tool} session matched {ref!r} for cwd {cwd}")


def read_resolved_session(
    candidate: dict[str, Any], max_tool_chars: int = 300
) -> dict[str, Any]:
    tool = candidate["tool"]
    if tool == "claude":
        return read_claude_session(candidate["path"], max_tool_chars)
    if tool == "codex":
        return read_codex_session(candidate["path"], max_tool_chars)
    if tool == "amp":
        return read_amp_session(candidate["path"], max_tool_chars)
    if tool == "devin":
        if candidate.get("source") == "devin-next":
            return read_devin_next_session(candidate["path"], candidate["session_id"], max_tool_chars)
        return read_devin_cli_session(candidate["path"], max_tool_chars)
    if tool == "opencode":
        return read_opencode_session(candidate["path"], candidate["session_id"], max_tool_chars)
    if tool == "qoder":
        return read_qoder_session(candidate["path"], max_tool_chars)
    if tool == "commandcode":
        return read_commandcode_session(
            candidate["path"], max_tool_chars, cwd_hint=candidate.get("cwd")
        )
    if tool == "grok":
        return read_grok_session(candidate["path"], max_tool_chars)
    if tool == "zcode":
        return read_zcode_session(candidate["path"], candidate["session_id"], max_tool_chars)
    if tool == "maka":
        return read_maka_session(candidate["path"], candidate["session_id"], max_tool_chars)
    return read_cursor_session(candidate, max_tool_chars)
