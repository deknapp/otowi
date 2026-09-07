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
    #: "internal" -- both ends inside the study area.
    #: "inbound"  -- lives outside, works inside; enters at a gateway.
    #: "outbound" -- lives inside, works outside; leaves at a gateway.
    kind: str = "internal"


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


def block_centroids(
    bbox: tuple[float, float, float, float] | None = BBOX,
) -> dict[str, tuple[float, float]]:
    """Census blocks and their centroids, optionally limited to the study area.

    Passing ``bbox=None`` returns every block in the state. That is needed for
    external trips: to place a commute from Albuquerque at the right gateway we
    have to know where in Albuquerque it started, and that block is by
    definition outside the study area.

    A block centroid is a point standing in for an area. For the dense blocks
    in Santa Fe that is a fine approximation; for a rural block spanning
    several kilometres of Rio Arriba County it is a coarse one, and the error
    lands on the first road the trip is attached to rather than on the route
    as a whole.
    """
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
            if bbox is None:
                centroids[row["tabblk2020"]] = (lat, lon)
                continue
            west, south, east, north = bbox
            if west <= lon <= east and south <= lat <= north:
                centroids[row["tabblk2020"]] = (lat, lon)

    if bbox is None:
        log.info("%d census blocks statewide", len(centroids))
    else:
        log.info("%d census blocks inside the study area", len(centroids))
    return centroids


def commute_flows(
    year: int = DEFAULT_YEAR,
    centroids: dict[str, tuple[float, float]] | None = None,
    *,
    min_jobs: int = 1,
    include_external: bool = True,
    bbox: tuple[float, float, float, float] = BBOX,
) -> list[Flow]:
    """Origin-destination pairs that put a vehicle on a road we model.

    Three cases, and only the third is genuinely out of reach:

    * **Both ends inside** -- 39,697 pairs, 52,197 workers. The whole journey
      is inside the study area.
    * **One end inside** -- 46,020 pairs, 47,528 workers. Nearly as much again.
      Someone living in Santa Fe and working in Albuquerque drives the Santa Fe
      half of that trip on roads we model, and the first version of this
      function threw all of it away. These are kept, marked ``inbound`` or
      ``outbound``, and attached to a boundary gateway by
      :mod:`otowi.gateways` rather than to their real out-of-area end.
    * **Neither end inside** -- pure through traffic. Not represented here;
      LODES is home-to-work pairs and cannot see a trip that merely passes
      through. This remains a known gap.

    The external end keeps its real coordinates so that gateway choice has
    something to work with; the trip is placed at the boundary later, not here.
    """
    # External trips need coordinates for blocks outside the study area, so the
    # lookup has to be statewide when they are wanted.
    if centroids is None:
        centroids = block_centroids(bbox=None if include_external else bbox)

    west, south, east, north = bbox

    def inside(point: tuple[float, float]) -> bool:
        lat, lon = point
        return west <= lon <= east and south <= lat <= north

    path = fetch_od(year)
    flows: list[Flow] = []
    total_pairs = 0
    counts = {"internal": 0, "inbound": 0, "outbound": 0}

    with gzip.open(path, "rt", newline="") as handle:
        for row in csv.DictReader(handle):
            total_pairs += 1
            home = row["h_geocode"]
            work = row["w_geocode"]
            home_point = centroids.get(home)
            work_point = centroids.get(work)
            if home_point is None or work_point is None:
                continue

            home_in, work_in = inside(home_point), inside(work_point)
            if not home_in and not work_in:
                # Neither end is ours. LODES cannot tell us whether this trip
                # passes through the study area, so it is left out.
                continue
            if home_in and work_in:
                kind = "internal"
            elif work_in:
                kind = "inbound"
            else:
                kind = "outbound"
            if kind != "internal" and not include_external:
                continue

            jobs = int(row["S000"])
            if jobs < min_jobs:
                continue
            if home == work:
                # Living and working in the same census block is a walk, or a
                # drive too short to route. Either way it is not network load.
                continue

            counts[kind] += 1
            flows.append(
                Flow(
                    home_block=home,
                    work_block=work,
                    home_lat=home_point[0],
                    home_lon=home_point[1],
                    work_lat=work_point[0],
                    work_lon=work_point[1],
                    jobs=jobs,
                    kind=kind,
                )
            )

    log.info(
        "%d of %d statewide pairs touch the study area "
        "(%d internal, %d inbound, %d outbound; %d workers)",
        len(flows), total_pairs, counts["internal"], counts["inbound"],
        counts["outbound"], sum(f.jobs for f in flows),
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
