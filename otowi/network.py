"""Getting the road network out of OpenStreetMap and into SUMO.

Two steps, deliberately separate:

  1. ``fetch_osm``    -- ask Overpass for the roads in the study area, once,
                         and keep the answer on disk forever.
  2. ``build_network`` -- hand that to SUMO's ``netconvert``, which turns OSM
                         ways into lanes, junctions, connections and traffic
                         lights.

They are separate because they fail differently and cost differently. The
Overpass query is a request against a free, shared, volunteer-run service and
should happen approximately never; netconvert is local, fast, and worth
re-running every time we change our mind about how to treat a road class.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import httpx

from .config import BBOX, CACHE_DIR, HIGHWAY_TYPES

# Several public Overpass instances run the same API. If the main one is busy
# -- and for a query this size it often is -- try the next rather than
# hammering the first.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

# Generous: this is ~12,000 ways and every node on them. Measured against the
# study area on 2026-09-05, so it is a known quantity, not a guess.
OVERPASS_TIMEOUT_S = 900


def _query() -> str:
    """The Overpass QL for every road we model in the study area.

    ``(._;>;)`` is the important part: it recurses from the matched ways down
    to the nodes they are built from. Without it Overpass returns ways with
    node references and no coordinates, and netconvert has nothing to work
    with.
    """
    west, south, east, north = BBOX
    pattern = "|".join(t for t in HIGHWAY_TYPES if not t.endswith("_link"))
    return f"""
[out:xml][timeout:{OVERPASS_TIMEOUT_S}];
(
  way["highway"~"^({pattern})(_link)?$"]({south},{west},{north},{east});
);
(._;>;);
out body;
""".strip()


def osm_path() -> Path:
    return CACHE_DIR / "study-area.osm"


def fetch_osm(*, force: bool = False) -> Path:
    """Download the study area from Overpass, once.

    The result is cached and never expires on its own. A road network is not
    a live quantity -- it changes on the timescale of construction projects,
    not of page loads -- and Overpass is a donated resource. Pass ``force`` to
    deliberately refresh it.
    """
    path = osm_path()
    if path.exists() and not force and path.stat().st_size > 0:
        return path

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    body = _query()
    last: Exception | None = None

    for endpoint in OVERPASS_ENDPOINTS:
        try:
            with httpx.stream("POST", endpoint, data={"data": body},
                              timeout=OVERPASS_TIMEOUT_S,
                              follow_redirects=True) as resp:
                resp.raise_for_status()
                # Stream to a temporary file: the response is tens of
                # megabytes and a partial write must never look like a
                # complete cache entry.
                tmp = path.with_suffix(".partial")
                with tmp.open("wb") as fh:
                    for chunk in resp.iter_bytes(1 << 16):
                        fh.write(chunk)
            if tmp.stat().st_size < 1024:
                raise RuntimeError(f"{endpoint} returned {tmp.stat().st_size} bytes")
            tmp.replace(path)
            return path
        except (httpx.HTTPError, RuntimeError) as exc:
            last = exc
            time.sleep(3)

    raise RuntimeError(f"every Overpass endpoint failed; last error: {last}")


def netconvert_available() -> bool:
    return shutil.which("netconvert") is not None


def network_path() -> Path:
    return CACHE_DIR / "study-area.net.xml"


def build_network(*, force: bool = False) -> Path:
    """Turn the OSM extract into a SUMO network.

    The options here are choices about what kind of model this is, not
    boilerplate:

    ``--geometry.remove`` and ``--ramps.guess`` clean up the artefacts of
    hand-mapped OSM geometry, which otherwise become spurious junctions that
    slow traffic for no physical reason.

    ``--junctions.join`` merges the clusters of nodes that OSM uses to
    represent one real intersection. Without it a single Santa Fe intersection
    can become four junctions in a row, each imposing its own delay, and the
    model invents congestion that does not exist.

    ``--tls.guess-signals`` and ``--tls.join`` do the same for traffic lights.
    Signal timing is the single biggest lever on urban travel time, so getting
    the signals structurally right matters before any of it is calibrated.
    """
    out = network_path()
    if out.exists() and not force:
        return out
    if not netconvert_available():
        raise RuntimeError(
            "netconvert not found. Install SUMO first:\n"
            "  brew tap dlr-ts/sumo && brew install sumo")

    osm = fetch_osm()
    cmd = [
        "netconvert",
        "--osm-files", str(osm),
        "--output-file", str(out),
        "--geometry.remove",
        "--ramps.guess",
        "--junctions.join",
        "--tls.guess-signals",
        "--tls.discard-simple",
        "--tls.join",
        "--roundabouts.guess",
        "--remove-edges.isolated",
        "--keep-edges.by-vclass", "passenger",
        "--no-turnarounds",
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out
