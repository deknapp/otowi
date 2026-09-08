"""Getting the road network out of OpenStreetMap and into SUMO.

Two steps, deliberately separate:

  1. ``fetch_osm``    -- ask Overpass for the roads in the study area, once,
                         and keep the answer on disk forever.
  2. ``build_network`` -- hand that to SUMO's ``netconvert``, which turns OSM
                         ways into lanes, junctions, connections and traffic
                         lights.

They are separate because they fail differently and cost differently. The
Overpass query is a request against a free, shared, volunteer-run service and
should happen approximately never; netconvert is local, fast, and worth
re-running every time we change our mind about how to treat a road class.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import BBOX, CACHE_DIR, HIGHWAY_TYPES

log = logging.getLogger(__name__)

# Several public Overpass instances run the same API. If the main one is busy
# -- and for a query this size it often is -- try the next rather than
# hammering the first.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Overpass runs behind an Apache that answers httpx's default user-agent with
# a bare 406 Not Acceptable -- no error message, nothing to suggest the query
# is fine and only the client string is objectionable. Identifying the tool
# and where it comes from is also simply the polite thing to do to a service
# that costs someone else money.
HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}

# Per-tile, not for the whole area. One query for all ~12,000 ways plus every
# node on them sat in the Overpass queue for twenty minutes without returning a
# byte; nine smaller queries come back in seconds each. Tiling is also the
# better-behaved thing to do to a donated service.
OVERPASS_TIMEOUT_S = 180

# 3x3 over the study area. Ways crossing a tile boundary are returned by both
# tiles, which is fine -- the merge is keyed on OSM id.
TILE_GRID = 3

# How many times to go round the endpoint list before giving up on a tile,
# and the base pause between attempts.
ROUNDS = 4
BACKOFF_S = 5


def _tiles(grid: int = TILE_GRID) -> list[tuple[float, float, float, float]]:
    """Split the study area into a grid of (west, south, east, north) boxes."""
    west, south, east, north = BBOX
    dx = (east - west) / grid
    dy = (north - south) / grid
    return [(west + i * dx, south + j * dy, west + (i + 1) * dx, south + (j + 1) * dy)
            for i in range(grid) for j in range(grid)]


def _query(box: tuple[float, float, float, float]) -> str:
    """The Overpass QL for every road we model in one tile.

    ``(._;>;)`` is the important part: it recurses from the matched ways down
    to the nodes they are built from. Without it Overpass returns ways with
    node references and no coordinates, and netconvert has nothing to work
    with.
    """
    west, south, east, north = box
    pattern = "|".join(t for t in HIGHWAY_TYPES if not t.endswith("_link"))
    return f"""
