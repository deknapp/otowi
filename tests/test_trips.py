"""Tests for turning measured flows into vehicles.

The bug these exist for: attaching trips to the nearest plausible edge
produced 2,877 vehicles of which duarouter loaded 2,012, having silently
discarded every trip whose endpoints could not be connected. The missing 30%
looked like congestion in the results. Nothing in the pipeline noticed,
because SUMO reported success at every step.

So the tests here are mostly about *not losing things quietly*.
"""

from __future__ import annotations

import random

import pytest

from otowi import trips
from otowi.demand import Flow


# --------------------------------------------------------------- fake network
#
# A tiny stand-in for a sumolib net. Real networks need a 70 MB file and a
# projection; the logic under test is graph reachability and bookkeeping.


class FakeEdge:
    def __init__(self, edge_id, kind="residential", special=False, allows=True):
        self._id = edge_id
        self._kind = kind
        self._special = special
        self._allows = allows
        self.outgoing: list[FakeEdge] = []
        self.incoming: list[FakeEdge] = []

    def getID(self):
        return self._id

    def getType(self):
        return f"highway.{self._kind}"

    def isSpecial(self):
        return self._special

    def allows(self, vclass):
        return self._allows

    def getOutgoing(self):
        return self.outgoing

    def getIncoming(self):
        return self.incoming


class FakeNet:
    def __init__(self, edges, positions=None):
        self._edges = {edge.getID(): edge for edge in edges}
        self._positions = positions or {}

    def getEdges(self):
        return list(self._edges.values())

    def getEdge(self, edge_id):
        return self._edges[edge_id]

    def convertLonLat2XY(self, lon, lat):
        return (lon, lat)

    def getNeighboringEdges(self, x, y, radius):
        found = []
        for edge_id, (ex, ey) in self._positions.items():
            distance = ((ex - x) ** 2 + (ey - y) ** 2) ** 0.5
            if distance <= radius:
                found.append((self._edges[edge_id], distance))
        return found


def link(a, b):
    """One-way a -> b."""
    a.outgoing.append(b)
    b.incoming.append(a)


def two_way(a, b):
    link(a, b)
    link(b, a)


# ------------------------------------------------------------ reachable core


def test_the_core_is_the_largest_mutually_reachable_group():
    big = [FakeEdge(f"b{i}") for i in range(4)]
    for i in range(len(big) - 1):
        two_way(big[i], big[i + 1])

    small = [FakeEdge(f"s{i}") for i in range(2)]
    two_way(small[0], small[1])

    core = trips.reachable_core(FakeNet(big + small))

    assert {e.getID() for e in big} <= core
    assert not ({e.getID() for e in small} & core)


def test_an_isolated_edge_is_not_in_the_core():
    a, b, orphan = FakeEdge("a"), FakeEdge("b"), FakeEdge("orphan")
    two_way(a, b)
    core = trips.reachable_core(FakeNet([a, b, orphan]))
    assert "orphan" not in core


def test_a_one_way_stub_is_excluded_even_though_it_is_connected():
    """You can drive in and not out. Strongly connected, not merely connected."""
    a, b, stub = FakeEdge("a"), FakeEdge("b"), FakeEdge("stub")
    two_way(a, b)
    link(b, stub)  # one-way into the stub only

    core = trips.reachable_core(FakeNet([a, b, stub]))

    assert {"a", "b"} <= core
    assert "stub" not in core


def test_motorways_are_never_endpoints():
    a, b = FakeEdge("a"), FakeEdge("b")
    motorway = FakeEdge("m", kind="motorway")
    two_way(a, b)
    two_way(b, motorway)

    core = trips.reachable_core(FakeNet([a, b, motorway]))
    assert "m" not in core


# --------------------------------------------------------------- attachment


def test_attachment_refuses_edges_outside_the_core():
    """The whole point: a block's nearest road may be a severed fragment."""
    good_a, good_b = FakeEdge("good_a"), FakeEdge("good_b")
    two_way(good_a, good_b)
    fragment = FakeEdge("fragment")

    net = FakeNet(
        [good_a, good_b, fragment],
        positions={"fragment": (0.0, 0.0), "good_a": (10.0, 0.0), "good_b": (11.0, 0.0)},
    )
    core = trips.reachable_core(net)
    assert "fragment" not in core

    # The block sits right on top of the fragment and 10 away from the core.
    attachment = trips.attach_blocks(net, {"blk": (0.0, 0.0)}, max_distance_m=50, core=core)

    assert attachment.edge_by_block["blk"] == "good_a"
    assert not attachment.unplaced


def test_a_block_with_no_routable_road_in_range_is_reported_not_dropped():
    good_a, good_b = FakeEdge("good_a"), FakeEdge("good_b")
    two_way(good_a, good_b)
    net = FakeNet([good_a, good_b], positions={"good_a": (500.0, 0.0), "good_b": (501.0, 0.0)})

    attachment = trips.attach_blocks(net, {"far": (0.0, 0.0)}, max_distance_m=50)

    assert attachment.unplaced == ["far"]
    assert attachment.summary()["blocks_placed"] == 0
    assert attachment.summary()["placement_rate"] == 0.0


