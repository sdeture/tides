#!/usr/bin/env python3
"""Live check of the background-work guard with the plugin loaded (spends real model calls; not part of
run_offline.py). One `claude -p` at a time, in an isolated config dir and project folder, with this repo as the
plugin (--plugin-dir) and Tides state in a temp folder.

  A. wait:  Claude runs `tides begin`, starts a background shell (sleep 45), writes its #dream. The curation must
            wait, then finish after the shell reports, with the report in the newest section, after the dream.
  B. race:  the background shell (sleep 5) ends while Claude is still busy in the same turn (foreground sleep 20),
            before the dream. Where does its report land?
  Both then resume the successor with the wake text and ask the woken Claude what the task reported.

    ANTHROPIC_API_KEY=... python3 tests/live/guard_live.py [A] [B]

Needs an API key in the environment (an isolated config dir has no login). Prints facts and counts only.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CC = os.path.expanduser("~/.claude/local/node_modules/.bin/claude")
SCRIPT = REPO / "scripts" / "tides"


def child_env(cfgdir, home):
    keep = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "LANG", "ANTHROPIC_API_KEY") if k in os.environ}
    keep.update({"CLAUDE_CONFIG_DIR": str(cfgdir), "TIDES_HOME": str(home), "TIDES_HEADLESS": "1"})
    return keep


def claude(cwd, env, prompt, sid=None, resume=None, timeout=900):
    a = [CC, "-p", "--model", "haiku", "--plugin-dir", str(REPO), "--permission-mode", "bypassPermissions",
         "--output-format", "json"]
    a += ["--session-id", sid] if sid else ["--resume", resume]
    t0 = time.time()
    r = subprocess.run(a + [prompt], cwd=cwd, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       timeout=timeout)
    try:
        d = json.loads(r.stdout)
        d = d[-1] if isinstance(d, list) else d
    except Exception:
        d = {"is_error": True, "stderr": r.stderr[-400:], "stdout": r.stdout[-400:]}
    d["_secs"] = round(time.time() - t0, 1)
    return d


def records(p):
    out = []
    for line in open(p, encoding="utf-8"):
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


def text(r):
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return "\n".join(str(b.get("text") or b.get("content") or "") for b in c or [] if isinstance(b, dict))


def where(recs, needle):
    """[(index, type, tier)] of records whose text holds needle."""
    return [(i, r.get("type"), (r.get("tides") or {}).get("tier")) for i, r in enumerate(recs) if needle in text(r)]


def scenario(name, marker, shell, middle):
    base = Path(tempfile.mkdtemp(prefix=f"tides-live-{name}-", dir="/private/tmp"))
    cfgdir, home, cwd = base / "cfg", base / "home", base / "proj"
    for d in (cfgdir, home, cwd):
        d.mkdir()
    env = child_env(cfgdir, home)
    sid = str(uuid.uuid4())
    prompt = (f"This is a test of a memory plugin; follow these steps exactly, in order, in this one reply. "
              f"(1) Run this Bash command and read its output: sh {SCRIPT} begin   "
              f"(2) Start a background Bash command with run_in_background set to true: {shell}   "
              + (f"(3) Run this foreground Bash command and wait for it: {middle}   " if middle else "")
              + f"({'4' if middle else '3'}) End your reply with a short message that starts with #dream and says "
              "one sentence about this test. Do not wait for the background command yourself.")
    d = claude(str(cwd), env, prompt, sid=sid)
    out = {"scenario": name, "first_call_ok": not d.get("is_error"), "secs": d["_secs"], "err": d.get("stderr", "")[:300]}
    st_path = home / "sessions" / f"{sid}.json"
    st = json.loads(st_path.read_text()) if st_path.exists() else {}
    out["state"] = {k: st.get(k) for k in ("phase", "trigger", "superseded_by", "cut_pinned") if k in st}
    out["waited_on"] = [t.get("description") for t in st.get("waiting_on") or []]
    log = (home / "tides.log").read_text() if (home / "tides.log").exists() else ""
    out["log"] = [line[20:140] for line in log.splitlines() if sid[:8] in line][-8:]
    old = next(cfgdir.glob(f"projects/*/{sid}.jsonl"), None)
    if old:
        orec = records(old)
        out["old_transcript"] = {"records": len(orec), "task_notification_at": where(orec, "<task-notification>"),
                                 "marker_at": [h for h in where(orec, marker) if h[1] in ("user", "assistant")
                                               and "follow these steps" not in text(orec[h[0]])],
                                 "dream_at": [i for i, r in enumerate(orec) if r.get("type") == "assistant"
                                              and "#dream" in text(r)][:3],
                                 "notice_at": where(orec, "Tides · curation 1.")[:2],
                                 "waiting_note_at": where(orec, "your dream is saved")[:2]}
    succ = st.get("successor_path")
    if succ and Path(succ).exists():
        srec = records(succ)
        dream_i = next((i for i, r in enumerate(srec) if (r.get("tides") or {}).get("tier") == "dream"), None)
        # the prompt names the marker too: count only what came back from the task
        hits = [h for h in where(srec, marker) if "follow these steps" not in text(srec[h[0]])]
        notes = where(srec, "<task-notification>")
        sys.path.insert(0, str(REPO / "scripts"))
        os.environ["TIDES_HOME"] = str(home)
        import tides
        out["successor"] = {"records": len(srec), "dream_at": dream_i, "marker_at": hits,
                            "marker_after_dream": bool(hits) and dream_i is not None and all(i > dream_i for i, _, _ in hits),
                            "task_notification_at": notes,
                            "notification_after_dream": bool(notes) and dream_i is not None
                                                        and all(i > dream_i for i, _, _ in notes),
                            "validate": tides.validate(Path(succ)) or "ok"}
        wake = json.loads((home / "sessions" / f"{st['superseded_by']}.json").read_text()).get("wake_text", "")
        out["wake_text_tail"] = wake[-400:]
        # resume the successor, as the wake would: the API must accept it, and the woken Claude must see the report
        r = claude(str(cwd), dict(env, TIDES_WAKE="1"), wake + "\n\nOne line, no tools: did a background task's "
                   "report reach you after your dream, and what did its output or summary say?",
                   resume=st["superseded_by"])
        out["resumed"] = {"ok": not r.get("is_error"), "secs": r["_secs"], "reply": (r.get("result") or "")[:300],
                          "err": (r.get("stderr") or "")[:200]}
    out["base"] = str(base)
    return out


if __name__ == "__main__":
    which = sys.argv[1:] or ["A", "B"]
    if "A" in which:
        print(json.dumps(scenario("A", "SHELL-DONE-7731", "sleep 45; echo SHELL-DONE-7731", None), indent=1))
    if "B" in which:
        print(json.dumps(scenario("B", "RACE-DONE-4417", "sleep 5; echo RACE-DONE-4417", "sleep 20"), indent=1))
