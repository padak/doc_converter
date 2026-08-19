"""Pytest configuration shared by the whole test suite.

Puts the repository root on ``sys.path`` so ``converter`` is importable when
the tests run without the package having been installed.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def client() -> Iterator["TestClient"]:  # noqa: F821 - imported lazily below
    """A TestClient bound to the conversion service."""
    from fastapi.testclient import TestClient

    from converter.server import app

    with TestClient(app) as test_client:
        yield test_client
