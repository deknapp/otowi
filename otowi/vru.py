"""The people who were not driving.

`fatalities` finds 40 people killed on foot or on a bike in this study area
over six years, and then cannot say much about them. Forty is enough to notice
that a quarter of the deaths here were outside a vehicle and that most happened
after dark; it is not enough to say which streets, or at what hour, or whether
that hour is the same one that is dangerous for drivers. `tru` answers the hour
question from published totals, but those totals are per county and carry no
coordinate, so they cannot say which street.

NMDOT's **Vulnerable Road User Safety Assessment** answers both. It is a
federally mandated analysis -- every state had to produce one under the
Bipartisan Infrastructure Law -- and New Mexico published the underlying crash
records as an open ArcGIS layer rather than only a PDF. Every pedestrian and
pedalcycle crash in the state from 2013 onward, with a latitude, a longitude,
an hour, a KABCO severity, the lighting condition, and whether drink was
involved. **758 of them are inside this bounding box**, against FARS's 40
deaths -- and unlike the 40, they include the people who were hit and lived,
which is most of them.

That makes the vulnerable-road-user question answerable with the same machinery
as the driver question: snap to an edge, divide by exposure, rank. With two
differences that have to be carried rather than smoothed over.

**There is no exposure denominator for walking.** Nobody counts pedestrians on
these streets. A crash rate needs to know how many people crossed, and the only
thing available is how many *cars* went past, which answers a different
question -- "how dangerous is this road to be near", not "how dangerous is it
to walk down". Both are worth having and they are not the same number, so
anything per-vehicle-kilometre here is labelled as what it is.

**Their dangerous hour is not the driver's dangerous hour.** Drivers concentrate
their risk after midnight. The people hit while walking concentrate in the
evening, when the light goes and they are still out. A routing tool that applies
one hourly curve to both modes will tell a pedestrian to travel at exactly the
wrong time, which is worse than telling them nothing.

The companion layer, **High Injury Network**, is NMDOT's own ranking of the
corridors: 79 segments in Santa Fe County alone, each with a crash severity
index, an AADT, and its pedestrian killed-or-serious count. It is the
independent check on this project's corridor ranking -- a list built by the
state, from the state's own file, without reference to anything here.

NMDOT crash data is collected under 23 U.S.C. 409 and may not be used as
evidence in an action for damages against a road authority.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict, field
from pathlib import Path

import httpx

from .config import BBOX, CACHE_DIR

log = logging.getLogger(__name__)

#: NMDOT's public ArcGIS Online organisation. The VRU layers are open and
#: unauthenticated; the service root lists about two hundred others.
ARCGIS = "https://services.arcgis.com/hOpd7wfnKm16p9D9/arcgis/rest/services"

CRASHES_LAYER = f"{ARCGIS}/VRU_crashes/FeatureServer/0"
#: The High Injury Network sits at layer 1, not 0. Layer 0 answers with an
#: empty schema rather than an error, which is a quiet way to get nothing.
HIN_LAYER = f"{ARCGIS}/High_Injury_Network_Corridor_Segments/FeatureServer/1"

HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}

#: The service caps a response at 2,000 features and says so in its metadata.
#: The bounding box holds fewer than that, but paging costs nothing to write
#: and a silent truncation at 2,000 would look exactly like a smaller study
#: area.
PAGE = 1000

CRASH_FIELDS = [
    "OBJECTID", "CRASH_YEAR", "HOUR", "DAY_OF_WEEK", "KABCO",
    "LIGHTING", "ALCOHOL_INVOLVEMENT", "DRUG_INVOLVEMENT",
    "PEDESTRIAN_INVOLVEMENT", "PEDALCYCLE_INVOLVEMENT",
    "COUNTY", "CITY", "PRIMARY_STREET", "GIS_DERIVED_ROUTE_NAME",
    "ROAD_SYSTEM__URBAN__RURAL_OR_RU",
]

#: KABCO is the national injury scale as the officer coded it at the scene.
#: K and A together are "killed or seriously injured" -- the threshold every
#: safety programme is written against, because B and C absorb a great deal of
#: reporting variation and O is a crash where nobody was hurt at all.
SEVERITY = {
    "K": "killed",
    "A": "suspected serious injury",
    "B": "suspected minor injury",
    "C": "possible injury",
    "O": "no injury",
}


@dataclass(frozen=True)
class VruCrash:
    """One pedestrian or cyclist struck by a vehicle."""

    object_id: int
    year: int
    hour: int | None
    day: str
    severity: str
    lighting: str
    alcohol: bool
    drugs: bool
    pedestrian: bool
    pedalcycle: bool
    county: str
    city: str
    street: str
    route: str
    lat: float
    lon: float

    @property
    def killed_or_serious(self) -> bool:
        return self.severity in ("K", "A")

    @property
    def is_dark(self) -> bool:
        return self.lighting.lower().startswith("dark")

    @property
    def mode(self) -> str:
        if self.pedestrian and self.pedalcycle:
            return "both"
        return "pedestrian" if self.pedestrian else "cyclist"


@dataclass(frozen=True)
class Corridor:
    """One High Injury Network segment, as NMDOT ranked it.

    ``severity_index`` is NMDOT's own weighting of the crashes on the segment,
    not a rate -- it does not divide by AADT or by length, so a busy long road
    scores highly partly for being busy and long. It is carried unchanged
    because the point of this layer is to be the state's ranking rather than
    an improvement on it.
    """

    object_id: int
    name: str
    county: str
    city: str
    severity_index: float
    length_mi: float
    vru_crashes: int
    ped_ka: int
    bike_ka: int
    ksi: int
    aadt: int | None
    speed_limit: int | None
    lanes: int | None
    #: The segment's own shape, as [[lon, lat], ...] paths. Carried because
    #: the whole point of the state's ranking is to be drawn next to this
    #: project's own, and a ranking without a line on a map is a table.
    paths: list[list[list[float]]] = field(default_factory=list)


# ------------------------------------------------------------------ fetching


def _envelope() -> str:
    west, south, east, north = BBOX
    return json.dumps({"xmin": west, "ymin": south, "xmax": east, "ymax": north,
                       "spatialReference": {"wkid": 4326}})


def _query(layer: str, fields: list[str], *, geometry: bool = True) -> list[dict]:
    """Every feature of ``layer`` inside the study area, paged.

    ArcGIS answers a query larger than its record cap by truncating it and
    setting ``exceededTransferLimit``, which is easy to miss and looks like a
    smaller study area. Paging until it stops saying so is the only safe read.
    """
    features: list[dict] = []
    offset = 0
    while True:
        params = {
            "geometry": _envelope(),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "where": "1=1",
            "outFields": ",".join(fields),
            "returnGeometry": "true" if geometry else "false",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE),
            "f": "json",
        }
        response = httpx.get(f"{layer}/query", params=params, headers=HEADERS,
                             timeout=120.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS refused the query: {payload['error']}")
        page = payload.get("features", [])
        features.extend(page)
        if not payload.get("exceededTransferLimit") or not page:
            break
        offset += len(page)
    return features


def crashes_path() -> Path:
    return CACHE_DIR / "nmdot_vru_crashes.json"


def corridors_path() -> Path:
    return CACHE_DIR / "nmdot_high_injury_network.json"


def _flag(value) -> bool:
    return str(value).strip() in ("1", "Y", "Yes", "true", "True")


def fetch(*, force: bool = False) -> list[VruCrash]:
    """Every pedestrian and cyclist crash inside the study area, cached."""
    path = crashes_path()
    if path.exists() and not force:
        return [VruCrash(**row) for row in json.loads(path.read_text())]

    log.info("querying NMDOT VRU crashes")
    found: list[VruCrash] = []
    for feature in _query(CRASHES_LAYER, CRASH_FIELDS):
        attributes = feature.get("attributes", {})
        geometry = feature.get("geometry") or {}
        lon, lat = geometry.get("x"), geometry.get("y")
        if lon is None or lat is None:
            continue
        hour = attributes.get("HOUR")
        found.append(VruCrash(
            object_id=int(attributes.get("OBJECTID") or 0),
            year=int(attributes.get("CRASH_YEAR") or 0),
            hour=int(hour) if isinstance(hour, int) and 0 <= hour < 24 else None,
            day=(attributes.get("DAY_OF_WEEK") or "").strip(),
            severity=(attributes.get("KABCO") or "").strip().upper(),
            lighting=(attributes.get("LIGHTING") or "").strip(),
            alcohol=_flag(attributes.get("ALCOHOL_INVOLVEMENT")),
            drugs=_flag(attributes.get("DRUG_INVOLVEMENT")),
            pedestrian=_flag(attributes.get("PEDESTRIAN_INVOLVEMENT")),
            pedalcycle=_flag(attributes.get("PEDALCYCLE_INVOLVEMENT")),
            county=(attributes.get("COUNTY") or "").strip(),
            city=(attributes.get("CITY") or "").strip(),
            street=(attributes.get("PRIMARY_STREET") or "").strip().title(),
            route=(attributes.get("GIS_DERIVED_ROUTE_NAME") or "").strip(),
            lat=float(lat), lon=float(lon),
        ))

    log.info("  %d VRU crashes inside the study area", len(found))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in found]))
    return found


HIN_FIELDS = [
    "OBJECTID", "Road_name", "County", "City", "Crash_Severity_Index",
    "length_mi", "VRU_crash_count", "Ped_KA_crash_count",
    "Bike_KA_crash_count", "sum_unrolled_ksi_count", "AADT",
    "Speed_limit", "Number_of_lanes",
]


def fetch_corridors(*, force: bool = False) -> list[Corridor]:
    """NMDOT's own High Injury Network segments inside the study area."""
    path = corridors_path()
    if path.exists() and not force:
        return [Corridor(**row) for row in json.loads(path.read_text())]

    log.info("querying NMDOT High Injury Network")
    found: list[Corridor] = []
    for feature in _query(HIN_LAYER, HIN_FIELDS):
        a = feature.get("attributes", {})
        paths = [[[round(x, 5), round(y, 5)] for x, y in path]
                 for path in (feature.get("geometry") or {}).get("paths", [])]

        def number(key, cast=int):
            value = a.get(key)
            return cast(value) if value not in (None, "") else None

        found.append(Corridor(
            object_id=int(a.get("OBJECTID") or 0),
            name=(a.get("Road_name") or "").strip(),
            county=(a.get("County") or "").strip(),
            city=(a.get("City") or "").strip(),
            severity_index=float(a.get("Crash_Severity_Index") or 0.0),
            length_mi=float(a.get("length_mi") or 0.0),
            vru_crashes=int(a.get("VRU_crash_count") or 0),
            ped_ka=int(a.get("Ped_KA_crash_count") or 0),
            bike_ka=int(a.get("Bike_KA_crash_count") or 0),
            ksi=int(a.get("sum_unrolled_ksi_count") or 0),
            aadt=number("AADT"),
            speed_limit=number("Speed_limit"),
            lanes=number("Number_of_lanes"),
            paths=paths,
        ))

    log.info("  %d High Injury Network segments", len(found))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(c) for c in found]))
    return found


