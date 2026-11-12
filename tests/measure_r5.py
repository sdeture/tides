#!/usr/bin/env python3
"""R5: what tool-call records would cost on real archived sections. Prints counts and sizes only, never
conversation content.

    python3 -I tests/measure_r5.py ARCHIVE.jsonl.gz [...]           # one row per archive
    python3 -I tests/measure_r5.py --slice 32000 ARCHIVE.jsonl.gz    # the last 32k tokens of the section only
    python3 -I tests/measure_r5.py --emit DIR ARCHIVE.jsonl.gz       # also write on/off test transcripts of the
                                                                     # middle section (for a real token count)

Each archive is a transcript just before a curation; its previous whole section (F_old) is what the next
curation turns into a messages-only section. Record figures are est_tokens (chars/3.6, the plain estimate) and, in "real~" columns,
est_record_tokens (chars/2.1, calibrated against a real count); message figures are est_tokens.
"""
import gzip
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

os.environ.setdefault("TIDES_HOME", tempfile.mkdtemp(prefix="tides_measure_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tides  # noqa: E402

SETTINGS = (("name+input 120", 120, 0), ("A 200/150", 200, 150), ("B 300/300", 300, 300), ("C 600/800", 600, 800))


def usage(r):
    return tides.usage_total((r.get("message") or {}).get("usage") or {}) if r.get("type") == "assistant" else 0


def digests(F, cfg, section):
    groups = []
    units = tides.units_from(F, groups)
    for i, u in enumerate(units):
        u["id"] = f"m{i + 1}"
    recs, stats = tides.build_digests(groups, cfg, section)
    return units, groups, recs, stats


def measure(gz, slice_tokens=None, emit=None):
    raw = gzip.open(gz).read()
    with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as f:
        f.write(raw)
        p = f.name
    try:
        T = tides.read_tiers(p, None, 150000)
    finally:
        os.remove(p)
    F = T["F_old"]
    us = [usage(r) for r in F if usage(r) and not r.get(tides.MARK, {}).get("tier") == "dream"]
    if slice_tokens and us:
        top = max(us)
        start = next(i for i, r in enumerate(F) if usage(r) and usage(r) >= top - slice_tokens)
        while start > 0 and tides.user_text(F[start]) is None:
            start -= 1          # begin the slice at a human turn
        F = F[start:]
        us = [usage(r) for r in F if usage(r)]
    span = (max(us) - min(us)) if us else 0
    units, groups, _, _ = digests(F, dict(tides.DEFAULTS), 10 ** 9)
    calls = sum(len(p) for _, p in groups)
    m_tok = sum(tides.est_tokens(u["text"]) for u in units)
    reports = sum(tides.est_tokens(u["text"]) for u in units if u["text"].lstrip().startswith("<task-notification>"))
    row = {"archive": Path(gz).name[:3], "section_tok_by_usage": span, "calls": calls,
           "calls_per_1k": round(calls / (span / 1000), 2) if span else None,
           "messages_only_tok": m_tok, "agent_report_tok": reports,
           "agent_report_share": round(reports / m_tok, 2) if m_tok else None}
    S = slice_tokens or 150000
    for label, i, r in SETTINGS:
        cfg = dict(tides.DEFAULTS, tool_input_chars=i, tool_result_chars=r)
        _, _, recs, _ = digests(F, cfg, 10 ** 9)
        d = sum(tides.est_tokens(x["text"]) for x in recs)
        real = sum(tides.est_record_tokens(x["text"]) for x in recs)
        row[label] = d
        row[label + " real~"] = real
        row[label + " real~ %sec"] = round(100 * real / S, 1)
    _, _, recs, stats = digests(F, dict(tides.DEFAULTS), S)
    row["default capped (5%) real~"] = sum(tides.est_record_tokens(x["text"]) for x in recs)
    row["default lines left out"] = stats["left_out"]
    row["default results left out"] = stats["results_left_out"]
    if emit:
        write_pair(units, recs, Path(emit), Path(gz).name[:3] + ("_slice" if slice_tokens else ""))
    return row


def write_pair(units, recs, outdir, tag):
    """Two minimal transcripts holding only the messages-only section, with and without its records, each
    ending in a short prompt, for `claude -p --resume` to count for real."""
    outdir.mkdir(parents=True, exist_ok=True)
    follows = {}
    for d in recs:
        follows.setdefault(d["after"], []).append(d)
    for mode in ("off", "on"):
        sid = str(uuid.uuid4())
        tmpl = {"cwd": str(outdir), "userType": "external", "entrypoint": "cli", "version": "2.1.322"}
        out, parent = [], None

        def add(rec):
            nonlocal parent
            out.append(rec)
            parent = rec["uuid"]

        add(tides.mk("user", "⟦tides · section 1 · messages only⟧", "2026-11-01T00:00:00.000Z", tmpl, sid, parent,
                     {"tier": "div", "n": 1}, is_meta=True))
        for u in units:
            add(tides.mk(u["role"], f"⟦{u['id']}⟧ " + u["text"], "2026-11-01T00:00:01.000Z", tmpl, sid, parent,
                         {"tier": "M", "id": u["id"], "sec": 1}, model="claude-haiku-4-5"))
            if mode == "on":
                for d in follows.get(u["id"], []):
                    add(tides.mk("user", d["text"], "2026-11-01T00:00:01.000Z", tmpl, sid, parent,
                                 {"tier": "D", "sec": 1, "after": u["id"]}, is_meta=True))
        add(tides.mk("assistant", "#dream measuring", "2026-11-01T00:00:02.000Z", tmpl, sid, parent,
                     {"tier": "dream", "n": 1}, model="claude-haiku-4-5"))
        out.append({"type": "last-prompt", "lastPrompt": "", "leafUuid": parent, "sessionId": sid})
        path = outdir / f"{sid}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        problems = tides.validate(path)
        print(json.dumps({"emitted": tag, "mode": mode, "sid": sid, "records": len(out), "validate": problems or "ok",
                          "est_tokens": sum(tides.est_tokens(tides.assistant_text(r) if r["type"] == "assistant"
                                                             else r["message"]["content"]) for r in out if r.get("message"))}))


if __name__ == "__main__":
    args = sys.argv[1:]
    sl = em = None
    if "--slice" in args:
        sl = int(args[args.index("--slice") + 1])
        del args[args.index("--slice"):args.index("--slice") + 2]
    if "--emit" in args:
        em = args[args.index("--emit") + 1]
        del args[args.index("--emit"):args.index("--emit") + 2]
    for gz in args:
        print(json.dumps(measure(gz, sl, em)))
