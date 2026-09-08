"""The geography, and where everything lives.

One module holds the study area so that every other module agrees on it. The
bounding box is deliberately generous in the north-south direction and tight
east-west: the interesting traffic in this region runs along a small number of
corridors, not across a grid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("OTOWI_DATA", REPO_ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"


@dataclass(frozen=True)
class Place:
    name: str
    latitude: float
    longitude: float
    note: str = ""


# The anchors of the study area. Coordinates are the town centres; the
# bounding box below is what actually gets extracted.
PLACES = {
    "santa_fe": Place("Santa Fe", 35.6870, -105.9378,
                      "the southern anchor, and the in-town network"),
    "pojoaque": Place("Pojoaque", 35.8781, -106.0217,
                      "where the Los Alamos traffic leaves US-84/285 onto NM-502"),
    "otowi": Place("Otowi Bridge", 35.8742, -106.1400,
                   "NM-502 crosses the Rio Grande here, then climbs to the mesa; "
                   "the hard bottleneck on the Los Alamos commute"),
    "los_alamos": Place("Los Alamos", 35.8809, -106.2970,
                        "the destination that makes the morning peak tidal"),
    "espanola": Place("Espanola", 35.9911, -106.0806,
                      "the junction: US-84/285 splits toward Abiquiu and Taos"),
    "abiquiu_lake": Place("Abiquiu Lake", 36.2381, -106.4283,
                          "weekend recreational demand, a different regime "
                          "entirely from the weekday commute"),
}

# west, south, east, north -- the order Overpass and osmium both expect.
# Chosen to contain every anchor above with a few km of margin, and to stop
# before Taos and before the Albuquerque metro, neither of which we model.
BBOX = (-106.50, 35.55, -105.78, 36.30)

# Only these road classes are extracted. A microscopic simulation of every
# residential cul-de-sac between here and Abiquiu would be enormous and would
# not change a single number we care about; residential streets matter as trip
# origins, not as through capacity.
HIGHWAY_TYPES = [
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary", "secondary_link",
    "tertiary", "tertiary_link",
    "unclassified", "residential",
]

# The corridors the project exists to say something about. Named so that
# results can be reported per corridor rather than as one regional average,
# which would hide exactly the congestion that matters.
CORRIDORS = {
    "us84_285_north": "Santa Fe to Pojoaque and Espanola (US-84/285)",
    "nm502_los_alamos": "Pojoaque to Los Alamos over Otowi Bridge (NM-502)",
    "us84_abiquiu": "Espanola to Abiquiu Lake (US-84)",
    "i25_santa_fe": "I-25 through Santa Fe",
    "st_francis": "St Francis Drive (US-84/285 in town)",
    "cerrillos": "Cerrillos Road",
}

# ---------------------------------------------------------------- returning
#
# LODES measures home-to-work flows and nothing else, so a model built from it
# alone contains only the trip *to* work. Run over a whole day that produces a
# morning peak and an empty evening, because nobody ever drives home -- which
# is not a small error in a corridor model, it is half the traffic.
#
# The return trip has to be generated, and the only honest way to say when it
# leaves is to say what was assumed. ACS publishes departure time to work
# (B08302) and travel time to work (B08303). It publishes nothing at all about
# the return, and no other free source crosses return time with an
# origin-destination pair. So:
#
#   return departure = outbound departure + time away from home
#
# where "time away" is sampled per vehicle. Folding the commute into the same
# quantity is deliberate -- it needs one assumption instead of two, and the
# outbound commute duration is a model *output* that is not known when trips
# are generated.
#
# The mean is the BLS American Time Use Survey figure for hours worked on days
# worked by full-time workers (~8.6 h) plus a round-trip commute and breaks.
# The spread is wide on purpose: it is standing in for part-time work, shift
# lengths, and overtime all at once, none of which this model can distinguish.
#
# **This is the single largest unmeasured input in a whole-day run, and the
# timing of the evening peak is almost entirely determined by it.** It is a
# constant here rather than buried in a function so that it can be varied and
# the result reported as a sensitivity, which is the only defensible way to
# use a number nobody measured.
TIME_AWAY_MEAN_H = 9.6
TIME_AWAY_SD_H = 1.8
TIME_AWAY_MIN_H = 4.0
TIME_AWAY_MAX_H = 14.0

# Peak windows, local time. A full 24-hour microscopic run over this area is
# heavy and mostly empty; the questions worth asking live in these two windows.
AM_PEAK = (6, 9)
PM_PEAK = (15, 18)

#: The whole day. Only meaningful with return trips generated -- without them
#: a 24-hour run is a morning peak followed by fifteen empty hours.
FULL_DAY = (0, 24)