# ------------------------------------------------------------------ reading


#: Street names arrive as the officer typed them. "Cerrillos Rd" and "Cerrillos
#: Road" are 117 crashes and 7 crashes on the same road, and ranking without
#: joining them puts the road's own overflow eight rows below itself. Only the
#: suffix is normalised -- expanding abbreviations is safe, guessing at
#: "St Michaels" (Saint, not Street) is not, so anything ambiguous is left
#: alone and the result is a better join rather than a canonical one.
_SUFFIXES = {
    "RD": "ROAD", "DR": "DRIVE", "AVE": "AVENUE", "AV": "AVENUE",
    "BLVD": "BOULEVARD", "HWY": "HIGHWAY", "TRL": "TRAIL", "LN": "LANE",
    "PL": "PLACE", "CT": "COURT", "PKWY": "PARKWAY", "CIR": "CIRCLE",
    "STE": "STREET", "STR": "STREET",
}


def _normalise_street(name: str) -> str:
    """One street name, joined to its own variants where that is unambiguous."""
    words = name.upper().replace(",", " ").split()
    if not words:
        return ""
    # A trailing route designator -- "St Francis Dr  US 84/285" -- is the same
    # road under two names, and the second one is already its own row.
    words = [w.rstrip(".") for w in words]
    if words[-1] in _SUFFIXES:
        words[-1] = _SUFFIXES[words[-1]]
    return " ".join(words).title()


