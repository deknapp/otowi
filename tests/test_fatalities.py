"""A crash count is not a risk.

US-84/285 has more fatal crashes than any road in the study area, and it is
also the road the most people drive. Ranking roads by crash count ranks them by
how busy they are. The number worth having is deaths per unit of travel, and
the hard part is the denominator -- which is why this lives in a traffic model
rather than a spreadsheet.

Two ways of getting it wrong are pinned here, because both were made:

* the *unit of analysis*. Per SUMO edge -- a couple of hundred metres -- every
  road returns exactly one crash at three hundred to eight hundred deaths per
  billion vehicle-kilometres, against a US average near seven, because the
  exposure has been conditioned on where the crash was.
* the *evidence*. One death on a quiet road produces a larger point estimate
  than a road that kills someone every year.
"""

from __future__ import annotations

import pytest

from otowi import fatalities
from otowi.fatalities import Crash, RoadRisk


def crash(**kw) -> Crash:
    base = dict(case_id="350001", year=2022, lat=35.7, lon=-106.0, fatalities=1,
                hour=8, county="Santa Fe", road="US-84", route_kind="US Highway",
                light="Daylight", harm="Rollover")
    base.update(kw)
    return Crash(**base)


def risk(**kw) -> RoadRisk:
    base = dict(road_id="US84P", name="US-84", fatalities=4, crashes=4,
                length_km=24.9, vehicle_km=6.5e8, counted_share=0.9, years=6.0)
    base.update(kw)
    return RoadRisk(**base)


class TestRates:
    def test_rate_is_deaths_per_billion_vehicle_km(self):
        r = risk(fatalities=10, vehicle_km=1e9)
        assert r.per_billion_veh_km == pytest.approx(10.0)

    def test_a_real_corridor_lands_near_the_national_average(self):
        # US-84 in the study area: 4 deaths over 6 years, ~11,800 veh/day on
        # 24.9 km. The US average is about 7 per billion vehicle-km, and a
        # unit-of-analysis error puts this in the hundreds.
        assert 1.0 < risk().per_billion_veh_km < 30.0

    def test_no_exposure_is_no_rate_rather_than_a_division_by_zero(self):
        assert risk(vehicle_km=0.0).per_billion_veh_km == 0.0
        assert risk(vehicle_km=0.0).lower_bound == 0.0


class TestRankingOnEvidence:
    def test_the_lower_bound_is_below_the_point_estimate(self):
        r = risk()
        assert 0 < r.lower_bound < r.per_billion_veh_km

    def test_few_deaths_are_discounted_more_than_many(self):
        """Three deaths and thirty are not equally good evidence of a rate."""
        thin = risk(fatalities=1, vehicle_km=1e8)
        thick = risk(fatalities=30, vehicle_km=3e9)
        # Same point estimate.
        assert thin.per_billion_veh_km == pytest.approx(thick.per_billion_veh_km)
        # Not the same evidence.
        assert thin.lower_bound < thick.lower_bound / 2

    def test_zero_deaths_has_no_lower_bound(self):
        assert fatalities._poisson_lower_bound(0) == 0.0


class TestWhatGetsRanked:
    def test_a_short_stretch_is_never_ranked(self):
        """The unit of analysis cannot be chosen by looking at the crashes."""
        assert not risk(length_km=0.4, vehicle_km=1e9).rankable
        assert risk(length_km=24.9, vehicle_km=1e9).rankable

    def test_a_quiet_road_is_never_ranked(self):
        assert not risk(vehicle_km=1e5).rankable

    def test_a_corridor_with_no_deaths_is_not_ranked(self):
        assert not risk(fatalities=0).rankable

    def test_summarise_says_how_little_it_can_rank(self):
        risks = {
            "US84P": risk(fatalities=4),
            "SIDE": risk(road_id="SIDE", name="", fatalities=6, length_km=0.5),
        }
        crashes = [crash(fatalities=4), crash(fatalities=6)]
        out = fatalities.summarise(crashes, risks)
        # Only the long corridor is rankable, so most of the dying is not.
        assert out["rankable_corridors"] == 1
        assert out["fatalities_on_ranked_corridors"] == 4
        assert out["share_of_deaths_ranked"] == pytest.approx(0.4)


