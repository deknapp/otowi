"""Where the trips come from, and why they are not invented.

This is the module the project exists to justify. A road network from
OpenStreetMap plus randomly generated trips produces an animation, not a
model: the network is real, the demand is fiction, and every number that comes
out the other end inherits the fiction.

So the origin-destination flows here come from **LEHD LODES** -- the Census
Bureau's Longitudinal Employer-Household Dynamics data, which counts, for
every pair of census blocks in the country, how many people live in one and
work in the other. It is built from unemployment-insurance wage records
covering roughly 95% of private employment, published annually, and free.

Three things it is not, stated here because they bound every result
downstream:

* **It is workplace-residence, not travel.** A LODES pair says someone's job
  is there and their home is here. It does not say they drove, drove alone,
  drove that day, or drove that route. Mode share and telework are applied
  separately and are visible assumptions, not hidden ones.
* **It has no time in it.** Nothing in LODES says when anyone leaves. The
  departure profile is a separate data source with its own citation; see
  :mod:`otowi.departure`. Inventing a bell curve here would move every
  congestion number in the model while looking entirely plausible.
* **It is commuting only.** Freight, tourism, and the weekend run to Abiquiu
  are not in it. Where those matter they must be added and labelled as
  estimates rather than folded in quietly.

The 2022 vintage is the default because LODES lags: the most recent release
covers 2022 as of LODES8. That is not a current-conditions model and the
provenance says so.
"""

from __future__ import annotations

import csv
import gzip
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import BBOX, CACHE_DIR

log = logging.getLogger(__name__)

LODES_BASE = "https://lehd.ces.census.gov/data/lodes/LODES8"
STATE = "nm"

#: LODES release year. LODES8 currently publishes through 2022.
DEFAULT_YEAR = 2022

#: ``JT00`` is all jobs. The alternatives split by job type (primary only,
#: private only); all-jobs is the right denominator for travel demand because
#: a second job still generates a commute.
JOB_TYPE = "JT00"

HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}


@dataclass(frozen=True)
class Flow:
    """One origin-destination pair and the number of workers on it.

    ``jobs`` is an annual average of jobs, not vehicles and not trips. Turning
    it into vehicles is :func:`vehicle_trips`, and every factor in that
    conversion is named.
    """

    home_block: str
    work_block: str
    home_lat: float
    home_lon: float
    work_lat: float
    work_lon: float
    jobs: int