def by_hour(crashes: list[VruCrash]) -> list[int]:
    counts = [0] * 24
    for crash in crashes:
        if crash.hour is not None:
            counts[crash.hour] += 1
    return counts


def worst_streets(crashes: list[VruCrash], count: int = 10) -> list[dict]:
    """Where people are hit, ranked by killed-or-seriously-injured.

    Ranked on KSI rather than on all crashes on purpose. A crash where nobody
    was hurt is real and is worth counting, but it is also the category most
    sensitive to whether anyone bothered to file a report, and a street that
    lands at the top of a KSI list is there because of something that happened
    to a person.
    """
    streets: dict[str, dict] = {}
    for crash in crashes:
        name = _normalise_street(crash.street)
        if not name:
            continue
        row = streets.setdefault(name, {
            "street": name, "crashes": 0, "killed_or_serious": 0,
            "killed": 0, "after_dark": 0, "pedestrian": 0, "cyclist": 0})
        row["crashes"] += 1
        row["killed_or_serious"] += crash.killed_or_serious
        row["killed"] += crash.severity == "K"
        row["after_dark"] += crash.is_dark
        row["pedestrian"] += crash.pedestrian
        row["cyclist"] += crash.pedalcycle
    ranked = sorted(streets.values(),
                    key=lambda r: (-r["killed_or_serious"], -r["crashes"]))
    return ranked[:count]


