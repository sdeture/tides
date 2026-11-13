#!/usr/bin/env python3
"""The single entry point for the offline tests. No model calls, no network, nothing outside a temp folder.

    python3 tests/run_offline.py              # the module in scripts/tides.py
    python3 tests/run_offline.py --baseline   # the frozen 0.3.1 module (tests of new behaviour should fail)
    python3 tests/run_offline.py -k guard     # only tests whose name contains "guard"
"""
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
args = sys.argv[1:]
if "--baseline" in args:
    os.environ["TIDES_UNDER_TEST"] = str(HERE / "baseline" / "tides_0_3_1.py")
    args.remove("--baseline")
sys.path.insert(0, str(HERE))
import _common  # noqa: E402  (scrubs the environment and sets TIDES_HOME before anything loads tides)

print(f"module under test: {_common.UNDER_TEST}\ntemp home: {_common.TMP} (removed at exit unless TIDES_KEEP_TMP=1)")
loader = unittest.TestLoader()
if "-k" in args:
    loader.testNamePatterns = [f"*{args[args.index('-k') + 1]}*"]
suite = loader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
