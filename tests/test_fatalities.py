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
