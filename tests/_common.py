"""Shared setup for the offline tests. Import this before anything else.

* Every CLAUDE* variable, TMUX and TERM_PROGRAM are removed from the environment, so no test can
  open a terminal window, read the parent Claude's argv, or signal the parent process.
* TIDES_HOME points at a fresh temporary folder, so nothing touches ~/.claude/tides.
* The module under test is scripts/tides.py of this repo, or the file named by TIDES_UNDER_TEST
  (used to run the suite against tests/baseline/tides_0_3_1.py and watch the new tests fail).
"""
import copy
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

for k in list(os.environ):
    if k.startswith("CLAUDE") or k in ("TMUX", "TERM_PROGRAM", "TIDES_CEILING", "TIDES_SECTIONS",
                                       "TIDES_SECTION_TOKENS", "TIDES_OFF", "TIDES_WAKE",
                                       "DISABLE_COMPACT", "DISABLE_AUTO_COMPACT"):
        del os.environ[k]
os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"     # never "cli": the wake path must not open windows
TMP = Path(tempfile.mkdtemp(prefix="tides_offline_"))
os.environ["TIDES_HOME"] = str(TMP / "home")
os.environ["HOME_FOR_TESTS"] = str(TMP)

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
FIXTURES = TESTS / "fixtures"
BASELINE_PATH = TESTS / "baseline" / "tides_0_3_1.py"
UNDER_TEST = Path(os.environ.get("TIDES_UNDER_TEST") or REPO / "scripts" / "tides.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.copy_to_clipboard = lambda s: False      # never touch the person's clipboard
    return mod


T = _load(UNDER_TEST, "tides_under_test")
B = _load(BASELINE_PATH, "tides_baseline")

FRAME_PREFIX = "⟦tides · record of the tool calls made in the turn above"
MARK_RE = re.compile(r"⟪…(\d+) characters cut…⟫")


def cfg_for(mod, **over):
    c = dict(mod.DEFAULTS)
    c.update(dict(section_tokens=20000, sections=3, wake="manual", window_tokens=200000, ceiling=150000))
    c.update(over)
    return c


def records(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def tier(r):
    return (r.get("tides") or {}).get("tier")


def text_of(r):
    c = (r.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def digest_records(path):
    return [r for r in records(path) if tier(r) == "D"]


def section_of(r):
    return (r.get("tides") or {}).get("sec")


def new_case_dir(name):
    d = TMP / "projects" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_for_stop_curation(sid, path, n_done=0, plan=None, next_id=1, extra=None):
    st = {"seen_start": 1.0, "lineage": sid, "phase": "curating", "trigger": "stop", "cut": None,
          "plan": plan or [], "started": 4102444800.0, "path": str(path), "reminded": False,
          "curation": n_done, "next_id": next_id}
    if n_done:
        st["curated_session"] = True
    st.update(extra or {})
    return st


def deep(x):
    return copy.deepcopy(x)
