#!/usr/bin/env python3
"""Turn the suite on the code: apply one deliberate bug at a time to a copy of scripts/tides.py, run the offline
suite against it (TIDES_UNDER_TEST), and report which tests caught it. A mutant nobody catches is a gap.

    python3 tests/mutate.py            # all mutants
    python3 tests/mutate.py -k gate    # those whose name contains "gate"
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = (HERE.parent / "scripts" / "tides.py").read_text(encoding="utf-8")

MUTANTS = [
    # the guard
    ("finish gate removed (dream finishes while tasks run)",
     'if running and not st.get("force") and not forced_now(path, st, cfg):', "if False:"),
    ("internal task types not filtered",
     'str(t.get("type")) in INTERNAL_TASK_TYPES', "False"),
    ("nudge ignores running tasks",
     "live = tasks_in_flight(path, st) if is_due else []", "live = []"),
    ("begin ignores running tasks",
     "live = tasks_in_flight(path, st)\n    if live and not force:", "live = []\n    if live and not force:"),
    ("compaction always reported off (so a tide is always forceable)",
     "def compaction_off():\n", "def compaction_off():\n    return True\n"),
    ("deferred finish drops what came after the dream",
     'tail = [r for r in tail_after_dream(path, st, st["dream_saved"]) if r.get("type") in CHAIN]', "tail = []"),
    ("wake text says nothing about tasks cut off",
     'if report.get("cut_off"):\n        c = report["cut_off"]', 'if False:\n        c = report["cut_off"]'),
    # the records
    ("budget ignored",
     "budget = int(section * share)", "budget = 10 ** 12"),
    ("share not clamped at 10%",
     'share = min(MAX_TOOL_SHARE, max(0.0, float(cfg.get("tool_share", DEFAULTS["tool_share"]))))',
     'share = max(0.0, float(cfg.get("tool_share", DEFAULTS["tool_share"])))'),
    ("budget cuts newest first",
     'order = [(i, j) for i, r in enumerate(recs) for j in range(len(r["calls"]))]',
     'order = [(i, j) for i, r in enumerate(recs) for j in range(len(r["calls"]))][::-1]'),
    ("results never left out (skip straight to inputs)",
     'c["drop_result"] = True\n            redo(i)', "pass"),
    ("inputs never shortened",
     'c["in_cap"] = to\n                redo(i)', "pass"),
    ("no slack before cutting",
     "if len(s) <= cap + CUT_SLACK:", "if len(s) <= cap:"),
    ("carried sections lose their records",
     'T["Ds"].get(sec, [])', "[]"),
    ("records written as non-meta user turns",
     '{"tier": "D", "sec": sec, "after": after_id}, is_meta=True))',
     '{"tier": "D", "sec": sec, "after": after_id}, is_meta=False))'),
    # validate()
    ("validate: unanswered calls not checked",
     'problems.append(f"{unanswered} tool call(s) not answered in the next message")', "pass"),
    ("validate: orphan results not checked",
     'problems.append(f"{orphans} tool result(s) without a call just before them")', "pass"),
    ("validate: text-before-results not checked",
     'problems.append(f"{late_order} message(s) with text before their tool results")', "pass"),
    ("validate: duplicate ids not checked",
     'problems.append(f"{len(dup)} duplicate tool id(s)")', "pass"),
    ("validate: last-assistant tool call not checked",
     'problems.append("the last assistant message makes a tool call")', "pass"),
    ("validate: tool blocks in Tides records not checked",
     'problems.append(f"{wrote_tools} record(s) Tides wrote hold a tool block")', "pass"),
    ("validate: thinking in Tides records not checked",
     'problems.append(f"{wrote_thinking} record(s) Tides wrote hold a thinking block")', "pass"),
    ("validate: record framing not checked",
     'problems.append(f"{bad_d} tool-call record(s) not framed as a quoted user-side meta record")', "pass"),
    ("validate: first message not checked",
     'problems.append("first message is not a user turn")', "pass"),
]

# The grader's 36 (GRADE.md, "My mutations"), with targets moved to where the code now is. One is now the intended
# behaviour (unknown statuses count as running), so its inverse is here instead.
GRADER = [
    ("G R1 a call with no result is shown as an empty result", 'if c["result"] is None:\n        return NO_RESULT', 'if c["result"] is None:\n        return EMPTY_RESULT'),
    ("G R1 several text blocks of one result are joined with nothing", "text = BLOCK_SEP.join(parts)", 'text = "".join(parts)'),
    ("G R1 description cap 80 -> 2", "DESC_CHARS = 80 ", "DESC_CHARS = 2 "),
    ("G R1 unknown tools show no input at all", 'fields = [("input", json.dumps(rest, ensure_ascii=False, separators=(",", ":")))]', "fields = []"),
    ("G R1 is_error ignored", '"error" if result.get("is_error") else "ok"', '"ok"'),
    ("G R1 parallel calls in reverse block order", 'pend.append((b, results.get(b.get("id"))))', 'pend.insert(0, (b, results.get(b.get("id"))))'),
    ("G R1b carried section's divider forgets it has records", 'T["Sinfo"].get(sec) if tools_on else None)', "None if tools_on else None)"),
    ("G R1b carried records lose their place", 'follows.setdefault(d.get("after"), []).append(d)', 'follows.setdefault("zz" if sec != n else d.get("after"), []).append(d)'),
    ("G R2 cut strips whitespace at the cut edge", "return quote(s[:h]) + CUT_BREAK", "return quote(s[:h].rstrip()) + CUT_BREAK"),
    ("G R2 cut count off by one", 'CUT_BREAK + CUT_MARK.format(k=len(s) - h - t) + quote', 'CUT_BREAK + CUT_MARK.format(k=len(s) - h - t + 1) + quote'),
    ("G R4 default result cap 150 -> 15", '"tool_result_chars": 150,', '"tool_result_chars": 15,'),
    ("G R4 default input cap 200 -> 20", '"tool_input_chars": 200,', '"tool_input_chars": 20,'),
    ("G R4 default share 5% -> 10%", '"tool_share": 0.05,', '"tool_share": 0.10,'),
    ("G R4 default share 5% -> 0.5%", '"tool_share": 0.05,', '"tool_share": 0.005,'),
    ("G R4 records counted at 3.6 chars/token, not 2.1", "RECORD_CHARS_PER_TOKEN = 2.1", "RECORD_CHARS_PER_TOKEN = 3.6"),
    ("G R4 divider's result count also counts dropped lines", 'sum(1 for c in kept if c["drop_result"])', 'sum(1 for c in every if c["drop_result"])'),
    ("G R4 tool-share stored uncapped", 'c["tool_share"] = min(MAX_TOOL_SHARE, share)', 'c["tool_share"] = share'),
    ("G R7 validate: K records exempt from the no-tool-block rule", 'TIDES_WRITTEN = ("K", "M", "D", "div", "dream", "note")', 'TIDES_WRITTEN = ("M", "D", "div", "dream", "note")'),
    ("G R7 validate: duplicate tool_result ids not noticed", "if t in seen_res:\n                        dup.add(t)", "if False:\n                        dup.add(t)"),
    ("G R7 validate: records of one API message not merged", 'if (j is not None and mid and msgs[j]["id"] == mid', 'if (False and msgs[j]["id"] == mid'),
    ("G R7 a failed check no longer stops the wake", "if problems:\n        bad = Path", "if False:\n        bad = Path"),
    ("G R10 0.3.x section gets the records divider", 'T["Sinfo"].get(sec) if tools_on else None)', '{"tools": "records"} if tools_on else None)'),
    ("G R11 (inverse, now intended) a task with an unknown status is not counted",
     'if not isinstance(t, dict) or str(t.get("type")) in INTERNAL_TASK_TYPES:',
     'if not isinstance(t, dict) or str(t.get("type")) in INTERNAL_TASK_TYPES or str(t.get("status")) not in RUNNING_STATUSES:'),
    ("G R11 force margin 90% -> 10%", "FORCE_AT = 0.90", "FORCE_AT = 0.10"),
    ("G R11 the old window is killed even when tasks are running in it", 'if cfg.get("end_old", True) and pid and not busy:', 'if cfg.get("end_old", True) and pid:'),
    ("G R11 a person speaking cancels a waiting curation", 'if st.get("phase") == "curating" and not st.get("dream_saved"):', 'if st.get("phase") == "curating":'),
    ("G R11 a waiting curation is cancelled after 3 minutes, not 3 hours", "> 3 * 3600", "> 3 * 60"),
    ("G R11 completions ever seen (not only since launch) clear a task", 'done.get(key, -1) >= t["since"] - 5', "done.get(key, -1) >= 0"),
    ("G R11 `later` keeps the saved dream", "    for k in WAIT_KEYS:\n        st.pop(k, None)\n    save_state(sid, st)\n    print(f\"Tides: postponed.", "    save_state(sid, st)\n    print(f\"Tides: postponed."),
    ("G R11 Monitor launches not recorded", 'if not (background or (name == "Monitor" and tid)):', "if not background:"),
    ("G R12 the notice's result figure is wrong", "{d['results_shown']} of", "{d['results']} of"),
    ("G R15 waited-for tasks not named in the wake text", 'if report.get("waited"):\n        w = report["waited"]', 'if False:\n        w = report["waited"]'),
    ("G R15 shell tasks get no output path", 'elif launch.get("output"):\n            t["where"] = "output: " + launch["output"]\n        elif t.get("id"):', 'elif launch.get("output"):\n            t["where"] = "output: " + launch["output"]\n        elif False:'),
    ("G R3 thinking leaks into message units", 'return "\\n\\n".join(b.get("text", "") for b in c\n                       if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip())', 'return "\\n\\n".join((b.get("text") or b.get("thinking") or "") for b in c\n                       if isinstance(b, dict) and b.get("type") in ("text", "thinking"))'),
    ("G R1 a tool-only turn's record is anchored to nothing", "calls.append((units[-1] if units else None, list(pend)))", 'calls.append((units[-1] if units and units[-1]["role"] == "assistant" else None, list(pend)))'),
    ("G R1 results matched to the wrong call", 'pend.append((b, results.get(b.get("id"))))', "pend.append((b, next(iter(results.values()), None)))"),
]

# The fix round's own changes, each undone.
FIXROUND = [
    ("F4 quoted text is not quoted", "return LINE_BREAK_RE.sub(lambda m: m.group(0) + QUOTE, s)", "return s"),
    ("F4 a real cut is not on its own line", 'CUT_BREAK = "\\n  "', 'CUT_BREAK = ""'),
    ("F4 validate does not check a record's shape", "elif record_shape_problems(c):", "elif False:"),
    ("F9 a cut may split an emoji sequence", 'while 0 < h < len(s) and (_joins(s[h]) or s[h - 1] == "\\u200d"):', "while False:"),
    ("F9 a non-string description is dropped from the input", 'rest = {k: v for k, v in inp.items() if not (k == "description" and desc is not None)}', 'rest = {k: v for k, v in inp.items() if k != "description"}'),
    ("F9 tool-calls off keeps carried records", 'T["Ds"].get(sec, []) if tools_on else []', 'T["Ds"].get(sec, [])'),
    ("D1 results never shrink evenly", "for step in (s for s in RESULT_CAP_STEPS if s < res_cap):", "for step in ():"),
    ("F1 a cut-off task is also 'waited for'", "if (t.get(\"id\") or t.get(\"description\")) not in still]", "]"),
    ("F2 the person is not told of a hold", 'if not hold.get("told_person"):', "if False:"),
    ("F2 Claude is told of a hold every time", 'if hold and not hold.get("told_claude") and', "if hold and"),
    ("F2 notes are not given at the next prompt", "told = pending_notes(sid, st, None)", 'told = ""'),
    ("F2 status does not say a tide is held", "        if is_due and held:\n            print(", "        if False:\n            print("),
    ("F3 a dropped wait's dream is not saved to a file", 'f.write_text(st.get("dream_saved") or "", encoding="utf-8")', "pass"),
    ("F7 the cut is not pinned while waiting", 'st["cut_pinned"] = bool(st["cut"])', 'st["cut_pinned"] = False'),
    ("F7 a begin-started cut is the latest begin", "return next((cut for printed, cut in hits if printed), hits[0][1])", "return hits[0][1]"),
    ("F8 the fallback scan does not spare the old window", 'busy = busy or inp.get("background_tasks")', 'busy = inp.get("background_tasks")'),
    ("F9 first-turn launches are ignored", 'if float(l.get("t") or 0) >= since:', 'if snap.get("reported") and float(l.get("t") or 0) >= since:'),
    ("F9 turning Tides off leaves a curation waiting", 'if stale or (st.get("dream_saved") and not cfg.get("enabled", True)):', "if stale:"),
    ("W the native-1M table is ignored", "or NATIVE_1M_RE.search(mid))", "or False)"),
    ("W a context past 200k does not imply 1M", "return 1000000 if peak > 200000 and win <= 200000 else win", "return win"),
    ("L the dream hangs from a dropped attachment (0.4.0-pre line)",
     'leaf = resolve(st.get("cut"))\n        leaf = leaf if leaf in written else None',
     'leaf = st.get("cut") if st.get("cut") in {r.get("uuid") for r in NEW} else None'),
    ("L a task report mid-curation cancels it", "if TASK_NOTE_PROMPT.match(inp.get(\"prompt\") or \"\"):", "if False:"),
    ("L a report from the curation region is dropped", "    for note, ts in notes:\n", "    for note, ts in []:\n"),
    ("L a queued report is not read", 'elif r.get("type") == "attachment" and (r.get("attachment") or {}).get("type") == "queued_command":',
     "elif False:"),
    ("L a report is carried twice", "if m.group(0) not in [n for n, _ in out]:", "if True:"),
    ("L the wake text does not count carried reports", 'if report.get("notes_carried") else "")', 'if False else "")'),
    ("L begin looks only under ~/.claude", "for root in roots + [HOME / \".claude\"]:", "for root in [HOME / \".claude\"]:"),
    ("L SessionStart forgets the transcript path", 'st["path"] = str(inp["transcript_path"])', "pass"),
    ("W a state sized by the old rule is never re-sized", 'if st.get("window_rule") == WINDOW_RULE or cfg.get("window_tokens") else None', "if True else None"),
]
ORDER = [   # the coordinator's budget order (11-12): results to 30, inputs to 100, results go, inputs to 60, lines
    ("O results go before inputs reach 100 (the fix round's order)", "    shorten_inputs(MID_INPUT_CHARS)\n", ""),
    ("O inputs go to 60 before any result goes", "shorten_inputs(MID_INPUT_CHARS)", "shorten_inputs(SHORT_INPUT_CHARS)"),
    ("O inputs never reach 60", "    shorten_inputs(SHORT_INPUT_CHARS)\n", ""),
    ("O the middle input step is 60", "MID_INPUT_CHARS = 100", "MID_INPUT_CHARS = 60"),
    ("O inputs shorten newest first", "        for i, j in order:\n            if total <= budget:\n                break\n            c = recs[i][\"calls\"][j]\n            if c[\"in_cap\"] > to:",
     "        for i, j in order[::-1]:\n            if total <= budget:\n                break\n            c = recs[i][\"calls\"][j]\n            if c[\"in_cap\"] > to:"),
]
REGRADE = [   # the regrade at 538d024: N1-N5, the wake's report sentence, and the grader's missed mutants
    ("N1 a tool result's notification text is a report",
     'if not isinstance(src, str) or not src.lstrip().startswith("<task-notification>"):',
     'if not isinstance(src, str):'),
    ("N1 tool_result blocks are read for reports",
     'b.get("text") or "" for b in c or [] if isinstance(b, dict) and b.get("type") == "text")',
     'str(b.get("text") or b.get("content") or "") for b in c or [] if isinstance(b, dict))'),
    ("N2 the notice claims the full input cap", "            shrunk = d[\"inputs_mid\"] + d[\"inputs_short\"]\n",
     "            shrunk = 0\n"),
    ("N2 inputs shortened to 100 are counted as 60", '"inputs_mid": cuts.count(MID_INPUT_CHARS),', '"inputs_mid": 0,'),
    ("N2 'none stay' still gives a length", 'if not d["results_shown"] else', "if False else"),
    ("N3 records off: older sections 'stay as they are'", '("stay as they are.)" if cfg.get("tool_calls", True) else',
     '("stay as they are.)" if True else'),
    ("N4 a report in the tail is carried again", "notes = [(n, ts) for n, ts in notes if n not in in_tail]", "pass"),
    ("N5 a cron alone holds nothing", "    held = (running or []) + crons\n", "    held = running or []\n"),
    ("N5 crons do not stop the nudge or begin", ' + scan + list(snap.get("crons") or [])', " + scan"),
    ("W the wake says every report arrived", "report_arrived=any(k and k in fin for k in (t.get(\"id\"), t.get(\"tool_use_id\")))",
     "report_arrived=True"),
    ("G a carried report loses its origin", 'rec["origin"] = {"kind": "task-notification"}', "pass"),
    ("G SessionStart overwrites a superseded window's path",
     'if inp.get("transcript_path") and not st.get("superseded_by"):', 'if inp.get("transcript_path"):'),
    ("G a non-dict input is shown as {}", 'fields = [("input", json.dumps(raw, ensure_ascii=False, separators=(",", ":")))]',
     'fields = [("input", "{}")]'),
]
MUTANTS = MUTANTS + GRADER + FIXROUND + ORDER + REGRADE


def run(name, old, new):
    if SRC.count(old) != 1:
        return name, None, f"target found {SRC.count(old)} times"
    d = tempfile.mkdtemp(prefix="tides_mutant_")
    p = Path(d) / "tides.py"
    p.write_text(SRC.replace(old, new), encoding="utf-8")
    env = dict(os.environ, TIDES_UNDER_TEST=str(p))
    try:
        r = subprocess.run([sys.executable, str(HERE / "run_offline.py")], env=env, capture_output=True, text=True,
                           timeout=600)
    finally:
        shutil.rmtree(d, True)
    out = r.stdout + r.stderr
    caught = sorted(set(re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)))
    return name, caught, None


if __name__ == "__main__":
    k = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else ""
    missed = 0
    for name, old, new in MUTANTS:
        if k and k not in name:
            continue
        name, caught, err = run(name, old, new)
        if err:
            print(f"?? {name}: {err}")
            missed += 1
        elif not caught:
            print(f"MISSED  {name}")
            missed += 1
        else:
            print(f"caught  {name}: {len(caught)} test(s), e.g. {', '.join(caught[:3])}")
    print("missed:", missed)
