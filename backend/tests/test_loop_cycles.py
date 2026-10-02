"""Bounded loop mode: ``--cycles N`` / ``--once`` run N cycles, then stop.

The bounded run is the supervised proof format: every cycle is logged, the
shutdown is audit-logged, and an engaged kill switch / Ctrl-C still stops it
early. The e2e test drives the real ``run_once`` with the offline cycle
pieces patched (no stories, no state file, disengaged kill switch).
"""

from __future__ import annotations

import pytest

from app.loop import main, run_cycles
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import make_settings


def _quiet_cycle(monkeypatch) -> None:
    """Make one real cycle fully offline and side-effect-free."""
    monkeypatch.setattr("app.loop._fetch_stories", lambda settings, audit: [])
    monkeypatch.setattr("app.loop._load_state", lambda: {})
    monkeypatch.setattr("app.loop._write_state", lambda state: None)
    monkeypatch.setattr(
        "app.loop.read_killswitch",
        lambda: {"engaged": False, "reason": None, "engaged_at": None},
    )


def _audit(settings) -> AuditLog:
    return AuditLog(resolve_db_path(settings.DATABASE_PATH))


# ---------------------------------------------------------------- run_cycles
def test_two_bounded_cycles_run_then_shutdown_is_audit_logged(
    tmp_path, monkeypatch, capsys
):
    settings = make_settings(tmp_path)
    _quiet_cycle(monkeypatch)

    assert run_cycles(settings, 2, interval=0) == 0

    audit = _audit(settings)
    assert len(audit.query("cycle_start")) == 2
    assert len(audit.query("cycle_end")) == 2
    shutdown = audit.query("loop_shutdown")
    assert len(shutdown) == 1
    assert shutdown[0]["payload"]["reason"] == "cycles_exhausted"
    assert shutdown[0]["payload"]["cycles_run"] == 2

    out = capsys.readouterr().out
    assert '"cycles_remaining": 1' in out  # per-cycle progress
    assert "cycles_exhausted" in out


def test_kill_switch_halts_before_any_cycle_and_is_audited(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        "app.loop.read_killswitch",
        lambda: {"engaged": True, "reason": "manual", "engaged_at": "x"},
    )

    assert run_cycles(settings, 3, interval=0) == 0

    audit = _audit(settings)
    assert audit.query("cycle_start") == []  # never ran a cycle
    shutdown = audit.query("loop_shutdown")
    assert len(shutdown) == 1
    assert shutdown[0]["payload"]["reason"] == "kill_switch_engaged"
    assert shutdown[0]["payload"]["cycles_run"] == 0


def test_a_failing_cycle_does_not_abort_the_bounded_run(
    tmp_path, monkeypatch, capsys
):
    settings = make_settings(tmp_path)

    def boom(_settings):
        raise RuntimeError("cycle exploded")

    monkeypatch.setattr("app.loop.run_once", boom)

    assert run_cycles(settings, 2, interval=0) == 0
    out = capsys.readouterr().out
    assert "cycle exploded" in out  # tolerated and reported
    shutdown = _audit(settings).query("loop_shutdown")
    assert shutdown[0]["payload"]["reason"] == "cycles_exhausted"
    assert shutdown[0]["payload"]["cycles_run"] == 2


def test_ctrl_c_stops_unbounded_and_is_audited(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)

    def interrupted(_settings):
        raise KeyboardInterrupt

    monkeypatch.setattr("app.loop.run_once", interrupted)

    assert run_cycles(settings, None, interval=0) == 0
    shutdown = _audit(settings).query("loop_shutdown")
    assert shutdown[0]["payload"]["reason"] == "interrupted"
    assert shutdown[0]["payload"]["cycles_run"] == 0


# ------------------------------------------------------------------------ CLI
def _cli(monkeypatch, tmp_path):
    """Patch the CLI's Settings + run_once; return (settings, recorded calls)."""
    # LOOP_INTERVAL_SEC=0: --watch must not sleep the real 300s default here
    settings = make_settings(tmp_path, LOOP_INTERVAL_SEC=0)
    calls: list[int] = []

    def recording_run_once(_settings):
        calls.append(len(calls) + 1)
        return {"status": "ok", "mode": "PAPER"}

    monkeypatch.setattr("app.loop.Settings", lambda: settings)
    monkeypatch.setattr("app.loop.run_once", recording_run_once)
    return settings, calls


def test_once_is_shorthand_for_one_cycle(tmp_path, monkeypatch):
    settings, calls = _cli(monkeypatch, tmp_path)

    assert main(["--once"]) == 0
    assert len(calls) == 1
    shutdown = _audit(settings).query("loop_shutdown")
    assert shutdown[0]["payload"]["reason"] == "cycles_exhausted"
    assert shutdown[0]["payload"]["cycles_run"] == 1


def test_watch_with_cycles_runs_exactly_n(tmp_path, monkeypatch):
    settings, calls = _cli(monkeypatch, tmp_path)

    assert main(["--watch", "--cycles", "2"]) == 0
    assert len(calls) == 2
    shutdown = _audit(settings).query("loop_shutdown")
    assert shutdown[0]["payload"]["cycles_run"] == 2


def test_plain_cycles_runs_n_without_watch(tmp_path, monkeypatch):
    settings, calls = _cli(monkeypatch, tmp_path)

    assert main(["--cycles", "3"]) == 0
    assert len(calls) == 3
    assert _audit(settings).query("loop_shutdown")[0]["payload"]["cycles_run"] == 3


def test_default_single_cycle_is_unchanged(tmp_path, monkeypatch):
    """No flags: one cycle, no shutdown event — the pre-existing default."""
    settings, calls = _cli(monkeypatch, tmp_path)

    assert main([]) == 0
    assert len(calls) == 1
    assert _audit(settings).query("loop_shutdown") == []


def test_cycles_must_be_positive(tmp_path, monkeypatch):
    _cli(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        main(["--cycles", "0"])
    with pytest.raises(SystemExit):
        main(["--cycles", "-2"])
