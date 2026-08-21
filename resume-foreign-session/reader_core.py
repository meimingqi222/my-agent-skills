#!/usr/bin/env python3
"""Common utilities, data structures, constants, and work-index for session readers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

TOOLS = (
    "claude",
    "codex",
    "cursor",
    "amp",
    "devin",
    "opencode",
    "opencode2",
    "qoder",
    "commandcode",
    "grok",
    "zcode",
    "maka",
    "pi",
    "dsh",
)
ANY_TOOL = "any"
SELECTABLE = TOOLS + (ANY_TOOL,)

TOOL_ALIASES = {
    "command-code": "commandcode",
    "cc": "commandcode",
    "grok-build": "grok",
    "grokbuild": "grok",
    "z-code": "zcode",
    "maka-agent": "maka",
    "opencode-2": "opencode2",
    "opencodev2": "opencode2",
    "pi-coding-agent": "pi",
    "pi-agent": "pi",
    "deepseek": "dsh",
    "deepseek-harness": "dsh",
    "deepseek-cli": "dsh",
}

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
CODEX_ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-fA-F-]{36})\.jsonl(?:\.zst)?$"
)
GENERATED_META_RE = re.compile(r"^\s*<[a-z][A-Za-z0-9_.:-]*(?:\s|/?>)")
INTERRUPTED_RE = re.compile(r"^\s*\[Request interrupted by user", re.IGNORECASE)

HARNESS_PREAMBLE_RES = (
    re.compile(r"^\s*#\s+\S+\.md instructions for\b", re.IGNORECASE),
    re.compile(r"^\s*<INSTRUCTIONS>"),
    re.compile(r"^\s*Another language model started to solve this problem\b", re.IGNORECASE),
    re.compile(r"^\s*This session is being continued from a previous\b", re.IGNORECASE),
)

CURSOR_SKIPPED_ROLES = {
    "system",
    "developer",
    "instruction",
    "instructions",
    "preamble",
}
CLAUDE_KNOWN_TYPES = {
    "user",
    "assistant",
    "system",
    "summary",
    "custom-title",
    "ai-title",
    "content-replacement",
    "progress",
    "file-history-snapshot",
    "attribution-snapshot",
    "queue-operation",
    "last-prompt",
    "tag",
    "agent-name",
    "agent-color",
    "agent-setting",
    "mode",
    "worktree-state",
    "context-collapse-commit",
    "context-collapse-snapshot",
    "permission-mode",
    "attachment",
    "file-history-delta",
}
QODER_KNOWN_TYPES = CLAUDE_KNOWN_TYPES | {
    "workspace-directories",
    "runtime-config",
}
COMMANDCODE_KNOWN_TYPES = {
    "session",
    "message",
    "model_change",
}
CODEX_SAFE_TOP_LEVEL = {
    "session_meta",
    "response_item",
    "compacted",
    "event_msg",
}
CODEX_IGNORED_TOP_LEVEL = {
    "turn_context",
    "world_state",
    "inter_agent_communication",
    "inter_agent_communication_metadata",
}
MAKA_KNOWN_TYPES = {
    "user",
    "assistant",
    "tool_call",
    "tool_result",
    "turn_state",
    "token_usage",
    "system_note",
    "permission_decision",
}
MAKA_CHUNK_MARKER = '{"$maka":"session-message-chunks-v1"}'


class ReaderError(RuntimeError):
    """An operator-facing session reader error."""


class AmbiguousReference(ReaderError):
    """A free-text reference matched more than one session."""

    def __init__(self, reference: str, matches: list[dict[str, Any]]):
        self.reference = reference
        self.matches = matches
        super().__init__(f"reference {reference!r} matched {len(matches)} sessions")


def _warning(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _add_warning(warnings: list[dict[str, str]], code: str, message: str) -> None:
    if not any(item["code"] == code and item["message"] == message for item in warnings):
        warnings.append(_warning(code, message))


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    output: list[str] = []
    for char in text:
        if char in ("\n", "\t"):
            output.append(char)
        elif unicodedata.category(char) in {"Cc", "Cs"}:
            output.append("\ufffd")
        else:
            output.append(char)
    return "".join(output)


def _one_line(value: Any, limit: int) -> str:
    text = " ".join(_safe_text(value).split())
    if limit < 1:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _json_preview(value: Any, limit: int) -> str:
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            raw = repr(value)
    return _one_line(raw, limit)


def _is_harness_preamble(text: str) -> bool:
    return any(pattern.match(text) for pattern in HARNESS_PREAMBLE_RES)


def _is_generated_meta_text(text: str) -> bool:
    return bool(
        GENERATED_META_RE.match(text)
        or INTERRUPTED_RE.match(text)
        or _is_harness_preamble(text)
    )


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, dict):
        return [content]
    return []


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "output", "content"):
            value = content.get(key)
            if isinstance(value, str):
                return value
    return ""


def _turn(
    role: str,
    *,
    text: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    tool_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "role": role,
        "text": _safe_text(text),
        "tool_calls": tool_calls or [],
        "tool_results": tool_results or [],
        "inert": True,
    }


def _assistant_action(turn: dict[str, Any]) -> str:
    if turn["text"]:
        return _one_line(turn["text"], 400)
    if turn["tool_calls"]:
        names = ", ".join(call.get("name") or "unknown" for call in turn["tool_calls"])
        return f"called inert foreign tool(s): {names}"
    if turn["tool_results"]:
        return "recorded inert foreign tool output"
    return ""


TOOL_PATH_PARAMS: dict[str, tuple[tuple[str, ...], bool]] = {
    "edit": (("file_path", "filePath", "path"), True),
    "multiedit": (("file_path", "filePath", "path"), True),
    "write": (("file_path", "filePath", "path"), True),
    "patch": (("file_path", "filePath", "path"), True),
    "notebookedit": (("notebook_path", "notebookPath"), True),
    "edit_file": (("target_file", "file_path", "filePath", "path"), True),
    "create_file": (("path", "file_path"), True),
    "write_file": (("path", "file_path", "filePath", "target_file"), True),
    "search_replace": (("file_path", "path", "target_file"), True),
    "str_replace": (("path", "file_path"), True),
    "str_replace_editor": (("path", "file_path"), True),
    "read": (("file_path", "filePath", "path"), False),
    "read_file": (("target_file", "file_path", "filePath", "absolutePath", "path"), False),
    "view": (("path", "file_path"), False),
    "view_file": (("path", "file_path"), False),
}

PATCH_TOOLS = {"apply_patch", "applypatch"}

SHELL_TOOLS = {
    "bash",
    "shell",
    "exec",
    "local_shell",
    "shell_command",
    "run_command",
    "run_terminal_cmd",
    "terminal",
    "powershell",
}

PLAN_TOOLS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "todowrite": ("todos", ("content", "activeForm", "task"), "status"),
    "todo_write": ("todos", ("content", "task"), "status"),
    "update_plan": ("plan", ("step", "content"), "status"),
}
TASK_LABEL_KEYS = ("activeForm", "description", "content", "title", "prompt")
PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$",
    re.MULTILINE,
)
COMMIT_MSG_RE = re.compile(
    r"git\s+commit\b[^\n]*?\s-[a-zA-Z]*m[a-zA-Z]*\s+"
    r"(\"((?:[^\"\\]|\\.)*)\"|'([^']*)')",
)
COMMIT_HEREDOC_RE = re.compile(
    r"git\s+commit\b[^\n]*?<<-?\s*['\"]?(\w+)['\"]?\n(.*?)^\1$",
    re.MULTILINE | re.DOTALL,
)
GIT_PUSH_RE = re.compile(r"git\s+push\b([^\n&|;]*)")
SHELL_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|\.{1,2}/|/)[\w./\\+-]*\.\w{1,8}")

AGENT_INTERNAL_PARTS = {
    ".agents",
    ".amp",
    ".claude",
    ".codex",
    ".commandcode",
    ".cursor",
    ".devin",
    ".git",
    ".grok",
    ".maka",
    ".opencode",
    ".zcode",
    ".venv",
    "__pycache__",
    "node_modules",
    "site-packages",
}
PLAN_STATUS_MARKS = {
    "completed": "x",
    "done": "x",
    "complete": "x",
    "in_progress": ">",
    "in-progress": ">",
    "active": ">",
    "pending": " ",
    "todo": " ",
    "not_started": " ",
    "cancelled": "-",
    "canceled": "-",
    "skipped": "-",
}


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _as_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


SHELL_COMMAND_KEYS = ("command", "cmd", "script", "shell_command", "input", "code")


def _shell_command_text(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    args = _as_dict(raw)
    for key in SHELL_COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, list):
            parts = [item for item in value if isinstance(item, str)]
            if parts:
                return parts[-1] if len(parts) > 1 else parts[0]
    return _as_text(raw)


def _first_string(source: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _normalize_base(cwd: str) -> str:
    base = _safe_text(cwd).strip().replace("\\", "/").rstrip("/")
    return base


def _normalize_path(raw: str, cwd: str | None) -> str | None:
    text = _safe_text(raw).strip().strip("\"'`")
    if not text or "\n" in text:
        return None
    if "\\\\" in text:
        text = text.replace("\\\\", "\\")
    text = text.replace("\\", "/").rstrip("/")
    if sys.platform == "win32":
        match = re.match(r"^/([A-Za-z])/(.*)$", text)
        if match:
            text = f"{match.group(1).upper()}:/{match.group(2)}"
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return None
    if any(part.casefold() in AGENT_INTERNAL_PARTS for part in parts):
        return None
    if cwd:
        base = _normalize_base(cwd)
        if base and text.casefold().startswith(base.casefold() + "/"):
            return text[len(base) + 1 :]
    return text


def _is_outside_cwd(path: str) -> bool:
    return path.startswith("/") or bool(re.match(r"^[A-Za-z]:/", path))


def _is_scratch(path: str) -> bool:
    lowered = path.casefold().split("/")
    return any(part in {"tmp", "temp", "scratchpad", ".cache"} for part in lowered)


class WorkIndex:
    """Accumulates file touches and plan state from raw tool calls."""

    def __init__(self) -> None:
        self._files: dict[str, dict[str, int]] = {}
        self._shell: dict[str, int] = {}
        self._plan: list[dict[str, str]] | None = None
        self._plan_source: str | None = None
        self._tasks: dict[str, dict[str, str]] = {}
        self._task_counter = 0
        self._commits: list[str] = []
        self._pushed = False

    def record(self, name: Any, raw: Any) -> None:
        if not isinstance(name, str) or not name.strip():
            return
        key = name.strip().casefold()
        key = key.rsplit(".", 1)[-1]
        args = _as_dict(raw)
        entry = TOOL_PATH_PARAMS.get(key)
        if entry is not None:
            params, mutates = entry
            value = _first_string(args, params)
            if value:
                self._touch(value, mutates)
        elif key in PATCH_TOOLS:
            payload = next(
                (
                    args.get(field)
                    for field in ("patchText", "patch_text", "patch", "input")
                    if isinstance(args.get(field), str)
                ),
                raw if isinstance(raw, str) else "",
            )
            for match in PATCH_PATH_RE.findall(payload):
                self._touch(next((value for value in match if value), ""), True)
        elif key in SHELL_TOOLS:
            command = _shell_command_text(raw)
            for match in SHELL_PATH_RE.findall(command)[:8]:
                self._shell[match] = self._shell.get(match, 0) + 1
            self._record_git(command)

        spec = PLAN_TOOLS.get(key)
        if spec is not None:
            list_key, label_keys, status_key = spec
            items = args.get(list_key)
            if isinstance(items, list):
                plan = [
                    {
                        "status": str(item.get(status_key) or "pending"),
                        "label": _first_string(item, label_keys),
                    }
                    for item in items
                    if isinstance(item, dict)
                ]
                if plan:
                    self._plan = plan
                    self._plan_source = name.strip()
        elif key == "taskcreate" or key == "task_create":
            items = args.get("tasks")
            if isinstance(items, list):
                self._tasks = {
                    str(index): {
                        "status": "pending",
                        "label": _first_string(item, ("subject", "task", "content", "title")),
                    }
                    for index, item in enumerate(items, self._task_counter + 1)
                    if isinstance(item, dict)
                }
                self._task_counter += len(items)
            else:
                self._task_counter += 1
                self._tasks[str(self._task_counter)] = {
                    "status": "pending",
                    "label": _first_string(args, TASK_LABEL_KEYS),
                }
            self._plan_source = "TaskCreate/TaskUpdate"
        elif key == "taskupdate" or key == "task_update":
            raw_id = str(args.get("id") or args.get("taskId") or args.get("task_id") or "")
            task = self._tasks.get(raw_id)
            if task is None and raw_id[:1].upper() == "T" and raw_id[1:].isdigit():
                task = self._tasks.get(raw_id[1:])
            if task is not None:
                status = args.get("status")
                if isinstance(status, str) and status:
                    task["status"] = status
                label = _first_string(args, TASK_LABEL_KEYS)
                if label and not task["label"]:
                    task["label"] = label

    def _record_git(self, command: str) -> None:
        for match in COMMIT_MSG_RE.finditer(command):
            message = match.group(2) if match.group(2) is not None else match.group(3)
            subject = _one_line((message or "").replace('\\"', '"'), 200)
            if subject and subject not in self._commits:
                self._commits.append(subject)
        for match in COMMIT_HEREDOC_RE.finditer(command):
            body = match.group(2) or ""
            subject = next(
                (_one_line(line, 200) for line in body.split("\n") if line.strip()), ""
            )
            if subject and subject not in self._commits:
                self._commits.append(subject)
        if not self._pushed:
            for match in GIT_PUSH_RE.finditer(command):
                if "--dry-run" not in match.group(1):
                    self._pushed = True
                    break

    def _touch(self, raw_path: str, mutates: bool) -> None:
        counts = self._files.setdefault(raw_path, {"write": 0, "read": 0})
        counts["write" if mutates else "read"] += 1

    def files(self, cwd: str | None) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}

        def slot(path: str) -> dict[str, Any]:
            return merged.setdefault(
                path, {"path": path, "write": 0, "read": 0, "mentioned": 0}
            )

        for raw, counts in self._files.items():
            path = _normalize_path(raw, cwd)
            if path is None:
                continue
            entry = slot(path)
            entry["write"] += counts["write"]
            entry["read"] += counts["read"]
        for raw, count in self._shell.items():
            path = _normalize_path(raw, cwd)
            if path is None or _is_outside_cwd(path):
                continue
            slot(path)["mentioned"] += count
        return sorted(
            merged.values(),
            key=lambda item: (
                1 if _is_scratch(item["path"]) else 0,
                0 if item["write"] else 1,
                -item["write"],
                -item["read"],
                -item["mentioned"],
                item["path"],
            ),
        )

    def git_activity(self) -> dict[str, Any] | None:
        if not self._commits and not self._pushed:
            return None
        return {"commits": list(self._commits), "pushed": self._pushed}

    def plan(self) -> dict[str, Any] | None:
        items = self._plan
        if items is None and self._tasks:
            items = [dict(task) for task in self._tasks.values()]
        if not items:
            return None
        return {"source": self._plan_source or "plan", "items": items}


def _finalize_result(
    result: dict[str, Any], index: WorkIndex | None = None
) -> dict[str, Any]:
    turns = result.setdefault("turns", [])
    warnings = result.setdefault("warnings", [])
    result["files_touched"] = index.files(result.get("cwd")) if index else []
    result["plan_state"] = index.plan() if index else None
    result["git_activity"] = index.git_activity() if index else None
    result["last_user_request"] = next(
        (
            _one_line(turn["text"], 400)
            for turn in reversed(turns)
            if turn["role"] == "user" and turn["text"]
        ),
        None,
    )
    result["last_assistant_action"] = next(
        (
            action
            for turn in reversed(turns)
            if turn["role"] == "assistant"
            for action in [_assistant_action(turn)]
            if action
        ),
        None,
    )
    result["warnings"] = sorted(warnings, key=lambda item: (item["code"], item["message"]))
    for field in (
        "title",
        "cwd",
        "branch",
        "created_at",
        "updated_at",
        "source_repo_root_path",
        "prior_context",
    ):
        result.setdefault(field, None)
    return result


def _timestamp_sort_key(record: dict[str, Any], index: int) -> tuple[str, int]:
    timestamp = record.get("timestamp")
    return (timestamp if isinstance(timestamp, str) else "", index)


def _timestamp_to_millis(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return number * 1000 if abs(number) < 1_000_000_000_000 else number
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _iso_from_millis(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _mtime_millis(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0


def _within(updated_at_ms: int, within_min: int, now_ms: int | None = None) -> bool:
    if within_min <= 0:
        return True
    now = int(time.time() * 1000) if now_ms is None else now_ms
    return 0 <= now - updated_at_ms <= within_min * 60 * 1000


def slugify(cwd: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in cwd)


class _SqliteReadonlyContext:
    """Context manager wrapper ensuring read-only SQLite connections are closed."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        return self._connection

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._connection.close()


