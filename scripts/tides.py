#!/usr/bin/env python3
"""Tides: a continuous transcript for Claude Code.

Every SECTION tokens, Claude curates its own working memory instead of being
compacted. At each curation:
  * the oldest tier (kept memories + the previous messages-only section) is
    re-chosen by Claude, up to a small budget (10% of a section by default);
  * the previous full section keeps its messages but sets aside tool calls,
    tool results and thinking;
  * the newest section stays whole.
Claude then writes a #dream and wakes up in a new transcript built from those
tiers. Nothing is deleted: every pre-curation transcript is archived (gzip).

Standard library only (Python 3.8+). One file on purpose: easy to read, easy to
audit, cheap for a Claude to install.
"""
import gzip
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "0.2.2"
HOME = Path.home()
DATA = Path(os.environ.get("TIDES_HOME", str(HOME / ".claude" / "tides")))
STATES = DATA / "sessions"
ARCHIVE = DATA / "archive"
LOGFILE = DATA / "tides.log"
SCRIPT = Path(__file__).resolve()
CLI = ("sh " + shlex.quote(str(SCRIPT.parent / "tides"))) if (SCRIPT.parent / "tides").exists() \
    else ("python3 " + shlex.quote(str(SCRIPT)))

DEFAULTS = {
    "ceiling": 120000,         # most context Claude lives in, tokens (capped at 80% of the window)
    "sections": 3,             # sections under the ceiling; 3 is the minimum
    "section_tokens": 0,       # explicit section size; 0 = derive from ceiling and sections
    "keep_ratio": 0.10,        # long-term keep budget, as a share of a section
    "nudge_ratio": 1.5,        # mid-task reminder once a section runs this long
    "wake": "auto",            # auto | window | manual
    "wake_command": "",        # e.g. "claude --resume {sid}"; blank = default
    "intro": True,             # one short line at session start telling Claude
    "end_old": True,           # after a new window opens, let the old Claude exit
    "enabled": True,
}

MARK = "tides"                  # custom field on every record Tides writes
TAG_RE = re.compile(r"^⟦(m\d+)⟧ ")
DREAM_RE = re.compile(r"(?m)^[ \t>*_]*#dream\b")
CHAIN = ("user", "assistant", "attachment", "system")
DROP_ATTACHMENTS = {"prompt_snapshot"}   # system prompt re-renders fresh after a tide
CHARS_PER_TOKEN = 3.6


# ───────────────────────────── small utilities ─────────────────────────────

def log(*parts):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


def config(st=None):
    """Global settings, then this conversation's own (`set ... --here`), then env overrides."""
    c = dict(DEFAULTS)
    try:
        c.update(json.loads((DATA / "config.json").read_text()))
    except Exception:
        pass
    if st and st.get("settings"):
        c.update(st["settings"])
    for env, key in (("TIDES_SECTION_TOKENS", "section_tokens"), ("TIDES_CEILING", "ceiling")):
        if os.environ.get(env):
            c[key] = parse_tokens(os.environ[env], c[key])
    if os.environ.get("TIDES_SECTIONS"):
        c["sections"] = int(os.environ["TIDES_SECTIONS"])
    c["sections"] = max(3, int(c.get("sections") or 3))
    return c


def parse_tokens(s, default=None):
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", str(s))
    if not m:
        return default
    n = float(m.group(1)) * {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6}[m.group(2)]
    return int(n)


def ktok(n):
    return f"{n / 1000:.0f}k" if n >= 10000 else (f"{n / 1000:.1f}k" if n >= 1000 else str(int(n)))


def est_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 4


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_minus(ts, ms):
    try:
        t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) - timedelta(milliseconds=ms)
        return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except Exception:
        return ts


def write_json_atomic(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    os.replace(tmp, path)


def load_state(sid):
    try:
        return json.loads((STATES / f"{sid}.json").read_text())
    except Exception:
        return {}


def save_state(sid, st):
    write_json_atomic(STATES / f"{sid}.json", st)


# ───────────────────────────── transcript reading ──────────────────────────

def read_records(path):
    recs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                pass   # a half-written last line
    return recs


def usage_total(u):
    return sum(int(u.get(k) or 0) for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))