class TestCrashParsing:
    def test_both_directions_of_a_road_are_one_road(self):
        assert fatalities.undirected("-14568790#2") == "14568790#2"
        assert fatalities.undirected("14568790#2") == "14568790#2"

    def test_route_labels_are_what_people_call_the_road(self):
        assert fatalities.route_label("NM68P") == "NM-68"
        assert fatalities.route_label("US84P") == "US-84"
        assert fatalities.route_label("I25P") == "I-25"
        # A local segment id has no such name and is left alone.
        assert fatalities.route_label("FL1820P") == "FL1820"

    def test_after_dark_is_read_from_the_light_condition(self):
        assert crash(light="Dark - Not Lighted").is_dark
        assert crash(light="Dark - Lighted").is_dark
        assert not crash(light="Daylight").is_dark

    def test_the_bbox_filter_keeps_the_study_area_only(self):
        from otowi.config import BBOX
        west, south, east, north = BBOX
        assert fatalities._in_bbox((west + east) / 2, (south + north) / 2)
        assert not fatalities._in_bbox(-104.7, 34.9)   # Guadalupe County, I-40


class TestWhenNotJustWhere:
    """"When do crashes happen" and "when is driving dangerous" are different
    questions. Most crashes happen when most people are driving, which is a
    fact about traffic and not about risk."""

    def test_the_busy_hour_is_not_the_dangerous_one(self):
        # 20 deaths at 08:00 against 5 at 03:00 -- but eighty times the driving.
        crashes = ([crash(hour=8) for _ in range(20)] +
                   [crash(hour=3) for _ in range(5)])
        travel = {h: 1.0 for h in range(24)}
        travel[8] = 800.0
        travel[3] = 10.0

        rows = {r["hour"]: r for r in fatalities.by_hour(crashes, travel)}
        assert rows[3]["relative_risk"] > rows[8]["relative_risk"], (
            "counting crashes without dividing by travel ranks by busyness"
        )

    def test_an_hour_with_no_crashes_is_not_declared_safe(self):
        """One quiet hour in a six-year window is a small number, not a fact."""
        crashes = [crash(hour=h) for h in (2, 4)] * 3      # nothing at 03:00
        travel = {h: 1.0 for h in range(24)}
        rows = {r["hour"]: r for r in fatalities.by_hour(crashes, travel)}
        assert rows[3]["deaths"] == 0
        assert rows[3]["relative_risk"] > 0, "smoothing must carry its neighbours"

    def test_smoothing_wraps_around_midnight(self):
        values = [0.0] * 24
        values[23] = 3.0
        smoothed = fatalities._smooth(values)
        assert smoothed[0] > 0, "23:00 and 00:00 are an hour apart"

    def test_relative_risk_is_scaled_so_average_is_one(self):
        crashes = [crash(hour=h) for h in range(24)]
        travel = {h: 1.0 for h in range(24)}
        rows = fatalities.by_hour(crashes, travel)
        assert all(r["relative_risk"] == pytest.approx(1.0, abs=0.01) for r in rows)