def test_road_class_beats_proximity_for_trip_origins():
    """A commute starts on a residential street, not on the arterial it joins."""
    primary = FakeEdge("primary", kind="primary")
    residential = FakeEdge("residential", kind="residential")
    two_way(primary, residential)

    net = FakeNet(
        [primary, residential],
        positions={"primary": (1.0, 0.0), "residential": (20.0, 0.0)},
    )
    attachment = trips.attach_blocks(net, {"blk": (0.0, 0.0)}, max_distance_m=100)

    assert attachment.edge_by_block["blk"] == "residential"


# ------------------------------------------------------------------ sampling


def test_poisson_preserves_small_fractional_demand():
    """Rounding would delete it: the median flow is one or two workers."""
    rng = random.Random(0)
    draws = [trips._poisson(0.3, rng) for _ in range(20000)]
    mean = sum(draws) / len(draws)
    assert 0.27 < mean < 0.33
    assert any(d > 0 for d in draws)


def test_poisson_of_zero_is_zero():
    assert trips._poisson(0.0, random.Random(0)) == 0
    assert trips._poisson(-1.0, random.Random(0)) == 0


def test_departures_land_inside_the_window_and_are_sorted():
    class Bin:
        def __init__(self, start, end):
            self.start_hour, self.end_hour = start, end

    weighted = [(Bin(6.0, 6.5), 0.5), (Bin(8.5, 9.5), 0.5)]
    seconds = trips._sample_departures(500, weighted, (6, 9), random.Random(0))

    assert seconds == sorted(seconds)
    assert all(0 <= s <= 3 * 3600 for s in seconds), "a departure fell outside the window"


def test_generation_accounts_for_every_worker_it_drops():
    """Dropped demand must be counted, not silently absent."""
    a, b = FakeEdge("a"), FakeEdge("b")
    two_way(a, b)
    net = FakeNet([a, b])

    attachment = trips.Attachment(edge_by_block={"h1": "a", "w1": "b"})
    flows = [
        Flow("h1", "w1", 35.7, -106.0, 35.8, -106.1, jobs=100),
        Flow("h1", "missing", 35.7, -106.0, 35.8, -106.1, jobs=40),   # unplaced end
        Flow("h1", "h1", 35.7, -106.0, 35.7, -106.0, jobs=7),         # same edge
    ]

    class Bin:
        start_hour, end_hour = 7.0, 7.5

    _, stats = trips.generate(net, flows, attachment, [(Bin(), 1.0)], window=(6, 9))

    assert stats["workers_dropped_unplaced_block"] == 40
    assert stats["workers_dropped_same_edge"] == 7
    assert stats["vehicles"] > 0


def test_generation_is_reproducible_for_a_seed():
    a, b = FakeEdge("a"), FakeEdge("b")
    two_way(a, b)
    net = FakeNet([a, b])
    attachment = trips.Attachment(edge_by_block={"h": "a", "w": "b"})
    flows = [Flow("h", "w", 35.7, -106.0, 35.8, -106.1, jobs=200)]

    class Bin:
        start_hour, end_hour = 7.0, 7.5

    first, _ = trips.generate(net, flows, attachment, [(Bin(), 1.0)], seed=42)
    again, _ = trips.generate(net, flows, attachment, [(Bin(), 1.0)], seed=42)
    other, _ = trips.generate(net, flows, attachment, [(Bin(), 1.0)], seed=43)

    assert first == again
    assert first != other


def test_scale_reports_itself():
    """A scaled run does not reproduce congestion and must say so in its output."""
    a, b = FakeEdge("a"), FakeEdge("b")
    two_way(a, b)
    net = FakeNet([a, b])
    attachment = trips.Attachment(edge_by_block={"h": "a", "w": "b"})
    flows = [Flow("h", "w", 35.7, -106.0, 35.8, -106.1, jobs=1000)]

    class Bin:
        start_hour, end_hour = 7.0, 7.5

    full, full_stats = trips.generate(net, flows, attachment, [(Bin(), 1.0)], scale=1.0)
    tenth, tenth_stats = trips.generate(net, flows, attachment, [(Bin(), 1.0)], scale=0.1)

    assert tenth_stats["scale"] == 0.1
    assert len(tenth) < len(full)


def test_written_trips_are_sorted_by_departure(tmp_path):
    """SUMO requires it, and unsorted input is accepted silently then mis-ordered."""
    vehicles = [
        {"from": "a", "to": "b", "depart": 500.0},
        {"from": "a", "to": "b", "depart": 10.0},
    ]
    vehicles.sort(key=lambda t: t["depart"])
    path = trips.write_trips(vehicles, path=tmp_path / "t.trips.xml")

    text = path.read_text()
    assert text.index('depart="10.00"') < text.index('depart="500.00"')


def test_written_trips_declare_a_passenger_vehicle_type(tmp_path):
    path = trips.write_trips(
        [{"from": "a", "to": "b", "depart": 1.0}], path=tmp_path / "t.trips.xml"
    )
    assert 'vClass="passenger"' in path.read_text()