def summarise(crashes: list[VruCrash], corridors: list[Corridor]) -> dict:
    """What the layer says, before any exposure denominator is applied."""
    if not crashes:
        return {"crashes": 0}

    years = sorted({c.year for c in crashes if c.year})
    killed = [c for c in crashes if c.severity == "K"]
    ksi = [c for c in crashes if c.killed_or_serious]
    dark = [c for c in crashes if c.is_dark]
    counts = by_hour(crashes)

    # The evening block, and the reason this module exists: it is neither when
    # drivers crash nor when the roads are empty.
    evening = sum(counts[17:23])
    ksi_counts = by_hour(ksi)

    out = {
        "crashes": len(crashes),
        "years": (years[0], years[-1]) if years else None,
        "killed": len(killed),
        "killed_or_serious": len(ksi),
        "share_after_dark": round(len(dark) / len(crashes), 3),
        "share_after_dark_when_killed": round(
            sum(c.is_dark for c in killed) / len(killed), 3) if killed else 0.0,
        "share_alcohol": round(sum(c.alcohol for c in crashes) / len(crashes), 3),
        "pedestrians": sum(c.pedestrian for c in crashes),
        "cyclists": sum(c.pedalcycle for c in crashes),
        "by_hour": counts,
        "ksi_by_hour": ksi_counts,
        "share_1700_2300": round(evening / sum(counts), 3) if sum(counts) else 0.0,
        "ksi_share_1700_2300": round(sum(ksi_counts[17:23]) / sum(ksi_counts), 3)
        if sum(ksi_counts) else 0.0,
        "worst_streets": worst_streets(crashes),
    }

    if corridors:
        ranked = sorted(corridors, key=lambda c: -c.severity_index)
        out["high_injury_network"] = {
            "segments": len(corridors),
            "worst": [{
                "name": c.name, "city": c.city,
                "severity_index": round(c.severity_index, 1),
                "vru_crashes": c.vru_crashes, "ped_ka": c.ped_ka,
                "aadt": c.aadt, "speed_limit": c.speed_limit,
                "length_mi": round(c.length_mi, 2),
            } for c in ranked[:10]],
        }
        # The state's ranking aggregated the way this project reports: per
        # road, not per segment, so it can be read against otowi's corridors.
        roads: dict[str, dict] = {}
        for corridor in corridors:
            row = roads.setdefault(corridor.name, {
                "name": corridor.name, "segments": 0, "miles": 0.0,
                "severity_index": 0.0, "vru_crashes": 0, "ped_ka": 0, "ksi": 0})
            row["segments"] += 1
            row["miles"] += corridor.length_mi
            row["severity_index"] += corridor.severity_index
            row["vru_crashes"] += corridor.vru_crashes
            row["ped_ka"] += corridor.ped_ka
            row["ksi"] += corridor.ksi
        out["high_injury_network"]["by_road"] = sorted(
            roads.values(), key=lambda r: -r["severity_index"])[:10]
    return out


def relative_risk(crashes: list[VruCrash], travel: dict[int, float], *,
                  smooth: bool = True) -> list[dict]:
    """Per kilometre *driven*, which is not the same as per kilometre walked.

    The denominator is vehicle travel, because vehicle travel is the only
    thing this project can measure. So this answers "at what hour is a
    kilometre of driving most likely to hit somebody on foot", which is a
    question about the driver's exposure and a real one. It is **not** a
    pedestrian's risk of being hit, which would need to divide by how many
    people were walking, and nobody counts that.

    Smoothed by default, unlike the driver curve: 758 crashes over 24 hours is
    thirty or so an hour, and two quiet hours in a row would otherwise read as
    a safe stretch of evening.
    """
    from .fatalities import _smooth

    counts = by_hour(crashes)
    total = sum(counts) or 1
    total_travel = sum(travel.values()) or 1.0

    crash_share = _smooth([n / total for n in counts]) if smooth else \
        [n / total for n in counts]
    travel_share = _smooth([travel.get(h, 0.0) / total_travel for h in range(24)]) \
        if smooth else [travel.get(h, 0.0) / total_travel for h in range(24)]

    rows = []
    for hour in range(24):
        share_t = travel_share[hour]
        rows.append({
            "hour": hour,
            "crashes": counts[hour],
            "share_of_crashes": round(crash_share[hour], 4),
            "share_of_travel": round(share_t, 4),
            "relative_risk": round(crash_share[hour] / share_t, 2) if share_t else 0.0,
        })
    return rows
