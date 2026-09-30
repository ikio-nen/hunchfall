"""Mechanical guard for RFC-001 decision D2: ``/extension/scan`` is prediction-only.

The scan pipeline may never construct the paper engine — an approved scan
records the signal and gate outcome only. This test fails if ``app.scan`` ever
imports ``PaperEngine`` / the ``app.execution`` package or binds the name at
runtime. The module docstring is allowed to *mention* PaperEngine; only real
identifiers and imports are checked.
"""

from __future__ import annotations

import ast
import inspect

import app.scan

FORBIDDEN_NAMES = {"PaperEngine"}
FORBIDDEN_PACKAGE = "app.execution"


def _module_tree() -> ast.Module:
    return ast.parse(inspect.getsource(app.scan))


def test_scan_source_never_names_the_paper_engine():
    identifiers: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            identifiers.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    assert not identifiers & FORBIDDEN_NAMES, (
        f"app/scan.py references {sorted(identifiers & FORBIDDEN_NAMES)} — "
        "the extension scan must stay prediction-only"
    )
    bad_modules = sorted(
        name
        for name in modules
        if name == FORBIDDEN_PACKAGE or name.startswith(f"{FORBIDDEN_PACKAGE}.")
    )
    assert not bad_modules, (
        f"app/scan.py imports {bad_modules} — paper execution is forbidden on "
        "the extension scan path"
    )


def test_scan_module_exposes_no_paper_engine():
    assert "PaperEngine" not in vars(app.scan)