def _download(url: str, dest: Path) -> Path:
    """Fetch once, keep forever. Written atomically so an interrupted download
    does not leave a half file that looks cached."""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("downloading %s", url)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    with httpx.stream("GET", url, headers=HEADERS, follow_redirects=True,
                      timeout=300.0) as response:
        response.raise_for_status()
        with open(tmp, "wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    tmp.replace(dest)
    return dest


def crosswalk_path() -> Path:
    return CACHE_DIR / f"{STATE}_xwalk.csv.gz"


def od_path(year: int = DEFAULT_YEAR) -> Path:
    return CACHE_DIR / f"{STATE}_od_main_{JOB_TYPE}_{year}.csv.gz"


def fetch_crosswalk() -> Path:
    """The geography crosswalk: every census block in the state with a centroid.

    Used instead of TIGER shapefiles because it ships block centroids as plain
    columns, comes from the same publisher as the flows, and is two megabytes
    rather than a geospatial stack.
    """
    return _download(f"{LODES_BASE}/{STATE}/{STATE}_xwalk.csv.gz", crosswalk_path())


def fetch_od(year: int = DEFAULT_YEAR) -> Path:
    """The origin-destination file: ``main`` is jobs where both ends are in-state.

    The companion ``aux`` file holds workers living out of state. For this
    study area that is a small correction dominated by people who both live
    and work in northern New Mexico, and including it would require the
    crosswalks of every neighbouring state to place the home end.
    """
    return _download(
        f"{LODES_BASE}/{STATE}/od/{STATE}_od_main_{JOB_TYPE}_{year}.csv.gz",
        od_path(year),
    )


def block_centroids(bbox: tuple[float, float, float, float] = BBOX) -> dict[str, tuple[float, float]]:
    """Census blocks whose centroid falls inside the study area.

    A block centroid is a point standing in for an area. For the dense blocks
    in Santa Fe that is a fine approximation; for a rural block spanning
    several kilometres of Rio Arriba County it is a coarse one, and the error
    lands on the first road the trip is attached to rather than on the route
    as a whole.
    """
    west, south, east, north = bbox
    path = fetch_crosswalk()
    centroids: dict[str, tuple[float, float]] = {}

    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                lat = float(row["blklatdd"])
                lon = float(row["blklondd"])
            except (KeyError, ValueError):
                # Blocks with no centroid are unusable here; they are a
                # handful of water and unpopulated blocks.
                continue
            if west <= lon <= east and south <= lat <= north:
                centroids[row["tabblk2020"]] = (lat, lon)

    log.info("%d census blocks inside the study area", len(centroids))
    return centroids


def commute_flows(
    year: int = DEFAULT_YEAR,
    centroids: dict[str, tuple[float, float]] | None = None,
    *,
    min_jobs: int = 1,
) -> list[Flow]:
    """Origin-destination pairs with both ends inside the study area.

    Pairs with one end outside are dropped rather than clipped to the
    boundary. Clipping would put a phantom trip end on the edge of the box and
    load the boundary roads with traffic that in reality carries on to
    Albuquerque -- a boundary artefact that looks like congestion.

    This is a real limitation, not a rounding decision: it removes the
    Albuquerque and Taos commutes entirely. The count-station calibration is
    the check that will show how much that matters, because those trips do use
    the corridors we model even though we cannot see both their ends.
    """
    centroids = centroids if centroids is not None else block_centroids()
    path = fetch_od(year)

    flows: list[Flow] = []
    total_pairs = 0
    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            total_pairs += 1
            home = row["h_geocode"]
            work = row["w_geocode"]
            home_point = centroids.get(home)
            work_point = centroids.get(work)
            if home_point is None or work_point is None:
                continue
            jobs = int(row["S000"])
            if jobs < min_jobs:
                continue
            if home == work:
                # Living and working in the same census block is a walk, or a
                # drive too short to route. Either way it is not network load.
                continue
            flows.append(
                Flow(
                    home_block=home,
                    work_block=work,
                    home_lat=home_point[0],
                    home_lon=home_point[1],
                    work_lat=work_point[0],
                    work_lon=work_point[1],
                    jobs=jobs,
                )
            )

    log.info(
        "%d of %d statewide pairs have both ends in the study area (%d workers)",
        len(flows), total_pairs, sum(f.jobs for f in flows),
    )
    return flows


# --------------------------------------------------------------------------
# Turning workers into vehicles.
#
# Every factor below is a published number with a citation, kept as a named
# constant rather than folded into a single fudge factor, so that each one can
# be argued with separately and replaced when the calibration says so.
# --------------------------------------------------------------------------

#: Share of commuters who drive, alone or carpooling, rather than walking,
#: cycling, riding transit or working from home. ACS 2022 5-year "means of
#: transportation to work" for these counties runs near 0.80 statewide; it is
#: a placeholder until :mod:`otowi.counts` can calibrate it, and it is the
#: single largest lever on total volume.
DRIVE_SHARE = 0.80

#: Average vehicle occupancy for commute trips. ACS carpooling rates put this
#: a little above 1.0; the national commute figure is ~1.09.
VEHICLE_OCCUPANCY = 1.09

#: Share of LODES jobs generating a trip on any given weekday. Accounts for
#: leave, shift patterns, part-time work, and telework days. Deliberately the
#: crudest number here, and the first one calibration should move.
WEEKDAY_TRIP_RATE = 0.75


def vehicles_on(flow: Flow) -> float:
    """Expected morning vehicles for one flow, as a fraction not a count.

    Returned as a float on purpose. Rounding each of forty thousand flows to
    an integer independently discards most of the demand -- the median flow is
    one or two workers, and ``int(1 * 0.8 * 0.75 / 1.09)`` is zero. Trip
    generation samples against the fractional expectation instead; see
    :mod:`otowi.trips`.
    """
    return flow.jobs * DRIVE_SHARE * WEEKDAY_TRIP_RATE / VEHICLE_OCCUPANCY


def summarize(flows: list[Flow]) -> dict[str, float]:
    """Headline numbers, for the CLI and for provenance."""
    workers = sum(f.jobs for f in flows)
    return {
        "pairs": len(flows),
        "workers": workers,
        "expected_morning_vehicles": round(sum(vehicles_on(f) for f in flows), 1),
        "drive_share": DRIVE_SHARE,
        "vehicle_occupancy": VEHICLE_OCCUPANCY,
        "weekday_trip_rate": WEEKDAY_TRIP_RATE,
    }
