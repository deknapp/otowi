"""The tile merge has to put every node before every way.

This is the regression test for a bug that cost a whole network. netconvert
parses OSM as ordered sections; once nodes and ways interleave it warns once
("Expected different XML section") and then quietly keeps only what it had
already understood. Appending nine Overpass tiles in fetch order produces
node,way,node,way,... and netconvert built 911 edges out of 11,710 ways --
with every motorway and trunk road in northern New Mexico missing, and no
traffic lights at all.

Nothing about the file was malformed. It had all 131,881 nodes and all 11,710
ways, one <osm> root, valid XML. That is what made it hard to see: the check
that the fetch was complete passed, because the fetch *was* complete.
"""

from __future__ import annotations

import pytest

from otowi import network


def _tile(node_ids: list[int], way_id: int) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6">']
    for i in node_ids:
        lines.append(f'  <node id="{i}" lat="35.{600 + i}" lon="-106.{100 + i}"/>')
    lines.append(f'  <way id="{way_id}">')
    lines += [f'    <nd ref="{i}"/>' for i in node_ids]
    lines += ['    <tag k="highway" v="residential"/>', "  </way>", "</osm>", ""]
    return "\n".join(lines)


@pytest.fixture
def two_tiles(tmp_path, monkeypatch):
    """Two tiles that share a node, as adjacent Overpass tiles really do."""
    first = tmp_path / "tile-00.osm"
    second = tmp_path / "tile-01.osm"
    first.write_text(_tile([1, 2, 3], way_id=100))
    second.write_text(_tile([3, 4, 5], way_id=101))
    tiles = [first, second]

    monkeypatch.setattr(network, "_tiles", lambda: [None, None])
    monkeypatch.setattr(network, "_fetch_tile", lambda box, index: tiles[index])
    monkeypatch.setattr(network, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(network, "osm_path", lambda: tmp_path / "study-area.osm")
    return tmp_path / "study-area.osm"


def _sections(path) -> list[str]:
    """The order of node/way blocks in the file, collapsed to transitions."""
    seen: list[str] = []
    last = None
    for line in path.read_text().splitlines():
        stripped = line.lstrip()
        kind = ("node" if stripped.startswith("<node ")
                else "way" if stripped.startswith("<way ") else None)
        if kind and kind != last:
            seen.append(kind)
            last = kind
    return seen


def test_all_nodes_are_written_before_any_way(two_tiles):
    network.fetch_osm(force=True)
    assert _sections(two_tiles) == ["node", "way"]


def test_shared_elements_are_not_duplicated(two_tiles):
    """A way or node on a tile boundary comes back from both tiles.

    netconvert rejects the duplicate outright, so this is not cosmetic either.
    """
    network.fetch_osm(force=True)
    text = two_tiles.read_text()
    assert text.count('<node id="3"') == 1
    assert text.count("<node ") == 5
    assert text.count("<way ") == 2
