"""Responsive layout guard: phone / tablet / desktop, awkward data (long names, many resources, error + transition states).

Fails on sideways scrolling, elements outside the viewport, clipped or spilling text, and tap targets under 38px.
Screenshots land in shots/audit/ for a visual look. Details: scripts/ui_audit.py
"""
import sys
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def test_layout_is_clean_at_all_widths():
    import ui_audit
    try:
        findings = ui_audit.run()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    assert findings == [], "\n".join(findings)
