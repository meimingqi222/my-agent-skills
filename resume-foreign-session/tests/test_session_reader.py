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
from readers.opencode import read_opencode_session, read_opencode2_session, read_zcode_session
from readers.pi import read_pi_session
from readers.dsh import read_dsh_session
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

    def test_opencode2_reader(self):
        """Test opencode v2 reader (session_v2 + session_message schema)."""
        td = tempfile.mkdtemp()
        try:
            db_path = Path(td) / "opencode.db"
            conn = sqlite3.connect(db_path)
            # v2 schema: session_v2 + session_message + todo
            conn.execute(
                "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                "title TEXT, time_created INTEGER, time_updated INTEGER, "
                "time_archived INTEGER, agent TEXT, model TEXT, parent_id TEXT, "
                "version TEXT)"
            )
            conn.execute(
                "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, "
                "type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, "
                "data TEXT)"
            )
            conn.execute(
                "CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT, "
                "priority TEXT, position INTEGER, time_created INTEGER, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO session_v2 VALUES ('ses_v2_1', ?, 'V2 Title', 1000, 2000, "
                "NULL, 'build', '{\"id\":\"test-model\",\"providerID\":\"opencode\"}', NULL, '0.0.0-beta')",
                (td,)
            )
            # user message: text at top level
            msg1_data = json.dumps({"time": {"created": 1000}, "text": "v2 user query"})
            # assistant message: content array with text + tool parts
            msg2_data = json.dumps({
                "time": {"created": 2000, "completed": 2001},
                "agent": "build",
                "model": {"id": "test-model", "providerID": "opencode"},
                "content": [
                    {"type": "text", "text": "v2 assistant reply"},
                    {
                        "type": "tool",
                        "id": "call_1",
                        "name": "edit",
                        "state": {
                            "status": "completed",
                            "input": {"path": "src/main.rs", "oldString": "old", "newString": "new"},
                            "content": [{"type": "text", "text": "File updated"}],
                        },
                    },
                ],
            })
            conn.execute("INSERT INTO session_message VALUES ('m1', 'ses_v2_1', 'user', 0, 1000, 1000, ?)", (msg1_data,))
            conn.execute("INSERT INTO session_message VALUES ('m2', 'ses_v2_1', 'assistant', 1, 2000, 2001, ?)", (msg2_data,))
            # todo entry
            conn.execute("INSERT INTO todo VALUES ('ses_v2_1', 'Fix bug', 'completed', 'high', 0, 1000, 1000)")
            conn.commit()
            conn.close()

            res = read_opencode2_session(db_path, "ses_v2_1")
            self.assertEqual(res["tool"], "opencode2")
            self.assertEqual(res["source"], "opencode-v2")
            self.assertEqual(res["session_id"], "ses_v2_1")
            self.assertEqual(res["title"], "V2 Title")
            self.assertEqual(res["cwd"], td)
            self.assertEqual(res["model"], "test-model")
            self.assertEqual(res["agent"], "build")
            # 2 turns: user + assistant
            self.assertEqual(len(res["turns"]), 2)
            # user turn
            self.assertEqual(res["turns"][0]["role"], "user")
            self.assertIn("v2 user query", res["turns"][0]["text"])
            # assistant turn with tool call
            self.assertEqual(res["turns"][1]["role"], "assistant")
            self.assertIn("v2 assistant reply", res["turns"][1]["text"])
            self.assertEqual(len(res["turns"][1]["tool_calls"]), 1)
            self.assertEqual(res["turns"][1]["tool_calls"][0]["name"], "edit")
            # tool result extracted from state.content
            self.assertEqual(len(res["turns"][1]["tool_results"]), 1)
            self.assertIn("File updated", res["turns"][1]["tool_results"][0]["content"])
            # files touched via WorkIndex
            files = res.get("files_touched", [])
            self.assertTrue(any("src/main.rs" in f.get("path", "") for f in files))
            # plan state from todo table
            plan = res.get("plan_state")
            self.assertIsNotNone(plan)
            self.assertTrue(any("Fix bug" in item.get("label", "") for item in plan["items"]))
            # digest renders
            digest = sr.build_digest(res)
            self.assertGreaterEqual(digest["turns_total"], 2)
            rendered = sr.render_digest(res)
            self.assertIn("V2 Title", rendered)
        finally:
            gc.collect()
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_opencode2_reader_error_and_compaction(self):
        """Test opencode v2 reader handles error states and compaction markers."""
        td = tempfile.mkdtemp()
        try:
            db_path = Path(td) / "opencode.db"
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, directory TEXT, "
                "title TEXT, time_created INTEGER, time_updated INTEGER, "
                "time_archived INTEGER, agent TEXT, model TEXT, parent_id TEXT, "
                "version TEXT)"
            )
            conn.execute(
                "CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT, "
                "type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER, "
                "data TEXT)"
            )
            conn.execute(
                "INSERT INTO session_v2 VALUES ('ses_v2_2', ?, 'Error Test', 1000, 2000, "
                "NULL, 'build', NULL, NULL, '0.0.0-beta')",
                (td,)
            )
            # user message
            msg1 = json.dumps({"time": {"created": 1000}, "text": "do something"})
            # assistant with a failed tool call (error in state)
            msg2 = json.dumps({
                "time": {"created": 2000, "completed": 2001},
                "content": [
                    {
                        "type": "tool",
                        "id": "call_err",
                        "name": "read",
                        "state": {
                            "status": "error",
                            "input": {"path": "missing.rs"},
                            "error": {"type": "unknown", "message": "File not found"},
                        },
                    },
                ],
            })
            # compaction marker
            msg3 = json.dumps({"time": {"created": 1500}})
            # synthetic message (should be dropped)
            msg4 = json.dumps({"time": {"created": 1600}, "text": "harness injected"})
            conn.execute("INSERT INTO session_message VALUES ('m1', 'ses_v2_2', 'user', 0, 1000, 1000, ?)", (msg1,))
            conn.execute("INSERT INTO session_message VALUES ('m2', 'ses_v2_2', 'assistant', 1, 2000, 2001, ?)", (msg2,))
            conn.execute("INSERT INTO session_message VALUES ('m3', 'ses_v2_2', 'compaction', 2, 1500, 1500, ?)", (msg3,))
            conn.execute("INSERT INTO session_message VALUES ('m4', 'ses_v2_2', 'synthetic', 3, 1600, 1600, ?)", (msg4,))
            conn.commit()
            conn.close()

            res = read_opencode2_session(db_path, "ses_v2_2")
            self.assertEqual(res["tool"], "opencode2")
            # 2 turns: user + assistant (compaction and synthetic dropped)
            self.assertEqual(len(res["turns"]), 2)
            # tool result is an error
            assistant_turn = res["turns"][1]
            self.assertEqual(len(assistant_turn["tool_results"]), 1)
            self.assertTrue(assistant_turn["tool_results"][0]["is_error"])
            self.assertIn("File not found", assistant_turn["tool_results"][0]["content"])
            # warnings: history_compacted + harness_text_dropped
            warning_codes = {w["code"] for w in res["warnings"]}
            self.assertIn("history_compacted", warning_codes)
            self.assertIn("harness_text_dropped", warning_codes)
        finally:
            gc.collect()
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_pi_reader(self):
        """Test pi (pi-coding-agent) JSONL session reader."""
        td = tempfile.mkdtemp()
        try:
            session_file = Path(td) / "2026-08-21T08-56-34-036Z_test-pi-session.jsonl"
            records = [
                {"type": "session", "version": 3, "id": "test-pi-session", "timestamp": "2026-08-21T08:56:34.036Z", "cwd": td},
                {"type": "model_change", "id": "m1", "parentId": None, "timestamp": "2026-08-21T08:56:34.099Z", "provider": "openrouter", "modelId": "test-model"},
                {"type": "thinking_level_change", "id": "t1", "parentId": "m1", "timestamp": "2026-08-21T08:56:34.099Z", "thinkingLevel": "medium"},
                {"type": "message", "id": "u1", "parentId": "t1", "timestamp": "2026-08-21T08:57:23.443Z", "message": {"role": "user", "content": [{"type": "text", "text": "hello pi"}], "timestamp": 1787302643439}},
                {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "2026-08-21T08:57:25.000Z", "message": {"role": "assistant", "content": [
                    {"type": "thinking", "thinking": "user said hello"},
                    {"type": "text", "text": "Hi! I'm pi."},
                    {"type": "toolCall", "id": "call-1", "name": "bash", "arguments": {"command": "ls -la"}},
                ]}},
                {"type": "message", "id": "r1", "parentId": "a1", "timestamp": "2026-08-21T08:57:26.000Z", "message": {"role": "toolResult", "toolCallId": "call-1", "toolName": "bash", "content": [{"type": "text", "text": "total 0\ndrwxr-xr-x 2 user staff 64"}]}},
                {"type": "message", "id": "a2", "parentId": "r1", "timestamp": "2026-08-21T08:57:27.000Z", "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "Done!"},
                ]}},
            ]
            with session_file.open("w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

            res = read_pi_session(session_file)
            self.assertEqual(res["tool"], "pi")
            self.assertEqual(res["source"], "pi-coding-agent")
            self.assertEqual(res["session_id"], "test-pi-session")
            self.assertEqual(res["cwd"], td)
            self.assertEqual(res["model"], "test-model")
            # 3 turns: user + 2 assistant (toolResult 附加到 assistant)
            self.assertEqual(len(res["turns"]), 3)
            # user turn
            self.assertEqual(res["turns"][0]["role"], "user")
            self.assertIn("hello pi", res["turns"][0]["text"])
            # first assistant turn with tool call + result
            self.assertEqual(res["turns"][1]["role"], "assistant")
            self.assertIn("Hi! I'm pi.", res["turns"][1]["text"])
            self.assertEqual(len(res["turns"][1]["tool_calls"]), 1)
            self.assertEqual(res["turns"][1]["tool_calls"][0]["name"], "bash")
            # tool result attached
            self.assertEqual(len(res["turns"][1]["tool_results"]), 1)
            self.assertIn("total 0", res["turns"][1]["tool_results"][0]["content"])
            # second assistant turn (no tools)
            self.assertEqual(res["turns"][2]["role"], "assistant")
            self.assertIn("Done!", res["turns"][2]["text"])
            # thinking records skipped -> warning
            warning_codes = {w["code"] for w in res["warnings"]}
            self.assertIn("unsafe_records_skipped", warning_codes)
            # digest renders
            digest = sr.build_digest(res)
            self.assertGreaterEqual(digest["turns_total"], 3)
            rendered = sr.render_digest(res)
            self.assertIn("pi", rendered.lower())
        finally:
            gc.collect()
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_dsh_reader(self):
        """Test DSH (DeepSeek Harness) zstd-compressed JSONL session reader."""
        try:
            import zstandard
        except ImportError:
            self.skipTest("zstandard not available")
        td = tempfile.mkdtemp()
        try:
            session_dir = Path(td) / "session-test-dsh-uuid"
            session_dir.mkdir()
            session_file = session_dir / "session.jsonl.zstd"
            records = [
                {"type": "session", "version": 0, "id": "session-test-dsh-uuid", "createdAt": 1787305966585, "cwd": td, "delegationDepth": 0, "agentPreset": "standard"},
                {"type": "permission/preset", "seq": 0, "time": 1787305966706, "data": {"preset": "workspace-write", "origin": "default"}},
                {"type": "session/title", "seq": 11, "time": 1787306265633, "data": {"title": "测试 DSH", "source": {"kind": "fallback"}}},
                {"type": "user/message", "seq": 7, "time": 1787306087428, "data": {"content": [{"type": "text", "text": "你好"}], "source": {"kind": "user"}, "role": "user", "id": "u1"}},
                {"type": "user/message", "seq": 8, "time": 1787306087429, "data": {"content": [{"type": "text", "text": "system context"}], "source": {"kind": "plugin"}, "role": "user", "id": "u2"}},
                {"type": "assistant/message", "seq": 291, "time": 1787306091847, "data": {"turn": 1, "step": 1, "message": {"role": "assistant", "content": [
                    {"type": "reasoning", "text": "user said hello"},
                    {"type": "text", "text": "你好！我是 DSH。"},
                ], "source": {"kind": "model", "model": "deepseek-v4-flash"}}}},
                {"type": "tool/call", "seq": 398, "time": 1787306298999, "data": {"turn": 3, "step": 1, "callId": "call-1", "name": "web_search", "arguments": '{"queries":["test query"]}'}},
                {"type": "tool/result", "seq": 399, "time": 1787306299011, "data": {"turn": 3, "step": 1, "message": {"source": {"kind": "tool", "callId": "call-1"}, "content": [
                    {"type": "tool-result", "toolCallId": "call-1", "content": [{"type": "text", "text": "Search results here"}], "isError": False},
                ], "role": "user", "id": "r1"}}},
            ]
            # 写入 zstd 压缩的 JSONL
            jsonl_content = "\n".join(json.dumps(r) for r in records) + "\n"
            cctx = zstandard.ZstdCompressor()
            compressed = cctx.compress(jsonl_content.encode("utf-8"))
            session_file.write_bytes(compressed)

            res = read_dsh_session(session_file)
            self.assertEqual(res["tool"], "dsh")
            self.assertEqual(res["source"], "deepseek-harness")
            self.assertEqual(res["session_id"], "session-test-dsh-uuid")
            self.assertEqual(res["cwd"], td)
            self.assertEqual(res["title"], "测试 DSH")
            self.assertEqual(res["model"], "deepseek-v4-flash")
            # 3 turns: user (plugin 消息被跳过) + assistant + tool/call
            self.assertEqual(len(res["turns"]), 3)
            # user turn
            self.assertEqual(res["turns"][0]["role"], "user")
            self.assertIn("你好", res["turns"][0]["text"])
            # assistant turn
            self.assertEqual(res["turns"][1]["role"], "assistant")
            self.assertIn("你好！我是 DSH。", res["turns"][1]["text"])
            # tool/call turn with result
            self.assertEqual(res["turns"][2]["role"], "assistant")
            self.assertEqual(len(res["turns"][2]["tool_calls"]), 1)
            self.assertEqual(res["turns"][2]["tool_calls"][0]["name"], "web_search")
            self.assertEqual(len(res["turns"][2]["tool_results"]), 1)
            self.assertIn("Search results", res["turns"][2]["tool_results"][0]["content"])
            # plugin message dropped -> warning
            warning_codes = {w["code"] for w in res["warnings"]}
            self.assertIn("harness_text_dropped", warning_codes)
            # digest renders
            digest = sr.build_digest(res)
            self.assertGreaterEqual(digest["turns_total"], 3)
            rendered = sr.render_digest(res)
            self.assertIn("DSH", rendered)
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
