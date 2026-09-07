"""Tests for boundary gateways.

Half the region's commuting has one end outside the study area, and the first
version of the demand model discarded all of it — 47,528 workers against the
52,197 it kept. These tests cover the machinery that keeps them: finding where
roads cross the boundary, choosing which crossing a given trip uses, and
timing its arrival there.

The specific mistake worth guarding: I-25 enters the box and runs through
several edges before it meets anything strongly connected, so a test that only
looks at a gateway's immediate neighbours misses the single most important
entry point in the region.
"""

from __future__ import annotations

import pytest

from otowi import gateways
from otowi.gateways import Gateway


class FakeEdge:
    def __init__(self, edge_id, kind="primary", special=False):
        self._id, self._kind, self._special = edge_id, kind, special
        self.incoming: list[FakeEdge] = []
        self.outgoing: list[FakeEdge] = []
        self.shape = [(0.0, 0.0), (1.0, 1.0)]

    def getID(self):
        return self._id

    def getType(self):
        return f"highway.{self._kind}"

    def isSpecial(self):
        return self._special

    def allows(self, vclass):
        return True

    def getIncoming(self):
        return self.incoming

    def getOutgoing(self):
        return self.outgoing

    def getShape(self):
        return self.shape


class FakeNet:
    def __init__(self, edges):
        self._edges = list(edges)

    def getEdges(self):
        return self._edges

    def convertXY2LonLat(self, x, y):
        return (-106.0 + x / 1000.0, 35.8 + y / 1000.0)


def link(a, b):
    a.outgoing.append(b)
    b.incoming.append(a)


# ------------------------------------------------------------------ finding


def test_an_edge_with_no_predecessor_feeding_the_core_is_an_entry():
    entry, core_edge = FakeEdge("entry"), FakeEdge("core")
    link(entry, core_edge)
    found = gateways.find(FakeNet([entry, core_edge]), {"core"})

    assert [(g.edge_id, g.direction) for g in found] == [("entry", "in")]


def test_an_edge_with_no_successor_fed_by_the_core_is_an_exit():
    core_edge, leaving = FakeEdge("core"), FakeEdge("leaving")
    link(core_edge, leaving)
    found = gateways.find(FakeNet([core_edge, leaving]), {"core"})

    assert [(g.edge_id, g.direction) for g in found] == [("leaving", "out")]


def test_a_gateway_several_edges_from_the_core_is_still_found():
    """This is the I-25 case, and an adjacency test fails it.

    The interstate enters the study area and runs through a chain of edges
    before joining anything strongly connected. Only the first has no
    predecessor, and its immediate neighbours are all outside the core.
    """
    chain = [FakeEdge(f"i25_{i}", kind="motorway") for i in range(6)]
    for a, b in zip(chain, chain[1:]):
        link(a, b)
    core_edge = FakeEdge("core")
    link(chain[-1], core_edge)

    found = gateways.find(FakeNet(chain + [core_edge]), {"core"})

    assert any(g.edge_id == "i25_0" and g.direction == "in" for g in found)


def test_a_severed_fragment_that_never_reaches_the_core_is_not_a_gateway():
    orphan_a, orphan_b = FakeEdge("a"), FakeEdge("b")
    link(orphan_a, orphan_b)
    core_edge = FakeEdge("core")  # unconnected to the fragment

    found = gateways.find(FakeNet([orphan_a, orphan_b, core_edge]), {"core"})
    assert not [g for g in found if g.edge_id in {"a", "b"}]


def test_the_hop_limit_stops_a_long_fragment_becoming_a_gateway():
    chain = [FakeEdge(f"e{i}") for i in range(30)]
    for a, b in zip(chain, chain[1:]):
        link(a, b)
    core_edge = FakeEdge("core")
    link(chain[-1], core_edge)

    near = gateways.find(FakeNet(chain + [core_edge]), {"core"}, max_hops=100)
    far = gateways.find(FakeNet(chain + [core_edge]), {"core"}, max_hops=3)

    assert any(g.edge_id == "e0" for g in near)
    assert not any(g.edge_id == "e0" for g in far)


def test_residential_roads_are_never_gateways():
    """Attaching regional demand to a clipped side street invents a rat run."""
    entry, core_edge = FakeEdge("entry", kind="residential"), FakeEdge("core")
    link(entry, core_edge)
    assert gateways.find(FakeNet([entry, core_edge]), {"core"}) == []


# ----------------------------------------------------------------- choosing


def gw(edge_id, lon, lat, direction="in"):
    return Gateway(edge_id, lon, lat, "motorway", direction)


def test_the_chosen_gateway_minimises_the_whole_journey():
    """Not the one nearest the external end.

    An Albuquerque-to-Los Alamos trip routed via whichever boundary road
    happens to be closest to Albuquerque can enter on the wrong side of the
    study area entirely.
    """
    # External point south, internal destination north-west.
    south = gw("south", -106.1, 35.55)
    east = gw("east", -105.79, 35.90)

    chosen, _ = gateways.choose(
        [south, east],
        external_lon=-106.6, external_lat=35.1,     # Albuquerque-ish
        internal_lon=-106.30, internal_lat=35.88,   # Los Alamos
        direction="in",
    )
    assert chosen.edge_id == "south"


def test_only_gateways_of_the_requested_direction_are_considered():
    entry = gw("entry", -106.1, 35.55, "in")
    exit_ = gw("exit", -106.1, 35.55, "out")

    chosen, _ = gateways.choose([entry, exit_], -106.6, 35.1, -106.0, 35.8, "out")
    assert chosen.edge_id == "exit"


def test_choosing_with_no_gateway_of_that_direction_returns_none():
    assert gateways.choose([gw("a", -106.0, 35.6, "in")],
                           -106.6, 35.1, -106.0, 35.8, "out") is None


def test_the_external_distance_is_returned_for_timing():
    chosen, outside_m = gateways.choose(
        [gw("g", -106.0, 35.6)], -106.0, 35.0, -106.0, 35.8, "in"
    )
    # Roughly 0.6 degrees of latitude, about 66 km.
    assert 60_000 < outside_m < 72_000


# ------------------------------------------------------------------ timing


def test_external_travel_time_scales_with_distance():
    assert gateways.external_travel_s(50_000) == pytest.approx(2000.0)
    assert gateways.external_travel_s(0) == 0.0


def test_a_distant_origin_arrives_at_the_boundary_later():
    """Otherwise the whole inbound peak lands half an hour early."""
    near = gateways.external_travel_s(10_000)
    far = gateways.external_travel_s(60_000)
    assert far > near + 1500


# ------------------------------------------------------------------ geometry


def test_haversine_matches_a_known_distance():
    # Santa Fe to Los Alamos is about 40 km in a straight line.
    d = gateways.haversine_m(-105.9378, 35.6870, -106.2970, 35.8809)
    assert 38_000 < d < 44_000


def test_haversine_is_zero_for_the_same_point():
    assert gateways.haversine_m(-106.0, 35.8, -106.0, 35.8) == pytest.approx(0.0)


def test_inside_bbox_agrees_with_the_configured_study_area():
    assert gateways.inside_bbox(-106.0, 35.8)      # Espanola-ish
    assert not gateways.inside_bbox(-106.65, 35.1)  # Albuquerque
