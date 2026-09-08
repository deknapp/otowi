"""Where people have actually been killed on these roads.

Everything else in this project models what *would* happen. This module reports
what did: every fatal crash NHTSA recorded inside the study area, from the
**Fatality Analysis Reporting System** -- a census, not a sample, of every crash
on a US public road that killed someone within thirty days. It has a coordinate
for each one.

**Why a count is not a risk, and why this module needs the simulation.**
US-84/285 has more fatal crashes than any road in the study area, and it is
also the road the most people drive. Ranking roads by crash count ranks them by
how busy they are, which everyone already knows. The number worth having is
fatalities per unit of travel -- deaths per billion vehicle-kilometres -- and
that needs an exposure denominator on every link.

That denominator is the reason this belongs in a traffic model rather than in a
spreadsheet. NMDOT's AADT covers the state highways and nothing else; the
simulation covers every link but carries only 14% of measured volume. So
exposure is taken from the **measurement** wherever a count segment matched,
and from the model, scaled to the calibration ratio, only where there is no
count -- and every reported rate says which of the two it used, because a
safety number resting on a model's opinion is a different object from one
resting on a traffic count.

**Rare events do not rank.** A single death on a quiet road produces an
enormous rate, and the whole list will otherwise be quiet roads that had one
bad night. Two defences, both stated rather than hidden: rates are only ranked
above a minimum exposure, and a Poisson lower confidence bound is reported
alongside the point estimate, which is the number to compare between links.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

from .config import BBOX, CACHE_DIR

log = logging.getLogger(__name__)

#: FARS publishes one national archive per year. There is a per-crash API too,
#: but it sits behind an edge cache that refuses anything without a browser's
#: fingerprint, and a static file that has not changed since publication is the
#: better dependency anyway.
FARS_URL = ("https://static.nhtsa.gov/nhtsa/downloads/FARS/"
            "{year}/National/FARS{year}NationalCSV.zip")

HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}

#: Fatal crashes are rare enough that one year of one county is noise. Six
#: years is the usual window for site ranking: long enough for the counts to
#: mean something, short enough that the road has not been rebuilt underneath
#: them.
DEFAULT_YEARS = (2018, 2023)

#: How far a crash coordinate may sit from an edge and still be attributed to
#: it. Larger than the count-station radius: FARS coordinates are geocoded from
#: a written location description as often as they are recorded from a device,
#: and the error is metres to tens of metres rather than sub-metre.
MATCH_RADIUS_M = 40.0

#: Below this, a rate is reported but never ranked. One death on a road nobody
#: drives produces a bigger number than a road that kills someone every year,
#: and a "most dangerous roads" list sorted on that is worse than no list.
MIN_EXPOSURE_VEH_KM = 2.0e6

#: And below this length a "corridor" is not one. Ranking short stretches means
#: ranking the stretch where the crash happened, which is circular: the
#: exposure denominator has been chosen by looking at the numerator. Per SUMO
#: edge -- a couple of hundred metres -- every road in the study area returns
#: exactly one crash at three hundred to eight hundred deaths per billion
#: vehicle-kilometres, against a US average near seven. The unit of analysis
#: has to be picked without reference to where the deaths are.
MIN_CORRIDOR_KM = 3.0


@dataclass(frozen=True)
class Crash:
    """One fatal crash, as FARS recorded it."""

    case_id: str
    year: int
    lat: float
    lon: float
    fatalities: int
    hour: int | None
    county: str
    road: str
    route_kind: str
    light: str
    harm: str

    @property
    def is_dark(self) -> bool:
        return self.light.lower().startswith("dark")


def cache_path(years: tuple[int, int]) -> Path:
    return CACHE_DIR / f"fars_{years[0]}_{years[1]}.json"


def _in_bbox(lon: float, lat: float) -> bool:
    west, south, east, north = BBOX
    return west <= lon <= east and south <= lat <= north


def _parse_year(blob: bytes, year: int) -> list[Crash]:
    """Pull the study area's fatal crashes out of one national archive.

    The archive is ~35 MB and holds every state; only New Mexico rows inside
    the bounding box are kept, so what lands on disk is a few hundred records.
    """
    crashes: list[Crash] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        name = next(n for n in archive.namelist()
                    if n.lower().endswith("accident.csv"))
        with archive.open(name) as handle:
            reader = csv.DictReader(io.TextIOWrapper(handle, encoding="latin-1"))
            for row in reader:
                if row.get("STATENAME") != "New Mexico":
                    continue
                try:
                    lat = float(row["LATITUDE"])
                    lon = float(row["LONGITUD"])
                except (TypeError, ValueError, KeyError):
                    continue
                # FARS uses 777.7777 / 888.8888 / 99.9999 for unknown.
                if not (-180 < lon < -60) or not (25 < lat < 50):
                    continue
                if not _in_bbox(lon, lat):
                    continue
                hour = row.get("HOUR")
                crashes.append(Crash(
                    case_id=str(row.get("ST_CASE", "")),
                    year=year,
                    lat=lat,
                    lon=lon,
                    fatalities=int(row.get("FATALS") or 0),
                    hour=int(hour) if (hour or "").isdigit() and int(hour) < 24 else None,
                    county=(row.get("COUNTYNAME") or "").split(" (")[0].title(),
                    road=row.get("TWAY_ID") or "",
                    route_kind=row.get("ROUTENAME") or "",
                    light=row.get("LGT_CONDNAME") or "",
                    harm=row.get("HARM_EVNAME") or "",
                ))
    return crashes


def fetch(years: tuple[int, int] = DEFAULT_YEARS, *, force: bool = False) -> list[Crash]:
    """Every fatal crash in the study area over ``years``, cached on disk.

    Each year is a separate ~35 MB download, which is why the result is cached
    and why the cache is keyed on the year range. Nothing here changes after
    publication.
    """
    path = cache_path(years)
    if path.exists() and not force:
        return [Crash(**row) for row in json.loads(path.read_text())]

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    collected: list[Crash] = []
    for year in range(years[0], years[1] + 1):
        url = FARS_URL.format(year=year)
        log.info("downloading FARS %d", year)
        try:
            response = httpx.get(url, headers=HEADERS, timeout=300.0,
                                 follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # A missing year is a gap in coverage, not a reason to fail: FARS
            # publishes about eighteen months behind, so the most recent year
            # in a range may simply not exist yet. It must be visible, though,
            # because a rate computed over five years and labelled six is
            # wrong by twenty per cent.
            log.warning("FARS %d unavailable (%s) -- excluded from the window",
                        year, exc)
            continue
        found = _parse_year(response.content, year)
        log.info("  %d fatal crashes inside the study area", len(found))
        collected.extend(found)

    path.write_text(json.dumps([asdict(c) for c in collected]))
    return collected


# ------------------------------------------------------------------ matching


def undirected(edge_id: str) -> str:
    """Both directions of one road, as a single key.

    SUMO models each direction as its own edge, and a crash on a two-way road
    lands on whichever side the coordinate fell. Keeping them apart would halve
    the exposure and double the rate on one side while zeroing the other, which
    is an artefact of the geocoding and not a fact about the road.
    """
    return edge_id[1:] if edge_id.startswith("-") else edge_id


def match_to_edges(net, crashes: list[Crash], *,
                   radius_m: float = MATCH_RADIUS_M) -> dict[str, list[Crash]]:
    """Attribute each crash to the nearest drivable edge, by road not direction.

    Unmatched crashes are counted and reported rather than dropped in silence:
    they are mostly on roads the study area does not model -- forest roads,
    tribal routes, and the streets below the extraction threshold -- and the
    share of them is the honest measure of how much of the region's dying this
    map can speak to.
    """
    by_road: dict[str, list[Crash]] = {}
    unmatched = 0

    for crash in crashes:
        x, y = net.convertLonLat2XY(crash.lon, crash.lat)
        best = None
        for edge, distance in net.getNeighboringEdges(x, y, radius_m):
            if edge.isSpecial() or not edge.allows("passenger"):
                continue
            if best is None or distance < best[1]:
                best = (edge.getID(), distance)
        if best is None:
            unmatched += 1
            continue
        by_road.setdefault(undirected(best[0]), []).append(crash)

    log.info("matched %d of %d fatal crashes to roads (%d off the modelled network)",
             len(crashes) - unmatched, len(crashes), unmatched)
    return by_road


# --------------------------------------------------------------------- rates


def _poisson_lower_bound(count: int, confidence: float = 0.90) -> float:
    """Lower bound on a Poisson mean, by the Wilson-Hilferty approximation.

    The point estimate from three deaths and from thirty deaths are both just
    "a rate", and treating them as comparable is how a road that had one bad
    night ends up top of a safety list. This is the number to rank on: it asks
    what rate the evidence will actually support, so a link with few crashes is
    penalised for its own uncertainty rather than flattered by it.
    """
    if count <= 0:
        return 0.0
    z = 1.2816 if confidence >= 0.90 else 1.0  # one-sided normal quantile
    return count * (1.0 - 1.0 / (9.0 * count) - z / (3.0 * math.sqrt(count))) ** 3


@dataclass
class RoadRisk:
    """One corridor's fatality record and the travel that produced it."""

    road_id: str
    name: str
    fatalities: int
    crashes: int
    length_km: float
    vehicle_km: float
    counted_share: float
    years: float

    @property
    def per_billion_veh_km(self) -> float:
        if self.vehicle_km <= 0:
            return 0.0
        return self.fatalities / self.vehicle_km * 1e9

    @property
    def lower_bound(self) -> float:
        """The rate the evidence supports, not the rate it suggests."""
        if self.vehicle_km <= 0:
            return 0.0
        return _poisson_lower_bound(self.fatalities) / self.vehicle_km * 1e9

    @property
    def veh_per_day(self) -> float:
        """Average daily traffic across the corridor, back out of the exposure."""
        divisor = self.length_km * 365.0 * self.years
        return self.vehicle_km / divisor if divisor > 0 else 0.0

    @property
    def rankable(self) -> bool:
        return (self.vehicle_km >= MIN_EXPOSURE_VEH_KM
                and self.length_km >= MIN_CORRIDOR_KM
                and self.fatalities > 0)

    def as_dict(self) -> dict:
        return {
            "road": self.road_id,
            "name": self.name,
            "fatalities": self.fatalities,
            "crashes": self.crashes,
            "length_km": round(self.length_km, 2),
            "veh_per_day": round(self.veh_per_day),
            "counted_share": round(self.counted_share, 2),
            "vehicle_km": round(self.vehicle_km),
            "per_billion_veh_km": round(self.per_billion_veh_km, 1),
            "lower_bound": round(self.lower_bound, 1),
            "rankable": self.rankable,
        }


