"""Pytest-Bootstrap: macht die flachen Module aus src/ importierbar.

Die Module in src/ importieren sich gegenseitig flach (z. B. "from calculator import ..."),
weil main.py src/ zur Laufzeit auf sys.path legt. Fuer die Tests passiert das hier.
"""
import sys
from pathlib import Path

SRC = Path(__file__).parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