def context_tokens(path):
    """Context size at the most recent real API call, from the transcript's usage fields."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    with open(path, "rb") as f:
        for chunk in (256 * 1024, 4 * 1024 * 1024, size):
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", "replace")
            for line in reversed(data.splitlines()):
                if '"usage"' not in line or '"assistant"' not in line or f'"{MARK}"' in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("type") != "assistant" or r.get("isSidechain"):
                    continue
                n = usage_total((r.get("message") or {}).get("usage") or {})
                if n:
                    return n
            if chunk >= size:
                break
    return None


def msg_id(r):
    m = r.get("message")
    return m.get("id") if isinstance(m, dict) else None


def tool_ids(r, kind):
    c = (r.get("message") or {}).get("content")
    if not isinstance(c, list):
        return []
    key = "id" if kind == "tool_use" else "tool_use_id"
    return [b.get(key) for b in c if isinstance(b, dict) and b.get("type") == kind]


def active_records(recs, leaf=None):
    """The records Claude Code would load for this conversation: the path from the
    leaf back to the root, plus the side-branches it hangs parallel tool calls on."""
    by = {r["uuid"]: r for r in recs if r.get("uuid") and r.get("type") in CHAIN}
    if leaf not in by:
        leaf = None
        for r in reversed(recs):
            if r.get("uuid") in by and not r.get("isSidechain"):
                leaf = r["uuid"]
                break
    act = set()
    cur = by.get(leaf)
    while cur is not None and cur["uuid"] not in act:
        act.add(cur["uuid"])
        cur = by.get(cur.get("parentUuid"))
    for _ in range(4):   # satellites: sibling blocks of the same API message, and their tool results
        uses = {t for u in act for t in tool_ids(by[u], "tool_use")}
        grew = False
        for r in recs:
            u = r.get("uuid")
            if u not in by or u in act or r.get("isSidechain"):
                continue
            p = by.get(r.get("parentUuid"))
            if p is None or p["uuid"] not in act:
                continue
            same_msg = r.get("type") == "assistant" and msg_id(r) and msg_id(r) == msg_id(p)
            answers = r.get("type") == "user" and (
                r.get("sourceToolAssistantUUID") in act or any(t in uses for t in tool_ids(r, "tool_result")))
            if same_msg or answers:
                act.add(u)
                grew = True
        if not grew:
            break
    return [r for r in recs if r.get("uuid") in act]


# ───────────────────────────── messages from records ───────────────────────

NOISE_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                  "Caveat: The messages below", "[Request interrupted")


def user_text(r):
    """Text of a human turn, or None if this record is not one (tool result, meta, noise)."""
    if r.get("type") != "user" or r.get("isMeta"):
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        s = c
    elif isinstance(c, list):
        parts = []
        for b in c:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "tool_result":
                return None
            if t == "text":
                parts.append(b.get("text", ""))
            elif t in ("image", "document"):
                parts.append(f"[{t}]")
        s = "\n".join(p for p in parts if p)
    else:
        return None
    s = s.strip()
    if not s or s.startswith(NOISE_PREFIXES):
        return None
    if "<command-name>" in s:
        name = re.search(r"<command-name>(.*?)</command-name>", s, re.S)
        args = re.search(r"<command-args>(.*?)</command-args>", s, re.S)
        s = ((name.group(1) if name else "") + " " + (args.group(1) if args else "")).strip()
        return s or None
    return s


def assistant_text(r):
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    return "\n\n".join(b.get("text", "") for b in c
                       if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip())


def units_from(records):
    """Turn full records into messages-only units: each human turn, and each assistant
    turn with its text blocks joined (tool calls, results, thinking set aside)."""
    units, buf, buf_ts = [], [], None

    def flush():
        nonlocal buf, buf_ts
        text = "\n\n".join(t.strip() for t in buf if t.strip())
        if text:
            units.append({"role": "assistant", "text": text, "ts": buf_ts})
        buf, buf_ts = [], None

    for r in records:
        meta = r.get(MARK) or {}
        if meta.get("tier") == "div":
            continue
        if meta.get("tier") == "dream":
            flush()
            units.append({"role": "assistant", "text": assistant_text(r).strip(), "ts": r.get("timestamp")})
            continue
        if r.get("type") == "user":
            t = user_text(r)
            if t is not None:
                flush()
                units.append({"role": "user", "text": t, "ts": r.get("timestamp")})
        elif r.get("type") == "assistant":
            t = assistant_text(r)
            if t.strip():
                buf.append(t)
                buf_ts = buf_ts or r.get("timestamp")
    flush()
    return [u for u in units if u["text"]]


def unit_from_record(r):
    meta = r.get(MARK) or {}
    if r.get("type") == "user":
        c = (r.get("message") or {}).get("content")
        text = c if isinstance(c, str) else "\n".join(
            b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
    else:
        text = assistant_text(r)
    text = TAG_RE.sub("", text, count=1)
    return {"id": meta.get("id"), "role": r.get("type"), "text": text, "ts": r.get("timestamp"),
            "trunc": meta.get("trunc")}


# ───────────────────────────── reading the tiers ───────────────────────────

def human_prompt_positions(active):
    """[(index_in_active, context_tokens_at_that_prompt)] for every human prompt."""
    out, pending = [], []
    for i, r in enumerate(active):
        if user_text(r) is not None and not (r.get(MARK) or {}):
            pending.append(i)
        elif r.get("type") == "assistant" and not r.get(MARK):
            n = usage_total((r.get("message") or {}).get("usage") or {})
            if n and pending:
                out.extend((j, n) for j in pending)
                pending = []
    out.extend((j, None) for j in pending)
    return out


def read_tiers(path, cut=None, section_tokens=40000):
    """Split the live conversation into K (kept units), M (messages-only units),
    F_old (full records from the last tide), and NEW (records since the last dream)."""
    recs = read_records(path)
    active = active_records(recs, cut)
    if cut:
        ids = [r.get("uuid") for r in active]
        if cut in ids:
            active = active[: ids.index(cut) + 1]
    K = [unit_from_record(r) for r in active if (r.get(MARK) or {}).get("tier") == "K"]
    Ms = {}
    for r in active:
        meta = r.get(MARK) or {}
        if meta.get("tier") == "M":
            u = unit_from_record(r)
            Ms.setdefault(int(meta.get("sec") or meta.get("n") or 0), []).append(u)
    Ms = [(sec, Ms[sec]) for sec in sorted(Ms)]          # oldest section first
    M = [u for _, units in Ms for u in units]
    dream_idx = max((i for i, r in enumerate(active) if (r.get(MARK) or {}).get("tier") == "dream"), default=None)
    if dream_idx is not None:
        f_old = [r for r in active[: dream_idx + 1] if (r.get(MARK) or {}).get("tier") in ("F", "dream")]
        new = active[dream_idx + 1:]
        first = False
    else:
        # Never curated (or compacted since): the last ~section stays whole,
        # everything before it becomes messages-only.
        pos = human_prompt_positions(active)
        total = context_tokens(path) or 0
        start = None
        for i, n in pos:
            if n is not None and n >= total - section_tokens:
                start = i
                break
        if start is None:
            start = pos[-1][0] if pos else 0
        f_old = [r for r in active[:start] if r.get("type") in CHAIN]
        new = active[start:]
        first = True
    return {"records": recs, "active": active, "K": K, "M": M, "Ms": Ms, "F_old": f_old, "NEW": new,
            "first": first}


def split_sections(Ms, N):
    """With N sections, N-2 messages-only sections stay after a curation (the old full
    section becomes the newest of them). Older ones join the kept memories as candidates."""
    k = max(0, len(Ms) + 3 - N)
    return Ms[:k], Ms[k:]          # (to re-choose from, to carry)


def candidates_for(T, cfg):
    demote, _ = split_sections(T["Ms"], cfg["sections"])
    return T["K"] + [u for _, units in demote for u in units]


# ───────────────────────────── building the next transcript ────────────────

def truncate(text, n):
    if not n or len(text) <= 2 * n + 20:
        return text
    return text[:n].rstrip() + " […] " + text[-n:].lstrip()


def parse_keep(args, candidates):
    """keep args like: m3 m7:300 m12-m15 none  →  [(id, trunc)] in priority order."""
    known = {u["id"]: u for u in candidates}
    order = [u["id"] for u in candidates]
    picks, unknown = [], []
    for a in args:
        a = a.strip().strip(",")
        if not a or a.lower() in ("none", "nothing", "--none"):
            continue
        m = re.fullmatch(r"m?(\d+)(?:\s*-\s*m?(\d+))?(?::(\d+))?", a)
        if not m:
            unknown.append(a)
            continue
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        trunc = int(m.group(3)) if m.group(3) else None
        hits = [i for i in order if lo <= int(i[1:]) <= hi]
        if not hits:
            unknown.append(a)
        for i in hits:
            if i not in [p[0] for p in picks]:
                picks.append((i, trunc))
    return picks, unknown, known


def mk(kind, text, ts, tmpl, sid, parent, meta, is_meta=False, model=None):
    rec = {k: tmpl[k] for k in ("cwd", "gitBranch", "userType", "entrypoint", "version") if k in tmpl}
    rec.update({"parentUuid": parent, "isSidechain": False, "type": kind, "uuid": str(uuid.uuid4()),
                "timestamp": ts, "sessionId": sid, MARK: meta})
    if kind == "user":
        rec["message"] = {"role": "user", "content": text}
        if is_meta:
            rec["isMeta"] = True
    else:
        rec["message"] = {"id": "msg_tides_" + uuid.uuid4().hex[:20], "type": "message", "role": "assistant",
                          "model": model or "unknown", "content": [{"type": "text", "text": text}],
                          "stop_reason": "end_turn", "stop_sequence": None,
                          "usage": {"input_tokens": 0, "output_tokens": 0}}
    return rec


def section_tokens_estimate(path, records):
    """Tokens the whole section adds: context now, minus context when it began."""
    start = next((usage_total((r.get("message") or {}).get("usage") or {}) for r in records
                  if r.get("type") == "assistant" and not r.get(MARK)
                  and usage_total((r.get("message") or {}).get("usage") or {})), None)
    now = context_tokens(path) or 0
    return max(0, now - start) if start else 0


def build_successor(path, st, dream_text, cfg):
    """Write the next transcript. Returns (new_session_id, new_path, report dict)."""
    S = section_size(path, st, cfg)
    n = int(st.get("curation", 0)) + 1
    st["cut"] = resolve_cut(path, st)
    if not st["cut"]:
        raise RuntimeError("couldn't find where the curation began")
    T = read_tiers(path, st["cut"], S)
    active = T["active"]
    if not active:
        raise RuntimeError("no conversation found to curate")
    tmpl = next((r for r in reversed(active) if r.get("type") in ("user", "assistant") and r.get("cwd")), active[-1])
    model = next(((r.get("message") or {}).get("model") for r in reversed(active)
                  if r.get("type") == "assistant" and not r.get(MARK)), None)
    sid = str(uuid.uuid4())
    out, parent = [], None
    next_id = int(st.get("next_id", 1))

    def add(rec):
        nonlocal parent
        out.append(rec)
        parent = rec["uuid"]

    # K: chosen from the old K and the messages-only sections leaving the window
    demote, carry = split_sections(T["Ms"], cfg["sections"])
    cands = T["K"] + [u for _, units in demote for u in units]
    picks = st.get("plan") or []
    chosen = {i: tr for i, tr in picks}
    K_new = [dict(u, trunc=chosen[u["id"]] if chosen[u["id"]] else u.get("trunc"))
             for u in cands if u["id"] in chosen]
    # M: the old full section (and the dream at its end), messages only
    M_new = units_from(T["F_old"])
    for u in M_new:
        u["id"] = f"m{next_id}"
        next_id += 1
    NEW = [r for r in T["NEW"] if r.get("type") in CHAIN or r.get("type") == "file-history-snapshot"]

    first_ts = (K_new[0]["ts"] if K_new else (M_new[0]["ts"] if M_new else None)) or now_iso()
    k_tok = sum(est_tokens(truncate(u["text"], u.get("trunc"))) for u in K_new)
    m_tok = sum(est_tokens(u["text"]) for u in M_new) + sum(est_tokens(u["text"]) for _, us in carry for u in us)

    if K_new:
        add(mk("user", f"⟦tides · kept memories · {len(K_new)} you chose at curation {n} from everything "
                       f"before section {n} · ~{ktok(k_tok)} tokens⟧",
               iso_minus(first_ts, 2), tmpl, sid, parent, {"tier": "div", "n": n}, is_meta=True))
        for u in K_new:
            add(mk(u["role"], f"⟦{u['id']}⟧ " + truncate(u["text"], u.get("trunc")), u["ts"] or first_ts,
                   tmpl, sid, parent, {"tier": "K", "id": u["id"], "trunc": u.get("trunc")}, model=model))
    for sec, units in carry + ([(n, M_new)] if M_new else []):
        tok = sum(est_tokens(u["text"]) for u in units)
        add(mk("user", f"⟦tides · section {sec} · messages only · tool calls, "
                       f"results and thinking set aside at curation {sec} · ~{ktok(tok)} tokens⟧",
               iso_minus(units[0]["ts"] or first_ts, 1), tmpl, sid, parent, {"tier": "div", "n": n}, is_meta=True))
        for u in units:
            add(mk(u["role"], f"⟦{u['id']}⟧ " + u["text"], u["ts"] or first_ts, tmpl, sid, parent,
                   {"tier": "M", "id": u["id"], "sec": sec}, model=model))

    # F: the newest section, whole (verbatim records, re-homed into the new session)
    if NEW:
        first_new_ts = next((r.get("timestamp") for r in NEW if r.get("timestamp")), now_iso())
        add(mk("user", f"⟦tides · section {n + 1} · whole⟧", iso_minus(first_new_ts, 1), tmpl, sid, parent,
               {"tier": "div", "n": n}, is_meta=True))
        keep_ids = {r.get("uuid") for r in NEW if r.get("uuid")}
        dropped = {r["uuid"]: r.get("parentUuid") for r in NEW
                   if r.get("type") == "attachment" and (r.get("attachment") or {}).get("type") in DROP_ATTACHMENTS}

        def resolve(p):
            seen = set()
            while p in dropped and p not in seen:
                seen.add(p)
                p = dropped[p]
            return p

        tail = parent
        for r in NEW:
            if r.get("uuid") in dropped:
                continue
            r = json.loads(json.dumps(r))
            if "sessionId" in r:
                r["sessionId"] = sid
            if "session_id" in r:
                r["session_id"] = sid
            if r.get("uuid"):
                p = resolve(r.get("parentUuid"))
                r["parentUuid"] = p if p in keep_ids and p not in dropped else tail
                r[MARK] = {"tier": "F", "n": n}
            out.append(r)
            if r.get("uuid") and r.get("type") in CHAIN:
                parent = r["uuid"]
        # parent of the dream: the live leaf of the section
        leaf = st.get("cut") if st.get("cut") in keep_ids else None
        if leaf:
            parent = leaf

    dream_text = (dream_text or "").strip() or "#dream"
    add(mk("user", f"⟦tides · curation {n} · the dream you wrote before waking⟧", now_iso(), tmpl, sid, parent,
           {"tier": "div", "n": n}, is_meta=True))
    dream = mk("assistant", dream_text, now_iso(), tmpl, sid, parent, {"tier": "dream", "n": n}, model=model)
    add(dream)

    # session metadata: resume leaf, title, permission mode
    for r in reversed(T["records"]):
        if r.get("type") == "permission-mode":
            out.append(dict(r, sessionId=sid))
            break
    title = (st.get("title") or "conversation").strip()[:60]
    out.append({"type": "custom-title", "customTitle": f"🌅 {title} · tide {n}", "sessionId": sid})
    out.append({"type": "last-prompt", "lastPrompt": "", "leafUuid": dream["uuid"], "sessionId": sid})

    new_path = Path(path).parent / f"{sid}.jsonl"
    with open(new_path, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    report = {"n": n, "sid": sid, "path": str(new_path), "K": len(K_new), "K_tok": k_tok,
              "M": len(M_new), "M_tok": m_tok, "F_records": len(NEW),
              "F_tok": section_tokens_estimate(path, NEW),
              "next_id": next_id}
    return sid, new_path, report


# ───────────────────────────── validation ──────────────────────────────────

def validate(path):
    """Rough check that Claude Code can load this: one chain from leaf to root,
    every tool_use answered, and the conversation opens with a user turn."""
    recs = read_records(path)
    leaf = next((r.get("leafUuid") for r in reversed(recs) if r.get("type") == "last-prompt"), None)
    act = active_records(recs, leaf)
    problems = []
    if not act:
        return ["empty"]
    if act[0].get("parentUuid") is not None:
        problems.append("root has a parent")
    first_msg = next((r for r in act if r.get("type") in ("user", "assistant")), None)
    if first_msg is None or first_msg.get("type") != "user":
        problems.append("first message is not a user turn")
    uses, results = [], set()
    for r in act:
        if r.get("type") == "assistant":
            uses += tool_ids(r, "tool_use")
        elif r.get("type") == "user":
            results |= set(tool_ids(r, "tool_result"))
    missing = [u for u in uses if u not in results]
    if missing:
        problems.append(f"{len(missing)} tool call(s) without results")
    orphans = results - set(uses)
    if orphans:
        problems.append(f"{len(orphans)} tool result(s) without a call")
    return problems


# ───────────────────────────── the words Claude reads ──────────────────────

def notice_text(path, st, cfg, reason="due"):
    S = section_size(path, st, cfg)
    B = int(S * cfg["keep_ratio"])
    n = int(st.get("curation", 0)) + 1
    T = read_tiers(path, st.get("cut"), S)
    cands = candidates_for(T, cfg)
    ctx = context_tokens(path) or 0
    lines = [f"🌙 Tides · curation {n}. Your context is ~{ktok(ctx)} tokens. This happens about every "
             f"{ktok(S)} new tokens, and afterwards you wake up in the same conversation with your recent work "
             "in place."]
    if cands:
        lines += [
            "",
            f"1. Choose what to keep from your oldest tier: the messages tagged ⟦{cands[0]['id']}⟧ to "
            f"⟦{cands[-1]['id']}⟧ above. Up to ~{ktok(B)} tokens, most important first. Whatever you leave out "
            "goes to the archive.",
            f"     {CLI} keep ID ID:300 FIRST-LAST",
            "   (`ID:300` keeps a message's first and last 300 characters; `keep none` keeps nothing.)",
        ]
    else:
        lines += ["", "1. Nothing to choose this time: there is no oldest tier yet."]
    lines += [
        f"2. Section {n} keeps its messages. Its tool calls, results and thinking are set aside; if a tool "
        "result matters, carry its gist into your dream."
        + (f" (You keep {cfg['sections'] - 2} messages-only sections; older ones stay as they are.)"
           if cfg["sections"] > 3 else ""),
        f"3. Section {n + 1}, the newest, stays whole.",
        "",
        "Then write your dream: a message that starts with #dream. A note, a review, a poem, whatever you want "
        "to wake up next to. Everything between this notice and the dream is dropped, so think out loud or play "
        f"if you like. Nothing is deleted: the full transcript is archived in {ARCHIVE}/. Mid-task? Finish the "
        f"step first, or run `{CLI} later`.",
    ]
    return "\n".join(lines)


def wake_text(report):
    parts = []
    if report["K"]:
        parts.append(f"{report['K']} kept memories (~{ktok(report['K_tok'])})")
    if report["M"]:
        parts.append(f"section {report['n']} as messages (~{ktok(report['M_tok'])})")
    parts.append(f"section {report['n'] + 1} whole")
    return (f"🌅 Tides · awake after curation {report['n']}. You're in the new transcript: "
            + " → ".join(parts) + " → your dream. Nothing was deleted (archive: "
            f"{ARCHIVE}/). Carry on however you like: the conversation, the task, or a question.")


# ───────────────────────────── waking ──────────────────────────────────────

def applescript_str(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Value-taking options declared in Claude Code 2.1.280, hidden ones included (from the binary).
# `--help` refines this at runtime; anything still unknown is handled by relaunch_args.
KNOWN_ARITY = {}
for _names, _kind in (
    ("--add-dir --allow-tools --allowedTools --allowed-tools --disallowedTools --disallowed-tools --betas --channels "
     "--file --mcp-config --sparse --tag --tools --with -H --header -e --env "
     "--dangerously-load-development-channels", "variadic"),
    ("--cloud --from-pr --json --prompt-suggestions --rc --remote --remote-control --teleport -d --debug "
     "-r --resume -w --worktree", "optional"),
    ("--ablation --advisor --agent --agent-color --agent-id --agent-name --agent-type --agents "
     "--append-subagent-system-prompt --append-subagent-system-prompt-file --append-system-prompt "
     "--append-system-prompt-file --attach-serve --autocompact --callback-port --config --correlation-id --cwd "
     "--debug-file --effort --environment --fallback-model --forward-home-settings --inherit-permission-mode "
     "--input-format --json-schema --judge-model --managed-settings --max-budget-usd --max-cost-usd "
     "--max-thinking-tokens --max-turns --messaging-socket-path --model --output-format --parent-session-id "
     "--permission-mode --permission-prompt-tool --permission-prompts --plan-mode-instructions --plugin-dir "
     "--plugin-dir-no-mcp --plugin-url --pool --prefill --prefill-b64 --project-config-root "
     "--remote-control-session-name-prefix --rewind-files --sdk-url --session-id --setting-sources --settings "
     "--system-prompt --system-prompt-file --system-prompt-snapshot --task-budget --team-name --teammate-mode "
     "--thinking --thinking-display --timeout --watch-artifact --watch-artifact-no-autoreact --workload "
     "-n --name -m --message -t --transport", "value")):
    for _n in _names.split():
        KNOWN_ARITY[_n] = _kind
TEAM_FLAGS = {"--agent-id", "--parent-session-id", "--team-name", "--teammate-mode"}


SESSION_FLAGS = {"-r", "--resume", "-c", "--continue", "--session-id", "--fork-session", "-p", "--print",
                 "--from-pr", "--teleport", "--cloud", "--bg", "--background", "-n", "--name"}
CARRY_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_", "VERTEX_", "CLOUD_ML_", "TIDES_")
CARRY_NAMES = {"CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy",
               "no_proxy", "NODE_EXTRA_CA_CERTS", "MAX_THINKING_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
               "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "DISABLE_COMPACT", "CLAUDE_CODE_SUBAGENT_MODEL"}


def proc_argv(pid):
    """The running Claude's exact argv (macOS: sysctl KERN_PROCARGS2; Linux: /proc)."""
    pid = int(pid)
    try:
        if sys.platform.startswith("linux"):
            data = Path(f"/proc/{pid}/cmdline").read_bytes()
            return [a.decode("utf-8", "replace") for a in data.split(b"\0") if a]
        if sys.platform == "darwin":
            import ctypes
            import ctypes.util
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            argmax = ctypes.c_int()
            size = ctypes.c_size_t(ctypes.sizeof(argmax))
            if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) == 0:
                buf = ctypes.create_string_buffer(argmax.value)
                size = ctypes.c_size_t(argmax.value)
                if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf, ctypes.byref(size), None, 0) == 0:
                    raw = buf.raw[:size.value]
                    argc = int.from_bytes(raw[:4], sys.byteorder)
                    rest = raw[4:]
                    i = rest.find(b"\0")
                    while i < len(rest) and rest[i] == 0:
                        i += 1
                    return [x.decode("utf-8", "replace") for x in rest[i:].split(b"\0")[:argc]]
    except Exception as e:
        log("proc_argv:", repr(e))
    try:
        return shlex.split(subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True,
                                          timeout=5).stdout.strip())
    except Exception:
        return None


