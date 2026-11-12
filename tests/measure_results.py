#!/usr/bin/env python3
"""How many tool results a messages-only section keeps under the budget (counts only, never content).

    python3 -I tests/measure_results.py ARCHIVE.jsonl.gz [...]

For each archive (a transcript just before a curation), its previous whole section is what the next curation
turns into a messages-only section. Prints, at a 150k section and at 5% and 10%: calls, results, results shown,
the cap results were shown at, results left out, and call lines left out.
"""
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("TIDES_HOME", tempfile.mkdtemp(prefix="tides_measure_"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import tides  # noqa: E402

for gz in sys.argv[1:]:
    with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as f:
        f.write(gzip.open(gz).read())
        p = f.name
    try:
        T = tides.read_tiers(p, None, 150000)
    finally:
        os.remove(p)
    groups = []
    for i, u in enumerate(tides.units_from(T["F_old"], groups)):
        u["id"] = f"m{i + 1}"
    row = {"archive": Path(gz).name[:3]}
    for share in (0.05, 0.10):
        _, d = tides.build_digests(groups, dict(tides.DEFAULTS, tool_share=share), 150000)
        row[f"{share:.0%}"] = {k: d[k] for k in ("calls", "results", "results_shown", "cap", "results_left_out", "left_out")}
    print(json.dumps(row))
