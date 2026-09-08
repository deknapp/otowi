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
from collections import Counter

import pytest

from otowi import config, trips
from otowi import gateways as gateway_module
from otowi.demand import Flow


# --------------------------------------------------------------- fake network
#
# A tiny stand-in for a sumolib net. Real networks need a 70 MB file and a
# projection; the logic under test is graph reachability and bookkeeping.


class FakeEdge:
    def __init__(self, edge_id, kind="residential", special=False, allows=True,
                 lanes=1, speed=13.9):
        self._id = edge_id
        self._kind = kind
        self._special = special
        self._allows = allows
        # Lanes and speed only matter to how arriving traffic is split between
        # a block's edges; every real edge has both.
        self._lanes = lanes
        self._speed = speed
        self.outgoing: list[FakeEdge] = []
        self.incoming: list[FakeEdge] = []

    def getLaneNumber(self):
        return self._lanes

    def getSpeed(self):
        return self._speed

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


def test_departures_are_clock_times_and_sorted():
    """Sampling is over the whole day; the window is applied afterwards by
    _place(). Getting that order backwards multiplied the morning peak by
    1.42 -- see day_weights()."""
    class Bin:
        def __init__(self, start, end):
            self.start_hour, self.end_hour = start, end

    weighted = [(Bin(6.0, 6.5), 0.5), (Bin(8.5, 9.5), 0.5)]
    seconds = trips._sample_departures(500, weighted, random.Random(0))

    assert seconds == sorted(seconds)
    assert all(6 * 3600 <= s <= 9.5 * 3600 for s in seconds)
    assert any(s > 9 * 3600 for s in seconds), "the late bin must not be clipped"


def test_place_filters_a_narrow_window_and_wraps_a_whole_day():
    # 07:30 is inside the morning window, at 90 minutes in.
    assert trips._place(7.5 * 3600, (6, 9)) == 1.5 * 3600
    # 17:00 is not, and is dropped rather than clamped to the window edge.
    assert trips._place(17 * 3600, (6, 9)) is None
    # A whole-day window takes everything, and a return leaving work at 30:15
    # is the same vehicle as one leaving at 06:15 -- the model is a typical day
    # in steady state, so today's night shift stands in for yesterday's.
    assert trips._place(17 * 3600, (0, 24)) == 17 * 3600
    assert trips._place(30.25 * 3600, (0, 24)) == 6.25 * 3600
    # And that wrap applies to a narrow window too, or the night shift's drive
    # home vanishes from the morning peak it is actually in.
    assert trips._place(30.25 * 3600, (6, 9)) == 0.25 * 3600
    assert trips._place(26 * 3600, (6, 9)) is None


def test_time_away_is_clipped_not_resampled():
    rng = random.Random(0)
    draws = [trips._sample_time_away(rng) / 3600 for _ in range(2000)]
    assert all(config.TIME_AWAY_MIN_H <= d <= config.TIME_AWAY_MAX_H for d in draws)
    mean = sum(draws) / len(draws)
    assert abs(mean - config.TIME_AWAY_MEAN_H) < 0.2


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


# ------------------------------------------------- arrivals are not a funnel


def test_one_employer_block_does_not_deliver_everyone_to_one_street():
    """A block holding a whole laboratory must not arrive on a single edge.

    This is the bug this test exists for: LODES reports jobs by census block,
    Los Alamos National Laboratory sits in a couple of blocks, and attaching a
    block to one edge put 6,080 of the 9,387 vehicles arriving in Los Alamos
    onto 6th Street. It deadlocked, and the delay propagated back down East
    Road and Diamond Drive onto NM-502 -- so the commute the model exists to
    measure became the worst-served trip in it.

    The vehicle count does not change when this regresses, which is why it
    needs a test rather than an eyeball on the summary.
    """
    arterial = FakeEdge("west_jemez", kind="secondary", lanes=2, speed=24.6)
    diamond = FakeEdge("diamond", kind="secondary", lanes=2, speed=15.7)
    side = FakeEdge("6th", kind="residential", lanes=1, speed=11.2)
    home = FakeEdge("home", kind="residential", lanes=1, speed=11.2)
    edges = [arterial, diamond, side, home]
    net = FakeNet(edges, positions={
        "west_jemez": (10.0, 0.0), "diamond": (12.0, 0.0),
        "6th": (5.0, 0.0), "home": (5000.0, 0.0),
    })
    core = {edge.getID() for edge in edges}

    attachment = trips.attach_blocks(
        net, {"work": (0.0, 0.0), "home": (0.0, 0.045)},
        max_distance_m=2000, core=core)

    rng = random.Random(0)
    picked = [attachment.choose_destination("work", rng) for _ in range(2000)]
    counts = Counter(picked)

    assert len(counts) >= 3, f"arrivals funnelled onto {list(counts)}"
    top = counts.most_common(1)[0][1] / len(picked)
    assert top < 0.75, f"one edge took {top:.0%} of arrivals"
    # Capacity decides the split, so the two-lane arterials outrank the
    # residential street they sit beside.
    assert counts["west_jemez"] > counts["6th"]