def corridors_from_counts(counted: dict) -> dict[str, str]:
    """Map each road to the NMDOT route it belongs to.

    The unit of analysis, and the third attempt at choosing one. Per SUMO edge
    is circular (see :data:`MIN_CORRIDOR_KM`). Per OSM street name is not much
    better: most edges here are unnamed, so 7,771 "corridors" appear, the
    highways that carry the deaths are among the unnamed ones, and the named
    ones are half a kilometre long.

    NMDOT's own route identifiers are the answer, and they arrive free with the
    count segments already matched for calibration. They are how the highway
    department thinks about its roads, they run for tens of kilometres, and
    every edge carrying one also carries a measured AADT -- so the exposure
    denominator is a traffic count rather than this model's opinion, on exactly
    the roads where people are dying.

    The cost is coverage: a road with no count segment gets no rate. Those
    crashes are still shown, and still counted in the total, but they are not
    ranked, because there is nothing honest to rank them by.
    """
    return {undirected(edge_id): segment.route_id
            for edge_id, segment in counted.items()}


def build(net, crashes_by_road: dict[str, list[Crash]], *,
          modelled: dict[str, float], counted: dict, years: float,
          model_carries: float) -> dict[str, RoadRisk]:
    """Fatality rate per corridor, with exposure from the best source available.

    Exposure prefers the count, because a safety rate resting on a traffic
    count is a different object from one resting on a model's opinion, and the
    difference should not be buried -- ``counted_share`` says what fraction of
    each corridor's vehicle-kilometres came from measurement. Where there is no
    count the modelled volume is divided by ``model_carries``, the calibration
    ratio, to estimate real traffic.

    Exposure sums over *directed* edges, because a vehicle-kilometre in each
    direction is two vehicle-kilometres of travel. Length sums over undirected
    ones, because two carriageways of the same kilometre are one kilometre of
    road. AADT is halved for the same reason: it is a two-way daily total and
    each direction carries about half of it.
    """
    route_of = corridors_from_counts(counted)
    veh_km: dict[str, float] = {}
    counted_km: dict[str, float] = {}
    lengths: dict[str, dict[str, float]] = {}

    for edge in net.getEdges():
        if edge.isSpecial() or not edge.allows("passenger"):
            continue
        edge_id = edge.getID()
        corridor = route_of.get(undirected(edge_id))
        if corridor is None:
            continue
        km = edge.getLength() / 1000.0
        segment = counted.get(edge_id)
        if segment is not None:
            per_day, is_counted = float(segment.aadt) / 2.0, True
        else:
            scale = model_carries if model_carries > 0 else 1.0
            per_day, is_counted = modelled.get(edge_id, 0.0) / scale, False

        contribution = per_day * km * 365.0 * years
        veh_km[corridor] = veh_km.get(corridor, 0.0) + contribution
        if is_counted:
            counted_km[corridor] = counted_km.get(corridor, 0.0) + contribution
        lengths.setdefault(corridor, {})[undirected(edge_id)] = km

    tallies: dict[str, list[Crash]] = {}
    for road, crash_list in crashes_by_road.items():
        corridor = route_of.get(road)
        if corridor is not None:
            tallies.setdefault(corridor, []).extend(crash_list)

    risks: dict[str, RoadRisk] = {}
    for corridor, total_km in veh_km.items():
        crash_list = tallies.get(corridor, [])
        risks[corridor] = RoadRisk(
            road_id=corridor,
            name=route_label(corridor),
            fatalities=sum(c.fatalities for c in crash_list),
            crashes=len(crash_list),
            length_km=sum(lengths.get(corridor, {}).values()),
            vehicle_km=total_km,
            counted_share=(counted_km.get(corridor, 0.0) / total_km
                           if total_km > 0 else 0.0),
            years=years,
        )
    return risks


