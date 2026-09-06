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

# Peak windows, local time. A full 24-hour microscopic run over this area is
# heavy and mostly empty; the questions worth asking live in these two windows.
AM_PEAK = (6, 9)
PM_PEAK = (15, 18)