def test_a_block_with_one_usable_road_still_works():
    """Spreading must not require somewhere to spread to."""
    only = FakeEdge("only", kind="residential")
    net = FakeNet([only], positions={"only": (5.0, 0.0)})
    attachment = trips.attach_blocks(
        net, {"blk": (0.0, 0.0)}, max_distance_m=100, core={"only"})

    rng = random.Random(0)
    assert attachment.choose_destination("blk", rng) == "only"


def test_choose_destination_falls_back_to_the_single_attached_edge():
    """An Attachment built by hand has no spread and must not raise."""
    attachment = trips.Attachment(edge_by_block={"w": "b"})
    assert attachment.choose_destination("w", random.Random(0)) == "b"
    assert attachment.choose_destination("missing", random.Random(0)) is None


# --------------------------------------------------------------- return trips
#
# LODES measures home-to-work flows and nothing else. Run over a whole day, a
# model built from it alone gives a morning peak and fifteen empty hours,
# because nobody ever drives home -- which is not a rounding error in a
# corridor model, it is half the traffic.


class MorningBin:
    start_hour, end_hour = 7.0, 7.5


def _one_flow_net():
    a, b = FakeEdge("a"), FakeEdge("b")
    two_way(a, b)
    net = FakeNet([a, b])
    attachment = trips.Attachment(edge_by_block={"h1": "a", "w1": "b"})
    flows = [Flow("h1", "w1", 35.7, -106.0, 35.8, -106.1, jobs=400)]
    return net, flows, attachment


def test_a_whole_day_run_has_people_driving_home():
    net, flows, attachment = _one_flow_net()
    day, stats = trips.generate(net, flows, attachment, [(MorningBin(), 1.0)],
                                window=(0, 24), seed=1)

    assert stats["return_trips"] > 0
    # Everyone who drove to work drove home again.
    assert stats["return_trips"] == stats["vehicles"] - stats["return_trips"]
    # And they went the other way.
    assert any(t["from"] == "b" and t["to"] == "a" for t in day)


def test_the_evening_is_not_empty():
    net, flows, attachment = _one_flow_net()
    day, _ = trips.generate(net, flows, attachment, [(MorningBin(), 1.0)],
                            window=(0, 24), seed=1)
    evening = [t for t in day if 15 * 3600 <= t["depart"] < 20 * 3600]
    assert evening, "a whole-day run with no evening traffic is the bug"
    assert all(t["from"] == "b" for t in evening), "the evening is people going home"


def test_returns_can_be_turned_off_and_then_it_is_empty():
    net, flows, attachment = _one_flow_net()
    day, stats = trips.generate(net, flows, attachment, [(MorningBin(), 1.0)],
                                window=(0, 24), seed=1, include_returns=False)
    assert stats["return_trips"] == 0
    assert not [t for t in day if 15 * 3600 <= t["depart"] < 20 * 3600]


def test_a_narrow_window_keeps_only_what_falls_inside_it():
    """The fix for the 1.42x over-count. A flow's whole-day drivers are
    expanded and then filtered, so a window holding 70% of departures gets
    70% of the vehicles -- not all of them squeezed into three hours."""
    net, flows, attachment = _one_flow_net()

    class Split:
        # Half depart at 07:00-07:30, half at 12:00-12:30.
        def __init__(self, start, end):
            self.start_hour, self.end_hour = start, end

    weighted = [(Split(7.0, 7.5), 0.5), (Split(12.0, 12.5), 0.5)]
    morning, stats = trips.generate(net, flows, attachment, weighted,
                                    window=(6, 9), seed=1, include_returns=False)
    whole, day_stats = trips.generate(net, flows, attachment, weighted,
                                      window=(0, 24), seed=1, include_returns=False)

    assert stats["trips_outside_window"] > 0
    assert 0.4 < len(morning) / len(whole) < 0.6, (
        "roughly half of departures fall in the morning window, so roughly "
        "half the vehicles should"
    )


