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
    """A digest record's text -> [{"name", "status", "description", "fields": [(label, value)]}], with every quoted
    value decoded back to what it shows: the line prefix of quoted text and the line break before a real cut mark
    are part of the record's shape, not of the value. Any line outside that shape fails the test."""
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
        elif line.startswith(C.QUOTE) or C.CUT_LINE_RE.match(line):
            add = ("\n" + line[len(C.QUOTE):]) if line.startswith(C.QUOTE) else line[2:]
            if field is not None:
                field[1] += add
            else:
                cur["head"] += add
        else:
            raise AssertionError("a line outside the record's shape: " + repr(line))
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
    """The first curation of tools_dense. The budget defaults to 10% here so that it does not bind: the
    structure tests see every call. (R4's tests set the budget themselves.)"""
    cfg_over.setdefault("tool_share", 0.10)
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

    def test_r2_a_field_barely_over_its_cap_stays_whole(self):
        # cutting a field by less than the mark's own length would make it longer, not shorter
        cap = 200
        for n in (cap, cap + T.CUT_SLACK):
            self.assertEqual(T.cut("x" * n, cap), "x" * n)
        s = "a" * 100 + "b" * (cap + T.CUT_SLACK + 1 - 200) + "c" * 100
        out = T.cut(s, cap)
        self.assertNotEqual(out, s)
        self.assertEqual(out, "a" * 100 + T.CUT_MARK.format(k=len(s) - cap) + "c" * 100)
        self.assertLess(len(out), len(s))

    def test_r2_no_character_outside_originals_and_constants(self):
        # every line is one of the record's fixed shapes, and once the fixed parts are taken away, what is left
        # of each value is pieces of the original call or result
        path, _ = first_curation("r2b")
        D = C.digest_records(path)
        self.assertTrue(D)
        originals = [desc for _, calls in EXPECTED for _, _, desc, _, _, _ in calls if desc]
        originals += [v for _, calls in EXPECTED for c in calls for _, v in c[3]]
        originals += [c[4] for _, calls in EXPECTED for c in calls if c[4] is not None]
        fixed = re.compile(r"⟪…\d+ characters cut…⟫|⟪result not kept: \d+ characters⟫|⟪no result recorded⟫|⟪empty⟫|"
                           r"(?: ?⟪\d+ [\w ]+ blocks?⟫)+$")
        pieces = 0
        for d in D:
            for g in parse_record(C.text_of(d)):
                for label, shown in ([("description", g["description"])] if g["description"] else []) + g["fields"]:
                    for piece in fixed.split(shown):
                        if piece and piece.strip():
                            pieces += 1
                            self.assertTrue(any(piece in o for o in originals), f"{g['name']} {label}: {piece[:60]!r}")
        self.assertGreater(pieces, 20)


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
            self.assertNotIn(C.FRAME_PREFIX, C.text_of(r))
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
        self.assertIn("quoted record", C.text_of(s2[1]["div"]), "a carried section's divider still says it has records")
        self.assertNotIn("set aside at curation", C.text_of(s2[1]["div"]))
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
        self.assertNotIn(C.FRAME_PREFIX, allK)
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
    return path, D, C.text_of(div), sum(real_tokens(C.text_of(r)) for r in D)


def real_tokens(text):
    """A record's cost as the spec now defines it (R4, amended 11-12): real tokens at the calibrated record
    rate, 2.1 characters per token (+4 for the record). Computed here, not by the module under test."""
    return int(len(text) / 2.1) + 4


