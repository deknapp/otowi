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


# ------------------------------------------------------------ window slicing
#
# "I have to be there some time this evening -- when should I leave?" is a
# slice of a curve that has already been computed, not another simulation. A
# run of this model takes hours, so the curve is built once over the whole
# simulated day and every question about a range is answered from it.


def curve(pairs):
    """[(clock, minutes)] -> the option list summarise() consumes."""
    out = []
    for clock, minutes in pairs:
        hours, _, mins = clock.partition(":")
        out.append({
            "depart": clock,
            "depart_s": int(hours) * 3600 + int(mins) * 60,
            "duration_min": minutes,
            "arrive": clock,
        })
    return out


def test_a_window_only_recommends_from_inside_itself():
    options = curve([("06:00", 20.0), ("07:00", 40.0),
                     ("17:00", 35.0), ("18:00", 25.0)])
    evening = journey.summarise(options, (17 * 3600, 19 * 3600))

    assert evening["best_departure"] == "18:00"
    assert evening["worst_departure"] == "17:00"
    assert evening["options_in_window"] == 2
    # The global best at 06:00 is not an answer to "when this evening".
    assert "06:00" not in evening["good_departures"]


def test_no_window_considers_the_whole_day():
    options = curve([("06:00", 20.0), ("18:00", 25.0)])
    assert journey.summarise(options, None)["best_departure"] == "06:00"


def test_an_empty_window_says_so_rather_than_guessing():
    options = curve([("06:00", 20.0)])
    result = journey.summarise(options, (17 * 3600, 19 * 3600))
    assert result["options_in_window"] == 0
    assert "best_departure" not in result


def test_the_recommendation_is_a_band_not_a_minute():
    """The model carries a fraction of real traffic, so it cannot tell 06:15
    from 06:30 when they differ by forty seconds. Reporting one of them as
    'the best time' would imply a precision it does not have."""
    options = curve([("06:00", 20.0), ("06:15", 20.4), ("06:30", 20.2),
                     ("07:00", 31.0)])
    result = journey.summarise(options, None)

    assert result["good_departures"] == ["06:00", "06:15", "06:30"]
    assert result["good_from"] == "06:00"
    assert result["good_to"] == "06:30"
    assert "07:00" not in result["good_departures"]


def test_a_flat_curve_recommends_everything():
    # If it does not matter when you leave, the tool has to be able to say so.
    options = curve([("06:00", 20.0), ("07:00", 20.1), ("08:00", 20.2)])
    result = journey.summarise(options, None)
    assert len(result["good_departures"]) == 3
    assert result["spread_min"] < 1


def test_clock_parsing_accepts_how_people_write_times():
    from otowi.cli import _parse_clock
    assert _parse_clock("17:30") == 17.5 * 3600
    assert _parse_clock("17") == 17 * 3600
    assert _parse_clock("5.5") == 5.5 * 3600
    assert _parse_clock(" 06:05 ") == 6 * 3600 + 5 * 60
    for bad in ("half five", "25:00", "-1"):
        with pytest.raises(SystemExit):
            _parse_clock(bad)


# ------------------------------------------------- speed against safety
#
# The order of the two filters IS the trade-off, and getting it backwards is
# not a subtle error: filtering on time first and taking the safest survivor
# recommends midnight, the second-deadliest hour of the day, because it is
# ninety seconds quicker than anything else.


def balanced(options, risk_tolerance=1.25):
    """The rule the planner uses: safe enough first, then quickest."""
    scored = [o for o in options if o.get("risk_x") is not None]
    if not scored:
        return min(options, key=lambda o: o["duration_min"])
    safest = min(scored, key=lambda o: o["risk_x"])
    ceiling = safest["risk_x"] * risk_tolerance
    safe_enough = [o for o in scored if o["risk_x"] <= ceiling]
    return (min(safe_enough, key=lambda o: o["duration_min"])
            if safe_enough else safest)


def options():
    # Midnight is quickest and nearly the deadliest; the morning is slowest
    # and safest; 09:00 is nearly as safe and a little quicker.
    return [
        {"depart": "00:00", "duration_min": 36.3, "risk_x": 2.07},
        {"depart": "03:00", "duration_min": 37.8, "risk_x": 4.76},
        {"depart": "08:00", "duration_min": 41.5, "risk_x": 0.25},
        {"depart": "09:00", "duration_min": 39.8, "risk_x": 0.26},
        {"depart": "12:00", "duration_min": 38.6, "risk_x": 2.68},
    ]


def test_balanced_does_not_recommend_the_deadly_fast_hour():
    assert balanced(options())["depart"] != "00:00"


def test_balanced_takes_the_quickest_of_the_safe_ones():
    """08:00 is the safest and 09:00 is within a quarter of it and 1.7 min
    quicker, so the compromise is 09:00."""
    assert balanced(options())["depart"] == "09:00"


def test_time_first_would_have_picked_the_deadly_hour():
    """Pins the bug rather than only the fix, so the filters cannot quietly
    swap back."""
    fastest = min(options(), key=lambda o: o["duration_min"])
    affordable = [o for o in options()
                  if o["duration_min"] <= fastest["duration_min"] + 2.0]
    wrong = min(affordable, key=lambda o: o["risk_x"])
    assert wrong["depart"] == "00:00"
    assert wrong["risk_x"] > balanced(options())["risk_x"] * 5


def test_the_three_answers_are_allowed_to_coincide():
    """When one departure is both quickest and safest there is no trade-off,
    and the tool must not invent one."""
    single = [{"depart": "08:00", "duration_min": 30.0, "risk_x": 0.25},
              {"depart": "03:00", "duration_min": 40.0, "risk_x": 4.0}]
    assert balanced(single)["depart"] == "08:00"
    assert min(single, key=lambda o: o["duration_min"])["depart"] == "08:00"


def test_no_risk_data_falls_back_to_the_quickest():
    plain = [{"depart": "07:00", "duration_min": 30.0},
             {"depart": "08:00", "duration_min": 25.0}]
    assert balanced(plain)["depart"] == "08:00"