def flag_arity(binary):
    """{flag: bool | value | optional | variadic}: the built-in table, refined by this
    Claude Code's own --help."""
    ar = dict(KNOWN_ARITY)
    try:
        out = subprocess.run(binary + ["--help"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
    except Exception:
        out = ""
    for line in out.splitlines():
        m = re.match(r"^  ((?:-\w, )?--[\w-]+(?:, --[\w-]+)*)(?:\s+(<[^>]*>|\[[^\]]*\]))?", line)
        if not m:
            continue
        arg = m.group(2) or ""
        kind = "bool" if not arg else "variadic" if "..." in arg else "value" if arg.startswith("<") else "optional"
        for name in (x.strip() for x in m.group(1).split(",")):
            ar[name] = kind
    return ar


def relaunch_args(args, arity):
    """The launch flags minus session selection and any launch prompt."""
    out, i = [], 0
    while i < len(args):
        t = args[i]
        if t == "--":
            break
        if not t.startswith("-"):
            i += 1                 # a positional prompt: never re-sent
            continue
        name = t.split("=", 1)[0]
        kind = "bool" if "=" in t else arity.get(name)
        if kind is None:   # an option this version doesn't declare: a value has no spaces, a prompt does
            nxt = args[i + 1] if i + 1 < len(args) else ""
            kind = "value" if nxt and not nxt.startswith("-") and not any(c.isspace() for c in nxt) else "bool"
        takes = []
        if kind == "value" and i + 1 < len(args):
            takes = [args[i + 1]]
        elif kind == "optional" and i + 1 < len(args) and not args[i + 1].startswith("-"):
            takes = [args[i + 1]]
        elif kind == "variadic":
            j = i + 1
            while j < len(args) and not args[j].startswith("-"):
                j += 1
            takes = args[i + 1:j]
        if name not in SESSION_FLAGS:
            out += [t] + takes
        i += 1 + len(takes)
    return out


def relaunch_command(old_recs):
    """[binary..., flags...] that starts Claude the way this one was started."""
    argv = proc_argv(os.environ["CLAUDE_PID"]) if os.environ.get("CLAUDE_PID") else None
    if not argv:
        return None
    binary, rest = argv[:1], argv[1:]
    if Path(binary[0]).name.startswith(("node", "bun")) and rest:      # `node cli.js ...`
        binary, rest = argv[:2], argv[2:]
    if not os.path.isabs(binary[0]):
        binary[0] = shutil.which(binary[0]) or os.environ.get("CLAUDE_CODE_EXECPATH") or binary[0]
    args = relaunch_args(rest, flag_arity(binary))
    if any(r.get("type") == "bridge-session" for r in old_recs) and "--remote-control" not in args:
        try:
            cfgdir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
            auto = json.loads((cfgdir / "settings.json").read_text()).get("remoteControlAtStartup")
        except Exception:
            auto = False
        if not auto:
            args.append("--remote-control")   # it was on the phone; keep it there
    return binary + args


def wake_env_file(sid):
    """Route-affecting env vars for the new window, in a private file it reads and deletes."""
    lines = [f"export {k}={shlex.quote(v)}" for k, v in sorted(os.environ.items())
             if k != "TIDES_WAKE" and (k in CARRY_NAMES or k.startswith(CARRY_PREFIXES))]
    d = DATA / "wake"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.env"):
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    if not lines:
        return None
    f = d / f"{sid}.env"
    fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as h:
        h.write("\n".join(lines) + "\n")
    return f


def wake(sid, cwd, cfg, text, inp=None, old_path=None):
    """Open the successor. Returns a short message for the human."""
    inp = inp or {}
    if cfg.get("wake_command"):
        launch = cfg["wake_command"].format(sid=sid, cwd=shlex.quote(cwd)) + " " + shlex.quote(text)
    else:
        argv = relaunch_command(read_records(old_path) if old_path else []) or ["claude"]
        launch = "exec " + " ".join(shlex.quote(a) for a in argv + ["--resume", sid, text])
    envf = wake_env_file(sid)
    load = f"[ -f {shlex.quote(str(envf))} ] && . {shlex.quote(str(envf))}; rm -f {shlex.quote(str(envf))}; " if envf else ""
    inner = f"cd {shlex.quote(cwd)} && {{ {load}TIDES_WAKE=1 {launch}; }}"
    cmd = "/bin/sh -c " + shlex.quote(inner)
    manual = f"claude --resume {sid} " + shlex.quote("🌅 awake")
    mode = cfg.get("wake", "auto")
    term = os.environ.get("TERM_PROGRAM", "")
    entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli")
    opened = False
    if mode in ("auto", "window") and entry == "cli":
        try:
            if os.environ.get("TMUX") and shutil.which("tmux"):
                subprocess.run(["tmux", "new-window", "-c", cwd, cmd], check=True, timeout=15, capture_output=True)
                opened = True
            elif platform.system() == "Darwin" and term in ("Apple_Terminal", "iTerm.app"):
                if term == "Apple_Terminal":
                    script = ['tell application "Terminal"', "activate", f"do script {applescript_str(cmd)}",
                              "end tell"]
                else:
                    script = ['tell application "iTerm"', "activate", "set w to (create window with default profile)",
                              f"tell current session of w to write text {applescript_str(cmd)}", "end tell"]
                args = ["osascript"]
                for line in script:
                    args += ["-e", line]
                subprocess.run(args, check=True, timeout=20, capture_output=True)
                opened = True
        except Exception as e:
            log("wake window failed:", repr(e))
    if opened:
        busy = inp.get("background_tasks") or inp.get("session_crons")
        pid = os.environ.get("CLAUDE_PID")
        if cfg.get("end_old", True) and pid and not busy:
            subprocess.Popen(["sh", "-c", f"sleep 4; kill -TERM {int(pid)} 2>/dev/null"], start_new_session=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return "🌅 Claude curated its memory and is waking up in a new window. This window's Claude will exit in a moment."
        return "🌅 Claude curated its memory and is waking up in a new window. You can close this one."
    title = (load_state(sid).get("title") or "conversation")[:60]
    n = load_state(sid).get("curation", "")
    if entry != "cli":
        if mode != "manual":
            copy_to_clipboard(manual)
        return (f"🌅 Claude curated its memory. Open the session list and choose “🌅 {title} · tide {n}”, "
                f"or in a terminal run: {manual}")
    if mode != "manual":
        copy_to_clipboard(f"/resume {sid}")
    return (f"🌅 Claude curated its memory. To continue, type  /resume  and choose “🌅 {title} · tide {n}” "
            f"(or paste  /resume {sid}  — it's on your clipboard).")


def copy_to_clipboard(s):
    for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip.exe"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=s.encode(), timeout=5, check=True)
                return True
            except Exception:
                pass
    return False


def archive(path, st):
    lineage = st.get("lineage") or Path(path).stem
    dest = ARCHIVE / lineage
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{int(st.get('curation', 0)) + 1:03d}_{Path(path).stem}.jsonl.gz"
    with open(path, "rb") as src, gzip.open(out, "wb") as dst:
        shutil.copyfileobj(src, dst)
    return out


# ───────────────────────────── session bookkeeping ─────────────────────────

def find_transcript(sid, hint=None):
    if hint and Path(hint).exists():
        return Path(hint)
    st = load_state(sid)
    if st.get("path") and Path(st["path"]).exists():
        return Path(st["path"])
    for p in (HOME / ".claude" / "projects").glob(f"*/{sid}.jsonl"):
        return p
    return None


def base_tokens(path, st):
    """Context size right after the last wake (or at the start of a never-curated session)."""
    if st.get("base"):
        return int(st["base"])
    recs = read_records(path)
    active = active_records(recs)
    after = 0
    for i, r in enumerate(active):
        if (r.get(MARK) or {}).get("tier") == "dream":
            after = i + 1
    for r in active[after:]:
        if r.get("type") == "assistant" and not r.get(MARK):
            n = usage_total((r.get("message") or {}).get("usage") or {})
            if n:
                st["base"] = n
                st["curated_session"] = after > 0
                return n
    return 0


def window_tokens(path, cfg):
    """Context window, from the transcript's model record ("[1m]" / "1M context"), else 200k."""
    if cfg.get("window_tokens"):
        return int(cfg["window_tokens"])
    win = 200000
    try:
        for r in read_records(path):
            if r.get("type") == "attachment" and (r.get("attachment") or {}).get("type") == "model":
                ident = (r["attachment"].get("identity") or {})
                big = "[1m]" in str(ident.get("modelId", "")).lower() or "1m context" in str(ident.get("marketingName", "")).lower()
                win = 1000000 if big else 200000
    except Exception:
        pass
    return win


def section_size(path, st, cfg):
    """Tokens per section. Derived so that, at curation time, overhead + every section +
    the keep bank fits under the ceiling even if no section shrinks; the ceiling itself is
    capped at 80% of the window so Claude Code's own compaction never fires first."""
    win = st.get("window") or window_tokens(path, cfg)
    st["window"] = win
    overhead = int(st.get("overhead") or 0)
    ceiling = min(int(cfg.get("ceiling") or DEFAULTS["ceiling"]), int(0.8 * win))
    N = max(3, int(cfg.get("sections") or 3))
    derived = max(8000, int((ceiling - overhead) / (N + cfg["keep_ratio"])))
    S = int(cfg.get("section_tokens") or 0)
    return min(S, derived) if S else derived


def due(path, st, cfg):
    ctx = context_tokens(path)
    if not ctx:
        return False, 0, 0
    base = base_tokens(path, st)
    if not st.get("overhead") and base and not st.get("curated_session") and not st.get("curation"):
        st["overhead"] = base          # system prompt + tools + first prompt, for sizing sections
    S = section_size(path, st, cfg)
    need = S if st.get("curated_session") or st.get("curation") else 2 * S
    if st.get("postponed_to") and ctx < int(st["postponed_to"]):
        return False, ctx, base
    return ctx - base >= need, ctx, base


def last_chain_uuid(path):
    recs = read_records(path)
    for r in reversed(recs):
        if r.get("uuid") and r.get("type") in CHAIN and not r.get("isSidechain"):
            return r["uuid"]
    return None


def resolve_cut(path, st):
    """Where the curation began, found in the transcript itself. (At Stop-hook time
    Claude Code may not have flushed the turn's final message yet, so a cut taken
    then can fall before it.)"""
    recs = read_records(path)
    n = int(st.get("curation", 0)) + 1
    by = {r.get("uuid"): r for r in recs if r.get("uuid")}
    if st.get("trigger") == "stop":
        marker = f"Tides · curation {n}."
        for r in reversed(recs):
            c = (r.get("message") or {}).get("content")
            if (r.get("type") == "user" and r.get("isMeta") and isinstance(c, str)
                    and c.startswith("Stop hook feedback") and marker in c):
                return r.get("parentUuid")
    if st.get("trigger") == "begin":
        active = active_records(recs)
        for i in range(len(active) - 1, -1, -1):
            r = active[i]
            if r.get("type") != "assistant":
                continue
            for b in (r.get("message") or {}).get("content") or []:
                cmd = (b.get("input") or {}).get("command", "") if isinstance(b, dict) and b.get("type") == "tool_use" else ""
                if re.search(r"tides(\.py)?[\"']?\s+(ui\s+)?(begin|now)\b", cmd):
                    mid = msg_id(r)
                    first = next(j for j, x in enumerate(active) if x.get("type") == "assistant" and msg_id(x) == mid)
                    return active[first - 1]["uuid"] if first > 0 else None
    return st.get("cut") if st.get("cut") in by else None


def conversation_title(recs):
    for kind, key in (("custom-title", "customTitle"), ("ai-title", "aiTitle")):
        for r in reversed(recs):
            if r.get("type") == kind and r.get(key):
                return re.sub(r"^🌅\s*|\s*·\s*tide \d+$", "", r[key]).strip()
    return None


def start_curation(sid, path, st, trigger):
    st.update({"phase": "curating", "trigger": trigger, "cut": None, "plan": [], "started": time.time(),
               "path": str(path), "reminded": False})
    st.pop("postponed_to", None)
    save_state(sid, st)


def cancel_curation(sid, st, why):
    st.update({"phase": "idle", "plan": [], "cut": None})
    log(sid[:8], "curation cancelled:", why)
    save_state(sid, st)


def finish_curation(sid, path, st, dream, cfg, cwd):
    """Build, check, archive, and wake. Any failure leaves the old session untouched."""
    st["title"] = st.get("title") or conversation_title(read_records(path)) or Path(cwd).name
    new_sid, new_path, rep = build_successor(path, st, dream, cfg)
    problems = validate(new_path)
    if problems:
        bad = Path(str(new_path) + ".failed")
        os.replace(new_path, bad)
        raise RuntimeError("new transcript failed its check: " + "; ".join(problems) + f" (kept at {bad})")
    arch = archive(path, st)
    succ = {"lineage": st.get("lineage") or sid, "curation": rep["n"], "next_id": rep["next_id"], "phase": "idle",
            "parent": sid, "awaiting_wake": True, "wake_text": wake_text(rep), "title": st["title"],
            "path": str(new_path), "created": time.time(), "curated_session": True, "archive": str(arch),
            "overhead": st.get("overhead"), "window": st.get("window"), "settings": st.get("settings")}
    save_state(new_sid, succ)
    st.update({"phase": "done", "superseded_by": new_sid, "successor_path": str(new_path), "archive": str(arch)})
    save_state(sid, st)
    log(sid[:8], "→", new_sid[:8], json.dumps(rep))
    return new_sid, rep


# ───────────────────────────── hooks ───────────────────────────────────────

def emit(obj):
    if obj:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False))


def hook_stop(inp, cfg):
    sid, cwd = inp.get("session_id"), inp.get("cwd") or os.getcwd()
    path = find_transcript(sid, inp.get("transcript_path"))
    if not sid or not path:
        return
    st = load_state(sid)
    if st.get("superseded_by") or not st.get("seen_start"):
        return   # (no seen_start: Tides was installed after this conversation started)
    cfg = config(st)
    st.setdefault("lineage", sid)
    st["path"] = str(path)
    last = inp.get("last_assistant_message") or ""
    if st.get("phase") == "curating":
        if time.time() - float(st.get("started") or 0) > 3 * 3600:
            return cancel_curation(sid, st, "stale")
        m = DREAM_RE.search(last)
        if m:
            try:
                new_sid, rep = finish_curation(sid, path, st, last[m.start():], cfg, cwd)
            except Exception as e:
                log(sid[:8], "build failed:", repr(e))
                cancel_curation(sid, load_state(sid), "build failed")
                return emit({"systemMessage": f"Tides couldn't build the new transcript ({e}). Nothing changed; "
                                              f"this conversation continues as usual."})
            return emit({"systemMessage": wake(new_sid, cwd, cfg, load_state(new_sid).get("wake_text", ""), inp,
                                               old_path=path)})
        if not st.get("reminded") and not inp.get("stop_hook_active"):
            st["reminded"] = True
            save_state(sid, st)
            return emit({"decision": "block", "reason":
                         "Tides: to finish the curation, end with a message that starts with #dream. "
                         f"(Or run `{CLI} later` to postpone.)"})
        return cancel_curation(sid, st, "stopped without a dream")
    if not cfg.get("enabled", True) or inp.get("stop_hook_active"):
        save_state(sid, st)
        return
    is_due, ctx, base = due(path, st, cfg)
    save_state(sid, st)
    if is_due and TEAM_FLAGS & set(proc_argv(os.environ.get("CLAUDE_PID") or 0) or []):
        st["enabled_here"] = False
        save_state(sid, st)
        return log(sid[:8], "teammate session; Tides stays out of it")
    if is_due and (inp.get("background_tasks") or inp.get("session_crons")):
        log(sid[:8], "due, but background work is running; waiting")
        return
    if is_due:
        start_curation(sid, path, st, "stop")
        emit({"decision": "block", "reason": notice_text(path, st, cfg),
              "systemMessage": "🌙 Claude is curating its memory (Tides). Back in a minute or two."})


def hook_session_start(inp, cfg):
    sid, src = inp.get("session_id"), inp.get("source")
    st = load_state(sid)
    st["seen_start"] = time.time()     # Tides acts only in conversations it saw begin or resume
    save_state(sid, st)
    if st.get("awaiting_wake") and src in ("resume", "startup"):
        st["awaiting_wake"] = False
        st["woke"] = time.time()
        save_state(sid, st)
        if os.environ.get("TIDES_WAKE"):
            return   # the wake text arrives as this session's first prompt
        text = st.get("wake_text") or "🌅 Tides · awake."
        if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli") == "cli":
            # interactive: Claude Code (2.1.x) ignores initialUserMessage here, so pass it as context
            return emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
        return emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "initialUserMessage": text}})
    if src == "compact" and st:
        for k in ("base", "cut", "plan", "postponed_to", "curated_session"):
            st.pop(k, None)
        st["phase"] = "idle"
        st["compacted"] = time.time()
        save_state(sid, st)
        return
    if src == "startup" and cfg.get("intro", True) and cfg.get("enabled", True):
        return emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                     "Tides is on: instead of compaction, every so often you'll curate your own working memory "
                     "(keep what matters, set old tool output aside) and keep going in the same conversation. "
                     f"Nothing is deleted. Ceiling {ktok(cfg['ceiling'])}, {cfg['sections']} sections."}})


