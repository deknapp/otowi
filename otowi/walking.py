"""What was on the street where somebody was hit.

`vru` says where the 758 pedestrian and cyclist crashes are and how they ended.
It does not say what the street offered the person who was crossing it, and
that is the question a walking tool has to answer, because it is the one with
a fix attached. "Cerrillos Road is dangerous" is already known to everybody who
lives here. "This crash happened four hundred metres from the nearest marked
crossing" is a claim about a specific gap in a specific mile of road.

NMDOT inventories its roadway assets and publishes them on the same open ArcGIS
service the crash layer sits on: every **sidewalk**, **bike lane** and
**crosswalk** on a state route, with a shape, a type and a condition. Inside
this bounding box that is 2,827 sidewalk runs, 796 bike lane runs and 7,964
marked crossings.

**The join is geometry, not identifiers.** Both sides carry coordinates, so the
distance from a crash to the nearest marked crossing is a measurement rather
than a lookup, and it does not depend on the route identifiers agreeing. That
matters here, because they do not always: the crash file records the street as
the officer typed it and the asset file records an NMDOT route code.

**The limitation that has to travel with every number.** This is the *state
highway* inventory. Cerrillos Road, St Francis Drive and St Michaels Drive are
state routes and are covered; the residential street somebody was hit on
crossing to a bus stop is not in it at all. So "no crosswalk within 200 m" is
reliable on the arterials and means nothing on a city street the state does not
maintain -- and the arterials are where the killed-and-seriously-hurt are, which
is the only reason this is worth computing. Every summary here says how much of
its own evidence sits on a covered road.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, asdict, field
from pathlib import Path

from .config import CACHE_DIR
from .vru import ARCGIS, VruCrash, _query

log = logging.getLogger(__name__)

#: The three asset layers, the fields worth keeping, and what each one is for.
ASSETS = {
    "sidewalk": {
        "layer": f"{ARCGIS}/Sidewalk/FeatureServer/0",
        "fields": ["OBJECTID", "ROUTENAME", "SIDEWALK_TYPE", "SIDE_OF_ROAD",
                   "CONDITION", "SURFACE_TYPE", "CURB_CUT_PRESENCE",
                   "ASSET_WIDTH"],
        "kind": "SIDEWALK_TYPE",
    },
    "bike_lane": {
        "layer": f"{ARCGIS}/Bike_Lane/FeatureServer/0",
        "fields": ["OBJECTID", "ROUTENAME", "BIKE_LANE_TYPE", "SEPARATION",
                   "SIDE_OF_ROAD", "ASSET_WIDTH"],
        "kind": "BIKE_LANE_TYPE",
    },
    "crosswalk": {
        "layer": f"{ARCGIS}/Crosswalk/FeatureServer/0",
        "fields": ["OBJECTID", "ROUTENAME", "CROSSING_TYPE", "CONDITION"],
        "kind": "CROSSING_TYPE",
    },
}

#: How far from a marked crossing a crash has to be before the crossing is
#: irrelevant to it. Picked from how people actually behave rather than from
#: the data: the pedestrian research that informs crossing-spacing guidance
#: puts the detour people will accept at roughly a hundred metres each way, so
#: a crossing more than that away was not a realistic option for whoever was
#: hit. It is a threshold on a continuous measurement, and the distance itself
#: is reported alongside so the threshold can be argued with.
NEAR_CROSSING_M = 100.0

#: Same idea for a sidewalk, but tighter. A sidewalk fifty metres away is not
#: the sidewalk you were walking on.
NEAR_SIDEWALK_M = 25.0

#: Grid cell for the spatial index, in degrees -- about 400 m here. Small
#: enough that a cell holds a handful of segments, large enough that a query
#: touching nine cells is nearly always enough.
CELL = 0.004

#: Metres per degree at this latitude. The study area spans 0.75 degrees, over
#: which the cosine changes by under 1%, so a fixed local scale is accurate to
#: well within the metre-level precision of the coordinates themselves.
_LAT_M = 110_900.0
_LON_M = 111_320.0 * math.cos(math.radians(35.85))


@dataclass(frozen=True)
class Asset:
    """One inventoried sidewalk run, bike lane run or marked crossing."""

    object_id: int
    kind: str            # sidewalk | bike_lane | crosswalk
    subtype: str         # "Marked Crosswalk With Refuge Island", etc.
    route: str
    side: str
    condition: str
    paths: list[list[list[float]]] = field(default_factory=list)


# ------------------------------------------------------------------ fetching


def assets_path(kind: str) -> Path:
    return CACHE_DIR / f"nmdot_{kind}.json"


def fetch_assets(kind: str, *, force: bool = False) -> list[Asset]:
    """One asset layer, clipped to the study area and cached."""
    if kind not in ASSETS:
        raise ValueError(f"unknown asset layer: {kind}")
    path = assets_path(kind)
    if path.exists() and not force:
        return [Asset(**row) for row in json.loads(path.read_text())]

    spec = ASSETS[kind]
    log.info("querying NMDOT %s", kind)
    found: list[Asset] = []
    for feature in _query(spec["layer"], spec["fields"]):
        a = feature.get("attributes", {})
        paths = [[[round(x, 5), round(y, 5)] for x, y in path]
                 for path in (feature.get("geometry") or {}).get("paths", [])]
        if not paths:
            continue
        found.append(Asset(
            object_id=int(a.get("OBJECTID") or 0),
            kind=kind,
            subtype=(a.get(spec["kind"]) or "").strip(),
            route=(a.get("ROUTENAME") or "").strip(),
            side=(a.get("SIDE_OF_ROAD") or "").strip(),
            condition=(a.get("CONDITION") or "").strip(),
            paths=paths,
        ))

    log.info("  %d %s features", len(found), kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(a) for a in found], separators=(",", ":")))
    return found


def fetch_all(*, force: bool = False) -> dict[str, list[Asset]]:
    return {kind: fetch_assets(kind, force=force) for kind in ASSETS}


# ------------------------------------------------------- measuring distances


def _metres(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    return math.hypot((lon2 - lon1) * _LON_M, (lat2 - lat1) * _LAT_M)


def _point_to_segment_m(px: float, py: float,
                        ax: float, ay: float, bx: float, by: float) -> float:
    """Distance from a point to a line *segment*, not to its infinite line.

    The difference matters: a crossing at one end of a long block is metres
    from the block's midpoint by the infinite line and a hundred metres from it
    in fact.
    """
    ux, uy = (bx - ax) * _LON_M, (by - ay) * _LAT_M
    vx, vy = (px - ax) * _LON_M, (py - ay) * _LAT_M
    length2 = ux * ux + uy * uy
    if length2 == 0.0:
        return math.hypot(vx, vy)
    t = max(0.0, min(1.0, (vx * ux + vy * uy) / length2))
    return math.hypot(vx - t * ux, vy - t * uy)


class Nearby:
    """A grid index over asset geometry, for "what is the closest one" queries.

    758 crashes against 7,964 crossings is six million segment comparisons done
    naively, which is slow enough to be annoying and grows badly. Bucketing
    every segment into 400 m cells turns each query into a scan of nine cells.

    The search widens by a ring at a time until it finds something or gives up,
    so a crash in an empty part of the county returns a distance of infinity
    rather than a wrong small number from whichever cell happened to be looked
    at first.
    """

    def __init__(self, assets: list[Asset]):
        self.cells: dict[tuple[int, int], list[tuple]] = {}
        for asset in assets:
            for path in asset.paths:
                for (ax, ay), (bx, by) in zip(path, path[1:]):
                    segment = (ax, ay, bx, by, asset)
                    for key in self._keys_for(ax, ay, bx, by):
                        self.cells.setdefault(key, []).append(segment)
                if len(path) == 1:
                    (ax, ay) = path[0]
                    segment = (ax, ay, ax, ay, asset)
                    self.cells.setdefault(self._key(ax, ay), []).append(segment)

    @staticmethod
    def _key(lon: float, lat: float) -> tuple[int, int]:
        return (int(math.floor(lon / CELL)), int(math.floor(lat / CELL)))

    def _keys_for(self, ax, ay, bx, by):
        """Every cell the segment's bounding box touches, so nothing is missed."""
        x0, x1 = sorted((ax, bx))
        y0, y1 = sorted((ay, by))
        for i in range(int(math.floor(x0 / CELL)), int(math.floor(x1 / CELL)) + 1):
            for j in range(int(math.floor(y0 / CELL)), int(math.floor(y1 / CELL)) + 1):
                yield (i, j)

    def nearest(self, lon: float, lat: float,
                limit_m: float = 1500.0) -> tuple[float, Asset | None]:
        i0, j0 = self._key(lon, lat)
        best, found = math.inf, None
        rings = int(math.ceil(limit_m / (CELL * _LON_M))) + 1
        for ring in range(rings + 1):
            for i in range(i0 - ring, i0 + ring + 1):
                for j in range(j0 - ring, j0 + ring + 1):
                    # Only the newly added ring; the inside was done already.
                    if ring and max(abs(i - i0), abs(j - j0)) != ring:
                        continue
                    for ax, ay, bx, by, asset in self.cells.get((i, j), ()):
                        distance = _point_to_segment_m(lon, lat, ax, ay, bx, by)
                        if distance < best:
                            best, found = distance, asset
            # A hit inside the ring already scanned cannot be beaten by
            # anything a ring further out, once the ring's inner edge is
            # further away than the best so far.
            if found is not None and best <= ring * CELL * _LON_M:
                break
        return (best, found) if best <= limit_m else (math.inf, None)


