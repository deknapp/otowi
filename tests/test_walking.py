"""Measuring a crash against what was built on the street.

All of this is geometry, and geometry is where a plausible-looking wrong answer
is easiest to ship. Three things are pinned:

* **Distance to a segment, not to its line.** A crossing at one end of a long
  block is metres from the block's midpoint by the infinite line and a hundred
  metres from it in fact. Getting this wrong makes every crash look close to a
  crossing, which is the direction of the headline.
* **The index does not change the answer.** Bucketing 8,000 crossings into
  400 m cells is a speed trick, and a speed trick that quietly returns the
  second-nearest crossing would be invisible. The tests compare it to a
  brute-force scan.
* **The control is the finding.** "10% of pedestrians were hit more than 100 m
  from a crossing" says nothing until the same number for the road itself is
  known, and the two have to be measured the same way.
"""

from __future__ import annotations

import math

import pytest

from otowi import walking
from otowi.walking import Asset


def asset(paths, kind="crosswalk", subtype="Marked Crosswalk") -> Asset:
    return Asset(object_id=1, kind=kind, subtype=subtype, route="TEST",
                 side="", condition="Good", paths=paths)


class TestDistance:
    def test_a_degree_of_latitude_is_about_111_km(self):
        assert walking._metres(-106.0, 35.0, -106.0, 36.0) == pytest.approx(110_900, rel=0.01)

    def test_distance_is_to_the_segment_not_to_its_infinite_line(self):
        # A crossing running east along the top of a block. A point past its
        # east end is not on it, however close the line it lies on passes.
        ax, ay, bx, by = -106.000, 35.700, -105.999, 35.700   # ~90 m of crossing
        off_the_end = walking._point_to_segment_m(-105.995, 35.700, ax, ay, bx, by)
        assert off_the_end > 300
        beside_it = walking._point_to_segment_m(-105.9995, 35.7001, ax, ay, bx, by)
        assert beside_it < 20

    def test_a_zero_length_segment_is_a_point(self):
        assert walking._point_to_segment_m(
            -106.0, 35.7, -106.0, 35.7, -106.0, 35.7) == pytest.approx(0.0)


class TestTheIndex:
    def grid(self, n=12):
        """Crossings on a lattice, far enough apart to have a clear winner."""
        return [asset([[[-106.0 + i * 0.003, 35.7 + j * 0.003],
                        [-106.0 + i * 0.003, 35.7 + j * 0.003 + 0.0002]]])
                for i in range(n) for j in range(n)]

    def brute(self, assets, lon, lat):
        best = math.inf
        for a in assets:
            for path in a.paths:
                for (ax, ay), (bx, by) in zip(path, path[1:]):
                    best = min(best, walking._point_to_segment_m(lon, lat, ax, ay, bx, by))
        return best

    def test_it_agrees_with_a_brute_force_scan(self):
        assets = self.grid()
        index = walking.Nearby(assets)
        for lon, lat in [(-106.0043, 35.7071), (-105.9902, 35.7150),
                         (-106.0000, 35.7000), (-105.9931, 35.7013)]:
            assert index.nearest(lon, lat)[0] == pytest.approx(
                self.brute(assets, lon, lat), abs=0.5)

    def test_a_segment_crossing_several_cells_is_found_from_all_of_them(self):
        # A kilometre-long line entered into one cell only would be invisible
        # from the far end of itself.
        long_line = asset([[[-106.02, 35.70], [-105.98, 35.70]]])
        index = walking.Nearby([long_line])
        assert index.nearest(-106.00, 35.7003)[0] < 60

    def test_nothing_within_the_limit_is_infinity_not_a_wrong_small_number(self):
        index = walking.Nearby([asset([[[-106.0, 35.7], [-106.0, 35.701]]])])
        distance, found = index.nearest(-105.0, 35.7, limit_m=1000.0)
        assert distance == math.inf and found is None

    def test_an_empty_layer_does_not_crash(self):
        assert walking.Nearby([]).nearest(-106.0, 35.7) == (math.inf, None)


class TestTheControl:
    def corridor(self, paths, name="Test Road"):
        from otowi.vru import Corridor
        return Corridor(object_id=1, name=name, county="Santa Fe", city="Santa Fe",
                        severity_index=100.0, length_mi=1.0, vru_crashes=10,
                        ped_ka=2, bike_ka=0, ksi=2, aadt=20000, speed_limit=35,
                        lanes=4, paths=paths)

    def test_the_road_is_sampled_evenly_along_its_length(self):
        # About 900 m of road at 20 m a step.
        road = self.corridor([[[-106.000, 35.700], [-105.990, 35.700]]])
        points = walking.control_points([road])
        assert 40 <= len(points) <= 50
        spacing = walking._metres(points[0][0], points[0][1],
                                  points[1][0], points[1][1])
        assert spacing == pytest.approx(walking.CONTROL_STEP_M, rel=0.2)

    def test_a_corridor_with_no_geometry_contributes_nothing(self):
        assert walking.control_points([self.corridor([])]) == []

    def test_the_profile_reports_the_tail_not_only_the_middle(self):
        # The whole finding is in the tail: the medians are close and the
        # share over 100 m is what separates the fatal crashes from the rest.
        profile = walking.distance_profile([1.0] * 90 + [500.0] * 10)
        assert profile["median_m"] == pytest.approx(1.0)
        assert profile["share_over_100m"] == pytest.approx(0.10)
        assert profile["n"] == 100

    def test_an_empty_set_of_distances_is_reported_as_empty(self):
        assert walking.distance_profile([])["n"] == 0

    def test_infinite_distances_are_excluded_rather_than_ranked_last(self):
        # A crash with nothing inventoried near by is not "very far from a
        # crossing", it is unmeasured, and averaging it in would be a lie in
        # the direction of the finding.
        profile = walking.distance_profile([10.0, 20.0, math.inf])
        assert profile["n"] == 2


class TestThinning:
    def test_the_ends_survive(self):
        path = [[-106.0, 35.7], [-106.0, 35.70001], [-106.0, 35.70002], [-106.0, 35.71]]
        from otowi.web import _thin
        thinned = _thin(path)
        assert thinned[0] == [-106.0, 35.7]
        assert thinned[-1] == [-106.0, 35.71]

    def test_survey_noise_is_dropped(self):
        from otowi.web import _thin
        # Forty points along ten metres: one survey track, one pixel.
        path = [[-106.0, 35.7 + i * 0.0000025] for i in range(40)]
        assert len(_thin(path)) == 2

    def test_a_two_point_line_is_left_alone(self):
        from otowi.web import _thin
        assert len(_thin([[-106.0, 35.7], [-106.0, 35.8]])) == 2
