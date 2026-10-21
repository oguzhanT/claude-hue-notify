#!/usr/bin/env python3
"""Old name of lampsignal.py, kept so hooks installed before the rename keep working."""
import os
import runpy
import sys

sys.argv[0] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lampsignal.py")
runpy.run_path(sys.argv[0], run_name="__main__")