# ------------------------------------------------------------------ the read


@dataclass(frozen=True)
class Exposure:
    """One crash, and what the street had to offer where it happened."""

    crash: VruCrash
    crosswalk_m: float
    sidewalk_m: float
    bike_lane_m: float

    @property
    def on_a_state_route(self) -> bool:
        """Whether the state inventories anything at all near here.

        Without this the absence of a crosswalk is ambiguous -- it could mean
        the road has no crossings, or that the road is a city street the state
        never surveyed. A sidewalk, bike lane or crossing within 250 m is the
        cheapest available evidence that this stretch was surveyed.
        """
        return min(self.crosswalk_m, self.sidewalk_m, self.bike_lane_m) < 250.0

    @property
    def far_from_a_crossing(self) -> bool:
        return self.crosswalk_m > NEAR_CROSSING_M


def assess(crashes: list[VruCrash],
           assets: dict[str, list[Asset]]) -> list[Exposure]:
    """Measure every crash against the inventory."""
    index = {kind: Nearby(items) for kind, items in assets.items()}
    out = []
    for crash in crashes:
        out.append(Exposure(
            crash=crash,
            crosswalk_m=index["crosswalk"].nearest(crash.lon, crash.lat)[0],
            sidewalk_m=index["sidewalk"].nearest(crash.lon, crash.lat)[0],
            bike_lane_m=index["bike_lane"].nearest(crash.lon, crash.lat)[0],
        ))
    return out


