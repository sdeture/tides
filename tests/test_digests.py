"""R1, R1b, R2, R3/R9, R4, R6, R8 (offline part), R10: the digest records."""
import contextlib
import io
import json
import os
import re
import unittest

import _common as C
from _conv import LONG_CMD, LONG_RESULT, CONFIG_TEXT, PLANTED, chatty, tools_dense
from _flow import Lineage, candidates, sections

T, B = C.T, C.B
FIELD_LABELS = ("command", "file", "pattern", "path", "url", "query", "agent", "skill", "input", "result")
FIELD_RE = re.compile(r"^  (%s): " % "|".join(FIELD_LABELS))
HEAD_RE = re.compile(r"^• (\S+) \((ok|error|no result)\)(?: · (.*))?$", re.S)
TRAILING_PLACEHOLDERS = re.compile(r"( ⟪\d+ [\w ]+ blocks?⟫)+$")
RESULT_NOT_KEPT = re.compile(r"^⟪result not kept: (\d+) characters⟫$")


def parse_record(text):
    """A digest record's text -> [{"name", "status", "description", "fields": [(label, value)]}]."""
    lines = text.split("\n")
    assert lines[0].startswith(C.FRAME_PREFIX), lines[0]
    calls, cur, field = [], None, None
    for line in lines[1:]:
        if line.startswith("• "):
            cur = {"head": line, "fields": []}
            calls.append(cur)
            field = None
        elif FIELD_RE.match(line):
            label, value = line[2:].split(": ", 1)
            field = [label, value]
            cur["fields"].append(field)
        elif field is not None:
            field[1] += "\n" + line
        else:
            cur["head"] += "\n" + line
    for c in calls:
        m = HEAD_RE.match(c["head"])
        assert m, c["head"]
        c["name"], c["status"], c["description"] = m.group(1), m.group(2), m.group(3)
    return calls


def check_exact(test, shown, original, where):
    """R2: shown is the whole original, or original[:h] + mark(k) + original[-t:] with h+k+t = len."""
    marks = list(C.MARK_RE.finditer(shown))
    if not marks:
        test.assertEqual(shown, original, where)
        return 0
    test.assertEqual(len(marks), 1, where)
    m = marks[0]
    head, tail, k = shown[:m.start()], shown[m.end():], int(m.group(1))
    test.assertGreater(k, 0, where)
    test.assertGreater(len(tail), 0, where)
    test.assertTrue(original.startswith(head), where)
    test.assertTrue(original.endswith(tail), where)
    test.assertEqual(len(head) + k + len(tail), len(original), where)
    return 1


# What the fixture's middle section must yield: (anchor unit text start, [(name, status, description,
# [(label, original)], result original or None, trailing placeholder)])
EXPECTED = [
    ("I'll read the build id file.", [
        ("Bash", "ok", "Show the build id", [("command", "cat build/ID.txt")], f"BUILD-ID: {PLANTED}\n", "")]),
    ("Reading it first.", [
        ("Read", "ok", None, [("file", "/tmp/tides-fixture/src/very/deep/nested/package/config/settings_production.py")], CONFIG_TEXT, ""),
        ("Edit", "ok", None, [("file", "/tmp/tides-fixture/src/config.py")],
         "The file /tmp/tides-fixture/src/config.py has been updated successfully.", ""),
        ("Write", "ok", None, [("file", "/tmp/tides-fixture/CHANGELOG.md")],
         "File created successfully at: /tmp/tides-fixture/CHANGELOG.md", "")]),
    ("Searching both at once.", [
        ("Grep", "ok", None, [("pattern", "TODO|FIXME"), ("path", "src/")],
         "\n".join(f"src/mod{i}.py:{i}: # TODO item {i}" for i in range(40)), ""),
        ("Glob", "ok", None, [("pattern", "tests/**/*.py")], "\n".join(f"tests/test_{i}.py" for i in range(30)), "")]),
    ("The docs list the options", [
        ("WebFetch", "ok", None, [("url", "https://example.org/docs/parser/reference/v2/all-options-explained-in-detail.html")], LONG_RESULT, ""),
        ("WebSearch", "ok", None, [("query", "parser option strict mode semantics")],
         "1. Strict mode - example.org\n2. Parser FAQ - example.org", "")]),
    ("The agent found two bugs.", [
        ("Agent", "ok", "Review the parser", [("agent", "general-purpose")], "Review: two bugs. " + LONG_RESULT[:900], ""),
        ("Skill", "ok", None, [("skill", "tufte")], "Launching skill: tufte", "")]),
    ("Take a screenshot of the page", [      # no assistant text: the record follows the human turn
        ("mcp__claude-in-chrome__computer", "ok", None, [("input", '{"action":"screenshot","tabId":12}')],
         "Screenshot taken of tab 12", " ⟪1 image block⟫"),
        ("NotebookEdit", "ok", None, [("file", "/tmp/tides-fixture/analysis.ipynb")], "Updated cell c3", "")]),
    ("Running it.", [
        ("Bash", "error", None, [("command", "python3 missing.py")],
         "python3: can't open file '/tmp/tides-fixture/missing.py': [Errno 2] No such file or directory", "")]),
    ("Running the long command.", [
        ("Bash", "ok", "Print sixty lines from a heredoc", [("command", LONG_CMD)], LONG_RESULT, "")]),
]


