"""Tests for "when should I leave".

The whole value of this feature is that the answer *changes with departure
time*. A bug that silently collapses the time dimension — using one average
travel time for the whole morning — produces a confident, plausible, and
completely useless answer: every departure looks identical. Most of these
tests exist to catch exactly that.
"""

from __future__ import annotations

import pytest

from otowi import journey
from otowi.journey import TravelTimes


class FakeEdge:
    def __init__(self, edge_id, length=1000.0, speed=25.0, name=""):
        self._id, self._length, self._speed, self._name = edge_id, length, speed, name
        self.outgoing: list[FakeEdge] = []

    def getID(self):
        return self._id

    def getLength(self):
        return self._length

    def getSpeed(self):
        return self._speed

    def getName(self):
        return self._name

    def getOutgoing(self):
        return self.outgoing

    def allows(self, vclass):
        return True


class FakeNet:
    def __init__(self, edges):
        self._edges = {e.getID(): e for e in edges}

    def getEdges(self):
        return list(self._edges.values())

    def getEdge(self, edge_id):
        return self._edges[edge_id]


def chain(*ids):
    """A -> B -> C ... returning the net and the edges."""
    edges = [FakeEdge(i) for i in ids]
    for a, b in zip(edges, edges[1:]):
        a.outgoing.append(b)
    return FakeNet(edges), edges


def times_with(interval_s, per_interval, free_flow):
    t = TravelTimes(interval_s=interval_s, free_flow=free_flow)
    t._by_interval = per_interval
    return t


# ------------------------------------------------------------ travel times


def test_cost_uses_the_interval_the_vehicle_actually_enters_in():
    """The point of the whole feature."""
    t = times_with(900, {0: {"e": 60.0}, 1: {"e": 600.0}}, {"e": 40.0})
    assert t.cost("e", 0) == 60.0        # 00:00, first interval
    assert t.cost("e", 1000) == 600.0    # past 900 s, second interval


def test_an_edge_with_no_data_in_an_interval_falls_back_to_free_flow():
    """No vehicles measured is an empty road, not an impassable one."""
    t = times_with(900, {0: {"other": 60.0}}, {"e": 40.0})
    assert t.cost("e", 0) == 40.0


def test_times_past_the_window_clamp_to_the_last_known_interval():
    """A journey running past the simulated window is charged the last
    evidence available, rather than crashing or silently becoming free-flow."""
    t = times_with(900, {0: {"e": 60.0}, 1: {"e": 300.0}}, {"e": 40.0})
    assert t.cost("e", 999999) == 300.0


def test_negative_times_clamp_to_the_first_interval():
    t = times_with(900, {0: {"e": 60.0}}, {"e": 40.0})
    assert t.cost("e", -500) == 60.0


def test_with_no_interval_data_at_all_everything_is_free_flow():
    t = times_with(900, {}, {"e": 42.0})
    assert t.cost("e", 0) == 42.0


# ------------------------------------------------------- routing over time


def test_a_route_is_found_along_a_chain():
    net, edges = chain("a", "b", "c")
    t = times_with(900, {0: {"a": 10.0, "b": 20.0, "c": 30.0}}, {})
    result = journey.route_at(net, t, "a", "c", 0.0)

    assert result.arrived
    assert result.duration_s == pytest.approx(50.0)
    assert [leg.edge_id for leg in result.legs] == ["b", "c"]


def test_the_same_trip_costs_more_when_it_departs_into_the_peak():
    """If this ever fails, the tool has nothing to say."""
    net, _ = chain("a", "b", "c")
    t = times_with(
        900,
        {0: {"a": 10.0, "b": 20.0, "c": 20.0},      # quiet
         1: {"a": 10.0, "b": 200.0, "c": 200.0}},   # peak
        {},
    )
    quiet = journey.route_at(net, t, "a", "c", 0.0)
    peak = journey.route_at(net, t, "a", "c", 1000.0)

    assert peak.duration_s > quiet.duration_s * 5


def test_cost_is_charged_for_when_each_edge_is_reached_not_for_departure():
    """A long first leg pushes the second leg into the next interval."""
    net, _ = chain("a", "b", "c")
    t = times_with(
        900,
        {0: {"b": 1000.0, "c": 10.0},     # b is slow and spans the interval
         1: {"b": 1000.0, "c": 500.0}},   # by the time we reach c it is slow
        {},
    )
    result = journey.route_at(net, t, "a", "c", 0.0)
    # Entering b at 0 costs 1000 s, so c is entered at 1000 s -> interval 1.
    assert result.duration_s == pytest.approx(1500.0)


def test_the_faster_of_two_paths_wins():
    a, fast, slow, dest = (FakeEdge("a"), FakeEdge("fast"),
                           FakeEdge("slow"), FakeEdge("dest"))
    a.outgoing = [fast, slow]
    fast.outgoing = [dest]
    slow.outgoing = [dest]
    net = FakeNet([a, fast, slow, dest])

    t = times_with(900, {0: {"fast": 10.0, "slow": 500.0, "dest": 5.0}}, {})
    result = journey.route_at(net, t, "a", "dest", 0.0)

    assert [leg.edge_id for leg in result.legs] == ["fast", "dest"]


def test_congestion_can_flip_which_path_is_better():
    """Time-dependent routing has to be able to change its mind."""
    a, hwy, back, dest = (FakeEdge("a"), FakeEdge("hwy"),
                          FakeEdge("back"), FakeEdge("dest"))
    a.outgoing = [hwy, back]
    hwy.outgoing = [dest]
    back.outgoing = [dest]
    net = FakeNet([a, hwy, back, dest])

    t = times_with(
        900,
        {0: {"hwy": 10.0, "back": 100.0, "dest": 5.0},    # highway is fine
         1: {"hwy": 900.0, "back": 100.0, "dest": 5.0}},  # highway is jammed
        {},
    )
    early = journey.route_at(net, t, "a", "dest", 0.0)
    late = journey.route_at(net, t, "a", "dest", 1000.0)

    assert [leg.edge_id for leg in early.legs] == ["hwy", "dest"]
    assert [leg.edge_id for leg in late.legs] == ["back", "dest"]


def test_an_unreachable_destination_is_reported_not_faked():
    a, b = FakeEdge("a"), FakeEdge("b")   # no link between them
    net = FakeNet([a, b])
    result = journey.route_at(net, times_with(900, {0: {}}, {}), "a", "b", 0.0)

    assert not result.arrived
    assert result.duration_s is None


def test_a_trip_to_where_you_already_are_takes_no_time():
    net, _ = chain("a", "b")
    result = journey.route_at(net, times_with(900, {0: {}}, {}), "a", "a", 0.0)
    assert result.duration_s == 0.0


# ----------------------------------------------------------------- sweep


def test_sweep_covers_the_window_at_the_requested_spacing():
    net, _ = chain("a", "b")
    t = times_with(900, {0: {"b": 60.0}}, {})
    results = journey.sweep(net, t, "a", "b", window=(6, 9), every_minutes=30)

    assert len(results) == 6                       # three hours, every 30 min
    assert [r.depart_s for r in results] == [0, 1800, 3600, 5400, 7200, 9000]


def test_sweep_reflects_a_peak_in_the_middle_of_the_window():
    net, _ = chain("a", "b")
    t = times_with(
        3600,
        {0: {"b": 60.0}, 1: {"b": 600.0}, 2: {"b": 60.0}},
        {},
    )
    results = journey.sweep(net, t, "a", "b", window=(6, 9), every_minutes=60)
    durations = [r.duration_s for r in results]

    assert durations[1] > durations[0]
    assert durations[1] > durations[2]
