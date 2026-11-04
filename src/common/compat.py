"""Compatibility helpers for the vendored FISCO BCOS Python SDK.

The official FISCO BCOS 2.x Python SDK predates Python 3.10 and still imports
names such as ``collections.Iterable`` that were removed from the standard
library.  Importing :func:`install_collections_shim` before touching the SDK
restores those aliases without modifying the vendored code.

The SDK is vendored under ``vendor/python-sdk`` (Apache-2.0) so the experiment
does not depend on PyPI packages that no longer exist for 3.x chains.
"""
from __future__ import annotations

import collections
import collections.abc
import os
import sys

_REMOVED_ALIASES = (
    "Iterable",
    "Mapping",
    "MutableMapping",
    "Sequence",
    "Set",
    "Hashable",
    "Callable",
    "Generator",
)

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SDK_ROOT = os.path.join(PROJECT_ROOT, "vendor", "python-sdk")


def install_collections_shim() -> None:
    """Restore ``collections.<ABC>`` aliases removed in Python 3.10+."""
    for name in _REMOVED_ALIASES:
        if not hasattr(collections, name):
            setattr(collections, name, getattr(collections.abc, name))


def add_sdk_path() -> str:
    install_collections_shim()
    if SDK_ROOT not in sys.path:
        sys.path.insert(0, SDK_ROOT)
    return SDK_ROOT