def first_curation(name, mod=None, **cfg_over):
    mod = mod or T
    c = tools_dense("44444444-4444-4444-8444-" + format(abs(sum(map(ord, name))) % 10 ** 12, "012d"))
    c.stop_notice(1)
    d = C.new_case_dir(name)
    path = c.write(d / f"{c.sid}.jsonl")
    st = C.state_for_stop_curation(c.sid, path)
    cfg = C.cfg_for(mod, **cfg_over)
    sid, new_path, rep = mod.build_successor(path, st, "#dream fixture", cfg)
    return new_path, rep


class R1Digests(unittest.TestCase):
    def test_r1_one_record_per_turn_with_every_field_in_order(self):
        path, _ = first_curation("r1")
        recs = C.records(path)
        chain = [r for r in recs if C.tier(r) in ("M", "D", "div", "K", "dream")]
        D = [r for r in chain if C.tier(r) == "D"]
        self.assertEqual(len(D), len(EXPECTED), "one record per assistant turn that made tool calls")
        for (anchor, calls), d in zip(EXPECTED, D):
            i = chain.index(d)
            prev = chain[i - 1]
            self.assertEqual(C.tier(prev), "M", "a record sits right after a message unit")
            self.assertIn(anchor, C.text_of(prev), "placed after its turn's text unit (or the human turn)")
            self.assertEqual(d["type"], "user")
            self.assertTrue(d.get("isMeta"))
            self.assertEqual(d["parentUuid"], prev["uuid"], "chained right after the unit")
            got = parse_record(C.text_of(d))
            self.assertEqual([g["name"] for g in got], [c[0] for c in calls], "call order (parallel: block order)")
            for g, (name, status, desc, fields, result, trailing) in zip(got, calls):
                self.assertEqual(g["status"], status, name)
                if desc is None:
                    self.assertIsNone(g["description"], name)
                else:
                    check_exact(self, g["description"], desc, name + " description")
                labels = [f[0] for f in g["fields"]]
                self.assertEqual(labels, [f[0] for f in fields] + ["result"], name)
                for (label, shown), (_, orig) in zip(g["fields"][:-1], fields):
                    check_exact(self, shown, orig, f"{name} {label}")
                shown_result = g["fields"][-1][1]
                if trailing:
                    self.assertTrue(shown_result.endswith(trailing), name)
                    shown_result = shown_result[: -len(trailing)]
                check_exact(self, shown_result, result, f"{name} result")
        # the planted token survives only inside a digest
        self.assertTrue(any(PLANTED in C.text_of(r) for r in D))
        self.assertFalse(any(PLANTED in C.text_of(r) for r in recs if C.tier(r) == "M"))

    def test_r1_agent_prompt_and_skill_args_are_not_shown(self):
        path, _ = first_curation("r1b_fields")
        text = "\n".join(C.text_of(r) for r in C.digest_records(path))
        self.assertIn("• Agent (ok) · Review the parser", text)
        self.assertIn("• Skill (ok)", text)
        self.assertNotIn("Review src/parser.py for bugs.", text)
        self.assertNotIn("bar chart", text)


