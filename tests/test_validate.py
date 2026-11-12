"""R7: validate() checks what the API checks, and each rule has a mutation that it must catch."""
import json
import unittest

import _common as C
from _conv import chatty, tools_dense
from _flow import Lineage

T = C.T


def successor(name, make=tools_dense, **over):
    c = make()
    c.stop_notice(1)
    path = c.write(C.new_case_dir(name) / f"{c.sid}.jsonl")
    _, new_path, _ = T.build_successor(path, C.state_for_stop_curation(c.sid, path), "#dream v", C.cfg_for(T, **over))
    return new_path


def rewrite(path, recs, suffix):
    out = path.with_name(path.stem + "." + suffix + ".jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out


def blocks(r):
    c = (r.get("message") or {}).get("content")
    return c if isinstance(c, list) else []


def first_index(recs, pred):
    return next(i for i, r in enumerate(recs) if pred(r))


def is_result(r):
    return r.get("type") == "user" and any(b.get("type") == "tool_result" for b in blocks(r))


def is_use(r):
    return r.get("type") == "assistant" and any(b.get("type") == "tool_use" for b in blocks(r))


class R7ValidatePasses(unittest.TestCase):
    def test_r7_every_fixture_successor_passes(self):
        paths = [successor("v_dense_on"), successor("v_dense_off", tool_calls=False), successor("v_chatty", chatty)]
        L = Lineage("v_lineage", dict(sections=4))
        for tag in ("a", "b", "c"):
            L.curate(T, tag=tag, keep_all=(tag == "c"))
        paths += L.paths[1:]
        for p in paths:
            self.assertEqual(T.validate(p), [], p.name)

    def test_r7_raw_fixtures_pass_too(self):
        for make in (tools_dense, chatty):
            c = make()
            p = c.write(C.new_case_dir("v_raw") / f"{c.sid}.jsonl")
            self.assertEqual(T.validate(p), [], make.__name__)


class R7Mutations(unittest.TestCase):
    """Each mutation breaks one rule; validate must name a problem."""

    def setUp(self):
        self.path = successor("v_mut_" + self._testMethodName)
        self.recs = C.records(self.path)
        self.assertEqual(T.validate(self.path), [])

    def caught(self, recs, label):
        p = rewrite(self.path, recs, label)
        problems = T.validate(p)
        self.assertTrue(problems, f"validate missed: {label}")
        return problems

    def test_mut_unanswered_tool_use(self):
        recs = list(self.recs)
        i = first_index(recs, is_result)
        child = next(r for r in recs if r.get("parentUuid") == recs[i]["uuid"])
        child["parentUuid"] = recs[i]["parentUuid"]
        del recs[i]
        self.caught(recs, "unanswered")

    def test_mut_result_not_in_next_message(self):
        recs = list(self.recs)
        i = first_index(recs, is_result)
        res = recs[i]
        # move the result after the next human turn
        j = next(k for k in range(i + 1, len(recs)) if recs[k].get("type") == "user" and not is_result(recs[k])
                 and not recs[k].get("isMeta"))
        nxt = next(r for r in recs if r.get("parentUuid") == res["uuid"])
        nxt["parentUuid"] = res["parentUuid"]
        after_j = next(r for r in recs if r.get("parentUuid") == recs[j]["uuid"])
        res["parentUuid"] = recs[j]["uuid"]
        after_j["parentUuid"] = res["uuid"]
        recs.insert(j + 1, recs.pop(i))
        self.caught(recs, "late_result")

    def test_mut_text_before_result(self):
        recs = list(self.recs)
        i = first_index(recs, is_result)
        res = recs[i]
        txt = json.loads(json.dumps(res))
        txt["uuid"] = "0000000a-0000-4000-8000-000000000001"
        txt["message"] = {"role": "user", "content": [{"type": "text", "text": "an interjection"}]}
        txt.pop("toolUseResult", None)
        txt.pop("sourceToolAssistantUUID", None)
        txt["parentUuid"] = res["parentUuid"]
        res["parentUuid"] = txt["uuid"]
        recs.insert(i, txt)
        self.caught(recs, "text_first")

    def test_mut_duplicate_tool_use_id(self):
        recs = list(self.recs)
        uses = [r for r in recs if is_use(r)]
        a, b = uses[0], uses[1]
        old = [x["id"] for x in blocks(b) if x.get("type") == "tool_use"][0]
        new = [x["id"] for x in blocks(a) if x.get("type") == "tool_use"][0]
        for r in recs:
            for x in blocks(r):
                if x.get("type") == "tool_use" and x.get("id") == old:
                    x["id"] = new
                if x.get("type") == "tool_result" and x.get("tool_use_id") == old:
                    x["tool_use_id"] = new
        self.caught(recs, "dup_id")

    def test_mut_tool_block_in_a_record_tides_wrote(self):
        recs = list(self.recs)
        m = next(r for r in recs if C.tier(r) == "M" and r["type"] == "assistant")
        m["message"]["content"].append({"type": "tool_use", "id": "toolu_planted", "name": "Bash", "input": {"command": "x"}})
        nxt = next(r for r in recs if r.get("parentUuid") == m["uuid"])
        res = {"parentUuid": m["uuid"], "isSidechain": False, "type": "user", "uuid": "0000000b-0000-4000-8000-000000000002",
               "timestamp": m["timestamp"], "sessionId": m["sessionId"], "tides": {"tier": "M", "id": "m999", "sec": 1},
               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_planted", "content": "x"}]}}
        nxt["parentUuid"] = res["uuid"]
        recs.insert(recs.index(m) + 1, res)
        problems = self.caught(recs, "tool_in_M")
        self.assertTrue(any("Tides wrote" in p or "tool block" in p for p in problems), problems)

    def test_mut_thinking_in_a_record_tides_wrote(self):
        recs = list(self.recs)
        m = next(r for r in recs if C.tier(r) == "dream")
        m["message"]["content"].insert(0, {"type": "thinking", "thinking": "x", "signature": "s"})
        self.caught(recs, "thinking_in_dream")

    def test_mut_thinking_in_a_digest_record(self):
        recs = list(self.recs)
        d = next(r for r in recs if C.tier(r) == "D")
        d["message"]["content"] = [{"type": "thinking", "thinking": "x", "signature": "s"},
                                   {"type": "text", "text": d["message"]["content"]}]
        self.caught(recs, "thinking_in_D")

    def test_mut_last_assistant_has_tool_use(self):
        recs = list(self.recs)
        dream = next(r for r in recs if C.tier(r) == "dream")
        dream["message"]["content"].append({"type": "tool_use", "id": "toolu_last", "name": "Bash", "input": {}})
        self.caught(recs, "last_tool_use")

    def test_mut_orphan_result(self):
        recs = list(self.recs)
        i = first_index(recs, is_result)
        recs[i]["message"]["content"].append({"type": "tool_result", "tool_use_id": "toolu_nobody", "content": "?"})
        self.caught(recs, "orphan")

    def test_mut_first_message_assistant(self):
        recs = list(self.recs)
        first = first_index(recs, lambda r: r.get("type") in ("user", "assistant"))
        a = next(r for r in recs if r.get("type") == "assistant")
        a = json.loads(json.dumps(a))
        a["uuid"] = "0000000c-0000-4000-8000-000000000003"
        a["parentUuid"] = None
        recs[first]["parentUuid"] = a["uuid"]
        recs.insert(first, a)
        self.caught(recs, "assistant_first")

    def test_mut_digest_not_meta_or_unframed(self):
        recs = list(self.recs)
        d = next(r for r in recs if C.tier(r) == "D")
        d["message"]["content"] = "• Bash (ok)\n  command: ls"
        self.caught(recs, "unframed_D")


if __name__ == "__main__":
    unittest.main()