def route_label(route_id: str) -> str:
    """"NM68P" -> "NM-68". NMDOT's ids are not what anyone calls the road."""
    stem = route_id[:-1] if route_id.endswith("P") else route_id
    for prefix in ("US", "NM", "I"):
        if stem.startswith(prefix) and stem[len(prefix):].isdigit():
            return f"{prefix}-{stem[len(prefix):]}"
    return stem


def summarise(crashes: list[Crash], risks: dict[str, RoadRisk]) -> dict:
    """Headline numbers, and the ones that qualify what the ranking is worth."""
    ranked = sorted((r for r in risks.values() if r.rankable),
                    key=lambda r: -r.lower_bound)
    dark = sum(1 for c in crashes if c.is_dark)
    ranked_deaths = sum(r.fatalities for r in ranked)
    total_deaths = sum(c.fatalities for c in crashes)
    return {
        "crashes": len(crashes),
        "fatalities": total_deaths,
        "corridors": len(risks),
        "rankable_corridors": len(ranked),
        # The coverage caveat, up front rather than in a footnote. Most of the
        # dying in this region happens on roads NMDOT does not count, so most
        # of it cannot be turned into a rate. Those crashes are still shown and
        # still counted; they are simply not ranked, because there is nothing
        # honest to rank them by.
        "fatalities_on_ranked_corridors": ranked_deaths,
        "share_of_deaths_ranked": (round(ranked_deaths / total_deaths, 3)
                                   if total_deaths else 0.0),
        "share_after_dark": round(dark / len(crashes), 3) if crashes else 0.0,
        "min_exposure_veh_km": MIN_EXPOSURE_VEH_KM,
        "min_corridor_km": MIN_CORRIDOR_KM,
        "years": next(iter(risks.values())).years if risks else 0,
        "worst": [r.as_dict() for r in ranked[:15]],
    }