[out:xml][timeout:{OVERPASS_TIMEOUT_S}];
(
  way["highway"~"^({pattern})(_link)?$"]({south},{west},{north},{east});
);
(._;>;);
out body;
""".strip()


def osm_path() -> Path:
    return CACHE_DIR / "study-area.osm"


def _fetch_tile(box: tuple[float, float, float, float], index: int) -> Path:
    """One tile, cached on disk. Endpoints are tried in rotation."""
    out = CACHE_DIR / f"tile-{index:02d}.osm"
    if out.exists() and out.stat().st_size > 1024:
        return out

    body = _query(box)
    last: Exception | None = None

    # Overpass answers 504 when it is busy, which it often is, and the same
    # tile succeeds seconds later. Several rounds over the endpoints with a
    # growing pause beats giving up on a transient overload -- and beats
    # hammering, which is how a shared service ends up blocking you.
    for attempt in range(ROUNDS):
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                resp = httpx.post(endpoint, data={"data": body}, headers=HEADERS,
                                  timeout=OVERPASS_TIMEOUT_S, follow_redirects=True)
                resp.raise_for_status()
                # A tile can legitimately be almost empty -- the south-west
                # corner of the study area is the Jemez wilderness, with
                # hardly a road on it. Only reject a non-XML response.
                if not resp.content.lstrip().startswith(b"<?xml"):
                    raise RuntimeError(
                        f"{endpoint} returned {len(resp.content)} bytes of non-XML")
                tmp = out.with_suffix(".partial")
                tmp.write_bytes(resp.content)
                tmp.replace(out)
                return out
            except (httpx.HTTPError, RuntimeError) as exc:
                last = exc
                time.sleep(BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"tile {index} failed after {ROUNDS} rounds: {last}")


def fetch_osm(*, force: bool = False) -> Path:
    """Download the study area from Overpass, tile by tile, and merge.

    Cached and never expiring on its own: a road network changes on the
    timescale of construction projects, not page loads, and Overpass is a
    donated resource. Pass ``force`` to deliberately refresh.

    Merging is keyed on OSM element id, because a way crossing a tile boundary
    comes back from both tiles and netconvert will not accept the duplicate.

    Elements are written **all nodes first, then all ways**, which is not
    cosmetic. netconvert parses OSM as ordered sections and stops trusting the
    file once they interleave -- it says "Expected different XML section" once
    and then quietly keeps only what it has already understood. Appending nine
    tiles in fetch order produces node,way,node,way,... and netconvert built a
    network out of roughly the first tile: 911 edges from 11,710 ways, with
    every motorway and trunk road in the study area missing and no traffic
    lights at all. The file was complete and well-formed the whole time, which
    is what made it hard to see.
    """
    path = osm_path()
    if path.exists() and not force and path.stat().st_size > 0:
        return path

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    from lxml import etree

    root = etree.Element("osm", version="0.6", generator="otowi")
    seen: set[tuple[str, str]] = set()
    counts = {"node": 0, "way": 0}
    collected: dict[str, list] = {"node": [], "way": []}

    for i, box in enumerate(_tiles()):
        tile = _fetch_tile(box, i)
        tree = etree.parse(str(tile))
        for el in tree.getroot():
            key = (el.tag, el.get("id", ""))
            if el.tag not in ("node", "way") or key in seen:
                continue
            seen.add(key)
            counts[el.tag] += 1
            collected[el.tag].append(el)

    for tag in ("node", "way"):
        for el in collected[tag]:
            root.append(el)

    tmp = path.with_suffix(".partial")
    etree.ElementTree(root).write(str(tmp), encoding="utf-8",
                                  xml_declaration=True)
    tmp.replace(path)
    print(f"merged {counts['way']} ways and {counts['node']} nodes")
    return path


#: OSM ``surface`` values that are not sealed road.
UNPAVED_SURFACES = {
    "dirt", "unpaved", "ground", "gravel", "earth", "sand", "compacted",
    "fine_gravel", "grass", "mud", "rock", "woodchips",
}

#: ``access`` / ``motor_vehicle`` values that mean the public may not drive it.
RESTRICTED_ACCESS = {
    "private", "no", "customers", "delivery", "forestry", "agricultural",
    "permit", "military",
}

#: Speed for an unpaved road that is otherwise open, in km/h. netconvert gives
#: an ``unclassified`` or ``residential`` way 50 km/h whatever it is made of,
#: which turns a graded dirt road into a routing shortcut. This is a modelling
#: assumption and a blunt one, but a dirt road is unambiguously slower than the
#: asphalt netconvert was pricing it as.
UNPAVED_SPEED_KMH = 30


def _is_unpaved(tags: dict[str, str]) -> bool:
    return tags.get("surface") in UNPAVED_SURFACES or bool(tags.get("tracktype"))


def _undrivable_reason(tags: dict[str, str]) -> str | None:
    """Why this way is not a route for a commuter car, or None if it is one.

    Deliberately narrow, and it took two attempts to get there. Restriction
    alone is far too blunt in this region: OSM tags Diamond Drive and West
    Jemez Road ``access=private`` because they cross LANL property, and those
    are the arterials carrying the exact commute the model exists to
    reproduce -- dropping them on the tag would have been a much worse bug than
    the one being fixed. Surface alone is no better: plenty of people in
    northern New Mexico drive to work from an unpaved street, and deleting
    those deletes trip origins.

    It is the **combination** that identifies a track: unsealed *and* either
    4WD-only or closed to the public. That is a locked forest road, not a
    commuter route, and it is the only rule here that drops one.
    """
    if not _is_unpaved(tags):
        return None
    if tags.get("4wd_only") == "yes":
        return "4wd_only"
    if tags.get("motor_vehicle") in RESTRICTED_ACCESS:
        return "motor_vehicle=" + tags["motor_vehicle"]
    # An explicit motor_vehicle=yes overrides a general access restriction --
    # standard OSM precedence, and how a road closed to through cycling but not
    # to driving is tagged.
    if (tags.get("access") in RESTRICTED_ACCESS
            and tags.get("motor_vehicle") not in ("yes", "designated", "permissive")):
        return "access=" + tags["access"]
    return None


def drivable_path() -> Path:
    return CACHE_DIR / "study-area.drivable.osm"


def filter_drivable(*, force: bool = False) -> Path:
    """Drop the ways a passenger car may not drive, before netconvert sees them.

    This exists because of a road that is not a road. FR 289 Dome Road is a
    dirt Forest Service track over the Jemez, tagged in OSM as ``residential``
    with ``surface=dirt``, one segment ``4wd_only=yes`` and another
    ``access=private``. netconvert does not read any of those tags: it saw
    "residential", gave it a lane and 50 km/h, and the router got a paved-
    equivalent shortcut from Cochiti to Los Alamos -- 55 km against the ~97 km
    the drive actually takes -- and put 400 to 835 vehicles an hour on it.

    Nothing failed. The network built, the assignment converged, the map drew a
    road across country where there is a washed-out track, and the calibration
    was being asked to explain traffic the model had invented a corridor for.
    That is the fourth bug on this project with the same shape.

    Kept separate from ``fetch_osm`` on purpose: the Overpass download is
    expensive and should happen approximately never, while what counts as
    drivable is a modelling opinion worth revising. Re-running this costs a few
    seconds and never touches the network.
    """
    out = drivable_path()
    src = fetch_osm()
    if out.exists() and not force and out.stat().st_mtime >= src.stat().st_mtime:
        return out

    from lxml import etree

    tree = etree.parse(str(src))
    root = tree.getroot()
    dropped: dict[str, list[str]] = {}
    slowed = 0
    for way in root.findall("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        reason = _undrivable_reason(tags)
        if reason is not None:
            dropped.setdefault(reason.split("=")[0], []).append(
                tags.get("name") or tags.get("ref") or way.get("id"))
            root.remove(way)
            continue
        # An unpaved road that anyone may drive stays in the network, but it
        # stops being priced as asphalt. Without this, every graded dirt road
        # is a 50 km/h alternative to the paved route beside it, and the
        # assignment cheerfully uses it. Only ever lowers a speed: an explicit
        # maxspeed, where a mapper has said what the road actually runs at, is
        # better evidence than this assumption.
        if _is_unpaved(tags) and not tags.get("maxspeed"):
            etree.SubElement(way, "tag", k="maxspeed",
                             v=f"{UNPAVED_SPEED_KMH}")
            slowed += 1

    # Nodes are left in place. netconvert ignores nodes no way references, and
    # keeping them means this pass never has to reason about which nodes are
    # shared with a way it kept -- the bug that would silently sever a road.
    tmp = out.with_suffix(".partial")
    etree.ElementTree(root).write(str(tmp), encoding="utf-8", xml_declaration=True)
    tmp.replace(out)

    total = sum(len(v) for v in dropped.values())
    log.info("dropped %d undrivable ways (%s)", total,
             ", ".join(f"{k}: {len(v)}" for k, v in sorted(dropped.items())))
    for reason, names in sorted(dropped.items()):
        sample = sorted({n for n in names if not n.isdigit()})[:6]
        if sample:
            log.info("  %s -- e.g. %s", reason, ", ".join(sample))
    print(f"kept {len(root.findall('way'))} drivable ways, "
          f"dropped {total} track{'' if total == 1 else 's'}, "
          f"slowed {slowed} unpaved to {UNPAVED_SPEED_KMH} km/h")
    return out


def find_tool(name: str) -> str | None:
    """Locate a SUMO binary.

    SUMO is installed from PyPI (``eclipse-sumo``), which puts its binaries in
    the same directory as the interpreter. Running ``.venv/bin/python`` does
    NOT put ``.venv/bin`` on PATH -- only ``activate`` does that -- so
    ``shutil.which`` alone finds nothing in the normal case. Look next to the
    running interpreter first, then fall back to PATH for a system install.
    """
    beside = Path(sys.executable).parent / name
    if beside.exists():
        return str(beside)
    return shutil.which(name)


def netconvert_available() -> bool:
    return find_tool("netconvert") is not None


def network_path() -> Path:
    return CACHE_DIR / "study-area.net.xml"


def build_network(*, force: bool = False) -> Path:
    """Turn the OSM extract into a SUMO network.

    The options here are choices about what kind of model this is, not
    boilerplate:

    ``--geometry.remove`` and ``--ramps.guess`` clean up the artefacts of
    hand-mapped OSM geometry, which otherwise become spurious junctions that
    slow traffic for no physical reason.

    ``--junctions.join`` merges the clusters of nodes that OSM uses to
    represent one real intersection. Without it a single Santa Fe intersection
    can become four junctions in a row, each imposing its own delay, and the
    model invents congestion that does not exist.

    ``--tls.guess-signals`` and ``--tls.join`` do the same for traffic lights.
    Signal timing is the single biggest lever on urban travel time, so getting
    the signals structurally right matters before any of it is calibrated.
    """
    out = network_path()
    if out.exists() and not force:
        return out
    tool = find_tool("netconvert")
    if tool is None:
        raise RuntimeError(
            "netconvert not found. Install the SUMO toolchain with:\n"
            "  pip install -r requirements.txt")

    osm = filter_drivable()
    cmd = [
        tool,
        "--osm-files", str(osm),
        "--output-file", str(out),
        "--geometry.remove",
        "--ramps.guess",
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        # Actuated signals, not fixed-time. This is the single largest
        # correction in this file and it was found by looking at why the model
        # gridlocked.
        #
        # netconvert's default is a static program: a fixed cycle with equal
        # green splits and offset=0 at every junction. On an arterial that is
        # catastrophic. Cerrillos Road carries 43 signalised junctions, and
        # with static timing every one of them turned green at the same instant
        # regardless of where traffic actually was -- there is no green wave,
        # no coordination, and no response to demand. The result was 0.3 m/s
        # sustained on a road posted at 27.8, which is not congestion but
        # deadlock.
        #
        # Real arterials here are coordinated, and we do not have the signal
        # timing plans that would let us reproduce that. Actuated control is
        # the standard substitute when the plans are unavailable: green is
        # extended while vehicles are still arriving and ends when they stop,
        # so a junction adapts to demand instead of enforcing a cycle nobody
        # chose. It is an approximation of coordination, not coordination.
        "--tls.default-type", "actuated",
        "--roundabouts.guess",
        "--remove-edges.isolated",
        "--keep-edges.by-vclass", "passenger",
        # Keep only the largest connected component. With the Dome Road track
        # removed there is no longer any way to drive from Cochiti to anywhere
        # else in the study area, because the road that actually serves it --
        # NM-22 down to I-25 -- crosses the southern boundary of BBOX and is
        # not in the network. That is the truth, and the model should carry it
        # as a gap rather than route around it over a forest track. Without
        # this option that corner survives as an island, duarouter fails on
        # every trip touching it, and the failures are easy to miss among the
        # ones we expect.
        "--keep-edges.components", "1",
        "--no-turnarounds",
        # Off by default, and needed here: counts.py has to match NMDOT and MPO
        # count stations to edges, and CORRIDORS is defined in terms of road
        # names ("St Francis Drive", "NM-502"). Without this the net has no
        # name attribute at all and every corridor has to be recovered from
        # geometry.
        "--output.street-names",
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    # netconvert reports "Success" while having silently discarded most of the
    # input, so the warnings are the only signal that anything went wrong.
    # Summarised rather than dumped: a clean run still emits a few hundred.
    for line in result.stderr.splitlines():
        if "Expected different XML section" in line:
            log.warning("netconvert could not read the whole file: %s", line.strip())
    return out
