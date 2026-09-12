"""The people who were not driving, and what their numbers cannot be asked.

FARS has 40 of them here over six years. NMDOT's Vulnerable Road User Safety
Assessment has 758 over eleven, with coordinates -- and it includes the people
who were hit and lived, which is most of them. That is enough to rank streets
and to see an hourly shape, and it is still not enough to compute a rate,
because nobody counts how many people walked down the street.

What is pinned here is the arithmetic that is easy to get subtly wrong: the
severity threshold (K and A, not any injury), the street-name join (117 crashes
on "Cerrillos Rd" and 7 on "Cerrillos Road" are one road), and the fact that
the exposure denominator is vehicle travel and therefore answers a driver's
question rather than a pedestrian's.
"""

from __future__ import annotations

import pytest

from otowi import vru
from otowi.vru import VruCrash, Corridor


def crash(**kw) -> VruCrash:
    base = dict(object_id=1, year=2019, hour=17, day="Friday", severity="B",
                lighting="Daylight", alcohol=False, drugs=False,
                pedestrian=True, pedalcycle=False, county="Santa Fe",
                city="Santa Fe", street="Cerrillos Rd", route="",
                lat=35.66, lon=-105.98)
    base.update(kw)
    return VruCrash(**base)


def corridor(**kw) -> Corridor:
    base = dict(object_id=1, name="Cerrillos Road", county="Santa Fe",
                city="Santa Fe", severity_index=288.0, length_mi=0.5,
                vru_crashes=30, ped_ka=5, bike_ka=1, ksi=6, aadt=32700,
                speed_limit=40, lanes=4)
    base.update(kw)
    return Corridor(**base)


class TestSeverity:
    def test_killed_or_serious_is_k_and_a_only(self):
        assert crash(severity="K").killed_or_serious
        assert crash(severity="A").killed_or_serious
        # B and C absorb most of the reporting variation between agencies,
        # which is why every safety programme is written against K and A.
        assert not crash(severity="B").killed_or_serious
        assert not crash(severity="C").killed_or_serious
        assert not crash(severity="O").killed_or_serious

    def test_dark_covers_both_lit_and_unlit_night(self):
        assert crash(lighting="Dark-Not Lighted").is_dark
        assert crash(lighting="Dark-Lighted").is_dark
        assert not crash(lighting="Daylight").is_dark
        assert not crash(lighting="Dusk").is_dark

    def test_mode_comes_from_the_two_flags(self):
        assert crash(pedestrian=True, pedalcycle=False).mode == "pedestrian"
        assert crash(pedestrian=False, pedalcycle=True).mode == "cyclist"


class TestStreetNames:
    def test_the_same_road_spelled_two_ways_is_one_road(self):
        # 117 crashes on "Cerrillos Rd" and 7 on "Cerrillos Road". Ranked
        # without the join, the road's own overflow sits eight rows below it.
        crashes = ([crash(street="Cerrillos Rd", severity="K")] * 3 +
                   [crash(street="Cerrillos Road", severity="A")] * 2)
        ranked = vru.worst_streets(crashes)
        assert len(ranked) == 1
        assert ranked[0]["street"] == "Cerrillos Road"
        assert ranked[0]["killed_or_serious"] == 5

    def test_an_ambiguous_abbreviation_is_left_alone(self):
        # "St Michaels Drive" is Saint, and "St Francis Drive" is Saint, and
        # there is no way to tell those from Street without a gazetteer. The
        # suffix is normalised; the prefix is not touched.
        assert vru._normalise_street("St Michaels Dr") == "St Michaels Drive"

    def test_ranking_is_on_people_hurt_not_on_reports_filed(self):
        crashes = ([crash(street="Quiet Road", severity="K")] +
                   [crash(street="Busy Road", severity="O")] * 20)
        assert vru.worst_streets(crashes)[0]["street"] == "Quiet Road"


class TestHours:
    def test_crashes_with_no_recorded_hour_are_dropped_not_bucketed(self):
        counts = vru.by_hour([crash(hour=None), crash(hour=17)])
        assert sum(counts) == 1 and counts[17] == 1

    def test_the_evening_block_is_where_the_serious_ones_are(self):
        crashes = ([crash(hour=19, severity="K")] * 6 +
                   [crash(hour=9, severity="C")] * 6)
        summary = vru.summarise(crashes, [])
        assert summary["ksi_share_1700_2300"] == pytest.approx(1.0)
        assert summary["share_1700_2300"] == pytest.approx(0.5)


class TestDarkness:
    def test_darkness_shows_up_in_the_outcome_not_the_count(self):
        # The real shape: about a quarter of these crashes happen after dark,
        # and about three quarters of the fatal ones do.
        crashes = ([crash(severity="K", lighting="Dark-Not Lighted")] * 3 +
                   [crash(severity="K", lighting="Daylight")] +
                   [crash(severity="C", lighting="Daylight")] * 8)
        summary = vru.summarise(crashes, [])
        assert summary["share_after_dark"] == pytest.approx(0.25)
        assert summary["share_after_dark_when_killed"] == pytest.approx(0.75)


class TestTheStatesOwnRanking:
    def test_segments_of_one_road_are_summed_to_the_road(self):
        corridors = [corridor(object_id=1, severity_index=288.0, vru_crashes=60),
                     corridor(object_id=2, severity_index=200.0, vru_crashes=70)]
        summary = vru.summarise([crash()], corridors)
        roads = summary["high_injury_network"]["by_road"]
        assert roads[0]["name"] == "Cerrillos Road"
        assert roads[0]["segments"] == 2
        assert roads[0]["vru_crashes"] == 130

    def test_no_corridors_is_not_an_error(self):
        assert "high_injury_network" not in vru.summarise([crash()], [])


class TestExposure:
    def test_the_denominator_is_driving_so_the_question_is_the_drivers(self):
        # Same number of crashes every hour, and one hour with a tenth of the
        # driving. That hour is ten times more likely to hit somebody per
        # kilometre driven -- which says nothing about how safe it is to walk
        # then, because nobody counted the people walking.
        crashes = [crash(hour=hour) for hour in range(24) for _ in range(10)]
        travel = {hour: 100.0 for hour in range(24)}
        travel[3] = 10.0
        rows = vru.relative_risk(crashes, travel, smooth=False)
        assert rows[3]["relative_risk"] > 5.0

    def test_an_hour_the_model_never_drives_is_not_infinitely_dangerous(self):
        crashes = [crash(hour=hour) for hour in range(24)]
        travel = {hour: 100.0 for hour in range(24)}
        travel[4] = 0.0
        rows = vru.relative_risk(crashes, travel, smooth=False)
        assert rows[4]["relative_risk"] == 0.0

    def test_smoothing_is_on_by_default_because_the_counts_are_thin(self):
        # 758 crashes over 24 hours is thirty an hour, and two quiet hours in
        # a row would otherwise read as a safe stretch of evening. Smoothing
        # also means a single empty travel hour no longer zeroes its own risk,
        # which is the behaviour wanted: the model having driven nothing at
        # 04:00 is a fact about the model.
        crashes = [crash(hour=hour) for hour in range(24)]
        travel = {hour: 100.0 for hour in range(24)}
        travel[4] = 0.0
        assert vru.relative_risk(crashes, travel)[4]["relative_risk"] > 0.0
