"""When people leave, from the only source that actually measures it.

LODES says who commutes between which pair of blocks. It says nothing at all
about time, and time is most of the question here: the Los Alamos commute is
interesting precisely because a large share of it departs inside the same
ninety minutes and crosses one bridge.

So the departure profile comes from **ACS table B08302, "Time of Departure to
Go to Work"** -- the American Community Survey question that asks respondents
what time they usually left home. It is the real distribution, at county
level, in half-hour bins.

Why the bulk file and not the API. The Census data API now requires a key. The
same table is published in the ACS Summary File as a plain pipe-delimited
download with no key and no signup, so that is what this uses. The file is
national and about 70 MB; it is streamed and filtered to the five study
counties as it arrives, and never written to disk in full.

What this profile is not:

* **It is not individual behaviour.** A half-hour bin holding 9,064 people
  does not say which minute any of them left. Trips are sampled uniformly
  inside their bin, which smooths the within-bin structure -- real departures
  cluster on the hour and half-hour, and this will not reproduce that.
* **It is not route- or destination-specific.** ACS reports departure time by
  county of residence, not by where someone is going. A Los Alamos commuter
  leaving Santa Fe is drawn from the Santa Fe County curve like everyone else,
  even though LANL start times are more clustered than the county average.
  Fixing that needs a source that crosses departure time with destination, and
  ACS does not publish one.
* **It is not "usually" in a strict sense.** The question asks about a usual
  week, so it under-represents irregular and shift work.

Each of these pushes the modelled peak toward being *flatter* than reality,
which means congestion here is more likely to be understated than invented.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import CACHE_DIR

log = logging.getLogger(__name__)

ACS_YEAR = 2022
ACS_URL = (
    "https://www2.census.gov/programs-surveys/acs/summary_file/"
    f"{ACS_YEAR}/table-based-SF/data/5YRData/acsdt5y{ACS_YEAR}-b08302.dat"
)

HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}

#: The counties the study area covers, by FIPS. Sandoval and Taos are included
#: because the bounding box clips their edges; their weight in the blended
#: profile is proportional to the workers actually inside the box.
COUNTIES = {
    "35028": "Los Alamos",
    "35039": "Rio Arriba",
    "35043": "Sandoval",
    "35049": "Santa Fe",
    "35055": "Taos",
}

#: B08302 variable -> (start hour, end hour) in local time, as published.
#: Variable 001 is the total and is excluded. The last two bins are wide
#: because ACS publishes them that way, and a wide bin sampled uniformly is
#: visibly wrong for the midday and evening tails -- which is why trip
#: generation works from the morning window and not from this table's whole
#: 24 hours.
BINS: list[tuple[str, float, float]] = [
    ("B08302_E002", 0.0, 5.0),
    ("B08302_E003", 5.0, 5.5),
    ("B08302_E004", 5.5, 6.0),
    ("B08302_E005", 6.0, 6.5),
    ("B08302_E006", 6.5, 7.0),
    ("B08302_E007", 7.0, 7.5),
    ("B08302_E008", 7.5, 8.0),
    ("B08302_E009", 8.0, 8.5),
    ("B08302_E010", 8.5, 9.0),
    ("B08302_E011", 9.0, 10.0),
    ("B08302_E012", 10.0, 11.0),
    ("B08302_E013", 11.0, 12.0),
    ("B08302_E014", 12.0, 16.0),
    ("B08302_E015", 16.0, 24.0),
]


@dataclass(frozen=True)
class Bin:
    """A half-hour (or wider) departure bin and how many workers are in it."""

    start_hour: float
    end_hour: float
    workers: int

    @property
    def width_hours(self) -> float:
        return self.end_hour - self.start_hour


def cache_path() -> Path:
    return CACHE_DIR / f"acs_b08302_{ACS_YEAR}.json"


def fetch_departure_table(*, force: bool = False) -> dict[str, dict[str, int]]:
    """County departure counts, streamed out of the national summary file.

    Stops reading as soon as all five counties have been seen. The file is
    ordered by geography and the New Mexico rows arrive early, so this reads a
    fraction of the 70 MB in practice.
    """
    path = cache_path()
    if path.exists() and not force:
        return json.loads(path.read_text())

    wanted = {f"0500000US{fips}": fips for fips in COUNTIES}
    found: dict[str, dict[str, int]] = {}
    header: list[str] | None = None

    log.info("streaming ACS B08302 from %s", ACS_URL)
    with httpx.stream("GET", ACS_URL, headers=HEADERS, follow_redirects=True,
                      timeout=300.0) as response:
        response.raise_for_status()
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if header is None:
                    header = line.split("|")
                    continue
                geo_id = line.split("|", 1)[0]
                if geo_id in wanted:
                    row = dict(zip(header, line.split("|"), strict=False))
                    found[wanted[geo_id]] = {
                        key: int(row[key]) for key, _, _ in BINS if row.get(key, "").lstrip("-").isdigit()
                    }
            if len(found) == len(wanted):
                break

    missing = set(COUNTIES) - set(found)
    if missing:
        raise RuntimeError(
            f"ACS B08302 did not contain {sorted(missing)}. The summary-file "
            "layout may have changed; check {ACS_URL}."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(found, indent=2, sort_keys=True))
    return found


def county_profile(fips: str, *, table: dict[str, dict[str, int]] | None = None) -> list[Bin]:
    """The departure distribution for one county, as bins."""
    table = table if table is not None else fetch_departure_table()
    counts = table[fips]
    return [Bin(start, end, counts.get(key, 0)) for key, start, end in BINS]


def blended_profile(
    weights: dict[str, float] | None = None,
    *,
    table: dict[str, dict[str, int]] | None = None,
) -> list[Bin]:
    """One profile for the whole study area, weighted by county.

    ``weights`` should be the number of modelled workers *living* in each
    county -- residence, because B08302 is a question about when the
    respondent left home. Passing None weights every county equally, which is
    wrong for this area (Santa Fe has seven times the commuters of Los Alamos)
    and exists only so the profile can be inspected without the demand loaded.
    """
    table = table if table is not None else fetch_departure_table()
    weights = weights or {fips: 1.0 for fips in COUNTIES}

    blended: list[Bin] = []
    for index, (_, start, end) in enumerate(BINS):
        total = 0.0
        for fips, weight in weights.items():
            county = county_profile(fips, table=table)
            share = county[index].workers / max(1, sum(b.workers for b in county))
            total += share * weight
        blended.append(Bin(start, end, int(round(total))))
    return blended


def morning_weights(bins: list[Bin], window: tuple[int, int]) -> list[tuple[Bin, float]]:
    """Restrict a profile to a simulation window and renormalize.

    Returns each overlapping bin with the share of *window* traffic it carries.
    A bin straddling the window edge contributes only the fraction of its width
    that falls inside, which assumes departures are uniform within the bin --
    the same assumption sampling makes, applied consistently.

    Renormalizing is the honest choice for a peak-window simulation: it models
    the shape of the morning, and explicitly does not claim to model the people
    who left at 4am or at noon. Their absence is a stated scope limit rather
    than a silent loss of vehicles.
    """
    start_hour, end_hour = window
    overlapping: list[tuple[Bin, float]] = []
    for item in bins:
        overlap = min(item.end_hour, end_hour) - max(item.start_hour, start_hour)
        if overlap <= 0 or item.workers <= 0:
            continue
        share_of_bin = overlap / item.width_hours
        overlapping.append((item, item.workers * share_of_bin))

    total = sum(weight for _, weight in overlapping)
    if total <= 0:
        raise ValueError(f"No departures fall inside window {window}")
    return [(item, weight / total) for item, weight in overlapping]


def day_weights(bins: list[Bin]) -> list[tuple[Bin, float]]:
    """The whole 24-hour profile, normalized. Every worker appears exactly once.

    Trip generation samples from *this*, never from a window-restricted
    profile, and then keeps the trips whose departure lands in the window being
    simulated. That ordering matters and getting it backwards was a bug:
    :func:`morning_weights` renormalizes the bin shares to the window, so
    combining it with a whole-day vehicle count placed 100% of a flow's drivers
    inside 06:00-09:00 when only 70.5% of departures happen then -- a 1.42x
    over-count of the morning peak, invisible in every output because the
    vehicle total looked exactly as intended.

    Filtering also gets the trips a renormalized window can never produce: the
    night-shift worker whose drive *home* lands in the morning peak.
    """
    total = sum(item.workers for item in bins)
    if total <= 0:
        raise ValueError("departure profile is empty")
    return [(item, item.workers / total) for item in bins if item.workers > 0]


def summarize(bins: list[Bin], window: tuple[int, int]) -> dict:
    """Headline numbers for the CLI and provenance."""
    inside = morning_weights(bins, window)
    peak_bin, peak_share = max(inside, key=lambda pair: pair[1])
    return {
        "window": f"{window[0]:02d}:00-{window[1]:02d}:00",
        "share_of_all_departures_in_window": round(
            sum(b.workers for b, _ in inside) / max(1, sum(b.workers for b in bins)), 3
        ),
        "peak_bin": f"{peak_bin.start_hour:04.1f}-{peak_bin.end_hour:04.1f}",
        "peak_bin_share_of_window": round(peak_share, 3),
    }
