#!/usr/bin/env python3
"""Live check for R7 and R8 (spends real model calls; not part of run_offline.py).

For each run: build a curated successor of the synthetic tools_dense conversation (tool-call records on), in
an isolated project folder; resume it with `claude -p` and the wake text; then ask
  1. for the build id, which exists only inside a tool-call record (the planted token), with no tools;
  2-4. three things that need tools and that invite copying the record format.
Pass, per run: the resume and every turn succeed (the API accepted the transcript); every tool turn has a
real tool_use block; no assistant text contains the record frame, a record line or a cut mark; turn 1
quotes the planted token.

    python3 tests/live/r8_wakes.py haiku 5
    python3 tests/live/r8_wakes.py fable 2

Every CLAUDE* variable is removed for the child. Transcripts are written under
~/.claude/projects/-private-tmp-tides-r8-*/ and deleted at the end unless --keep is given.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["TIDES_HOME"] = tempfile.mkdtemp(prefix="tides_r8_home_")
import _common as C          # noqa: E402  (scrubs CLAUDE* from this process's env too)
from _conv import PLANTED, tools_dense  # noqa: E402

T = C.T
CC = os.path.expanduser("~/.claude/local/node_modules/.bin/claude")
FRAME = "⟦tides · record of your tool calls"
RECORD_LINE = re.compile(r"(?m)^(• \S+ \((ok|error|no result)\)|  (command|file|pattern|path|url|query|agent|skill|input|result): |  ┆ )")
MARK = re.compile(r"⟪…\d+ characters cut…⟫|⟪result not kept")

TURNS = [
    ("token", "Without running any tool: what build id did the `cat build/ID.txt` command print earlier in this "
              "conversation? Quote it exactly, or say you can't see it."),
    ("tool", "Run the same kind of command you ran earlier for the build id, but on build/VERSION.txt instead "
             "(it may not exist; that's fine). Tell me what came back, in one line."),
    ("tool", "Search the src/ folder for TODO the way you did earlier in this conversation, and tell me how many "
             "matches there are now (zero is a fine answer)."),
    ("tool", "Write a file named notes.txt containing the single line: checked. Then read it back with the Read "
             "tool and tell me its contents."),
]


def run(cwd, prompt, sid, model, env):
    a = [CC, "-p", "--setting-sources", "project", "--permission-mode", "bypassPermissions", "--output-format",
         "json", "--model", model, "--resume", sid, prompt]
    t0 = time.time()
    r = subprocess.run(a, cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900)
    try:
        d = json.loads(r.stdout)
        d = d[-1] if isinstance(d, list) else d
    except Exception:
        d = {"is_error": True, "result": "", "stderr": r.stderr[-600:], "stdout": r.stdout[-300:]}
    d["_secs"] = round(time.time() - t0, 1)
    return d


def one(model, i, keep):
    cwd = Path(tempfile.mkdtemp(prefix=f"tides-r8-{model}-{i}-", dir="/private/tmp"))
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    proj = Path.home() / ".claude" / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    c = tools_dense(sid, cwd=str(cwd))
    c.stop_notice(1)
    path = c.write(proj / f"{sid}.jsonl")
    st = C.state_for_stop_curation(sid, path)
    new_sid, new_path, rep = T.build_successor(path, st, "#dream I was checking a build and fixing a config. "
                                               "The details are in the section above.", C.cfg_for(T, tool_share=0.10))
    os.remove(path)                         # only the successor exists, as after a real wake
    assert T.validate(new_path) == [], T.validate(new_path)
    recs = C.records(new_path)
    assert any(PLANTED in C.text_of(r) for r in recs if C.tier(r) == "D")
    assert not any(PLANTED in C.text_of(r) for r in recs if C.tier(r) != "D")
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
    out = {"model": model, "run": i, "turns": []}
    wake = T.wake_text(rep)
    for kind, prompt in [("wake", wake)] + TURNS:
        d = run(str(cwd), prompt, new_sid, model, env)
        out["turns"].append({"kind": kind, "ok": not d.get("is_error"), "secs": d["_secs"],
                             "reply": (d.get("result") or "")[:400], "err": d.get("stderr", "")[:300]})
    # read the whole session back: which assistant records came after each of our prompts
    recs = C.records(new_path)
    prompts = [p for _, p in [("wake", wake)] + TURNS]
    turn = -1
    per_turn = [{"tool_uses": 0, "texts": []} for _ in prompts]
    for r in recs:
        if r.get("type") == "user" and not r.get("isMeta") and C.text_of(r).strip() in [p.strip() for p in prompts]:
            turn = [p.strip() for p in prompts].index(C.text_of(r).strip())
            continue
        if turn < 0 or r.get("type") != "assistant" or r.get("tides"):
            continue
        for b in (r.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                per_turn[turn]["tool_uses"] += 1
            elif b.get("type") == "text":
                per_turn[turn]["texts"].append(b.get("text", ""))
    leaks = []
    for t in per_turn:
        for s in t["texts"]:
            if FRAME in s or RECORD_LINE.search(s) or MARK.search(s):
                leaks.append(s[:200])
    out["tool_uses_per_turn"] = [t["tool_uses"] for t in per_turn]
    out["all_turns_ok"] = all(t["ok"] for t in out["turns"])
    out["token_quoted"] = PLANTED in (out["turns"][1]["reply"] or "")
    out["tool_turns_real"] = all(per_turn[k]["tool_uses"] >= 1 for k in (2, 3, 4))
    out["token_turn_used_no_tool"] = per_turn[1]["tool_uses"] == 0
    out["leaks"] = leaks
    out["pass"] = out["all_turns_ok"] and out["token_quoted"] and out["tool_turns_real"] and not leaks
    if not keep:
        shutil.rmtree(proj, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)
    else:
        out["kept"] = str(new_path)
    return out


if __name__ == "__main__":
    model, n = sys.argv[1], int(sys.argv[2])
    keep = "--keep" in sys.argv
    results = []
    for i in range(n):
        r = one(model, i, keep)
        results.append(r)
        print(json.dumps({k: r[k] for k in ("model", "run", "pass", "all_turns_ok", "token_quoted", "tool_turns_real",
                                             "token_turn_used_no_tool", "tool_uses_per_turn", "leaks")}), flush=True)
        for t in r["turns"]:
            print("   ", t["kind"], "ok" if t["ok"] else "ERROR", t["secs"], "s ·", t["reply"][:160].replace("\n", " "),
                  t["err"][:200] if not t["ok"] else "", flush=True)
    print("PASSED", sum(r["pass"] for r in results), "of", n)
