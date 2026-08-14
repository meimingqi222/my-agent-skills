import gc
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

import reader_core as rc
import reader_discovery as rd
import reader_render as rr
import session_reader as sr
from readers.amp import read_amp_session
from readers.claude import read_claude_session
from readers.codex import read_codex_session
from readers.commandcode import read_commandcode_session
from readers.cursor import read_cursor_session
from readers.devin import read_devin_cli_session, read_devin_next_session
from readers.grok import read_grok_session
from readers.maka import read_maka_session
from readers.opencode import read_opencode_session, read_zcode_session
from readers.qoder import read_qoder_session


class TestSessionReaderSuite(unittest.TestCase):
    def test_agent_internal_parts(self):
        for part in (".maka", ".claude", ".codex", ".cursor", ".devin", ".grok", ".opencode", ".zcode", ".amp", ".commandcode"):
            self.assertIn(part, sr.AGENT_INTERNAL_PARTS)
            self.assertIn(part, rc.AGENT_INTERNAL_PARTS)

    def test_normalize_path(self):
        cwd = "D:/code/my-project"
        self.assertIsNone(sr._normalize_path("D:/code/my-project/.maka/runtime.sqlite", cwd))
        self.assertIsNone(sr._normalize_path("D:/code/my-project/.claude/settings.json", cwd))
        self.assertIsNone(sr._normalize_path("D:/code/my-project/.cursor/store.db", cwd))
        self.assertEqual(sr._normalize_path("D:/code/my-project/src/main.py", cwd), "src/main.py")
        self.assertEqual(sr._normalize_path("src/utils.py", cwd), "src/utils.py")

    def test_tool_aliases(self):
        self.assertEqual(sr.TOOL_ALIASES.get("maka-agent"), "maka")
        self.assertEqual(sr.TOOL_ALIASES.get("command-code"), "commandcode")
        self.assertEqual(sr.TOOL_ALIASES.get("cc"), "commandcode")
        self.assertEqual(sr.TOOL_ALIASES.get("grok-build"), "grok")
        self.assertEqual(sr.TOOL_ALIASES.get("grokbuild"), "grok")
        self.assertEqual(sr.TOOL_ALIASES.get("z-code"), "zcode")

    def test_work_index_and_patch_extraction(self):
        index = sr.WorkIndex()
        index.record("read_file", {"path": "src/a.py"})
        index.record("write_file", {"path": "src/b.py"})
        index.record("apply_patch", {"patchText": "*** Update File: src/c.py\n@@ -1 +1 @@\n-old\n+new"})
        index.record("todowrite", {"todos": [{"content": "Task 1", "status": "completed"}, {"content": "Task 2", "status": "pending"}]})
        index.record("bash", {"command": 'git commit -m "feat: test commit" && git push'})
        files = index.files("D:/code/my-project")
        self.assertTrue(any(f["path"] == "src/a.py" and f["read"] > 0 for f in files))
        self.assertTrue(any(f["path"] == "src/b.py" and f["write"] > 0 for f in files))
        self.assertTrue(any(f["path"] == "src/c.py" and f["write"] > 0 for f in files))
        git = index.git_activity()
        self.assertIsNotNone(git)
        self.assertEqual(len(git["commits"]), 1)
        self.assertTrue(git["pushed"])
        plan = index.plan()
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan["items"]), 2)

    def test_cli_list_any_buffered_output(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_code = sr.main(["any", "list", "--json", "--cwd", str(SKILL_DIR)])
        self.assertEqual(rc_code, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn("sessions", payload)
        self.assertEqual(payload.get("tool"), "any")

    def test_claude_parent_chain_and_fork_discard(self):
        """Test Claude graph resolution when a branch was rewound/forked."""
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "00000000-0000-0000-0000-000000000001.jsonl"
            records = [
                {"type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-08-14T00:00:00Z", "message": {"role": "user", "content": "initial"}},
                # Abandoned fork:
                {"type": "assistant", "uuid": "a_old", "parentUuid": "u1", "timestamp": "2026-08-14T00:00:01Z", "message": {"role": "assistant", "content": "abandoned response"}},
                {"type": "user", "uuid": "u_old", "parentUuid": "a_old", "timestamp": "2026-08-14T00:00:02Z", "message": {"role": "user", "content": "abandoned follow up"}},
                # Active fork (later timestamp):
                {"type": "assistant", "uuid": "a_new", "parentUuid": "u1", "timestamp": "2026-08-14T00:00:03Z", "message": {"role": "assistant", "content": "active response"}},
                {"type": "user", "uuid": "u_new", "parentUuid": "a_new", "timestamp": "2026-08-14T00:00:04Z", "message": {"role": "user", "content": "active follow up"}},
            ]
            with session_file.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            res = read_claude_session(session_file)
            texts = [t["text"] for t in res["turns"]]
            self.assertIn("initial", texts)
            self.assertIn("active response", texts)
            self.assertIn("active follow up", texts)
            self.assertNotIn("abandoned response", texts)
            self.assertNotIn("abandoned follow up", texts)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_commandcode_branch_discard_and_compaction(self):
        """Test CommandCode rewound branch discard and auto-compaction summary extraction."""
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "00000000-0000-0000-0000-000000000003.jsonl"
            records = [
                {"type": "session", "id": "s1", "cwd": td},
                # Compaction summary under user role:
                {"type": "message", "id": "m0", "parentId": "s1", "metadata": {"isSummary": True}, "message": {"role": "user", "content": [{"type": "text", "text": "Summary of prior work"}]}},
                {"type": "message", "id": "m1", "parentId": "m0", "timestamp": "2026-08-14T00:00:01Z", "message": {"role": "user", "content": [{"type": "text", "text": "user step 1"}]}},
                # Dead branch:
                {"type": "message", "id": "m_dead", "parentId": "m1", "timestamp": "2026-08-14T00:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "rewound work"}]}},
                # Live branch:
                {"type": "message", "id": "m2", "parentId": "m1", "timestamp": "2026-08-14T00:00:03Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "live work"}]}},
            ]
            with session_file.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            res = read_commandcode_session(session_file)
            self.assertEqual(res["tool"], "commandcode")
            # Compaction text moved to prior_context:
            self.assertIsNotNone(res["prior_context"])
            self.assertIn("Summary of prior work", res["prior_context"]["text"])
            # Live thread contains live work, not rewound work:
            texts = [t["text"] for t in res["turns"]]
            self.assertIn("live work", texts)
            self.assertNotIn("rewound work", texts)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_harness_preamble_filtering(self):
        """Test system instructions or injected preambles are filtered out from user queries."""
        preambles = [
            "# AGENTS.md instructions for project\nDo X",
            "<INSTRUCTIONS>\nSome system prompt\n</INSTRUCTIONS>",
            "Another language model started to solve this problem...",
            "[Request interrupted by user]",
        ]
        for p in preambles:
            self.assertTrue(sr._is_generated_meta_text(p))

    def test_maka_session_reader(self):
        td = tempfile.mkdtemp()
        try:
            db_path = Path(td) / "runtime.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE session_metadata (session_id TEXT PRIMARY KEY, title TEXT, payload_json TEXT, "
                "created_at INTEGER, last_used_at INTEGER, last_message_at INTEGER)"
            )
            conn.execute(
                "CREATE TABLE session_metadata_tombstones (session_id TEXT PRIMARY KEY)"
            )
            conn.execute(
                "CREATE TABLE session_messages (session_id TEXT, sequence INTEGER, message_type TEXT, "
                "message_ts INTEGER, record_json TEXT)"
            )
            header = json.dumps({"name": "Test Maka Session", "cwd": td})
            msg1 = json.dumps({"type": "user", "text": "hello maka"})
            msg2 = json.dumps({"type": "assistant", "text": "I will help you"})
            msg3 = json.dumps({"type": "tool_call", "toolName": "read_file", "args": {"path": "test.txt"}})
            conn.execute(
                "INSERT INTO session_metadata VALUES ('s1', 'Test Maka Session', ?, 1000, 2000, 2000)",
                (header,)
            )
            conn.execute("INSERT INTO session_messages VALUES ('s1', 1, 'user', 1000, ?)", (msg1,))
            conn.execute("INSERT INTO session_messages VALUES ('s1', 2, 'assistant', 2000, ?)", (msg2,))
            conn.execute("INSERT INTO session_messages VALUES ('s1', 3, 'tool_call', 2000, ?)", (msg3,))
            conn.commit()
            conn.close()

            res = read_maka_session(db_path, "s1")
            self.assertEqual(res["tool"], "maka")
            self.assertEqual(res["session_id"], "s1")
            self.assertEqual(len(res["turns"]), 2)
            digest = sr.build_digest(res)
            self.assertEqual(digest["turns_total"], 2)
            rendered = sr.render_digest(res)
            self.assertIn("Test Maka Session", rendered)
        finally:
            gc.collect()
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_amp_session_reader(self):
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "thread.json"
            thread = {
                "id": "amp-1",
                "title": "Amp Session",
                "created": 1000000,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "do work"}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "done work"}]}
                ]
            }
            session_file.write_text(json.dumps(thread), encoding="utf-8")
            res = read_amp_session(session_file)
            self.assertEqual(res["tool"], "amp")
            self.assertEqual(res["session_id"], "amp-1")
            self.assertEqual(len(res["turns"]), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_qoder_session_reader(self):
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "00000000-0000-0000-0000-000000000011.jsonl"
            records = [
                {"type": "user", "uuid": "u1", "parentUuid": None, "message": {"role": "user", "content": "qoder task"}},
                {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "message": {"role": "assistant", "content": "qoder reply"}},
            ]
            with session_file.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            res = read_qoder_session(session_file)
            self.assertEqual(res["tool"], "qoder")
            self.assertEqual(len(res["turns"]), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_codex_session_reader(self):
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "rollout-2026-08-14T00-00-00-00000000-0000-0000-0000-000000000002.jsonl"
            records = [
                {"type": "session_meta", "payload": {"id": "00000000-0000-0000-0000-000000000002", "source": "cli"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "text", "text": "test codex"}]}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "codex response"}]}},
            ]
            with session_file.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            res = read_codex_session(session_file)
            self.assertEqual(res["tool"], "codex")
            self.assertEqual(len(res["turns"]), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_grok_session_reader(self):
        td = tempfile.mkdtemp()
        try:
            session_dir = Path(td) / "grok_session_1"
            session_dir.mkdir()
            summary = {
                "info": {"id": "grok-1", "cwd": td},
                "generated_title": "Grok Session Test",
                "last_active_at": 1786000000000,
            }
            (session_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            records = [
                {"type": "user", "content": [{"type": "text", "text": "<user_query>grok prompt</user_query>"}]},
                {"type": "assistant", "content": [{"type": "text", "text": "grok answer"}]},
            ]
            with (session_dir / "chat_history.jsonl").open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            res = read_grok_session(session_dir)
            self.assertEqual(res["tool"], "grok")
            self.assertEqual(res["session_id"], "grok-1")
            self.assertEqual(len(res["turns"]), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_opencode_and_zcode_reader(self):
        td = tempfile.mkdtemp()
        try:
            db_path = Path(td) / "opencode.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, "
                "time_created INTEGER, time_updated INTEGER, agent TEXT, model TEXT, task_type TEXT, time_archived INTEGER)"
            )
            conn.execute(
                "CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)"
            )
            conn.execute(
                "INSERT INTO session VALUES ('ses_1', ?, 'Opencode Title', 1000, 2000, 'agent', 'model', NULL, 0)",
                (td,)
            )
            msg1 = json.dumps({"role": "user"})
            part1 = json.dumps({"type": "text", "text": "opencode query"})
            msg2 = json.dumps({"role": "assistant"})
            part2 = json.dumps({"type": "text", "text": "opencode answer"})
            conn.execute("INSERT INTO message VALUES ('m1', 'ses_1', 1000, ?)", (msg1,))
            conn.execute("INSERT INTO part VALUES ('p1', 'm1', 'ses_1', 1000, ?)", (part1,))
            conn.execute("INSERT INTO message VALUES ('m2', 'ses_1', 2000, ?)", (msg2,))
            conn.execute("INSERT INTO part VALUES ('p2', 'm2', 'ses_1', 2000, ?)", (part2,))
            conn.commit()
            conn.close()

            res = read_opencode_session(db_path, "ses_1")
            self.assertEqual(res["tool"], "opencode")
            self.assertEqual(len(res["turns"]), 2)

            res_z = read_zcode_session(db_path, "ses_1")
            self.assertEqual(res_z["tool"], "zcode")
            self.assertEqual(len(res_z["turns"]), 2)
        finally:
            gc.collect()
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_devin_cli_reader(self):
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "devin-cli-1.json"
            data = {
                "session_id": "devin-cli-1",
                "steps": [
                    {"source": "user", "message": "hello devin", "timestamp": "2026-08-14T00:00:00Z"},
                    {"source": "agent", "message": "hello user", "timestamp": "2026-08-14T00:00:01Z"}
                ]
            }
            session_file.write_text(json.dumps(data), encoding="utf-8")
            res = read_devin_cli_session(session_file)
            self.assertEqual(res["tool"], "devin")
            self.assertEqual(res["source"], "devin-cli")
            self.assertEqual(len(res["turns"]), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_cursor_reader(self):
        td = tempfile.mkdtemp()
        try:
            transcript = Path(td) / "cursor_session.jsonl"
            records = [
                {"role": "user", "content": [{"type": "text", "text": "cursor user query"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "cursor assistant answer"}]},
            ]
            with transcript.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            candidate = {
                "tool": "cursor",
                "source": "cursor-transcript",
                "session_id": "cur-1",
                "path": str(transcript),
                "title": "Cursor Test",
                "cwd": td,
                "updated_at_ms": 1786000000000,
            }
            res = read_cursor_session(candidate)
            self.assertEqual(res["tool"], "cursor")
            self.assertEqual(len(res["turns"]), 2)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_render_human_and_digest(self):
        sample_result = {
            "tool": "claude",
            "source": "claude-code",
            "session_id": "test-id",
            "title": "Test Title",
            "cwd": "D:/test",
            "branch": "main",
            "updated_at": "2026-08-14T00:00:00+00:00",
            "path": "D:/test/file.jsonl",
            "turns": [
                {"role": "user", "text": "do something", "tool_calls": [], "tool_results": []},
                {"role": "assistant", "text": "did it", "tool_calls": [], "tool_results": []},
            ],
            "warnings": [],
        }
        human = rr.render_human(sample_result)
        digest = rr.render_digest(sample_result)
        self.assertIn("INERT FOREIGN HISTORY", human)
        self.assertIn("INERT FOREIGN HISTORY", digest)
        self.assertIn("Test Title", human)
        self.assertIn("Test Title", digest)


if __name__ == "__main__":
    unittest.main()