def hook_prompt(inp, cfg):
    sid = inp.get("session_id")
    st = load_state(sid)
    if st.get("superseded_by"):
        said = (inp.get("prompt") or "").strip().lower().rstrip(".!")
        if said in ("stay here", "continue here"):
            st["stayed"] = st.pop("superseded_by")
            st["phase"] = "idle"
            path = find_transcript(sid, inp.get("transcript_path"))
            if path:   # not again right away: the person just chose this window
                st["postponed_to"] = (context_tokens(path) or 0) + section_size(path, st, cfg)
            save_state(sid, st)
            return emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                         "(Tides: the person chose to keep talking in this pre-curation window.)"}})
        return emit({"decision": "block", "reason":
                     f"This conversation continues in its curated transcript. To open it: "
                     f"claude --resume {st['superseded_by']}   ·   To keep talking here instead, type: stay here"})
    if st.get("phase") == "curating":
        cancel_curation(sid, st, "the person spoke mid-curation")


def hook_post_tool(inp, cfg):
    if inp.get("agent_id") or inp.get("agent_type"):
        return   # a subagent's tool call; Tides is for the main conversation
    sid = inp.get("session_id")
    st = load_state(sid)
    cfg = config(st)
    if (not cfg.get("enabled", True) or not st.get("seen_start") or st.get("phase") == "curating"
            or st.get("superseded_by")):
        return
    path = find_transcript(sid, inp.get("transcript_path"))
    if not path:
        return
    is_due, ctx, base = due(path, st, cfg)
    S = section_size(path, st, cfg)
    over = ctx - base - (S if st.get("curated_session") or st.get("curation") else 2 * S)
    if is_due and over >= S * (cfg["nudge_ratio"] - 1) and ctx - int(st.get("nudged_at") or 0) >= S // 4:
        st["nudged_at"] = ctx
        save_state(sid, st)
        emit({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext":
              f"🌙 Tides: a curation is due (context ~{ktok(ctx)}). At your next natural pause, between steps, run "
              f"`{CLI} begin`. Your task will be right here when you wake."}})
    else:
        save_state(sid, st)


