"""Fixtures applied to every test module."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_imperial_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide the developer's own IMPERIAL_* variables from the tests.

    `Settings` reads credentials and key paths straight from the
    environment, and anyone actually using this tool has those exported.
    Without this, a test asserting "complains that IMPERIAL_USERNAME is
    missing" quietly passes on CI and fails on the maintainer's machine —
    or worse, passes everywhere while silently exercising a different code
    path. Tests that want a value set it explicitly.
    """
    for name in list(os.environ):
        if name.startswith("IMPERIAL_"):
            monkeypatch.delenv(name)
