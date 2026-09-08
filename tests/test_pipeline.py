"""Tests for what `otowi run` actually does.

There is one thing worth asserting here and it is not arithmetic. The README
lists single-pass routing as a known defect: duarouter gives every driver the
path that is fastest on an empty road, so they all pick the same one and the
model invents congestion on one corridor while its alternatives sit empty.
Iterative assignment was written to fix that -- and for a while it was
reachable only by typing `otowi assign`, while `otowi run`, the command the
README tells people to use, still called the single-pass router. The fix was
in the repository and not in the pipeline, which is indistinguishable from not
having fixed it.

A test that pins the default path to the assignment is the cheapest thing that
would have caught it.
"""

from __future__ import annotations

import argparse

from otowi import cli


def _args(**kwargs) -> argparse.Namespace:
    defaults = dict(window=[6, 9], year=2022, seed=0, scale=1.0, force=False,
                    top=15, iterations=5, internal_only=False, port=8814,
                    no_browser=True, end_padding=10800, verbose=False)
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_run_routes_by_iterative_assignment_not_single_pass(monkeypatch):
    called: list[str] = []

    monkeypatch.setattr(cli.network, "network_path", lambda: _Exists(True))
    monkeypatch.setattr(cli.trips, "trips_path", lambda window: _Exists(True))
    monkeypatch.setattr(cli.simulate, "routes_path", lambda window: _Exists(False))
    monkeypatch.setattr(cli, "cmd_assign", lambda args: called.append("assign"))
    monkeypatch.setattr(cli, "cmd_route", lambda args: called.append("route"))
    monkeypatch.setattr(cli, "cmd_simulate", lambda args: called.append("simulate"))
    monkeypatch.setattr(cli, "cmd_calibrate", lambda args: called.append("calibrate"))

    cli.cmd_run(_args())

    assert "route" not in called, (
        "otowi run used single-pass duarouter; the README calls that a defect")
    assert called == ["assign", "simulate", "calibrate"]


def test_run_skips_routing_when_routes_already_exist(monkeypatch):
    called: list[str] = []

    monkeypatch.setattr(cli.network, "network_path", lambda: _Exists(True))
    monkeypatch.setattr(cli.trips, "trips_path", lambda window: _Exists(True))
    monkeypatch.setattr(cli.simulate, "routes_path", lambda window: _Exists(True))
    monkeypatch.setattr(cli, "cmd_assign", lambda args: called.append("assign"))
    monkeypatch.setattr(cli, "cmd_simulate", lambda args: called.append("simulate"))
    monkeypatch.setattr(cli, "cmd_calibrate", lambda args: called.append("calibrate"))

    cli.cmd_run(_args())

    assert called == ["simulate", "calibrate"]


class _Exists:
    """A stand-in for Path that only has to answer one question."""

    def __init__(self, exists: bool) -> None:
        self._exists = exists

    def exists(self) -> bool:
        return self._exists