class R4Budget(unittest.TestCase):
    def full_cost(self):
        _, D, div, cost = _budget_run("r4_full", 0.10)
        return D, cost

    def test_r4_under_budget_nothing_dropped(self):
        _, D, div, cost = _budget_run("r4_under", 0.10)
        self.assertTrue(D, "there must be records for nothing to be dropped from")
        self.assertNotRegex(div, r"left out|budget")
        self.assertFalse(any("⟪result not kept" in C.text_of(r) for r in D))

    def test_r4_at_budget_nothing_dropped_one_under_something_is(self):
        _, full = self.full_cost()
        self.assertLess(full, 2000, "fixture digests must fit under 10% of a 20k section for this test")
        _, D, div, cost = _budget_run("r4_at", (full + 0.5) / 20000)
        self.assertEqual(cost, full)
        self.assertNotRegex(div, r"left out|budget")
        _, D2, div2, cost2 = _budget_run("r4_at_minus", (full - 0.5) / 20000)
        self.assertLessEqual(cost2, full - 1)
        # one token over: every result gets the next smaller cap; none is dropped
        self.assertFalse(any("⟪result not kept" in C.text_of(r) for r in D2))
        self.assertRegex(div2, r"results shown up to 100 characters \(budget\)")

    def test_r4_results_shrink_evenly_before_any_is_dropped(self):
        Dfull, full = self.full_cost()
        full_calls = [c for r in Dfull for c in parse_record(C.text_of(r))]
        for step in (100, 60, 30):
            _, _, _, at_step = _budget_run(f"r4_cost_{step}", 0.10, tool_result_chars=step)
            _, D, div, cost = _budget_run(f"r4_even_{step}", (at_step + 0.5) / 20000)
            self.assertLessEqual(cost, at_step)
            self.assertFalse(any("⟪result not kept" in C.text_of(r) for r in D), step)
            calls = [c for r in D for c in parse_record(C.text_of(r))]
            self.assertEqual(len(calls), len(full_calls), "no line goes while results can still shrink")
            for c in calls:
                res = TRAILING_PLACEHOLDERS.sub("", c["fields"][-1][1])
                self.assertLessEqual(len(C.MARK_RE.sub("", res)), step + T.CUT_SLACK, c["head"])
            self.assertRegex(div, rf"results shown up to {step} characters \(budget\)")

    def test_r4_results_go_oldest_first(self):
        Dfull, full = self.full_cost()
        full_calls = [c for r in Dfull for c in parse_record(C.text_of(r))]
        _, _, _, at30 = _budget_run("r4_cost30", 0.10, tool_result_chars=30, tool_input_chars=100)
        for share_tokens in (at30 - 30, at30 - 80):
            _, D, div, cost = _budget_run(f"r4_order_{share_tokens}", (share_tokens + 0.5) / 20000)
            self.assertLessEqual(cost, share_tokens)
            calls = [c for r in D for c in parse_record(C.text_of(r))]
            self.assertEqual(len(calls), len(full_calls), "no line is dropped while results can still go")
            dropped = [bool(RESULT_NOT_KEPT.match(c["fields"][-1][1])) for c in calls]
            self.assertTrue(any(dropped))
            last = max(i for i, d in enumerate(dropped) if d)
            for i in range(last):
                if not dropped[i]:
                    shown = calls[i]["fields"][-1][1]
                    self.assertLessEqual(len(shown), len("⟪result not kept: 000 characters⟫"),
                                         "an older result stays only if leaving it out would save nothing")
            n = re.search(r"(\d+) results? left out", div)
            self.assertEqual(int(n.group(1)), sum(dropped), "the divider counts exactly the results shown as left out")
            for c in calls:     # results go before any input is shortened below 100
                for label, v in c["fields"][:-1]:
                    if C.MARK_RE.search(v):
                        self.assertEqual(len(C.MARK_RE.sub("", v)), 100, label)

    def test_r4_inputs_shorten_to_100_before_any_result_goes(self):
        Dfull, _ = self.full_cost()
        full_calls = [c for r in Dfull for c in parse_record(C.text_of(r))]
        _, _, _, at30 = _budget_run("r4_in_cost30", 0.10, tool_result_chars=30)
        _, _, _, at30_100 = _budget_run("r4_in_cost30_100", 0.10, tool_result_chars=30, tool_input_chars=100)
        self.assertLess(at30_100, at30, "the fixture must have an input longer than 100 characters")
        _, D, div, cost = _budget_run("r4_in_100", (at30_100 + 0.5) / 20000)
        self.assertLessEqual(cost, at30_100)
        self.assertFalse(any("⟪result not kept" in C.text_of(r) for r in D), "no result goes while inputs can shorten")
        calls = [c for r in D for c in parse_record(C.text_of(r))]
        self.assertEqual(len(calls), len(full_calls))
        cut_inputs = [(label, v) for c in calls for label, v in c["fields"][:-1] if C.MARK_RE.search(v)]
        self.assertTrue(cut_inputs)
        for label, v in cut_inputs:
            self.assertEqual(len(C.MARK_RE.sub("", v)), 100, label)
        self.assertRegex(div, r"results shown up to 30 characters \(budget\)")

    def test_r4_over_budget_order_results_inputs_results_inputs_lines(self):
        Dfull, full = self.full_cost()
        full_calls = [c for r in Dfull for c in parse_record(C.text_of(r))]
        _, D, div, cost = _budget_run("r4_tight", 0.012)
        budget = int(20000 * 0.012)
        self.assertLessEqual(cost, budget)
        calls = [c for r in D for c in parse_record(C.text_of(r))]
        m = re.search(r"(\d+) call records? left out", div)
        self.assertIsNotNone(m, div)
        self.assertEqual(int(m.group(1)), len(full_calls) - len(calls), "the divider counts the lines left out")
        self.assertGreater(len(calls), 0)
        self.assertEqual([c["head"][:40] for c in calls], [c["head"][:40] for c in full_calls[-len(calls):]],
                         "the lines that stay are the newest")
        n = re.search(r"(\d+) results? left out", div)
        shown_out = sum(1 for c in calls if RESULT_NOT_KEPT.match(c["fields"][-1][1]))
        self.assertEqual(int(n.group(1)) if n else 0, shown_out, "the result count covers only lines that stay")
        for c in calls:
            res = c["fields"][-1][1]
            if not RESULT_NOT_KEPT.match(res):
                self.assertLessEqual(len(C.MARK_RE.sub("", TRAILING_PLACEHOLDERS.sub("", res))), 30 + T.CUT_SLACK)
            for label, v in c["fields"][:-1]:
                if C.MARK_RE.search(v):
                    self.assertEqual(len(C.MARK_RE.sub("", v)), 60, label)
                else:
                    self.assertLessEqual(len(v), 60 + T.CUT_SLACK, label)

    def test_r4_defaults_and_the_record_rate_are_pinned(self):
        # Skylar approved these (11-12): on, 200/150 characters, 5% of a section, at most 10%, counted in real
        # tokens at 2.1 characters per token; results shrink 150 -> 100 -> 60 -> 30 before any is dropped
        self.assertIs(T.DEFAULTS["tool_calls"], True)
        self.assertEqual((T.DEFAULTS["tool_input_chars"], T.DEFAULTS["tool_result_chars"]), (200, 150))
        self.assertEqual(T.DEFAULTS["tool_share"], 0.05)
        self.assertEqual(T.MAX_TOOL_SHARE, 0.10)
        self.assertEqual(T.DESC_CHARS, 80)
        self.assertEqual(T.RECORD_CHARS_PER_TOKEN, 2.1)
        self.assertEqual(tuple(T.RESULT_CAP_STEPS), (100, 60, 30))
        # then inputs to 100 (description stays up to 80), results whole, inputs to 60, lines (coordinator, 11-12)
        self.assertEqual((T.MID_INPUT_CHARS, T.SHORT_INPUT_CHARS), (100, 60))
        for text in ("x" * 21, "y" * 2100, "⟦" + "z" * 999):
            self.assertEqual(T.est_record_tokens(text), real_tokens(text))

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


