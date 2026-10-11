"""Test harness that runs without Home Assistant installed.

Stubs just enough of ``homeassistant`` for the tool modules to import, and registers
``custom_components.cooper`` packages without executing their heavy ``__init__``.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]


def _install() -> None:
    class Tool:  # stands in for homeassistant.helpers.llm.Tool
        pass

    for name in (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.entity_registry",
        "homeassistant.helpers.area_registry",
        "homeassistant.helpers.floor_registry",
        "homeassistant.helpers.intent",
        "homeassistant.helpers.llm",
        "homeassistant.util",
        "homeassistant.util.json",
        "homeassistant.util.ulid",
    ):
        sys.modules.setdefault(name, MagicMock(name=name))
    # `from pkg import sub` reads the attribute off the parent, so link them up.
    for name in list(sys.modules):
        if name.startswith("homeassistant.") and name.count(".") >= 1:
            parent, _, child = name.rpartition(".")
            setattr(sys.modules[parent], child, sys.modules[name])
    sys.modules["homeassistant.helpers.llm"].Tool = Tool
    sys.modules["homeassistant.core"].callback = lambda f: f
    sys.modules["homeassistant.core"].split_entity_id = lambda e: e.split(".", 1)

    for pkg, rel in (
        ("custom_components", "custom_components"),
        ("custom_components.cooper", "custom_components/cooper"),
        ("custom_components.cooper.llm_api", "custom_components/cooper/llm_api"),
    ):
        mod = types.ModuleType(pkg)
        mod.__path__ = [str(ROOT / rel)]
        sys.modules[pkg] = mod


_install()