class TestOneDrive:
    """A regional ranking says which road is dangerous. A person wants to know
    whether the drive they actually make is."""

    class FakeEdge:
        def __init__(self, eid, metres):
            self._id, self._m = eid, metres
        def getLength(self):
            return self._m

    class FakeNet:
        def __init__(self, edges):
            self._e = {e._id: e for e in edges}
        def getEdge(self, eid):
            return self._e[eid]

    def _net(self):
        return self.FakeNet([self.FakeEdge("a", 10000), self.FakeEdge("b", 2000),
                             self.FakeEdge("c", 8000)])

    def test_distance_on_a_road_weights_its_rate(self):
        risks = {
            "NM68P": risk(road_id="NM68P", name="NM-68", fatalities=3,
                          vehicle_km=1.9e8, length_km=15.5),
            "NM502P": risk(road_id="NM502P", name="NM-502", fatalities=1,
                           vehicle_km=4.0e8, length_km=6.5),
        }
        out = fatalities.along_route(
            self._net(), ["a", "c"], risks,
            {"a": "NM68P", "c": "NM502P"})
        assert out["assessed_km"] == pytest.approx(18.0)
        # 10 km of the bad road and 8 of the good one lands between them.
        low = risks["NM502P"].per_billion_veh_km
        high = risks["NM68P"].per_billion_veh_km
        assert low < out["per_billion_veh_km"] < high

    def test_unassessable_distance_is_reported_not_hidden(self):
        """On a drive that is mostly city streets, most of it cannot be scored.
        Quietly averaging over the rest would overstate what is known."""
        risks = {"NM68P": risk(road_id="NM68P", name="NM-68", vehicle_km=1.9e8)}
        out = fatalities.along_route(
            self._net(), ["a", "b", "c"], risks, {"a": "NM68P"})
        assert out["assessed_km"] == pytest.approx(10.0)
        assert out["total_km"] == pytest.approx(20.0)
        assert out["assessed_share"] == pytest.approx(0.5)

    def test_a_drive_on_roads_with_no_counts_says_so(self):
        out = fatalities.along_route(self._net(), ["a", "b"], {}, {})
        assert out["per_billion_veh_km"] is None
        assert out["assessed_share"] == 0.0

    def test_the_reference_is_local_not_national(self):
        """Telling somebody their commute is above the US average when every
        road around them is would be true and useless."""
        risks = {"A": risk(fatalities=4, vehicle_km=1e9),
                 "B": risk(road_id="B", fatalities=6, vehicle_km=1e9)}
        assert fatalities.regional_average(risks) == pytest.approx(5.0)

    def test_unrankable_corridors_do_not_move_the_reference(self):
        risks = {"A": risk(fatalities=4, vehicle_km=1e9),
                 "tiny": risk(road_id="tiny", fatalities=50, vehicle_km=1e6,
                              length_km=0.3)}
        assert fatalities.regional_average(risks) == pytest.approx(4.0)


class TestRiskLedRecommendation:
    """The point of joining the two halves.

    On these corridors the journey time moves by a minute or two across a whole
    day and the chance of being killed moves by a factor of nineteen. A planner
    that ranks departures on minutes is answering the question that does not
    matter -- and worse, it recommends the empty road, which is fast precisely
    because it is the one people die on.
    """

    def hours(self):
        # 08:00 safe, 03:00 deadly, everything else ordinary.
        return {h: (0.25 if h == 8 else 4.76 if h == 3 else 1.0)
                for h in range(24)}

    def test_the_fastest_departure_is_not_the_one_to_recommend(self):
        options = [
            {"depart": "03:00", "duration_min": 30.0},   # empty road, fastest
            {"depart": "08:00", "duration_min": 34.0},   # slowest, safest
        ]
        hours = self.hours()
        for option in options:
            option["risk_x"] = hours[int(option["depart"][:2])]

        fastest = min(options, key=lambda o: o["duration_min"])
        safest = min(options, key=lambda o: o["risk_x"])

        assert fastest["depart"] == "03:00"
        assert safest["depart"] == "08:00"
        # Four minutes of driving against a nineteenfold difference in risk.
        assert fastest["duration_min"] < safest["duration_min"]
        assert safest["risk_x"] * 19 == pytest.approx(fastest["risk_x"], rel=0.01)

    def test_a_drive_carries_the_record_of_the_roads_it_uses(self):
        """The planner needs a per-journey number, not just a regional one."""
        class E:
            def __init__(self, m): self._m = m
            def getLength(self): return self._m
        class N:
            def __init__(self): self._e = {"hwy": E(9300), "street": E(32300)}
            def getEdge(self, i): return self._e[i]

        risks = {"US84P": risk(road_id="US84P", name="US-84", fatalities=4,
                               vehicle_km=6.5e8, length_km=24.9)}
        out = fatalities.along_route(N(), ["hwy", "street"], risks,
                                     {"hwy": "US84P"})
        assert out["per_billion_veh_km"] == pytest.approx(6.2, abs=0.2)
        # Most of this drive is city street with no traffic count, and the
        # answer has to say so rather than average over what it does not know.
        assert out["assessed_share"] == pytest.approx(0.22, abs=0.02)