# R6, as amended 11-12: with tool-calls off, the successor is what 0.3.1 wrote, except that each messages-only
# section's divider gains this one true phrase (agents' reports always rode whole; the old divider said otherwise).
PHRASE = " · task notifications (agents' reports) kept whole"


class R6Off(unittest.TestCase):
    def _both(self, path, st, cfg_over):
        since = T.now_iso()[:19]
        sB, pB, _ = B.build_successor(path, C.deep(st), "#dream same", C.cfg_for(B, **cfg_over))
        sT, pT, _ = T.build_successor(path, C.deep(st), "#dream same", C.cfg_for(T, tool_calls=False, **cfg_over))
        return normalise(pB, since), normalise(pT, since)

    def check(self, path, st, cfg_over):
        old, new = self._both(path, st, cfg_over)
        self.assertEqual(len(old), len(new), "the same records, in the same order")
        dividers = [i for i, line in enumerate(old) if "messages only · tool calls, results and thinking set aside" in line]
        self.assertTrue(dividers)
        for i, (a, b) in enumerate(zip(old, new)):
            if i in dividers:
                self.assertEqual(b.count(PHRASE), 1, "each messages-only divider gains the phrase once")
                self.assertEqual(b.replace(PHRASE, "", 1), a, "and nothing else in it changes")
            else:
                self.assertEqual(a, b, "every other record is byte-for-byte what 0.3.1 wrote")

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
                self.assertNotIn(C.FRAME_PREFIX, text)
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
        self.assertIn("tool calls, results and thinking set aside", C.text_of(s4[2]["div"]),
                      "its divider still says tool calls were set aside")
        self.assertNotIn("quoted record", C.text_of(s3[1]["div"]) + C.text_of(s4[2]["div"]), "and claims no records")
        self.assertTrue(s3[3]["D"] and s4[4]["D"], "sections built by the new code have digests")
        for p in L.paths[1:]:
            self.assertEqual(T.validate(p), [], p.name)


if __name__ == "__main__":
    unittest.main()
