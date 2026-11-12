"""R11 and R15: no curation is started or finished while background work runs; the handover."""
import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path

import _common as C
from _conv import tools_dense

T = C.T


def now_iso(offset=0.0):
    t = time.time() + offset
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1000):03d}Z"


def run(fn, *a, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*a, **kw)
    s = out.getvalue()
    try:
        return json.loads(s) if s.strip().startswith("{") else s
    except Exception:
        return s


SUB = {"id": "a0b1c2d3e4f5a6b7c", "type": "subagent", "status": "running", "description": "quiet sleeper",
       "agent_type": "general-purpose"}
SHELL = {"id": "bq7x2kd9m", "type": "shell", "status": "running", "description": "sleep forty",
         "command": "sleep 40; echo bash-done"}
INTERNAL = {"id": "dr1", "type": "dream", "status": "running", "description": "memory consolidation"}
TASKS_DIR = "/private/tmp/claude-501/-tmp-tides-fixture/SID/tasks"


class Case:
    """A conversation whose curation is due, with its Tides state."""

    def __init__(self, name, curating=False, extra_state=None):
        self.dir = C.new_case_dir("guard_" + name)
        self.sid = "66666666-6666-4666-8666-" + format(abs(sum(map(ord, name))) * 7919 % 10 ** 12, "012d")
        self.c = tools_dense(self.sid)
        # a background Bash launch whose result names the task folder, as Claude Code writes it
        self.c.user("Start the long job in the background.")
        self.c.tools([{"name": "Bash", "input": {"command": "sleep 40; echo bash-done", "description": "sleep forty",
                                                 "run_in_background": True},
                       "result": f"Command running in background with ID: {SHELL['id']}. Output is being written to: "
                                 f"{TASKS_DIR}/{SHELL['id']}.output. You will be notified when it completes."}],
                     usage=113000, after="Started.")
        if curating:
            self.c.stop_notice(1)
        self.path = Path(self.c.write(self.dir / f"{self.sid}.jsonl"))
        st = {"seen_start": 1.0, "lineage": self.sid, "path": str(self.path)}
        if curating:
            st.update(C.state_for_stop_curation(self.sid, self.path))
        st.update(extra_state or {})
        T.save_state(self.sid, st)
        self.cfgfile = T.DATA / "config.json"
        T.DATA.mkdir(parents=True, exist_ok=True)
        self.cfgfile.write_text(json.dumps({"section_tokens": 20000, "sections": 3, "wake": "manual",
                                            "ceiling": 150000, "window_tokens": 200000}))

    @property
    def st(self):
        return T.load_state(self.sid)

    def stop(self, last="All done.", tasks=None, **extra):
        inp = {"session_id": self.sid, "transcript_path": str(self.path), "cwd": "/tmp/tides-fixture",
               "hook_event_name": "Stop", "stop_hook_active": False, "last_assistant_message": last,
               "session_crons": []}
        if tasks is not None:
            inp["background_tasks"] = tasks
        inp.update(extra)
        return run(T.hook_stop, inp, T.config())

    def post_tool(self, name, tool_input, response, tool_use_id="toolu_launch01"):
        inp = {"session_id": self.sid, "transcript_path": str(self.path), "hook_event_name": "PostToolUse",
               "tool_name": name, "tool_input": tool_input, "tool_response": response, "tool_use_id": tool_use_id}
        return run(T.hook_post_tool, inp, T.config())

    def begin(self, force=False):
        os.environ["CLAUDE_CODE_SESSION_ID"] = self.sid
        try:
            return run(T.cmd_begin, T.config(), force=force)
        finally:
            del os.environ["CLAUDE_CODE_SESSION_ID"]

    def append(self, recs):
        with open(self.path, "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def leaf(self):
        recs = C.records(self.path)
        return next(r["uuid"] for r in reversed(recs) if r.get("uuid") and r.get("type") in ("user", "assistant", "system"))

    def say(self, text, ts=None, mid=None):
        r = {"parentUuid": self.leaf(), "isSidechain": False, "type": "assistant", "uuid": f"9{time.time_ns() % 10**11:011d}-a000-4000-8000-000000000000",
             "timestamp": ts or now_iso(), "sessionId": self.sid, "cwd": "/tmp/tides-fixture",
             "message": {"id": mid or f"msg_live_{time.time_ns()}", "type": "message", "role": "assistant",
                         "model": "claude-haiku-4-5", "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                         "usage": {"input_tokens": 1, "cache_read_input_tokens": 114000, "output_tokens": 10}}}
        self.append([r])
        return r

    def notify(self, task_id, tool_use_id, status="completed", ts=None, summary="finished", result=None):
        body = (f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<output-file>{TASKS_DIR}/{task_id}.output</output-file>\n<status>{status}</status>\n"
                f"<summary>{summary}</summary>\n" + (f"<result>{result}</result>\n" if result else "") + "</task-notification>")
        r = {"parentUuid": self.leaf(), "isSidechain": False, "type": "user", "uuid": f"8{time.time_ns() % 10**11:011d}-b000-4000-8000-000000000000",
             "timestamp": ts or now_iso(), "sessionId": self.sid, "cwd": "/tmp/tides-fixture",
             "origin": {"kind": "task-notification"}, "message": {"role": "user", "content": body}}
        self.append([r])
        return r

    def meta(self, text):
        r = {"parentUuid": self.leaf(), "isSidechain": False, "type": "user", "isMeta": True,
             "uuid": f"7{time.time_ns() % 10**11:011d}-c000-4000-8000-000000000000", "timestamp": now_iso(),
             "sessionId": self.sid, "message": {"role": "user", "content": text}}
        self.append([r])


class StartGate(unittest.TestCase):
    def test_r11_stop_does_not_start_a_tide_while_a_task_runs(self):
        k = Case("start_running")
        out = k.stop(tasks=[SUB])
        self.assertNotEqual(k.st.get("phase"), "curating")
        self.assertFalse(isinstance(out, dict) and out.get("decision") == "block")

    def test_r11_internal_tasks_do_not_hold_a_tide(self):
        k = Case("start_internal")
        out = k.stop(tasks=[INTERNAL])
        self.assertEqual(k.st.get("phase"), "curating", "Claude Code's own housekeeping must not block forever")
        self.assertEqual(out.get("decision"), "block")

    def test_r11_nothing_running_starts_as_before(self):
        k = Case("start_free")
        out = k.stop(tasks=[])
        self.assertEqual(k.st.get("phase"), "curating")
        self.assertIn("Tides · curation 1", out["reason"])


class FinishGate(unittest.TestCase):
    def test_r11_dream_with_running_tasks_waits_then_finishes_with_their_reports(self):
        k = Case("finish_wait", curating=True)
        dream = "#dream I was building the parser; the review agent is still out."
        out = k.stop(last=dream, tasks=[SUB, SHELL])
        st = k.st
        self.assertFalse(st.get("superseded_by"), "no successor while tasks run")
        self.assertEqual(st.get("phase"), "curating")
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("decision"), "block", "Claude is told the tide waits")
        for t in (SUB, SHELL):
            self.assertIn(t["description"], out["reason"])
        # Claude Code writes the dream, the hook feedback, Claude's answer; later the reports arrive
        k.say(dream)
        k.meta("Stop hook feedback:\n" + out["reason"])
        k.say("Understood; I'll wait for them.")
        out2 = k.stop(last="Understood; I'll wait for them.", tasks=[SUB, SHELL], stop_hook_active=True)
        self.assertFalse(k.st.get("superseded_by"))
        self.assertFalse(isinstance(out2, dict) and out2.get("decision") == "block", "told once, not every Stop")
        # the person speaks while it waits: the saved dream survives
        run(T.hook_prompt, {"session_id": k.sid, "prompt": "how's it going?", "transcript_path": str(k.path)}, T.config())
        self.assertEqual(k.st.get("phase"), "curating")
        k.notify(SUB["id"], "toolu_agent01", result="REVIEW-REPORT-7731: two bugs in the parser.")
        k.say("The review found two bugs.")
        k.notify(SHELL["id"], "toolu_shell01", summary='Background command "sleep forty" completed (exit code 0)')
        final = "Both are back: two bugs, and the sleep finished."
        out3 = k.stop(last=final, tasks=[])      # the final record is not in the file yet (the 170 ms race)
        st = k.st
        self.assertTrue(st.get("superseded_by"), out3)
        new = Path(st["successor_path"])
        self.assertEqual(T.validate(new), [])
        recs = C.records(new)
        texts = [C.text_of(r) for r in recs]
        dream_i = next(i for i, r in enumerate(recs) if C.tier(r) == "dream")
        self.assertIn("I was building the parser", texts[dream_i])
        after = "\n".join(texts[dream_i + 1:])
        self.assertIn("REVIEW-REPORT-7731", after, "the agent's report is kept, after the dream")
        self.assertIn("sleep forty", after)
        self.assertIn(final, after, "the turn's last message is kept even before Claude Code writes it")
        leaf = next(r["leafUuid"] for r in recs if r.get("type") == "last-prompt")
        self.assertEqual(leaf, [r for r in recs if r.get("uuid")][-1]["uuid"], "resume at the end of the tail")
        wake = T.load_state(st["superseded_by"])["wake_text"]
        self.assertIn("waited", wake)

    def test_r11_the_tail_becomes_the_newest_section_next_time(self):
        k = Case("finish_tail_next", curating=True)
        out = k.stop(last="#dream short", tasks=[SUB])
        k.say("#dream short")
        k.meta("Stop hook feedback:\n" + out["reason"])
        k.notify(SUB["id"], "toolu_agent01", result="TAIL-REPORT-55")
        k.say("Got the report.")
        k.stop(last="Got the report.", tasks=[])
        new_sid = k.st["superseded_by"]
        new = Path(k.st["successor_path"])
        S = 20000
        tiers = T.read_tiers(new, None, S)
        self.assertIn("TAIL-REPORT-55", "\n".join(C.text_of(r) for r in tiers["NEW"]))
        self.assertNotIn("TAIL-REPORT-55", "\n".join(C.text_of(r) for r in tiers["F_old"]))


class Advisory(unittest.TestCase):
    def test_r11_launch_then_begin_then_completion(self):
        k = Case("advisory")
        k.stop(tasks=[], last="ok")                      # the last Stop: nothing running
        T.save_state(k.sid, dict(k.st, phase="idle"))
        k.post_tool("Agent", {"description": "review the parser", "subagent_type": "general-purpose",
                              "prompt": "x", "run_in_background": True},
                    {"isAsync": True, "status": "async_launched", "agentId": "a77", "description": "review the parser",
                     "outputFile": f"{TASKS_DIR}/a77.output"}, tool_use_id="toolu_agent77")
        out = k.begin()
        self.assertIn("not starting", out)
        self.assertIn("review the parser", out)
        self.assertNotEqual(k.st.get("phase"), "curating")
        k.notify("a77", "toolu_agent77", result="done")
        out = k.begin()
        self.assertIn("Tides · curation 1", out)
        self.assertEqual(k.st.get("phase"), "curating")

    def test_r11_background_bash_launch_counts_too(self):
        k = Case("advisory_bash")
        k.stop(tasks=[], last="ok")
        T.save_state(k.sid, dict(k.st, phase="idle"))
        k.post_tool("Bash", {"command": "sleep 99", "description": "long wait", "run_in_background": True},
                    {"stdout": "", "stderr": "", "backgroundTaskId": "b99"}, tool_use_id="toolu_b99")
        self.assertIn("long wait", k.begin())
        k.notify("b99", "toolu_b99", status="killed")
        self.assertIn("Tides · curation 1", k.begin())

    def test_r11_a_quiet_agent_45_minutes_in_still_counts(self):
        k = Case("quiet45")
        # the last Stop saw the agent running, 45 minutes ago; its transcript has been silent since
        st = k.st
        st["tasks_seen"] = {"t": time.time() - 2700, "tasks": [SUB], "reported": True}
        T.save_state(k.sid, st)
        sub = Path(str(k.path)[:-6]) / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        f = sub / f"agent-{SUB['id']}.jsonl"
        f.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use",
                                                                   "content": []}}) + "\n")
        os.utime(f, (time.time() - 2700, time.time() - 2700))
        out = k.begin()
        self.assertIn("not starting", out)
        self.assertIn("quiet sleeper", out)
        # and the due-nudge stays quiet
        nudge = k.post_tool("Read", {"file_path": "/x"}, {"type": "text"})
        told = (nudge.get("hookSpecificOutput") or {}).get("additionalContext", "") if isinstance(nudge, dict) else ""
        self.assertNotIn("At your next natural pause", told, "no nudge to begin while a task runs")
        self.assertIn("waits while", told, "only the one-time word that the tide is waiting")
        self.assertFalse(k.post_tool("Read", {"file_path": "/y"}, {"type": "text"}), "then quiet")

    def test_r11_nudge_speaks_when_nothing_runs(self):
        k = Case("nudge_free")
        k.stop(tasks=[], last="ok")
        T.save_state(k.sid, dict(k.st, phase="idle"))
        nudge = k.post_tool("Read", {"file_path": "/x"}, {"type": "text"})
        self.assertIn("curation is due", json.dumps(nudge, ensure_ascii=False))

    def test_r11_older_claude_code_without_background_tasks_falls_back_to_the_file_scan(self):
        k = Case("fallback")
        k.stop(last="ok")          # no background_tasks key at all
        T.save_state(k.sid, dict(k.st, phase="idle"))
        sub = Path(str(k.path)[:-6]) / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "agent-zz1.jsonl").write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "stop_reason": "tool_use"}}) + "\n")
        (sub / "agent-zz1.meta.json").write_text(json.dumps({"description": "file-scan agent"}))
        self.assertIn("file-scan agent", k.begin())


