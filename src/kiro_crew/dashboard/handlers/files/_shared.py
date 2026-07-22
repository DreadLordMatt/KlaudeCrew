"""Late-bound ``_sel()`` audit-logger seam shared by the files handler package.

Centralized so tests monkeypatch a single definition and every submodule
resolves the same object.
"""

from __future__ import annotations


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811
    return _pkg.sel()