def summarise(exposures: list[Exposure]) -> dict:
    """What the inventory says about where people are hit.

    Restricted throughout to crashes on a stretch the state actually surveyed,
    and the share that survives that restriction is reported, because a
    percentage computed over an unknown denominator is not a percentage.
    """
    covered = [e for e in exposures if e.on_a_state_route]
    if not covered:
        return {"assessed": 0, "of": len(exposures)}

    walkers = [e for e in covered if e.crash.pedestrian]
    riders = [e for e in covered if e.crash.pedalcycle]
    serious = [e for e in walkers if e.crash.killed_or_serious]

    def share(rows, test):
        return round(sum(1 for r in rows if test(r)) / len(rows), 3) if rows else 0.0

    def median(values):
        values = sorted(v for v in values if math.isfinite(v))
        if not values:
            return None
        middle = len(values) // 2
        return round(values[middle] if len(values) % 2 else
                     (values[middle - 1] + values[middle]) / 2, 1)

    return {
        "assessed": len(covered),
        "of": len(exposures),
        "share_assessed": round(len(covered) / len(exposures), 3),
        "near_crossing_m": NEAR_CROSSING_M,
        "pedestrians": {
            "crashes": len(walkers),
            "far_from_a_crossing": share(walkers, lambda e: e.far_from_a_crossing),
            "median_distance_m": median(e.crosswalk_m for e in walkers),
        },
        "pedestrians_killed_or_serious": {
            "crashes": len(serious),
            "far_from_a_crossing": share(serious, lambda e: e.far_from_a_crossing),
            "median_distance_m": median(e.crosswalk_m for e in serious),
        },
        "cyclists": {
            "crashes": len(riders),
            "no_bike_lane": share(riders, lambda e: e.bike_lane_m > NEAR_SIDEWALK_M),
            "median_distance_m": median(e.bike_lane_m for e in riders),
        },
    }