class Force(unittest.TestCase):
    def test_r11_begin_force_overrides_and_r15_wake_names_the_tasks(self):
        k = Case("force")
        k.stop(tasks=[SUB, SHELL], last="ok")
        T.save_state(k.sid, dict(k.st, phase="idle"))
        out = k.begin(force=True)
        self.assertIn("Tides · curation 1", out)
        self.assertIn("quiet sleeper", out, "the notice names what will be cut off")
        st = k.st                              # finish through the Stop path, with the force flag begin set
        st["trigger"] = "stop"
        T.save_state(k.sid, st)
        k.meta("Stop hook feedback:\n🌙 Tides · curation 1. (fixture)")
        res = k.stop(last="#dream forced", tasks=[SUB, SHELL])
        st = k.st
        self.assertTrue(st.get("superseded_by"), res)
        wake = T.load_state(st["superseded_by"])["wake_text"]
        self.check_handover(wake, k)

    def check_handover(self, wake, k):
        self.assertIn("quiet sleeper", wake)
        self.assertIn("sleep forty", wake)
        self.assertIn(f"subagents/agent-{SUB['id']}.jsonl", wake, "the agent's transcript path")
        self.assertIn(f"{TASKS_DIR}/{SHELL['id']}.output", wake, "the shell task's output path")
        self.assertIn("still running", wake)
        self.assertIn("will not arrive here", wake)

    def test_r15_forced_at_ninety_percent_when_compaction_is_off(self):
        k = Case("forced90", curating=True)
        cfgdir = C.TMP / "cfg_off"
        cfgdir.mkdir(exist_ok=True)
        (cfgdir / "settings.json").write_text(json.dumps({"autoCompactEnabled": False}))
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfgdir)
        try:
            k.cfgfile.write_text(json.dumps({"section_tokens": 20000, "wake": "manual", "window_tokens": 120000}))
            st = k.st
            st["window"] = 120000
            T.save_state(k.sid, st)
            res = k.stop(last="#dream at the edge", tasks=[SUB, SHELL])
            st = k.st
            self.assertTrue(st.get("superseded_by"), res)
            self.check_handover(T.load_state(st["superseded_by"])["wake_text"], k)
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]

    def test_r11_never_forced_while_compaction_is_on(self):
        k = Case("forced_on", curating=True)
        cfgdir = C.TMP / "cfg_on"
        cfgdir.mkdir(exist_ok=True)
        (cfgdir / "settings.json").write_text(json.dumps({"autoCompactEnabled": True}))
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfgdir)
        try:
            k.cfgfile.write_text(json.dumps({"section_tokens": 20000, "wake": "manual", "window_tokens": 120000}))
            st = k.st
            st["window"] = 120000
            T.save_state(k.sid, st)
            k.stop(last="#dream at the edge", tasks=[SUB])
            self.assertFalse(k.st.get("superseded_by"))
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]

    def test_r11_start_gate_forced_at_ninety_percent_names_tasks(self):
        k = Case("forced_start")
        cfgdir = C.TMP / "cfg_off2"
        cfgdir.mkdir(exist_ok=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfgdir)
        os.environ["DISABLE_AUTO_COMPACT"] = "1"
        try:
            k.cfgfile.write_text(json.dumps({"section_tokens": 20000, "wake": "manual", "window_tokens": 120000}))
            st = k.st
            st["window"] = 120000
            T.save_state(k.sid, st)
            out = k.stop(tasks=[SUB])
            self.assertEqual(k.st.get("phase"), "curating")
            self.assertIn("quiet sleeper", out["reason"])
        finally:
            del os.environ["CLAUDE_CONFIG_DIR"]
            del os.environ["DISABLE_AUTO_COMPACT"]


if __name__ == "__main__":
    unittest.main()
