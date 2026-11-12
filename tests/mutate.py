#!/usr/bin/env python3
"""Turn the suite on the code: apply one deliberate bug at a time to a copy of scripts/tides.py, run the offline
suite against it (TIDES_UNDER_TEST), and report which tests caught it. A mutant nobody catches is a gap.

    python3 tests/mutate.py            # all mutants
    python3 tests/mutate.py -k gate    # those whose name contains "gate"
"""
import os
import re
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
     "if is_due and tasks_in_flight(path, st):", "if False:"),
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
     'c["in_cap"] = SHORT_INPUT_CHARS\n            redo(i)', "pass"),
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


def run(name, old, new):
    if SRC.count(old) != 1:
        return name, None, f"target found {SRC.count(old)} times"
    d = tempfile.mkdtemp(prefix="tides_mutant_")
    p = Path(d) / "tides.py"
    p.write_text(SRC.replace(old, new), encoding="utf-8")
    env = dict(os.environ, TIDES_UNDER_TEST=str(p))
    r = subprocess.run([sys.executable, str(HERE / "run_offline.py")], env=env, capture_output=True, text=True,
                       timeout=600)
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