# ───────────────────────────── commands Claude or the person runs ──────────

def current_session():
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        live = sorted(STATES.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        sid = live[0].stem if live else None
    path = find_transcript(sid) if sid else None
    return sid, path


def cmd_begin(cfg):
    sid, path = current_session()
    if not path:
        return print("Tides: couldn't find this conversation's transcript.")
    st = load_state(sid)
    cfg = config(st)
    st.setdefault("lineage", sid)
    start_curation(sid, path, st, "begin")
    print(notice_text(path, st, cfg))


def cmd_keep(args, cfg):
    sid, path = current_session()
    st = load_state(sid) if sid else {}
    cfg = config(st)
    if st.get("phase") != "curating":
        return print("Tides: no curation is in progress, so there's nothing to keep yet.")
    S = section_size(path, st, cfg)
    B = int(S * cfg["keep_ratio"])
    T = read_tiers(path, st.get("cut"), S)
    cands = candidates_for(T, cfg)
    picks, unknown, known = parse_keep(args, cands)
    plan, used, skipped = [], 0, []
    for i, tr in picks:
        t = est_tokens(truncate(known[i]["text"], tr or known[i].get("trunc")))
        if used + t <= B * 1.05:
            plan.append([i, tr])
            used += t
        else:
            skipped.append(i)
    st["plan"] = plan
    save_state(sid, st)
    msg = [f"Kept {len(plan)} (~{ktok(used)} of {ktok(B)} tokens): " + (" ".join(
        i + (f":{tr}" if tr else "") for i, tr in plan) or "nothing")]
    if skipped:
        msg.append("Over budget, not kept: " + " ".join(skipped) + " (trim with :N, or drop something above them).")
    if unknown:
        msg.append("Not found: " + " ".join(unknown) + f" (this tier is {cands[0]['id']}–{cands[-1]['id']})."
                   if cands else "Not found: " + " ".join(unknown))
    msg.append("You can run keep again to change this. When you're ready, write your #dream.")
    print("\n".join(msg))


def cmd_later(cfg):
    sid, path = current_session()
    st = load_state(sid) if sid else {}
    cfg = config(st)
    ctx = context_tokens(path) if path else 0
    q = section_size(path, st, cfg) // 4 if path else 10000
    st.update({"phase": "idle", "plan": [], "cut": None, "postponed_to": (ctx or 0) + q})
    save_state(sid, st)
    print(f"Tides: postponed. I'll ask again in about {ktok(q)} tokens.")


def cmd_status(cfg):
    sid, path = current_session()
    st = load_state(sid) if sid else {}
    cfg = config(st)
    print(f"Tides {VERSION} · {'on' if cfg.get('enabled', True) else 'off'} · ceiling {ktok(cfg['ceiling'])} · "
          f"{cfg['sections']} sections · keep {cfg['keep_ratio']:.0%} of a section · wake: {cfg.get('wake')}")
    if path:
        is_due, ctx, base = due(path, st, cfg)      # learns the fixed overhead first
        S = section_size(path, st, cfg)
        T = read_tiers(path, None, S)
        need = S if st.get("curated_session") or st.get("curation") else 2 * S
        print(f"This conversation: curation {st.get('curation', 0)} so far · context ~{ktok(ctx)} · "
              f"section ~{ktok(S)} (window {ktok(st.get('window') or 0)}, fixed overhead ~{ktok(st.get('overhead') or 0)}) · "
              f"next curation at ~{ktok(base + need)}" + (" (due now)" if is_due else ""))
        print(f"  kept memories: {len(T['K'])} · messages-only: {len(T['M'])} · whole: "
              f"{len([r for r in T['NEW'] if r.get('type') in ('user', 'assistant')])} records")
    print(f"Archive: {ARCHIVE}/   Settings: {CLI} set ceiling 400k | sections 4 | wake manual | off | on  (add --here for this conversation only)")


def cmd_set(args):
    here = "--here" in args
    args = [a for a in args if a != "--here"]
    sid, _ = current_session()
    st = load_state(sid) if (here and sid) else None
    try:
        c = (st.get("settings") or {}) if st is not None else json.loads((DATA / "config.json").read_text())
    except Exception:
        c = {}
    key, val = (args + ["", ""])[:2]
    if key in ("on", "off"):
        c["enabled"] = key == "on"
    elif key == "ceiling" and parse_tokens(val):
        c["ceiling"] = parse_tokens(val)
    elif key == "sections" and val.isdigit():
        c["sections"] = max(3, int(val))
    elif key == "section" and parse_tokens(val) is not None:
        c["section_tokens"] = parse_tokens(val)
    elif key == "keep" and val:
        c["keep_ratio"] = float(val)
    elif key == "wake" and val in ("auto", "window", "manual"):
        c["wake"] = val
    elif key == "wake_command":
        c["wake_command"] = " ".join(args[1:])
    elif key == "intro" and val:
        c["intro"] = val.lower() in ("1", "on", "true", "yes")
    else:
        return print("usage: set ceiling 400k | sections 4 | section 60k (0 = from ceiling) | keep 0.1 | "
                     "wake auto|window|manual | intro on|off | on | off   [--here = this conversation only]")
    if st is not None:
        st["settings"] = c
        save_state(sid, st)
        where = "this conversation (and its later tides)"
    else:
        write_json_atomic(DATA / "config.json", c)
        where = "every conversation"
    print(f"Tides: set for {where}: " + json.dumps(c))


def main(argv):
    cfg = config()
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd.startswith("hook-"):
        try:
            inp = json.loads(sys.stdin.read() or "{}")
        except Exception:
            inp = {}
        if os.environ.get("CLAUDE_CODE_REMOTE", "").lower() in ("1", "true"):
            return 0   # a cloud session: its transcript lives on Anthropic's side, so Tides can't switch it
        if os.environ.get("TIDES_OFF", "").lower() in ("1", "true", "yes"):
            return 0
        headless = os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli").startswith("sdk")
        if headless and cmd != "hook-start" and os.environ.get("TIDES_HEADLESS") != "1":
            return 0   # `claude -p` and SDK runs: nobody is there to wake the new window, so never curate
        try:
            {"hook-stop": hook_stop, "hook-start": hook_session_start, "hook-prompt": hook_prompt,
             "hook-posttool": hook_post_tool}[cmd](inp, cfg)
        except Exception as e:     # a hook must never break the session
            log("hook error", cmd, repr(e))
        return 0
    if cmd == "ui":   # /tides [now | later | set ... | on | off]
        rest = argv[2:]
        cmd, argv = (rest[0] if rest else "status"), argv[:2] + rest[1:]
        if cmd in ("on", "off"):
            return cmd_set([cmd]) or 0
        if cmd == "now":
            cmd = "begin"
    if cmd == "keep":
        cmd_keep(argv[2:], cfg)
    elif cmd == "begin":
        cmd_begin(cfg)
    elif cmd == "later":
        cmd_later(cfg)
    elif cmd == "set":
        cmd_set(argv[2:])
    elif cmd == "status":
        cmd_status(cfg)
    elif cmd == "validate":
        print(validate(argv[2]) or "ok")
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
