"""Make the in-place package importable however pytest is invoked. The repo is
run in place (pyproject: package = false), so `python -m pytest` works (CWD on
sys.path) but the bare `pytest` binary does not — this evens them out."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