def by_corridor(exposures: list[Exposure], corridors) -> list[dict]:
    """The state's High Injury Network roads, with what is built on them.

    Each crash is attributed to the nearest High Injury Network segment within
    40 m -- the same radius `fatalities` uses to put a crash on an edge, and
    for the same reason: these coordinates are geocoded from a written location
    as often as they are recorded from a device.
    """
    index = Nearby([
        Asset(object_id=c.object_id, kind="corridor", subtype="", route=c.name,
              side="", condition="", paths=c.paths)
        for c in corridors if c.paths
    ])

    roads: dict[str, dict] = {}
    for corridor in corridors:
        row = roads.setdefault(corridor.name, {
            "name": corridor.name, "miles": 0.0, "severity_index": 0.0,
            "aadt": None, "speed_limit": None,
            "crashes": 0, "killed_or_serious": 0, "killed": 0,
            "after_dark": 0, "far_from_a_crossing": 0, "pedestrians": 0,
            "cyclists": 0})
        row["miles"] += corridor.length_mi
        row["severity_index"] += corridor.severity_index
        if corridor.aadt:
            row["aadt"] = max(row["aadt"] or 0, corridor.aadt)
        if corridor.speed_limit:
            row["speed_limit"] = max(row["speed_limit"] or 0, corridor.speed_limit)

    for exposure in exposures:
        distance, corridor = index.nearest(exposure.crash.lon, exposure.crash.lat,
                                           limit_m=40.0)
        if corridor is None:
            continue
        row = roads[corridor.route]
        row["crashes"] += 1
        row["killed_or_serious"] += exposure.crash.killed_or_serious
        row["killed"] += exposure.crash.severity == "K"
        row["after_dark"] += exposure.crash.is_dark
        row["pedestrians"] += exposure.crash.pedestrian
        row["cyclists"] += exposure.crash.pedalcycle
        if exposure.crash.pedestrian and exposure.far_from_a_crossing:
            row["far_from_a_crossing"] += 1

    for row in roads.values():
        row["miles"] = round(row["miles"], 2)
        row["severity_index"] = round(row["severity_index"], 1)
        row["ksi_per_mile"] = round(row["killed_or_serious"] / row["miles"], 1) \
            if row["miles"] else 0.0
    return sorted(roads.values(), key=lambda r: -r["severity_index"])

# --------------------------------------------------- the number needs a control


#: How finely the High Injury Network is sampled to build the background
#: distribution. Twenty metres is finer than the crossings are spaced and finer
#: than the coordinates are accurate, so halving it changes nothing.
CONTROL_STEP_M = 20.0


