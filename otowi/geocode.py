"""Turning an address into a point, without becoming a burden to anyone.

The driving page offers six places because there are six places worth naming
in a regional commute model. A walking tool cannot work that way: the question
is "is it safe to ride from my house to work", and a dropdown cannot hold
everybody's house.

**OpenStreetMap's Nominatim** does the lookup. It is free, it is the same data
this project's network is built from -- so an address it finds is an address on
a street the router knows -- and it permits cross-origin requests, which means
the published static copy can use it directly with no server of its own.

It is also a donated service with a published usage policy, and the whole of
this module is about honouring it:

* **One request per submitted address, never per keystroke.** Autocomplete is
  the specific thing the policy asks people not to build, and it is also the
  thing that would make this a hundred requests where one would do.
* **A real User-Agent**, naming the project and where to complain about it.
* **At most one request a second**, enforced here rather than hoped for.
* **Cached on disk forever.** An address does not move. Running the same
  journey twice costs one lookup, and a developer iterating on the page costs
  one lookup per address for the life of the checkout.
* **Bounded to the study area**, which is politeness and accuracy at once:
  "Cerrillos Road" without a box is ambiguous across the country, and with one
  it is a street in Santa Fe.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

from .config import BBOX, CACHE_DIR

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"

#: The policy asks for an identifying User-Agent with a way to get in touch.
#: A generic one is the thing that gets a project blocked.
HEADERS = {
    "User-Agent": "otowi/0.1 (traffic safety simulation; "
                  "+https://github.com/deknapp/otowi)",
}

#: One request a second, which is the published limit. The lock makes it hold
#: across the server's request threads rather than per thread, where it would
#: mean nothing.
MIN_INTERVAL_S = 1.0
_throttle = threading.Lock()
_last_call = 0.0


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    kind: str = ""


def cache_path() -> Path:
    return CACHE_DIR / "geocode.json"


def _load_cache() -> dict:
    path = cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        # A truncated cache is not worth a traceback: it is a cache.
        return {}


def _normalise(query: str) -> str:
    """The cache key. Case and runs of whitespace are not part of an address."""
    return re.sub(r"\s+", " ", query.strip().lower())


def _wait_turn() -> None:
    global _last_call
    with _throttle:
        gap = time.monotonic() - _last_call
        if gap < MIN_INTERVAL_S:
            time.sleep(MIN_INTERVAL_S - gap)
        _last_call = time.monotonic()


def lookup(query: str, *, limit: int = 5, force: bool = False) -> list[Place]:
    """Candidate points for one address, best first, cached on disk.

    Returns an empty list rather than raising when nothing matches or the
    service is unreachable: a failed lookup is an answer the page has to show
    either way, and it is not an error in this program.
    """
    key = _normalise(query)
    if not key:
        return []

    cache = _load_cache()
    if key in cache and not force:
        return [Place(**row) for row in cache[key]]

    west, south, east, north = BBOX
    params = {
        "q": query,
        "format": "jsonv2",
        "limit": str(limit),
        # Nominatim wants the viewbox corners in this order, and `bounded`
        # makes it a filter rather than a preference.
        "viewbox": f"{west},{north},{east},{south}",
        "bounded": "1",
        "addressdetails": "0",
    }
    _wait_turn()
    try:
        response = httpx.get(NOMINATIM, params=params, headers=HEADERS,
                             timeout=20.0, follow_redirects=True)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("geocoding %r failed: %s", query, exc)
        return []

    found = [
        Place(name=row.get("display_name", ""),
              lat=float(row["lat"]), lon=float(row["lon"]),
              kind=row.get("type", ""))
        for row in payload if row.get("lat") and row.get("lon")
    ]

    cache[key] = [asdict(place) for place in found]
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1))
    return found
