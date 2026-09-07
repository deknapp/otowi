"""Ground truth: what NMDOT actually measured on these roads.

This is the module that decides whether anything else here is worth reading. A
simulation that runs is a simulation that runs; a simulation whose edge volumes
have been compared against counts someone took with a tube across the road is a
different object, and the difference is this file.

The source is NMDOT's published AADT layer -- annual average daily traffic per
highway segment, with the station ID it came from, the year, and two factors
that matter more than they look:

* **K factor**, the share of a day's traffic in the design hour, published as a
  percentage. Roughly 9-15 here.
* **D factor**, the share of that hour travelling in the busier direction, also
  a percentage. Roughly 55-75.

``AADT * K * D`` is therefore a directional peak-hour volume, which is the
thing a morning-peak simulation can be compared against. Comparing a modelled
peak hour to a raw daily total would be meaningless, and it is the mistake this
module exists to not make.

**What the comparison can and cannot show.** This model contains commuting and
nothing else -- no freight, no shopping, no school runs, no tourism. So it
should undercount every observed volume, and the interesting question is not
whether it matches but *by how much, and whether the shortfall is consistent*.
A uniform shortfall is a mode-share constant to fit. A shortfall that varies
wildly by corridor means the demand model is wrong somewhere specific.

**Held-out validation.** Segments are split into a fit half and a held-out
half by a hash of their station, not at random per run, so the split is stable
across runs and cannot be reshuffled until it flatters the result. Error is
reported on both, and the held-out number is the one that counts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as etree

import httpx

from .config import BBOX, CACHE_DIR

log = logging.getLogger(__name__)

AADT_URL = (
    "https://services.arcgis.com/hOpd7wfnKm16p9D9/arcgis/rest/services/"
    "Annual_Average_Daily_Traffic_2026/FeatureServer/0/query"
)

#: ArcGIS caps a single response; paginate with resultOffset.
PAGE_SIZE = 2000

HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}

#: How far a count segment's midpoint may be from an edge to be considered the
#: same road. Tight, because AADT segments follow the state highway centreline
#: and so does the OSM way; a match further away than this is usually the
#: frontage road or the opposite carriageway.
MATCH_RADIUS_M = 40.0

#: Maximum angle between the count segment and the edge, in degrees. Without
#: this a segment matches a cross street that happens to pass close to its
#: midpoint, which produces a confident comparison against the wrong road.
MATCH_BEARING_TOLERANCE_DEG = 30.0


@dataclass(frozen=True)
class CountSegment:
    """One measured highway segment."""

    route_id: str
    station_id: str | None
    aadt: int
    aadt_year: int
    k_factor: float
    d_factor: float
    lon: float
    lat: float
    bearing_deg: float

    @property
    def peak_hour_directional(self) -> float:
        """AADT converted to vehicles per hour in the busier direction.

        K and D are published as percentages, hence the two divisions. This is
        the design hour rather than specifically the morning peak: for a
        commuter corridor the two are usually the same hour, but on a
        recreational route like US-84 toward Abiquiu the design hour can be a
        summer weekend afternoon, and a morning-peak comparison there is
        measuring different traffic. Corridors are reported separately for
        this reason.
        """
        return self.aadt * (self.k_factor / 100.0) * (self.d_factor / 100.0)

    @property
    def key(self) -> str:
        return self.station_id or f"{self.route_id}@{self.lon:.5f},{self.lat:.5f}"

    def held_out(self, *, fraction: float = 0.5) -> bool:
        """Stable fit/held-out split, hashed rather than randomised.

        A split re-drawn each run is a split that can be re-drawn until the
        error looks good. Hashing the station means the same segments are held
        out on every run, on every machine, forever.
        """
        digest = hashlib.sha256(self.key.encode()).hexdigest()
        return (int(digest[:8], 16) % 1000) / 1000.0 < fraction


def cache_path() -> Path:
    return CACHE_DIR / "nmdot_aadt.json"


def fetch_aadt(
    bbox: tuple[float, float, float, float] = BBOX,
    *,
    force: bool = False,
) -> list[dict]:
    """Download every AADT segment intersecting the study area.

    Paginated, because ArcGIS returns at most 2,000 features per request and
    silently truncates rather than erroring -- another instance of the failure
    mode this project keeps meeting, so the loop checks ``exceededTransferLimit``
    instead of trusting a short page.
    """
    path = cache_path()
    if path.exists() and not force:
        return json.loads(path.read_text())

    west, south, east, north = bbox
    features: list[dict] = []
    offset = 0

    while True:
        params = {
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "AADT>0",
            "outFields": "RouteID,StationID,AADT,AADTYear,KFactor,DFactor",
            "returnGeometry": "true",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
            "f": "json",
        }
        response = httpx.get(AADT_URL, params=params, headers=HEADERS, timeout=120.0)
        response.raise_for_status()
        payload = response.json()

        if "error" in payload:
            raise RuntimeError(f"NMDOT AADT query failed: {payload['error']}")

        page = payload.get("features", [])
        features.extend(page)
        log.info("fetched %d AADT segments (%d total)", len(page), len(features))

        if not payload.get("exceededTransferLimit") or not page:
            break
        offset += len(page)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(features))
    return features


def _bearing(x1: float, y1: float, x2: float, y2: float) -> float:
    """Compass-free bearing in degrees, folded to 0-180.

    Folded because a road has an orientation but a count is not directional in
    the geometry -- a segment drawn south-to-north is the same road as one
    drawn north-to-south, and treating them as 180 degrees apart would reject
    correct matches.
    """
    angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
    return angle


def parse_segments(features: list[dict]) -> list[CountSegment]:
    """Turn ArcGIS polylines into midpoints with a bearing.

    A segment is represented by the midpoint of its longest path and the
    bearing there. That is a lossy summary of a polyline, and it is the right
    one here: matching is against a SUMO edge, which is also short and roughly
    straight, and comparing whole geometries would be precision the rest of
    this pipeline does not have.
    """
    segments: list[CountSegment] = []

    for feature in features:
        attributes = feature.get("attributes", {})
        paths = feature.get("geometry", {}).get("paths", [])
        if not paths:
            continue
        path = max(paths, key=len)
        if len(path) < 2:
            continue

        middle = len(path) // 2
        (x1, y1), (x2, y2) = path[middle - 1][:2], path[middle][:2]

        aadt = attributes.get("AADT")
        k = attributes.get("KFactor")
        d = attributes.get("DFactor")
        if not aadt or not k or not d:
            # Without both factors an AADT cannot be turned into a peak hour,
            # and comparing against the daily total would be worse than
            # skipping the segment.
            continue

        segments.append(
            CountSegment(
                route_id=attributes.get("RouteID") or "",
                station_id=attributes.get("StationID"),
                aadt=int(aadt),
                aadt_year=int(attributes.get("AADTYear") or 0),
                k_factor=float(k),
                d_factor=float(d),
                lon=(x1 + x2) / 2.0,
                lat=(y1 + y2) / 2.0,
                bearing_deg=_bearing(x1, y1, x2, y2),
            )
        )

    log.info("%d of %d AADT features usable (have AADT, K and D)",
             len(segments), len(features))
    return segments


def match_to_edges(
    net,
    segments: list[CountSegment],
    *,
    radius_m: float = MATCH_RADIUS_M,
    bearing_tolerance: float = MATCH_BEARING_TOLERANCE_DEG,
) -> dict[str, CountSegment]:
    """Map each count segment onto the network edge it measures.

    Both a radius and a bearing test, because either alone is wrong: distance
    alone matches a cross street passing near the midpoint, and bearing alone
    matches a parallel road a block over. Where several segments land on one
    edge the highest AADT wins, since the alternative -- averaging measurements
    taken at different points on different years -- invents a number nobody
    recorded.
    """
    matched: dict[str, CountSegment] = {}
    unmatched = 0

    for segment in segments:
        x, y = net.convertLonLat2XY(segment.lon, segment.lat)
        best = None

        for edge, distance in net.getNeighboringEdges(x, y, radius_m):
            if edge.isSpecial() or not edge.allows("passenger"):
                continue
            shape = edge.getShape()
            if len(shape) < 2:
                continue
            edge_bearing = _bearing(*shape[0][:2], *shape[-1][:2])
            difference = abs(edge_bearing - segment.bearing_deg)
            difference = min(difference, 180.0 - difference)
            if difference > bearing_tolerance:
                continue
            if best is None or distance < best[1]:
                best = (edge.getID(), distance)

        if best is None:
            unmatched += 1
            continue

        edge_id = best[0]
        existing = matched.get(edge_id)
        if existing is None or segment.aadt > existing.aadt:
            matched[edge_id] = segment

    log.info(
        "matched %d edges to count segments (%d segments unmatched)",
        len(matched), unmatched,
    )
    return matched


def simulated_hourly(edgedata: Path, window_hours: float) -> dict[str, float]:
    """Vehicles per hour on each edge, from SUMO's edgeData output.

    The edgeData interval covers the whole simulation window, so ``entered``
    is a window total and has to be divided by the window length before it can
    sit next to a peak-*hour* count. Comparing a three-hour total against an
    hourly measurement would overstate the model by a factor of three, which
    would look like the model being far too busy rather than like an arithmetic
    error.
    """
    volumes: dict[str, float] = {}
    for _, element in etree.iterparse(str(edgedata), events=("end",)):
        if element.tag != "edge":
            element.clear()
            continue
        entered = element.get("entered")
        if entered is not None:
            volumes[element.get("id")] = int(entered) / window_hours
        element.clear()
    return volumes


def compare(
    matched: dict[str, CountSegment],
    simulated: dict[str, float],
    *,
    held_out_fraction: float = 0.5,
) -> dict:
    """Model against measurement, reported separately for fit and held-out.

    The headline is GEH, the statistic traffic engineers actually use for this:

        GEH = sqrt( 2 * (m - c)^2 / (m + c) )

    It is preferred to a percentage error because it tolerates larger relative
    differences on small flows, where a difference of fifty vehicles means very
    little, and tightens on large ones, where it means a great deal. The
    conventional reading is that GEH < 5 is a good match on an individual link
    and that a model is usually accepted when 85% of links clear it.

    Nothing here is expected to clear that yet. This model contains commuting
    only, so it should undercount, and the ratio is reported alongside so the
    shortfall can be seen rather than only scored.
    """
    rows = []
    for edge_id, segment in matched.items():
        modelled = simulated.get(edge_id)
        if modelled is None:
            continue
        observed = segment.peak_hour_directional
        if observed <= 0:
            continue
        geh = math.sqrt(2 * (modelled - observed) ** 2 / (modelled + observed))
        rows.append({
            "edge": edge_id,
            "route": segment.route_id,
            "station": segment.station_id,
            "observed_veh_per_h": round(observed, 1),
            "modelled_veh_per_h": round(modelled, 1),
            "ratio": round(modelled / observed, 3),
            "geh": round(geh, 2),
            "held_out": segment.held_out(fraction=held_out_fraction),
            "aadt": segment.aadt,
            "aadt_year": segment.aadt_year,
        })

    def stats(subset: list[dict]) -> dict:
        if not subset:
            return {"links": 0}
        gehs = sorted(row["geh"] for row in subset)
        ratios = sorted(row["ratio"] for row in subset)
        return {
            "links": len(subset),
            "median_geh": round(gehs[len(gehs) // 2], 2),
            "share_geh_under_5": round(sum(1 for g in gehs if g < 5) / len(gehs), 3),
            "median_ratio_modelled_over_observed": round(ratios[len(ratios) // 2], 3),
        }

    return {
        "all": stats(rows),
        "fit": stats([row for row in rows if not row["held_out"]]),
        "held_out": stats([row for row in rows if row["held_out"]]),
        "links": rows,
    }