def _open_sqlite_readonly(path: Path) -> _SqliteReadonlyContext:
    try:
        database = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        database.execute("PRAGMA query_only = ON")
        return _SqliteReadonlyContext(database)
    except (OSError, sqlite3.Error) as exc:
        raise ReaderError(f"failed to open SQLite store {path}: {exc}") from exc


# How much of a recovered compaction summary the digest keeps. Long enough for
# the goal, the state, and the open tasks a real summary records; short enough
# that it cannot dominate the digest it is one section of.
PRIOR_CONTEXT_CHARS = 4000

# A session updated this recently may still have an agent attached to it,
# writing to the same working tree. Resuming it is a race, not a handoff.
LIVE_SESSION_MINUTES = 5


def _table_columns(database: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in database.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _paths_match(a: str | None, b: str) -> bool:
    """Compare two paths, case-insensitively on Windows.

    Tolerates the Windows ``\\\\?\\`` long-path device prefix and mixed
    forward/back slashes.
    """
    if not a:
        return False

    def normalized(value: str) -> str:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[len("\\\\?\\UNC\\"):]
        elif value.startswith("\\\\?\\"):
            value = value[len("\\\\?\\"):]
        return os.path.normpath(value)

    na = normalized(a)
    nb = normalized(b)
    if os.name == "nt":
        return na.lower() == nb.lower()
    return na == nb


def _read_plain_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    skipped = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
                else:
                    skipped += 1
    except OSError as exc:
        raise ReaderError(f"failed to read JSONL transcript {path}: {exc}") from exc
    return records, skipped