def test_a_night_shift_drives_home_through_the_morning_peak():
    """The trip a window-restricted profile can never produce: someone whose
    drive *home* lands inside the window being simulated."""
    net, flows, attachment = _one_flow_net()

    class NightBin:
        start_hour, end_hour = 21.0, 21.5   # leaves for work at 21:00

    morning, stats = trips.generate(net, flows, attachment, [(NightBin(), 1.0)],
                                    window=(6, 9), seed=1)

    assert morning, "the night shift's drive home is real traffic at 06:30"
    assert all(t["from"] == "b" and t["to"] == "a" for t in morning)
    assert stats["return_trips"] == len(morning)


def test_the_drive_home_is_spread_like_the_drive_to_work():
    """A census block is hundreds of houses, not one address.

    The first version of return trips reasoned from "home is one address" and
    sent every returning commuter in a block onto the single edge the block
    was attached to. 7,958 vehicles a day ended on one link, the top twenty
    links took 27% of all arrivals, and the assignment gridlocked: 15,094 jam
    teleports and 7.3% of trips never finishing, against 63 and 0.0% at a
    tenth of the demand. Workplace arrivals were already spread for exactly
    this reason; the return needed the same treatment.
    """
    edges = [FakeEdge(name) for name in ("h1", "h2", "h3", "w1")]
    for other in edges[1:]:
        two_way(edges[0], other)
    net = FakeNet(edges)

    attachment = trips.Attachment(
        edge_by_block={"home": "h1", "work": "w1"},
        destinations_by_block={
            "home": [("h1", 1.0), ("h2", 1.0), ("h3", 1.0)],
            "work": [("w1", 1.0)],
        },
    )
    flows = [Flow("home", "work", 35.7, -106.0, 35.8, -106.1, jobs=600)]

    day, _ = trips.generate(net, flows, attachment, [(MorningBin(), 1.0)],
                            window=(0, 24), seed=3)
    going_home = [t["to"] for t in day if t["from"] == "w1"]

    assert len(set(going_home)) == 3, (
        "every returning commuter in the block funnelled onto one edge"
    )


def test_a_return_trip_never_goes_the_wrong_way_through_a_gateway():
    """An entry gateway has no incoming edges, so nothing can arrive there; an
    exit gateway has no outgoing edges, so nothing placed on it can move.

    The first version of return trips reused the gateway the outbound leg had
    chosen, which pointed every gateway return the wrong way through a one-way
    door. duarouter discarded them -- about 25,000 vehicles, a quarter of the
    demand, gone without an error -- and the assignment gridlocked around the
    ones that remained: 15,094 jam teleports and 7.3% of trips never finishing.
    """
    inside_home, inside_work = FakeEdge("h1"), FakeEdge("w1")
    two_way(inside_home, inside_work)
    entry, exit_ = FakeEdge("gw-in"), FakeEdge("gw-out")
    net = FakeNet([inside_home, inside_work, entry, exit_])

    gateways = [
        gateway_module.Gateway("gw-in", -106.2, 35.6, "primary", "in"),
        gateway_module.Gateway("gw-out", -106.2, 35.6, "primary", "out"),
    ]
    attachment = trips.Attachment(edge_by_block={"h1": "h1", "w1": "w1"})

    # Lives outside, works inside: in through the entry, home via the exit.
    inbound = [Flow("out-of-box", "w1", 35.2, -106.6, 35.8, -106.1,
                    jobs=200, kind="inbound")]
    day, _ = trips.generate(net, inbound, attachment, [(MorningBin(), 1.0)],
                            window=(0, 24), seed=5, gateways=gateways)
    assert day
    assert all(t["from"] != "gw-out" for t in day), "started on an exit gateway"
    assert all(t["to"] != "gw-in" for t in day), "ended on an entry gateway"
    assert any(t["to"] == "gw-out" for t in day), "nobody left the study area"

    # Lives inside, works outside: out through the exit, home via the entry.
    outbound = [Flow("h1", "out-of-box", 35.8, -106.1, 35.2, -106.6,
                     jobs=200, kind="outbound")]
    day, _ = trips.generate(net, outbound, attachment, [(MorningBin(), 1.0)],
                            window=(0, 24), seed=5, gateways=gateways)
    assert day
    assert all(t["from"] != "gw-out" for t in day)
    assert all(t["to"] != "gw-in" for t in day)
    assert any(t["from"] == "gw-in" for t in day), "nobody came back into the box"
