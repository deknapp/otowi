"""Tests for calibration against NMDOT counts.

The arithmetic here is small and every piece of it is a way to be confidently
wrong: comparing a three-hour total against an hourly count, treating AADT as
if it were a peak hour, matching a count to the cross street it happens to sit
near, or reshuffling the held-out split until the error looks good.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from otowi import counts
from otowi.counts import CountSegment


def segment(**kwargs) -> CountSegment:
    defaults = dict(
        route_id="US84P", station_id="S1", aadt=20000, aadt_year=2025,
        k_factor=10.0, d_factor=60.0, lon=-106.0, lat=35.8, bearing_deg=90.0,
    )
    defaults.update(kwargs)
    return CountSegment(**defaults)


# ------------------------------------------------------- AADT -> peak hour


def test_peak_hour_applies_both_factors_as_percentages():
    """20,000 AADT at K=10%, D=60% is 1,200 vehicles per hour one way."""
    assert segment().peak_hour_directional == pytest.approx(1200.0)


def test_a_bigger_k_factor_means_a_peakier_road():
    flat = segment(k_factor=8.0).peak_hour_directional
    peaky = segment(k_factor=15.0).peak_hour_directional
    assert peaky > flat


def test_peak_hour_is_far_below_aadt():
    """Guards the mistake of comparing a modelled hour against a daily total."""
    seg = segment()
    assert seg.peak_hour_directional < seg.aadt / 10


# ------------------------------------------------------------- window maths


def test_simulated_volume_is_divided_by_the_window_length(tmp_path: Path):
    """A 3-hour total compared against an hourly count would treble the model."""
    edgedata = tmp_path / "edgedata.xml"
    edgedata.write_text(
        '<?xml version="1.0"?><meandata><interval begin="0" end="10800">'
        '<edge id="e1" entered="3000" speed="12.0"/>'
        "</interval></meandata>"
    )
    hourly = counts.simulated_hourly(edgedata, window_hours=3.0)
    assert hourly["e1"] == pytest.approx(1000.0)


def test_edges_with_no_entered_attribute_are_skipped(tmp_path: Path):
    edgedata = tmp_path / "edgedata.xml"
    edgedata.write_text(
        '<?xml version="1.0"?><meandata><interval begin="0" end="3600">'
        '<edge id="quiet" speed="20.0"/><edge id="busy" entered="600"/>'
        "</interval></meandata>"
    )
    hourly = counts.simulated_hourly(edgedata, window_hours=1.0)
    assert "quiet" not in hourly
    assert hourly["busy"] == pytest.approx(600.0)


# --------------------------------------------------------- held-out split


def test_the_split_is_stable_across_calls():
    """A split re-drawn each run is one that can be re-drawn until it flatters."""
    seg = segment(station_id="S-42")
    assert seg.held_out() == seg.held_out() == segment(station_id="S-42").held_out()


def test_the_split_is_roughly_the_requested_fraction():
    segments = [segment(station_id=f"S{i}") for i in range(2000)]
    held = sum(1 for s in segments if s.held_out(fraction=0.5))
    assert 0.45 < held / len(segments) < 0.55


def test_a_segment_without_a_station_still_gets_a_stable_key():
    anonymous = segment(station_id=None, lon=-106.123456, lat=35.812345)
    assert "US84P@" in anonymous.key
    assert anonymous.held_out() == anonymous.held_out()


# -------------------------------------------------------------------- GEH


def test_geh_is_zero_for_a_perfect_match():
    matched = {"e1": segment(aadt=20000, k_factor=10.0, d_factor=60.0)}
    result = counts.compare(matched, {"e1": 1200.0})
    assert result["links"][0]["geh"] == pytest.approx(0.0)
    assert result["links"][0]["ratio"] == pytest.approx(1.0)


def test_geh_tolerates_small_flows_more_than_large_ones():
    """The reason GEH is used instead of percentage error."""
    small = counts.compare(
        {"e": segment(aadt=1000, k_factor=10.0, d_factor=50.0)}, {"e": 75.0}
    )["links"][0]["geh"]
    large = counts.compare(
        {"e": segment(aadt=100000, k_factor=10.0, d_factor=50.0)}, {"e": 7500.0}
    )["links"][0]["geh"]
    # Same 50% relative error; GEH must penalise the large flow far more.
    assert large > small * 5


def test_compare_matches_the_published_geh_formula():
    modelled, observed = 900.0, 1200.0
    expected = math.sqrt(2 * (modelled - observed) ** 2 / (modelled + observed))
    matched = {"e1": segment(aadt=20000, k_factor=10.0, d_factor=60.0)}
    assert counts.compare(matched, {"e1": modelled})["links"][0]["geh"] == pytest.approx(
        expected, abs=0.01
    )


def test_edges_without_a_modelled_volume_are_left_out_not_scored_as_zero():
    """A missing model value is missing data, not a modelled flow of nothing."""
    matched = {"measured": segment(), "unmodelled": segment(station_id="S2")}
    result = counts.compare(matched, {"measured": 1200.0})
    assert result["all"]["links"] == 1


def test_fit_and_held_out_partition_the_links():
    matched = {f"e{i}": segment(station_id=f"S{i}") for i in range(200)}
    modelled = {f"e{i}": 1000.0 for i in range(200)}
    result = counts.compare(matched, modelled)
    assert result["fit"]["links"] + result["held_out"]["links"] == result["all"]["links"]


# --------------------------------------------------------------- bearings


def test_bearing_folds_opposite_directions_together():
    """A road drawn north-to-south is the same road as one drawn south-to-north."""
    northward = counts._bearing(0, 0, 0, 10)
    southward = counts._bearing(0, 10, 0, 0)
    assert northward == pytest.approx(southward)


def test_bearing_distinguishes_perpendicular_roads():
    east_west = counts._bearing(0, 0, 10, 0)
    north_south = counts._bearing(0, 0, 0, 10)
    assert abs(east_west - north_south) == pytest.approx(90.0)


# ---------------------------------------------------------------- parsing


def test_a_segment_missing_its_factors_is_skipped_not_guessed():
    """Without K and D an AADT cannot become a peak hour, and a daily total
    silently compared against an hourly model is worse than no comparison."""
    features = [
        {"attributes": {"RouteID": "A", "AADT": 1000, "KFactor": None,
                        "DFactor": 60, "AADTYear": 2025},
         "geometry": {"paths": [[[-106.0, 35.8], [-106.0, 35.9]]]}},
        {"attributes": {"RouteID": "B", "AADT": 1000, "KFactor": 10,
                        "DFactor": 60, "AADTYear": 2025},
         "geometry": {"paths": [[[-106.0, 35.8], [-106.0, 35.9]]]}},
    ]
    parsed = counts.parse_segments(features)
    assert [s.route_id for s in parsed] == ["B"]


def test_a_feature_with_no_geometry_is_skipped():
    features = [{"attributes": {"RouteID": "A", "AADT": 1000, "KFactor": 10,
                                "DFactor": 60, "AADTYear": 2025},
                 "geometry": {"paths": []}}]
    assert counts.parse_segments(features) == []
