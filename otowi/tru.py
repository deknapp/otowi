"""Every crash the state recorded, not only the ones that killed someone.

`fatalities` reads FARS, which is a census of fatal crashes and nothing else.
That is the right source for *where* people die -- it has a coordinate on every
record -- and the wrong one for *when*. Six years of the study area is on the
order of a hundred and fifty deaths spread over twenty-four hours, so the
hourly curve in `fatalities.by_hour` is built from single-digit counts per hour
and has to be smoothed before it can be looked at.

New Mexico keeps a second crash file, and it is much larger. Every police
Uniform Crash Report -- any incident on a public road with a death, an injury,
or $500 of damage -- lands in the NMDOT Traffic Records Bureau database, and
the **Traffic Research Unit** at UNM publishes it back as a per-community
report under NMDOT contract. Each report contains four bar charts of crashes by
hour of day: all crashes, alcohol-involved crashes, fatal-and-injury crashes,
and pedestrian-and-pedalcycle crashes. For the three counties this project
covers that is roughly **eleven thousand crashes** across 2019-2021, against
FARS's handful. The same curve, with three orders of magnitude more signal.

**What this adds that FARS cannot.**

- *An hourly shape worth the name.* All-severity counts per hour are in the
  hundreds, so the shape stands on its own without smoothing.
- *Pedestrians and cyclists as their own curve.* FARS has too few VRU deaths
  here to say anything about their hour of day. TRU has 250-odd VRU crashes
  over the same years, and they do **not** peak when vehicle crashes do -- they
  concentrate in the evening, when the light goes and people are still walking.
  A tool that applies the driver curve to a pedestrian gets the advice exactly
  backwards.
- *Alcohol as its own curve.* The late-night risk in the FARS numbers is
  usually explained by empty roads. The alcohol curve here peaks at the same
  hours, which says the mechanism is at least as much who is driving as how
  many.

**What it cannot do, stated plainly.** These reports are published per county
and per municipality; the study-area bounding box is neither. Santa Fe County
extends well south of it and Rio Arriba County far to the north, so the counts
are not counts *of the study area* and must never be reported as such. Only
the **shape** of the hourly distribution is used, on the assumption that when
people crash in Rio Arriba County does not depend much on which end of it they
are in. There is no coordinate on any of these records -- nothing here can be
snapped to an edge, which is why `fatalities` still does the per-road work.

And one legal note that belongs in the code rather than a footnote: NMDOT
crash data is collected under 23 U.S.C. 409 and may not be used as evidence in
any action for damages against a road authority. This module reads a published
summary, not the underlying file, but anything downstream of it inherits the
restriction.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

import httpx

from .config import CACHE_DIR

log = logging.getLogger(__name__)

BASE_URL = "https://gps.unm.edu/tru/reports/community-reports"

HEADERS = {
    "User-Agent": "otowi/0.1 (traffic simulation; +https://github.com/deknapp/otowi)",
}


@dataclass(frozen=True)
class Community:
    """One place TRU publishes a report for, and what it covers here."""

    name: str          # as it appears in the figure captions
    scope: str         # "county" or "city"
    slug: str          # as it appears in the URL
    note: str = ""


#: The reports worth reading for this study area. The three counties between
#: them contain the whole bounding box and a good deal besides; the three
#: municipalities are the parts of it where people actually walk, and are
#: carried separately because a county report is dominated by highway crashes
#: and a city report is not.
COMMUNITIES = {
    "santa_fe_county": Community(
        "Santa Fe County", "county", "santafe",
        "the southern half of the box, plus I-25 and everything south of it"),
    "los_alamos_county": Community(
        "Los Alamos County", "county", "losalamos",
        "the Los Alamos end of NM-502, and almost nothing else"),
    "rio_arriba_county": Community(
        "Rio Arriba County", "county", "rioarriba",
        "Espanola north; extends far beyond the box toward Chama"),
    "santa_fe_city": Community(
        "Santa Fe", "city", "santafe",
        "Cerrillos, St Francis, Saint Michaels -- where the VRU crashes are"),
    "espanola_city": Community(
        "Espanola", "city", "espanola",
        "the US-84/285 and NM-68 junction town"),
    "los_alamos_city": Community(
        "Los Alamos", "city", "losalamos",
        "Diamond Drive and the townsite"),
}

#: TRU has not published a community report every year. 2017, 2018 and
#: anything after 2021 do not exist at any URL shape tried; 2016 exists but
#: uses an underscore before the year where the later ones use a hyphen. These
#: are the years that are actually fetchable, verified by request.
YEARS = (2016, 2019, 2020, 2021)

#: 2016 is the odd one out, and hard-coding the separator is better than
#: probing both shapes on every fetch.
_SEPARATOR = {2016: "_"}


# --------------------------------------------------------------- the records


#: The caption wording changed between the 2019 and 2020 report templates --
#: "Pedestrian and Pedalcyclist" became "All Pedestrian and Pedalcycle" -- and
#: a series keyed on the caption would silently split in two. Everything is
#: normalised to these five names instead.
KINDS = {
    "crashes": "all",
    "alcohol-involved crashes": "alcohol",
    "fatal and injury crashes": "injury_or_fatal",
    "pedestrian and pedalcyclist crashes": "vru",
    "all pedestrian and pedalcycle crashes": "vru",
    "dwi arrests": "dwi_arrests",
}


@dataclass(frozen=True)
class HourlyCrashes:
    """One by-hour bar chart, read back off the page.

    ``counts`` is twenty-four integers, midnight first. ``missing`` is what the
    report's own footnote says was dropped for want of an hour -- carried
    because it is the only way to check the parse: the bars plus the missing
    ones have to equal the year's total in Table 1, and they do.
    """

    place: str
    scope: str
    year: int
    kind: str
    counts: list[int] = field(default_factory=list)
    missing: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts)


@dataclass(frozen=True)
class SeverityYear:
    """One row of Table 1: a year of crashes by severity, and how many had drink in them.

    Every report carries ten years of this, so a single 2021 report gives the
    2012-2021 series without fetching ten reports.
    """

    place: str
    scope: str
    year: int
    fatal: int
    injury: int
    property_damage: int
    total: int
    alcohol_fatal: int
    alcohol_injury: int
    alcohol_property_damage: int
    alcohol_total: int

    @property
    def killed_or_injured(self) -> int:
        return self.fatal + self.injury


# ------------------------------------------------------------------ fetching


def report_url(community: Community, year: int) -> str:
    folder = "counties" if community.scope == "county" else "cities"
    stem = "county" if community.scope == "county" else "city"
    sep = _SEPARATOR.get(year, "-")
    return f"{BASE_URL}/{year}/{folder}/{stem}_{community.slug}{sep}{year}.pdf"


def pdf_path(community: Community, year: int) -> Path:
    return CACHE_DIR / "tru" / f"{community.scope}_{community.slug}_{year}.pdf"


def cache_path() -> Path:
    return CACHE_DIR / "tru_hourly.json"


def download(community: Community, year: int, *, force: bool = False) -> Path | None:
    """One report PDF on disk, or ``None`` if TRU has not published it.

    A missing report is a gap in coverage rather than a failure -- the years
    are not contiguous and never will be -- so it is logged and skipped, the
    same way `fatalities.fetch` treats a FARS year that is not out yet.
    """
    path = pdf_path(community, year)
    if path.exists() and path.stat().st_size > 0 and not force:
        return path

    url = report_url(community, year)
    log.info("downloading TRU %s %d", community.name, year)
    try:
        response = httpx.get(url, headers=HEADERS, timeout=120.0,
                             follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("TRU %s %d unavailable (%s)", community.name, year, exc)
        return None

    if not response.content.startswith(b"%PDF"):
        log.warning("TRU %s %d did not return a PDF", community.name, year)
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)
    return path


# ------------------------------------------------------------------- parsing
#
# The numbers wanted here are the data labels printed above the bars of a chart
# in a PDF, which sounds worse than it is. Each label sits horizontally over
# its own bar, so sorting the numeric words in the plot area by x position puts
# them back in hour order -- and the y-axis tick labels, the only other numbers
# in the frame, are the ones to the left of where the hour labels start. Every
# chart in every report tried yields exactly twenty-four values, and every one
# of them adds up to the year's total in Table 1.


_FIGURE = re.compile(
    r"Figure\s+\d+:\s+(?P<what>.+?)\s+by\s+Hour\s+in\s+(?P<where>.+?),\s+(?P<year>\d{4})")
_MISSING = re.compile(r"had\s+([\d,]+)\s+[^.]*?for which hour data were missing")
_NUMBER = re.compile(r"^-?[\d,]+$")
_SEVERITY_ROW = re.compile(r"^(20\d\d)((?:\s+[\d,]+){8})\s*$")


def _lines(words: list[dict], tolerance: float = 3.0) -> list[list[dict]]:
    """Words grouped back into visual lines, in reading order.

    pdfplumber returns words, and the figure captions that say which chart is
    which are sentences. Grouping on ``top`` within a few points reassembles
    them without pulling in a text-extraction dependency that would have its
    own opinion about the page.
    """
    grouped: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if grouped and abs(grouped[-1][0]["top"] - word["top"]) <= tolerance:
            grouped[-1].append(word)
        else:
            grouped.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in grouped]


def _text(line: list[dict]) -> str:
    return " ".join(word["text"] for word in line)


def _chart_values(words: list[dict], top: float, bottom: float) -> tuple[list[int], int]:
    """The bar labels of one chart, in hour order, and its missing-hour count.

    ``top``/``bottom`` bracket the chart: from the caption down to the footnote
    or the next caption. Inside that band the hour axis is the row containing
    "a.m."/"p.m."; anything numeric above that row and not to the left of the
    axis's first label is a bar.
    """
    band = [w for w in words if top < w["top"] and w["bottom"] < bottom]
    axis = [w for w in band if w["text"] in ("a.m.", "p.m.")]
    if not axis:
        return [], 0

    # The hour labels themselves, and nothing else on their row -- the
    # footnote below starts further left, and taking its margin as the plot's
    # left edge lets the y-axis ticks in as if they were bars.
    axis_top = min(word["top"] for word in axis)
    axis_row = [w for w in band if abs(w["top"] - axis_top) <= 3]
    left_edge = min(word["x0"] for word in axis_row)

    labels = [w for w in band
              if w["bottom"] < axis_top - 1
              and w["x0"] > left_edge - 4
              and _NUMBER.match(w["text"])]
    labels.sort(key=lambda w: w["x0"])

    missing = 0
    for line in _lines([w for w in band if w["top"] > axis_top + 3]):
        found = _MISSING.search(_text(line))
        if found:
            missing = int(found.group(1).replace(",", ""))
            break

    return [int(w["text"].replace(",", "")) for w in labels], missing


def parse(path: Path) -> tuple[list[HourlyCrashes], list[SeverityYear]]:
    """Read one report: its four by-hour charts, and its ten-year severity table.

    Charts that do not come back as exactly twenty-four values are dropped with
    a warning rather than returned short. A nineteen-value hour curve would be
    wrong in a way that no downstream check would catch, since every hour after
    the gap would be shifted into its neighbour.
    """
    import pdfplumber

    hourly: list[HourlyCrashes] = []
    # Table 1 is on page 3 and the first figure caption is on page 5, so the
    # severity rows are read before anything on the page has said which
    # community this report is for. They are stamped with it at the end.
    pending: list[tuple[int, list[int]]] = []
    place = scope = None

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue
            lines = _lines(words)

            for index, line in enumerate(lines):
                text = _text(line)

                found = _FIGURE.search(text)
                if found:
                    where = found.group("where")
                    place = where
                    scope = "county" if where.endswith("County") else "city"
                    kind = KINDS.get(found.group("what").strip().lower())
                    if kind is None:
                        log.debug("unrecognised TRU chart: %s", found.group("what"))
                        continue

                    # The band runs to the next caption, not to the chart's
                    # own footnote -- that footnote is the missing-hour count,
                    # and it is the only way to check the parse. It sits below
                    # the hour axis, so it can never be mistaken for a bar.
                    top = line[0]["bottom"]
                    bottom = page.height
                    for later in lines[index + 1:]:
                        if _text(later).startswith(("Figure ", "Table ")):
                            bottom = later[0]["top"]
                            break

                    counts, missing = _chart_values(words, top, bottom)
                    if len(counts) != 24:
                        log.warning("TRU %s %s %s: got %d bars, not 24 -- dropped",
                                    where, found.group("year"), kind, len(counts))
                        continue
                    hourly.append(HourlyCrashes(
                        place=where, scope=scope, year=int(found.group("year")),
                        kind=kind, counts=counts, missing=missing))
                    continue

                row = _SEVERITY_ROW.match(text.replace(" ,", ","))
                if row:
                    numbers = [int(n.replace(",", "")) for n in row.group(2).split()]
                    pending.append((int(row.group(1)), numbers))

    if place is None:
        log.warning("TRU %s: no by-hour figure found, so no community name "
                    "either -- nothing usable in this report", path.name)
        return [], []

    severity = [SeverityYear(
        place=place, scope=scope, year=year,
        fatal=n[0], injury=n[1], property_damage=n[2], total=n[3],
        alcohol_fatal=n[4], alcohol_injury=n[5],
        alcohol_property_damage=n[6], alcohol_total=n[7],
    ) for year, n in pending]
    return hourly, severity


def check(hourly: list[HourlyCrashes], severity: list[SeverityYear]) -> list[str]:
    """Where the charts and the table disagree. Empty is the expected answer.

    The all-crash chart plus its missing-hour footnote must equal that year's
    total in Table 1, and the fatal-and-injury chart must equal fatal plus
    injury. Both hold exactly in every report read so far, which is the reason
    to trust a number scraped off a bar chart at all.
    """
    table = {(s.place, s.year): s for s in severity}
    problems = []
    for series in hourly:
        row = table.get((series.place, series.year))
        if row is None:
            continue
        if series.kind == "all":
            expected, got = row.total, series.total + series.missing
        elif series.kind == "injury_or_fatal":
            expected, got = row.killed_or_injured, series.total + series.missing
        elif series.kind == "alcohol":
            expected, got = row.alcohol_total, series.total + series.missing
        else:
            continue
        if expected != got:
            problems.append(
                f"{series.place} {series.year} {series.kind}: "
                f"chart {got} vs table {expected}")
    return problems


def fetch(*, years: tuple[int, ...] = YEARS, force: bool = False
          ) -> tuple[list[HourlyCrashes], list[SeverityYear]]:
    """Every hourly series and severity row for the study area, cached on disk.

    Nineteen PDFs at about a megabyte each, parsed once. Nothing here changes
    after publication, so the cache never needs invalidating except to pick up
    a year TRU has newly released.
    """
    path = cache_path()
    if path.exists() and not force:
        blob = json.loads(path.read_text())
        return ([HourlyCrashes(**row) for row in blob["hourly"]],
                [SeverityYear(**row) for row in blob["severity"]])

    hourly: list[HourlyCrashes] = []
    # The severity table repeats: every report carries the preceding ten years,
    # so the same (place, year) arrives once per report. Later reports win --
    # a crash record can be amended after publication, and the most recently
    # published figure is the one the state currently stands behind.
    deduped: dict[tuple[str, int], SeverityYear] = {}
    for community in COMMUNITIES.values():
        for year in sorted(years):
            report = download(community, year, force=force)
            if report is None:
                continue
            found_hourly, found_severity = parse(report)
            hourly.extend(found_hourly)
            for row in found_severity:
                deduped[(row.place, row.year)] = row

    severity = sorted(deduped.values(), key=lambda s: (s.place, s.year))

    for problem in check(hourly, severity):
        log.warning("TRU parse disagrees with the report's own table: %s", problem)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "hourly": [asdict(h) for h in hourly],
        "severity": [asdict(s) for s in severity],
    }, indent=1))
    return hourly, severity


# ----------------------------------------------------------------- combining


#: Which communities to add together for a study-area curve. The three counties
#: cover the bounding box between them and do not overlap each other; the city
#: reports are subsets of their counties, so mixing the two would double-count.
COUNTY_KEYS = ("santa_fe_county", "los_alamos_county", "rio_arriba_county")
CITY_KEYS = ("santa_fe_city", "espanola_city", "los_alamos_city")


def profile(hourly: list[HourlyCrashes], kind: str, *,
            places: tuple[str, ...] | None = None,
            years: tuple[int, ...] | None = None) -> list[int]:
    """Twenty-four numbers: one kind of crash, summed over places and years.

    ``places`` takes community *names* as they appear in the reports; pass the
    output of `names_for` rather than typing them. Summing counties is a sum of
    disjoint areas and is fine; summing a county and a city inside it is not,
    and is the one mistake this function will happily let you make.
    """
    counts = [0] * 24
    for series in hourly:
        if series.kind != kind:
            continue
        if places is not None and series.place not in places:
            continue
        if years is not None and series.year not in years:
            continue
        for hour in range(24):
            counts[hour] += series.counts[hour]
    return counts


def names_for(keys: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(COMMUNITIES[key].name for key in keys)


def relative_risk(counts: list[int], travel: dict[int, float], *,
                  smooth: bool = False) -> list[dict]:
    """How much more dangerous each hour is per kilometre driven, from TRU counts.

    The same arithmetic as `fatalities.by_hour` -- share of crashes over share
    of travel, scaled so an ordinary hour is 1.0 -- against the same exposure
    curve out of the simulation. Two independent numerators over one
    denominator: if a fatal-crash census and an all-severity state file
    disagree about which hours are dangerous, that is worth knowing, and if
    they agree the headline rests on more than a hundred and fifty deaths.

    Smoothing is off by default here, unlike in `fatalities`, because these
    counts do not need it -- the all-crash curve runs to hundreds per hour. It
    is available for the VRU curve, which does.
    """
    from .fatalities import _smooth

    total = sum(counts) or 1
    total_travel = sum(travel.values()) or 1.0

    crash_share = [n / total for n in counts]
    travel_share = [travel.get(hour, 0.0) / total_travel for hour in range(24)]
    if smooth:
        crash_share = _smooth(crash_share)
        travel_share = _smooth(travel_share)

    rows = []
    for hour in range(24):
        share_t = travel_share[hour]
        rows.append({
            "hour": hour,
            "crashes": counts[hour],
            "share_of_crashes": round(crash_share[hour], 4),
            "share_of_travel": round(share_t, 4),
            "relative_risk": round(crash_share[hour] / share_t, 2) if share_t > 0 else 0.0,
        })
    return rows


def summarise(hourly: list[HourlyCrashes], severity: list[SeverityYear]) -> dict:
    """What the reports say, before any exposure denominator is applied.

    Counts and shares only. The evening peak in the VRU curve is visible here
    without a simulation at all, which is the point: it is a fact about when
    people are hit, not an output of a model.
    """
    counties = names_for(COUNTY_KEYS)
    cities = names_for(CITY_KEYS)
    years = tuple(sorted({s.year for s in hourly}))

    out: dict = {"years": years, "counties": list(counties), "by_kind": {}}
    for kind in ("all", "injury_or_fatal", "alcohol", "vru"):
        counts = profile(hourly, kind, places=counties)
        out["by_kind"][kind] = {
            "total": sum(counts),
            "counts": counts,
            "peak_hour": max(range(24), key=lambda h: counts[h]) if sum(counts) else None,
        }

    # The VRU curve is the one worth carrying separately, and the city reports
    # are where it lives -- a county figure is diluted by highway crashes that
    # no pedestrian was anywhere near.
    vru_city = profile(hourly, "vru", places=cities)
    evening = sum(vru_city[18:22])
    out["vru_in_towns"] = {
        "total": sum(vru_city),
        "counts": vru_city,
        "share_1800_2200": round(evening / sum(vru_city), 3) if sum(vru_city) else 0.0,
    }

    alcohol = profile(hourly, "alcohol", places=counties)
    night = sum(alcohol[22:24]) + sum(alcohol[0:4])
    out["alcohol_at_night"] = {
        "total": sum(alcohol),
        "share_2200_0400": round(night / sum(alcohol), 3) if sum(alcohol) else 0.0,
    }

    county_rows = [s for s in severity if s.place in counties]
    if county_rows:
        span = tuple(sorted({s.year for s in county_rows}))
        out["severity"] = {
            "years": (span[0], span[-1]),
            "crashes": sum(s.total for s in county_rows),
            "fatal": sum(s.fatal for s in county_rows),
            "injury": sum(s.injury for s in county_rows),
            "property_damage": sum(s.property_damage for s in county_rows),
            "alcohol_involved": sum(s.alcohol_total for s in county_rows),
        }
        # How many crashes the state recorded for each one FARS calls fatal.
        # The reason to have this module at all, as a single number.
        out["severity"]["crashes_per_fatal"] = round(
            out["severity"]["crashes"] / max(out["severity"]["fatal"], 1), 1)
    return out
