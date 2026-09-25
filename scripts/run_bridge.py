#!/usr/bin/env python3
"""Pinned installed entry; preserve caller cwd so relative CLI paths keep meaning."""
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parents[1]
# Launch with -I: user site/PYTHONPATH/current directory cannot shadow this code.
sys.path.insert(0, str(root))
runpy.run_module("codex_bridge.cli", run_name="__main__", alter_sys=True)
