"""Drive whole curations offline: the stop notice, the module's own finish_curation, the woken
session continuing in the successor file, the next curation."""
import json
import zlib
from pathlib import Path

import _common as C
from _conv import Conv, tools_dense, work

CWD = "/tmp/tides-fixture"


def candidates(mod, path, st, cfg):
    S = mod.section_size(path, st, cfg)
    return mod.candidates_for(mod.read_tiers(path, mod.resolve_cut(path, st), S), cfg)


class Lineage:
    """A conversation taken through curations. mods: the module that performs each curation."""

    def __init__(self, name, cfg_over=None, first=None, sid=None):
        self.dir = C.new_case_dir(name)
        sid = sid or "33333333-3333-4333-8333-" + format(zlib.crc32(name.encode()) % 10 ** 12, "012d")
        c = first(sid) if first else tools_dense(sid)
        c.stop_notice(1)
        self.path = Path(c.write(self.dir / f"{sid}.jsonl"))
        self.sid = sid
        self.cfg_over = cfg_over or {}
        self.st = C.state_for_stop_curation(sid, self.path)
        self.paths = [self.path]
        self.reports = []

    def cfg(self, mod):
        return C.cfg_for(mod, **self.cfg_over)

    def curate(self, mod, plan=None, dream="#dream fixture dream", keep_all=False, tag=None, turns=3):
        cfg = self.cfg(mod)
        if keep_all:
            plan = [[u["id"], None] for u in candidates(mod, self.path, self.st, cfg)]
        self.st["plan"] = plan or []
        mod.save_state(self.sid, self.st)
        new_sid, rep = mod.finish_curation(self.sid, self.path, self.st, dream, cfg, CWD)
        self.reports.append(rep)
        n = rep["n"]
        self.sid, self.path = new_sid, Path(rep["path"])
        self.paths.append(self.path)
        # the woken session goes on working in the successor
        c = Conv.resume(self.path, start=f"2026-11-0{min(9, n + 2)}T10:00:00")
        work(c, n, tag or f"after{n}", n_turns=turns)
        c.stop_notice(n + 1)
        c.write(self.path)
        st = mod.load_state(self.sid)
        st.update({"seen_start": 1.0, "phase": "curating", "trigger": "stop", "cut": None, "plan": [],
                   "started": 4102444800.0, "reminded": False})
        self.st = st
        return rep


def sections(path):
    """{sec: {"M": [records], "D": [records], "div": record}} of a successor Tides wrote."""
    out = {}
    cur = None
    for r in C.records(path):
        t = C.tier(r)
        if t == "div" and "messages only" in C.text_of(r):
            cur = int(C.text_of(r).split("section ", 1)[1].split(" ", 1)[0])
            out.setdefault(cur, {"M": [], "D": [], "div": r})
        elif t in ("M", "D"):
            out.setdefault(C.section_of(r), {"M": [], "D": [], "div": None})[t].append(r)
    return out