def control_points(corridors, step_m: float = CONTROL_STEP_M) -> list[tuple[float, float]]:
    """Evenly spaced points along the state's High Injury Network.

    "Only 10% of pedestrians were hit more than 100 m from a marked crossing"
    is not a finding until it is known what share of the *road* is more than
    100 m from one. If crossings were every fifty metres, 10% would be
    damning; if they were every kilometre, it would be remarkable. The road
    itself, sampled evenly, is the comparison -- and it is the right one here
    because it holds the corridor constant: these are the same miles of
    Cerrillos Road the crashes are on.

    What it does not hold constant is where people walk, which nobody counts.
    A crossing is built where people cross, so some of the clustering below is
    the crossings following the pedestrians rather than the pedestrians being
    hit at the crossings. That cannot be separated without a count, and it is
    the reason this is reported as a distribution rather than a causal claim.
    """
    points: list[tuple[float, float]] = []
    for corridor in corridors:
        for path in corridor.paths:
            for (ax, ay), (bx, by) in zip(path, path[1:]):
                length = _metres(ax, ay, bx, by)
                steps = max(1, int(length // step_m))
                for step in range(steps):
                    t = step / steps
                    points.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return points


def distance_profile(distances) -> dict:
    """Median, quartile and tail of a set of distances, in metres."""
    values = sorted(d for d in distances if math.isfinite(d))
    if not values:
        return {"n": 0}

    def at(fraction):
        return round(values[min(len(values) - 1, int(len(values) * fraction))], 1)

    middle = len(values) // 2
    return {
        "n": len(values),
        "median_m": round(values[middle] if len(values) % 2 else
                          (values[middle - 1] + values[middle]) / 2, 1),
        "p75_m": at(0.75),
        "p90_m": at(0.90),
        "share_over_100m": round(
            sum(1 for v in values if v > NEAR_CROSSING_M) / len(values), 3),
    }


def lanes_and_crashes(exposures: list[Exposure], corridors,
                      bike_lanes: list[Asset]) -> dict:
    """The cyclist's version of the same question, and a worse answer.

    Where a pedestrian's crossing either exists or does not, a bike lane's
    absence is the normal condition: most of the High Injury Network has none,
    so a cyclist being far from one is unremarkable and being *near* one is the
    thing worth measuring. The comparison runs the same way regardless -- the
    road itself against the people hit on it -- and if the two distributions
    sit on top of each other, that is the finding: the lanes are not where the
    crashes are, in either direction.
    """
    lanes = Nearby(bike_lanes)
    on_hin = Nearby([
        Asset(object_id=c.object_id, kind="corridor", subtype="", route=c.name,
              side="", condition="", paths=c.paths)
        for c in corridors if c.paths
    ])
    riders = [e for e in exposures
              if e.crash.pedalcycle
              and on_hin.nearest(e.crash.lon, e.crash.lat, limit_m=40.0)[1] is not None]

    return {
        "road_itself": distance_profile(
            lanes.nearest(lon, lat)[0] for lon, lat in control_points(corridors)),
        "cyclist_hit": distance_profile(e.bike_lane_m for e in riders),
        "cyclist_killed_or_serious": distance_profile(
            e.bike_lane_m for e in riders if e.crash.killed_or_serious),
    }


def crossings_and_crashes(exposures: list[Exposure], corridors,
                          crosswalks: list[Asset]) -> dict:
    """Where pedestrians are hit, against where the crossings are.

    The answer is not the one the phrase "crossing outside a crosswalk" leads
    people to expect, and it points at two different fixes rather than one.
    """
    crossing_index = Nearby([
        Asset(object_id=c.object_id, kind="corridor", subtype="", route=c.name,
              side="", condition="", paths=c.paths)
        for c in corridors if c.paths
    ])

    def on_the_network(exposure: Exposure) -> bool:
        return crossing_index.nearest(
            exposure.crash.lon, exposure.crash.lat, limit_m=40.0)[1] is not None

    on_hin = [e for e in exposures if e.crash.pedestrian and on_the_network(e)]
    serious = [e for e in on_hin if e.crash.killed_or_serious]

    crossings = Nearby(crosswalks)
    return {
        "road_itself": distance_profile(
            crossings.nearest(lon, lat)[0]
            for lon, lat in control_points(corridors)),
        "pedestrian_hit": distance_profile(e.crosswalk_m for e in on_hin),
        "pedestrian_killed_or_serious": distance_profile(
            e.crosswalk_m for e in serious),
    }
