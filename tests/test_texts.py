"""R12: every sentence Tides shows about tool handling says what the code now does."""
import contextlib
import io
import json
import os
import unittest

import _common as C
from _conv import tools_dense

T = C.T
OLD_CLAIM = "tool calls, results and thinking set aside"


def first(name, **over):
    c = tools_dense("77777777-7777-4777-8777-" + format(sum(map(ord, name)) * 31 % 10 ** 12, "012d"))
    c.stop_notice(1)
    path = c.write(C.new_case_dir(name) / f"{c.sid}.jsonl")
    st = C.state_for_stop_curation(c.sid, path)
    return path, st, T.build_successor(path, C.deep(st), "#dream t", C.cfg_for(T, **over))


def divider(path):
    return next(C.text_of(r) for r in C.records(path) if C.tier(r) == "div" and "messages only" in C.text_of(r))


class R12Texts(unittest.TestCase):
    def test_r12_divider_matches_behaviour(self):
        _, _, (_, on, _) = first("t_on")
        _, _, (_, off, _) = first("t_off", tool_calls=False)
        self.assertNotIn(OLD_CLAIM, divider(on))
        self.assertIn("quoted record of its past tool calls", divider(on))
        self.assertIn(OLD_CLAIM, divider(off))
        for p in (on, off):
            self.assertIn("task notifications (agents' reports) kept whole", divider(p))

    def test_r12_notice_matches_behaviour(self):
        path, st, _ = first("t_notice")
        on = T.notice_text(path, C.deep(st), C.cfg_for(T))
        off = T.notice_text(path, C.deep(st), C.cfg_for(T, tool_calls=False))
        self.assertNotIn("Its tool calls, results and thinking are set aside", on)
        self.assertRegex(on, r"record of its \d+ tool calls?")
        self.assertIn("Records are not carried into the oldest tier", on)
        self.assertIn("Its tool calls, results and thinking are set aside", off)
        self.assertIn("agents' reports, stay as messages", off)

    def test_r12_wake_and_intro_and_status(self):
        _, _, (_, _, rep) = first("t_wake")
        self.assertIn("with records of its tool calls", T.wake_text(rep))
        _, _, (_, _, rep_off) = first("t_wake_off", tool_calls=False)
        self.assertNotIn("records", T.wake_text(rep_off))
        for on, want, unwanted in ((True, "older tool calls stay as short quoted records", "set old tool output aside"),
                                   (False, "set old tool output aside", "quoted records")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.hook_session_start({"session_id": "88888888-8888-4888-8888-00000000000" + str(int(on)),
                                      "source": "startup"}, C.cfg_for(T, tool_calls=on))
            intro = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
            self.assertIn(want, intro)
            self.assertNotIn(unwanted, intro)

    def test_r12_readme_and_command_help(self):
        readme = (C.REPO / "README.md").read_text()
        self.assertNotIn("sets aside tool output", readme)
        self.assertIn("a short record of the tools it used", readme)
        self.assertIn("Agents' reports stay whole", readme)
        self.assertIn("Tool records don't come along", readme)
        self.assertIn("the curation waits until they finish", readme)
        self.assertIn("/tides set tool-calls off", readme)
        help_ = (C.REPO / "commands" / "tides.md").read_text()
        self.assertIn("set tool-calls off", help_)


if __name__ == "__main__":
    unittest.main()
