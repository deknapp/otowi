"""A road that is not a road.

FR 289 Dome Road is a dirt Forest Service track over the Jemez. OSM tags it
``highway=residential``, ``surface=dirt``, with one segment ``4wd_only=yes``
and another ``access=private``. netconvert reads none of those: it saw
"residential", gave it a lane at 50 km/h, and the assignment got a paved-
equivalent shortcut from Cochiti to Los Alamos -- 55 km against the ~97 km the
drive actually takes -- and put 400 to 835 vehicles an hour on it.

Nothing failed. The network built, the assignment converged, the map drew a
road across country where there is a washed-out track, and the calibration was
being asked to explain traffic the model had invented a corridor for.

The rule that fixes it took two attempts, and the second test class below is
the more important one: restriction *alone* is far too blunt here. OSM tags
Diamond Drive and West Jemez Road ``access=private`` because they cross LANL
property, and those are the arterials carrying the commute this model exists
to reproduce. Dropping them would have been a much worse bug than the one
being fixed.
"""

from __future__ import annotations

import pytest

from otowi import network


def way(**tags: str) -> dict[str, str]:
    return tags


class TestDropsTracks:
    def test_unpaved_and_4wd_only(self):
        assert network._undrivable_reason(
            way(highway="residential", surface="dirt", **{"4wd_only": "yes"})
        ) == "4wd_only"

    def test_unpaved_and_gated(self):
        assert network._undrivable_reason(
            way(highway="residential", surface="dirt", access="private")
        ) == "access=private"

    def test_the_dome_road_segments_that_broke_the_chain(self):
        # The two real ways whose removal severs Cochiti from Los Alamos.
        assert network._undrivable_reason(
            way(highway="residential", name="Dome Road", ref="FR 289",
                surface="dirt", **{"4wd_only": "yes"}))
        assert network._undrivable_reason(
            way(highway="residential", name="FR 289, Dome Road",
                surface="dirt", access="private"))

    def test_tracktype_counts_as_unpaved(self):
        assert network._undrivable_reason(
            way(highway="unclassified", tracktype="grade3", access="no"))


class TestKeepsRealRoads:
    """The regression that matters most: a paved public arterial tagged
    private because it crosses lab property is still a road."""

    def test_paved_and_private_is_kept(self):
        # Diamond Drive, the main north-south arterial in Los Alamos.
        assert network._undrivable_reason(
            way(highway="tertiary", name="Diamond Drive", surface="asphalt",
                access="private")) is None

    def test_paved_primary_across_lab_land_is_kept(self):
        # West Jemez Road, NM-501.
        assert network._undrivable_reason(
            way(highway="primary", name="West Jemez Road", access="private")) is None

    def test_untagged_surface_is_not_assumed_unpaved(self):
        assert network._undrivable_reason(
            way(highway="residential", access="private")) is None

    def test_unpaved_but_open_is_kept(self):
        # Plenty of people here drive to work from an unpaved street. Deleting
        # those deletes trip origins.
        assert network._undrivable_reason(
            way(highway="residential", surface="gravel")) is None

    def test_explicit_motor_vehicle_yes_overrides_access(self):
        assert network._undrivable_reason(
            way(highway="residential", surface="dirt", access="private",
                motor_vehicle="yes")) is None


class TestUnpavedIsSlowed:
    """An unpaved road that stays is still not asphalt. Left at netconvert's
    50 km/h, every graded dirt road is an alternative to the paved route
    beside it, and the assignment uses it."""

    def test_open_unpaved_gets_a_speed(self, tmp_path, monkeypatch):
        src = tmp_path / "study-area.osm"
        src.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<osm version="0.6">\n'
            '  <node id="1" lat="35.60" lon="-106.10"/>\n'
            '  <node id="2" lat="35.61" lon="-106.11"/>\n'
            '  <way id="10">\n'
            '    <nd ref="1"/><nd ref="2"/>\n'
            '    <tag k="highway" v="residential"/>\n'
            '    <tag k="surface" v="dirt"/>\n'
            '  </way>\n'
            '  <way id="11">\n'
            '    <nd ref="1"/><nd ref="2"/>\n'
            '    <tag k="highway" v="residential"/>\n'
            '    <tag k="surface" v="dirt"/>\n'
            '    <tag k="4wd_only" v="yes"/>\n'
            '  </way>\n'
            '  <way id="12">\n'
            '    <nd ref="1"/><nd ref="2"/>\n'
            '    <tag k="highway" v="primary"/>\n'
            '    <tag k="surface" v="asphalt"/>\n'
            '  </way>\n'
            '</osm>\n')
        monkeypatch.setattr(network, "fetch_osm", lambda **kw: src)
        monkeypatch.setattr(network, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(network, "drivable_path",
                            lambda: tmp_path / "study-area.drivable.osm")

        out = network.filter_drivable(force=True)
        text = out.read_text()

        assert 'id="11"' not in text, "the 4WD track should be gone"
        assert 'id="10"' in text and 'id="12"' in text

        from lxml import etree
        ways = {w.get("id"): {t.get("k"): t.get("v") for t in w.findall("tag")}
                for w in etree.parse(str(out)).getroot().findall("way")}
        assert ways["10"]["maxspeed"] == str(network.UNPAVED_SPEED_KMH)
        assert "maxspeed" not in ways["12"], "paved roads are left alone"

    def test_an_explicit_maxspeed_is_never_overwritten(self, tmp_path, monkeypatch):
        # A mapper who recorded what the road actually runs at is better
        # evidence than our assumption.
        src = tmp_path / "study-area.osm"
        src.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<osm version="0.6">\n'
            '  <node id="1" lat="35.60" lon="-106.10"/>\n'
            '  <node id="2" lat="35.61" lon="-106.11"/>\n'
            '  <way id="10">\n'
            '    <nd ref="1"/><nd ref="2"/>\n'
            '    <tag k="highway" v="unclassified"/>\n'
            '    <tag k="surface" v="gravel"/>\n'
            '    <tag k="maxspeed" v="45 mph"/>\n'
            '  </way>\n'
            '</osm>\n')
        monkeypatch.setattr(network, "fetch_osm", lambda **kw: src)
        monkeypatch.setattr(network, "CACHE_DIR", tmp_path)
        monkeypatch.setattr(network, "drivable_path",
                            lambda: tmp_path / "study-area.drivable.osm")
        out = network.filter_drivable(force=True)
        from lxml import etree
        tags = {t.get("k"): t.get("v")
                for t in etree.parse(str(out)).getroot().find("way").findall("tag")}
        assert tags["maxspeed"] == "45 mph"


class TestDefaultPathAppliesTheFilter:
    """The lesson from the last three bugs on this project: assert that the
    default path does the thing, not merely that the thing exists. `otowi run`
    called single-pass routing for weeks because only `otowi assign` used the
    fix that had been written for it."""

    def test_build_network_reads_the_filtered_file(self, monkeypatch):
        import inspect
        source = inspect.getsource(network.build_network)
        assert "filter_drivable()" in source
        assert "fetch_osm()" not in source

    def test_build_network_keeps_one_component(self, monkeypatch):
        import inspect
        source = inspect.getsource(network.build_network)
        assert '"--keep-edges.components", "1"' in source
