"""Mechanical guard for RFC-001 D2 + RFC-003: the prediction surfaces stay prediction-only.

``/extension/scan`` (RFC-001) and the market guesser (RFC-003) may never
construct the paper engine or reach into the execution package. An approved
scan records the signal + gate outcome only, and a prediction is information —
never a trade intent.

Scope of the ``app.loop`` ban differs by RFC, deliberately:

* ``app.scan`` is **allowed** to import loop *helpers* read-only (RFC-001 D4 —
  ``_exposure_usd``, ``_hours_to_resolution``, …); it may not import
  ``app.execution`` or name ``PaperEngine``.
* ``app.predict`` / ``app.social`` (RFC-003) may import **neither** — the
  guesser must not touch the trading loop at all.

This test parses real identifiers, imports, and module attributes. Module
docstrings are allowed to *mention* the forbidden names.
"""

from __future__ import annotations

import ast
import inspect

import app.predict
import app.scan
import app.social

#: Names that must never be bound anywhere on a prediction surface.
FORBIDDEN_NAMES = {"PaperEngine"}

#: Packages forbidden for every prediction surface.
FORBIDDEN_PACKAGES_ALL = ("app.execution",)

#: Packages additionally forbidden for the RFC-003 guesser modules.
FORBIDDEN_PACKAGES_GUESSER = ("app.execution", "app.loop")

#: (module, forbidden packages) pairs under guard.
GUARDED = (
    (app.scan, FORBIDDEN_PACKAGES_ALL),
    (app.predict, FORBIDDEN_PACKAGES_GUESSER),
    (app.social, FORBIDDEN_PACKAGES_GUESSER),
)


def _collect(module) -> tuple[set[str], set[str]]:
    """Return (identifiers, imported module names) for a module's source."""
    tree = ast.parse(inspect.getsource(module))
    identifiers: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            identifiers.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
    return identifiers, modules


def _bad_modules(modules: set[str], forbidden: tuple[str, ...]) -> list[str]:
    """Imported module names inside a forbidden package."""
    return sorted(
        name
        for name in modules
        if any(
            name == pkg or name.startswith(f"{pkg}.") for pkg in forbidden
        )
    )


def test_prediction_modules_never_name_the_paper_engine():
    for module, _ in GUARDED:
        identifiers, _modules = _collect(module)
        named = identifiers & FORBIDDEN_NAMES
        assert not named, (
            f"{module.__name__} references {sorted(named)} — the prediction "
            "path must stay prediction-only"
        )


def test_prediction_modules_import_no_forbidden_package():
    for module, forbidden in GUARDED:
        _identifiers, modules = _collect(module)
        bad = _bad_modules(modules, forbidden)
        assert not bad, (
            f"{module.__name__} imports {bad} — forbidden on the prediction path"
        )


def test_guesser_modules_never_import_the_trading_loop():
    """RFC-003: the guesser must be fully independent of app.loop."""
    for module in (app.predict, app.social):
        _identifiers, modules = _collect(module)
        bad = _bad_modules(modules, ("app.loop",))
        assert not bad, f"{module.__name__} imports {bad} — the guesser must not touch the loop"


def test_prediction_modules_expose_no_paper_engine():
    for module, _ in GUARDED:
        assert "PaperEngine" not in vars(module), module.__name__
