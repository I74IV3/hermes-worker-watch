"""plugin/worker-watch — the installer manifest contract (ADR-0025).

``dashboard/manifest.json`` feeds the dashboard tab scanner; the loader and
``hermes plugins validate`` demand a ``plugin.yaml`` at the plugin root whose
declared surface matches what actually registers (catalog admission rule 6:
undeclared capability creep is a security issue). These pins hold that claim:

* the declared route list equals the mounted router's, in both directions;
* the two manifests agree on name and version, and every path ``plugin.yaml``
  names exists;
* the native entry registers no tool, hook or middleware — against a context
  that raises on any other attribute access, so a future ``ctx.register_*``
  cannot slip in as a no-op.

The YAML is read line-wise (the devtools test env has no YAML library); the
route block is asserted to parse, so a reshaped file fails here rather than
silently yielding an empty list.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]  # plugin/worker-watch
PLUGIN_YAML = PLUGIN_ROOT / "plugin.yaml"
DASHBOARD = PLUGIN_ROOT / "dashboard"

REQUIRED_KEYS = ("name", "version", "description", "author", "license", "tags")


def _yaml_text() -> str:
    return PLUGIN_YAML.read_text(encoding="utf-8")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_after(lines: list[str], key: str, start: int = 0) -> tuple[list[str], int]:
    """Lines nested under ``<key>:`` from ``start``, plus the index after the block."""
    for index in range(start, len(lines)):
        line = lines[index]
        if _indent(line) == 0 and line.split(":", 1)[0] == key:
            indent = _indent(lines[index])  # 0 for a top-level key
            block: list[str] = []
            for child in lines[index + 1 :]:
                if child.strip() and _indent(child) <= indent:
                    break
                block.append(child)
            return block, index + 1 + len(block)
    return [], len(lines)


def _scalars(lines: list[str]) -> dict[str, str]:
    """Top-level ``key: value`` entries; a folded ``>-`` block joins its lines."""
    found: dict[str, str] = {}
    for index, line in enumerate(lines):
        if _indent(line) != 0 or not line.strip() or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip()
        if value in (">-", ">", "|", "|-"):
            block, _end = _block_after(lines, key.strip(), index)
            value = " ".join(part.strip() for part in block if part.strip())
        if value:
            found[key.strip()] = value
    return found


def declared_routes(text: str) -> list[str]:
    """The ``dashboard:`` -> ``routes:`` block, one ``"METHOD /path"`` per entry."""
    lines = text.splitlines()
    dashboard, _end = _block_after(lines, "dashboard")
    routes: list[str] = []
    for index, line in enumerate(dashboard):
        if line.strip().startswith("routes:"):
            for entry in dashboard[index + 1 :]:
                if not entry.strip().startswith("- "):
                    break
                routes.append(entry.strip()[2:].strip().strip('"'))
    return routes


def _load_plugin_api():
    spec = importlib.util.spec_from_file_location(
        "worker_watch_plugin_api_manifest_pin", DASHBOARD / "plugin_api.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_native_entry():
    """Exec the plugin root's ``__init__.py`` (a hyphenated dir, so a synthetic name)."""
    spec = importlib.util.spec_from_file_location(
        "worker_watch_plugin_entry_manifest_pin",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def mounted_routes(router) -> list[str]:
    """``["GET /watch", …]`` for every route the dashboard would mount."""
    surface = []
    for route in router.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            surface.append(f"{method} {route.path}")
    return sorted(surface)


class StrictContext:
    """Records ``register_*`` calls; any other attribute access fails the test."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        if name.startswith("register_"):
            return lambda *args, **kwargs: self.calls.append(name)
        raise AssertionError(
            f"register() touched ctx.{name}; this plugin declares no capability and must not"
        )


def test_declared_routes_equal_the_mounted_router() -> None:
    declared = declared_routes(_yaml_text())
    assert declared, "plugin.yaml has no dashboard.routes block to pin"
    assert sorted(declared) == mounted_routes(_load_plugin_api().router)


def test_declared_surface_is_five_reads_and_one_write() -> None:
    declared = declared_routes(_yaml_text())
    assert sorted(m for m, _path in (entry.split(" ", 1) for entry in declared)) == [
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
        "POST",
    ]
    assert [entry for entry in declared if entry.startswith("POST")] == ["POST /restart"]


def test_manifest_agrees_with_the_dashboard_manifest() -> None:
    scalars = _scalars(_yaml_text().splitlines())
    missing = [key for key in REQUIRED_KEYS if not scalars.get(key)]
    assert not missing, f"plugin.yaml missing {missing}"
    dashboard = json.loads((DASHBOARD / "manifest.json").read_text(encoding="utf-8"))
    assert scalars["name"] == dashboard["name"] == "worker-watch"
    assert scalars["version"].strip('"') == dashboard["version"]
    # validator's own field check: name, version, description present
    assert scalars["description"].startswith("Dashboard tab for watching kanban work")


def test_named_dashboard_paths_exist() -> None:
    text = _yaml_text()
    assert "manifest: dashboard/manifest.json" in text
    assert "api: dashboard/plugin_api.py" in text
    assert (PLUGIN_ROOT / "dashboard" / "manifest.json").is_file()
    assert (PLUGIN_ROOT / "dashboard" / "plugin_api.py").is_file()


def test_capability_blocks_are_empty_and_the_write_is_disclosed() -> None:
    text = _yaml_text()
    for block in ("provides_tools:", "provides_hooks:", "capabilities:"):
        assert f"\n{block} []\n" in text, f"{block} must stay an empty list"
    description = _scalars(text.splitlines())["description"]
    assert "read-only" in description
    assert "kanban retry" in description, "the single write path must stay disclosed"
    assert "no telemetry" in description


def test_native_entry_registers_nothing() -> None:
    module = _load_native_entry()
    ctx = StrictContext()
    assert module.register(ctx) is None
    assert ctx.calls == []


def test_the_plugin_root_holds_the_entry_the_loader_needs() -> None:
    """``hermes plugins validate`` refuses a manifest-only dir ("nothing to load")."""
    assert PLUGIN_YAML.is_file()
    assert (PLUGIN_ROOT / "__init__.py").is_file()
    assert not (PLUGIN_ROOT / "desktop").exists(), "no desktop bundle is shipped"
    assert not (PLUGIN_ROOT / "plugin.json").exists(), "not a portable (MCP) package"