class R2Exact(unittest.TestCase):
    def test_r2_every_mark_is_exact_arithmetic(self):
        path, _ = first_curation("r2")
        D = C.digest_records(path)
        self.assertTrue(D, "the fixture must produce digests")
        marks = 0
        originals = {}
        for _, calls in EXPECTED:
            for name, status, desc, fields, result, trailing in calls:
                for label, orig in fields:
                    originals.setdefault((name, label), []).append(orig)
                originals.setdefault((name, "result"), []).append(result)
                if desc:
                    originals.setdefault((name, "description"), []).append(desc)
        for d in D:
            for g in parse_record(C.text_of(d)):
                items = [("description", g["description"])] if g["description"] else []
                items += g["fields"]
                for label, shown in items:
                    if label == "result":
                        shown = TRAILING_PLACEHOLDERS.sub("", shown)
                    for orig in originals[(g["name"], label)]:
                        cut = C.MARK_RE.search(shown)
                        head = shown[:cut.start()] if cut else shown
                        if orig.startswith(head) or orig == shown:
                            marks += check_exact(self, shown, orig, f"{g['name']} {label}")
                            break
                    else:
                        self.fail(f"{g['name']} {label}: shown text matches no original")
        self.assertGreaterEqual(marks, 5, "the long fields must have been cut, with marks")

    def test_r2_no_character_outside_originals_and_constants(self):
        path, _ = first_curation("r2b")
        self.assertTrue(C.digest_records(path))
        for d in C.digest_records(path):
            text = C.text_of(d)
            self.assertNotIn("…]", text, "no other cut notation")
            self.assertNotIn("[…]", text)


class R3KeptMemories(unittest.TestCase):
    def test_r3_r9_kept_memories_never_hold_digests_and_candidates_match_off(self):
        on = Lineage("r3_on", dict(sections=3))
        off = Lineage("r3_off", dict(sections=3, tool_calls=False))
        for L in (on, off):
            L.curate(T, tag="same")
        c_on = [(u["id"], u["text"]) for u in candidates(T, on.path, on.st, on.cfg(T))]
        c_off = [(u["id"], u["text"]) for u in candidates(T, off.path, off.st, off.cfg(T))]
        self.assertTrue(c_on)
        self.assertEqual(c_on, c_off, "the keep candidates are the same with digests on and off")
        self.assertTrue(any(C.tier(r) == "D" for r in C.records(on.path)), "the on lineage has digests to leak")
        on.curate(T, keep_all=True)
        K = [r for r in C.records(on.path) if C.tier(r) == "K"]
        self.assertTrue(K)
        for r in K:
            self.assertNotIn("⟦tides · record of the tool calls", C.text_of(r))
            self.assertIsNone(C.MARK_RE.search(C.text_of(r)))
        self.assertFalse(any(PLANTED in C.text_of(r) for r in K))


class R1bCarry(unittest.TestCase):
    def test_r1b_digests_carried_byte_identical_then_dropped(self):
        L = Lineage("r1b", dict(sections=4))
        L.curate(T, tag="c1")
        s1 = sections(L.path)
        d1 = [C.text_of(r) for r in s1[1]["D"]]
        self.assertTrue(d1, "section 1 has digests")
        L.curate(T, tag="c2")
        s2 = sections(L.path)
        self.assertEqual([C.text_of(r) for r in s2[1]["D"]], d1, "carried byte-identical")
        self.assertEqual([r["tides"]["after"] for r in s2[1]["D"]], [r["tides"]["after"] for r in s1[1]["D"]])
        self.assertTrue(s2[2]["D"], "the new section has its own digests")
        d2 = [C.text_of(r) for r in s2[2]["D"]]
        L.curate(T, tag="c3", keep_all=True)
        s3 = sections(L.path)
        self.assertNotIn(1, s3, "section 1 left the messages-only tiers")
        self.assertEqual([C.text_of(r) for r in s3[2]["D"]], d2, "section 2 still carried byte-identical")
        allK = "\n".join(C.text_of(r) for r in C.records(L.path) if C.tier(r) == "K")
        for t in d1:
            self.assertNotIn(t, allK)
        self.assertNotIn("⟦tides · record", allK)
        # each carried record still sits right after the unit it follows
        for sec in s3.values():
            ids = {r["uuid"]: r for r in sec["M"]}
            for d in sec["D"]:
                if d["tides"].get("after"):
                    self.assertIn(d["parentUuid"], ids)
                    self.assertEqual(ids[d["parentUuid"]]["tides"]["id"], d["tides"]["after"])


def _budget_run(name, share, section=20000, **over):
    path, rep = first_curation(name, tool_share=share, section_tokens=section, **over)
    D = C.digest_records(path)
    div = next(r for r in C.records(path) if C.tier(r) == "div" and "messages only" in C.text_of(r))
    return path, D, C.text_of(div), sum(T.est_tokens(C.text_of(r)) for r in D)


class R4Budget(unittest.TestCase):
    def full_cost(self):
        _, D, div, cost = _budget_run("r4_full", 0.10)
        return D, cost

    def test_r4_under_budget_nothing_dropped(self):
        _, D, div, cost = _budget_run("r4_under", 0.10)
        self.assertNotRegex(div, r"left out")
        self.assertFalse(any("⟪result not kept" in C.text_of(r) for r in D))

    def test_r4_at_budget_nothing_dropped_one_under_something_is(self):
        _, full = self.full_cost()
        self.assertLess(full, 2000, "fixture digests must fit under 10% of a 20k section for this test")
        _, D, div, cost = _budget_run("r4_at", (full + 0.5) / 20000)
        self.assertEqual(cost, full)
        self.assertNotRegex(div, r"left out")
        _, D2, div2, cost2 = _budget_run("r4_at_minus", (full - 0.5) / 20000)
        self.assertLessEqual(cost2, full - 1)
        self.assertTrue(any("⟪result not kept" in C.text_of(r) for r in D2))
        self.assertRegex(div2, r"1 result left out")
        last = parse_record(C.text_of(D2[-1]))[-1]
        self.assertNotRegex(last["fields"][-1][1], RESULT_NOT_KEPT, "the newest result stays")

    def test_r4_over_budget_order_results_then_inputs_then_lines(self):
        Dfull, full = self.full_cost()
        full_calls = [c for r in Dfull for c in parse_record(C.text_of(r))]
        # tight: results go, inputs shrink to 60, and then whole lines go, oldest first
        _, D, div, cost = _budget_run("r4_tight", 0.012)
        budget = int(20000 * 0.012)
        self.assertLessEqual(cost, budget)
        calls = [c for r in D for c in parse_record(C.text_of(r))]
        m = re.search(r"(\d+) call records? left out", div)
        self.assertIsNotNone(m, div)
        self.assertEqual(int(m.group(1)), len(full_calls) - len(calls), "the divider counts the lines left out")
        self.assertGreater(len(calls), 0)
        self.assertEqual([c["head"] for c in calls], [c["head"] for c in full_calls[-len(calls):]],
                         "the lines that stay are the newest")
        slack = getattr(T, "CUT_SLACK", 0)
        for c, f in zip(calls, full_calls[-len(calls):]):
            res = c["fields"][-1][1]
            if not RESULT_NOT_KEPT.match(res):
                self.assertEqual(res, f["fields"][-1][1], "a result is either left out or untouched")
                self.assertLessEqual(len(res), len("⟪result not kept: 000 characters⟫"), "only results too short to save anything stay")
            for (label, v), (_, fv) in zip(c["fields"][:-1], f["fields"][:-1]):
                mk = C.MARK_RE.search(v)
                if mk:
                    self.assertEqual(len(C.MARK_RE.sub("", v)), 60, label)
                else:
                    self.assertLessEqual(len(v), 60 + slack, label)

    def test_r4_results_go_oldest_first(self):
        Dfull, full = self.full_cost()
        full_calls = [c for r in Dfull for c in parse_record(C.text_of(r))]
        for share_tokens in (full - 40, full - 150, full - 300):
            _, D, div, cost = _budget_run(f"r4_order_{share_tokens}", (share_tokens + 0.5) / 20000)
            calls = [c for r in D for c in parse_record(C.text_of(r))]
            self.assertEqual(len(calls), len(full_calls), "no line is dropped while results can still go")
            dropped = [bool(RESULT_NOT_KEPT.match(c["fields"][-1][1])) for c in calls]
            self.assertTrue(any(dropped))
            last = max(i for i, d in enumerate(dropped) if d)
            for i in range(last):
                if not dropped[i]:
                    shown = full_calls[i]["fields"][-1][1]
                    self.assertLessEqual(len(shown), len("⟪result not kept: 000 characters⟫"),
                                         "an older result stays only if leaving it out would save nothing")
            n = re.search(r"(\d+) results? left out", div)
            self.assertEqual(int(n.group(1)), sum(dropped))

    def test_r4_share_never_above_ten_percent(self):
        _, _, _, c10 = _budget_run("r4_ten", 0.10, section=8000)
        _, _, _, c50 = _budget_run("r4_fifty", 0.50, section=8000)
        self.assertGreater(c10, 0)
        self.assertLessEqual(c50, 800)
        self.assertEqual(c10, c50)

    def test_r4_settings_set_and_shown(self):
        out = io.StringIO()
        cfgfile = T.DATA / "config.json"
        before = cfgfile.read_text() if cfgfile.exists() else None
        try:
            with contextlib.redirect_stdout(out):
                T.cmd_set(["tool-calls", "off"])
                T.cmd_set(["tool-call-chars", "300/250"])
                T.cmd_set(["tool-share", "8%"])
                T.cmd_set(["tool-share", "25%"])
            c = T.config()
            self.assertIs(c["tool_calls"], False)
            self.assertEqual((c["tool_input_chars"], c["tool_result_chars"]), (300, 250))
            self.assertAlmostEqual(c["tool_share"], 0.10)
            self.assertIn("10%", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.cmd_status(T.config())
            self.assertRegex(out.getvalue(), r"tool calls: set aside")
            with contextlib.redirect_stdout(io.StringIO()):
                T.cmd_set(["tool-calls", "on"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                T.cmd_status(T.config())
            self.assertRegex(out.getvalue(), r"tool calls: kept as records \(input 300, result 250 characters; up to 10% of a section\)")
        finally:
            if before is None:
                cfgfile.unlink(missing_ok=True)
            else:
                cfgfile.write_text(before)


def normalise(path, since):
    """uuids, session ids, msg_tides_ ids and now_iso() timestamps -> stable tokens."""
    seen = {}

    def tok(prefix, v):
        return seen.setdefault((prefix, v), f"<{prefix}{len([k for k in seen if k[0] == prefix])}>")

    out = []
    for r in C.records(path):
        r = json.loads(json.dumps(r))
        for k in ("uuid", "parentUuid", "leafUuid"):
            if r.get(k):
                r[k] = tok("u", r[k])
        for k in ("sessionId", "session_id"):
            if r.get(k):
                r[k] = "<sid>"
        m = r.get("message")
        if isinstance(m, dict) and str(m.get("id", "")).startswith("msg_tides_"):
            m["id"] = tok("m", m["id"])
        if r.get("timestamp") and r["timestamp"] >= since:
            r["timestamp"] = "<now>"
        out.append(json.dumps(r, ensure_ascii=False, sort_keys=True))
    return out


# The one sentence that changed with tool-calls off: the divider now says, truthfully, that agents'
# reports stay (they always did). Everything else must be byte-for-byte what 0.3.1 wrote.
OLD_DIV = re.compile(r"(⟦tides · section \d+ · messages only · tool calls, results and thinking set aside at curation \d+)( · ~)")
NEW_DIV_INSERT = r"\1 · task notifications (agents' reports) kept whole\2"


class R6Off(unittest.TestCase):
    def _both(self, path, st, cfg_over):
        since = T.now_iso()[:19]
        sB, pB, _ = B.build_successor(path, C.deep(st), "#dream same", C.cfg_for(B, **cfg_over))
        sT, pT, _ = T.build_successor(path, C.deep(st), "#dream same", C.cfg_for(T, tool_calls=False, **cfg_over))
        return normalise(pB, since), normalise(pT, since)

    def check(self, path, st, cfg_over):
        old, new = self._both(path, st, cfg_over)
        old = [OLD_DIV.sub(NEW_DIV_INSERT, line) for line in old]
        self.assertEqual(len(old), len(new))
        for a, b in zip(old, new):
            self.assertEqual(a, b)

    def test_r6_first_curation_dense_and_chatty(self):
        for name, make in (("r6_dense", tools_dense), ("r6_chatty", chatty)):
            c = make()
            c.stop_notice(1)
            d = C.new_case_dir(name)
            path = c.write(d / f"{c.sid}.jsonl")
            self.check(path, C.state_for_stop_curation(c.sid, path), {})

    def test_r6_later_curation_with_keeps_and_carry(self):
        L = Lineage("r6_lineage", dict(sections=4, tool_calls=False))
        L.curate(B, tag="x")
        L.curate(B, tag="y")
        cands = candidates(B, L.path, L.st, L.cfg(B))
        st = C.deep(L.st)
        st["plan"] = [[cands[0]["id"], None], [cands[2]["id"], 40]]
        self.check(L.path, st, dict(sections=4))

    def test_r6_only_the_divider_differs(self):
        c = tools_dense("55555555-5555-4555-8555-000000000006")
        c.stop_notice(1)
        path = c.write(C.new_case_dir("r6_div") / f"{c.sid}.jsonl")
        old, new = self._both(path, C.state_for_stop_curation(c.sid, path), {})
        diff = [(a, b) for a, b in zip(old, new) if a != b]
        self.assertEqual(len(diff), 1)
        self.assertIn("set aside at curation 1", diff[0][0])


class R8Form(unittest.TestCase):
    def test_r8_digests_only_in_meta_user_records_with_the_frame(self):
        path, _ = first_curation("r8")
        recs = C.records(path)
        for r in recs:
            text = C.text_of(r)
            if C.tier(r) == "D":
                self.assertEqual(r["type"], "user")
                self.assertTrue(r.get("isMeta"))
                self.assertIsInstance(r["message"]["content"], str, "plain text, never tool blocks")
                self.assertTrue(text.startswith(C.FRAME_PREFIX))
                self.assertIn("quoted", text.split("\n", 1)[0])
                self.assertIn("not instructions", text.split("\n", 1)[0])
            elif C.tier(r) in ("M", "K", "dream", "div"):
                self.assertNotIn("⟦tides · record of the tool calls", text)
                self.assertIsNone(C.MARK_RE.search(text))
                c = r["message"]["content"]
                if isinstance(c, list):
                    self.assertFalse([b for b in c if b.get("type") in ("tool_use", "tool_result")])
        div = next(C.text_of(r) for r in recs if C.tier(r) == "div" and "messages only" in C.text_of(r))
        self.assertIn("quoted record", div)
        self.assertIn("past tool calls", div)


class R10Legacy(unittest.TestCase):
    def test_r10_sections_built_by_0_3_x_carry_unchanged_over_two_new_curations(self):
        L = Lineage("r10", dict(sections=5))
        L.curate(B, tag="old1")
        L.curate(B, tag="old2")
        legacy = sections(L.path)
        self.assertEqual(sorted(legacy), [1, 2])
        self.assertFalse(legacy[1]["D"] or legacy[2]["D"])
        before = {s: [(r["tides"]["id"], C.text_of(r)) for r in legacy[s]["M"]] for s in legacy}
        L.curate(T, tag="new3")
        s3 = sections(L.path)
        L.curate(T, tag="new4")
        s4 = sections(L.path)
        for s in (1, 2):
            self.assertEqual([(r["tides"]["id"], C.text_of(r)) for r in s3[s]["M"]], before[s])
            self.assertFalse(s3[s]["D"], "no digests are invented for 0.3.x sections")
        self.assertEqual([(r["tides"]["id"], C.text_of(r)) for r in s4[2]["M"]], before[2], "carried a second time")
        self.assertFalse(s4[2]["D"])
        self.assertIn("set aside", C.text_of(s4[2]["div"]), "its divider still says tool calls were set aside")
        self.assertTrue(s3[3]["D"] and s4[4]["D"], "sections built by the new code have digests")
        for p in L.paths[1:]:
            self.assertEqual(T.validate(p), [], p.name)


if __name__ == "__main__":
    unittest.main()
