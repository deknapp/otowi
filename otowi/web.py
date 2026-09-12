"""A local map of the model and of where it is wrong.

The point of this is not the animation. It is that the calibration result --
that the model carries a small fraction of measured traffic, and that the
fraction varies enormously by corridor -- is a *spatial* claim, and a table of
GEH values does not let anyone check it. On a map you can see immediately that
the model is nearly absent from I-25 and much closer on the in-town streets,
which is the difference between "the model is bad" and "the model is missing
through traffic".

So there are three layers and the third is the interesting one:

* **Modelled** -- vehicles per hour the simulation put on each edge.
* **Measured** -- NMDOT's AADT converted to a directional peak hour.
* **Ratio** -- modelled over measured, on a diverging scale. This is the layer
  that shows the shape of the model's failure rather than its size.

Everything is served from localhost. Nothing is uploaded, and the page works
with no basemap if the tile server is unreachable -- the road network is
legible on its own, being the thing we drew.
"""

from __future__ import annotations

import http.server
import json
import logging
import socketserver
import threading
import webbrowser
from pathlib import Path

from .config import AM_PEAK, CACHE_DIR

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

#: Edges below this many vehicles per hour are left out of the modelled layer.
#: Not for correctness -- for legibility and page weight. The network has
#: 33,000 edges and most of them carry a handful of vehicles; drawing all of
#: them produces a grey haze that hides the corridors.
MIN_DRAWN_VEH_PER_H = 20.0

#: Coordinate precision. Five decimal places is about a metre, which is far
#: finer than anything else in this pipeline and keeps the file a third of the
#: size of full float repr.
COORD_PRECISION = 5


def geojson_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"map-am-{window[0]:02d}{window[1]:02d}.geojson"


def _shape_lonlat(net, edge) -> list[list[float]]:
    return [
        [round(v, COORD_PRECISION) for v in net.convertXY2LonLat(x, y)]
        for x, y in edge.getShape()
    ]


def build_geojson(
    net,
    modelled: dict[str, float],
    matched: dict,
    *,
    min_veh_per_h: float = MIN_DRAWN_VEH_PER_H,
    window_hours: float = 3.0,
) -> dict:
    """One LineString per edge worth drawing, carrying both numbers.

    A matched edge is always included even if it carries little modelled
    traffic, because an edge NMDOT measured and the model left empty is
    precisely the interesting case -- dropping it for being quiet would hide
    the failure this map exists to show.
    """
    features = []

    for edge in net.getEdges():
        edge_id = edge.getID()
        if edge.isSpecial():
            continue

        volume = modelled.get(edge_id, 0.0)
        segment = matched.get(edge_id)
        # The threshold is a rate. A whole-day run reports daily totals, so
        # scale it up rather than drawing every driveway that saw one car.
        floor = min_veh_per_h * (window_hours if window_hours >= 24 else 1.0)
        if segment is None and volume < floor:
            continue

        properties = {
            "id": edge_id,
            "name": edge.getName() or "",
            "modelled": round(volume, 1),
            "limit_kmh": round(edge.getSpeed() * 3.6),
        }
        if segment is not None:
            observed, _ = segment.target(window_hours)
            properties.update({
                "observed": round(observed, 1),
                "route": segment.route_id,
                "aadt": segment.aadt,
                "aadt_year": segment.aadt_year,
                "ratio": round(volume / observed, 4) if observed > 0 else None,
            })

        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": _shape_lonlat(net, edge)},
            "properties": properties,
        })

    return {"type": "FeatureCollection", "features": features}


def build(window: tuple[int, int] = AM_PEAK, *, force: bool = False) -> tuple[Path, dict]:
    """Assemble the map data from whatever the pipeline has already produced."""
    from . import counts, simulate
    from .network import network_path

    path = geojson_path(window)
    summary_path = path.with_suffix(".summary.json")
    if path.exists() and summary_path.exists() and not force:
        return path, json.loads(summary_path.read_text())

    edgedata = simulate.edgedata_path(window)
    if not edgedata.exists():
        raise SystemExit(
            f"No simulation output at {edgedata}.\nRun:  otowi run"
        )

    import sumolib

    net = sumolib.net.readNet(str(network_path()))
    hours = window[1] - window[0]
    modelled = counts.simulated_hourly(edgedata, window_hours=hours)
    segments = counts.parse_segments(counts.fetch_aadt())
    matched = counts.match_to_edges(net, segments)
    comparison = counts.compare(matched, modelled, window_hours=hours)

    data = build_geojson(net, modelled, matched, window_hours=hours)

    summary = {
        "window": f"{window[0]:02d}:00-{window[1]:02d}:00",
        # The map's three layers are all on this basis; saying so on the page
        # is the difference between a number and a claim.
        "basis": comparison["basis"],
        "unit": comparison["unit"],
        "drawn_edges": len(data["features"]),
        "measured_links": comparison["all"]["links"],
        "held_out": comparison["held_out"],
        "fit": comparison["fit"],
        **simulate.summarize_tripinfo(
            simulate.tripinfo_path(window), simulate.routes_path(window)
        ),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")))
    summary_path.write_text(json.dumps(summary, indent=2))
    log.info("wrote %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
    return path, summary


#: Loaded lazily and kept, because reading a 70 MB network per request would
#: make the trip planner unusable. Keyed by window so switching windows is
#: still correct.
_PLANNER_CACHE: dict[tuple[int, int], tuple] = {}


def _planner(window: tuple[int, int]):
    """The network, travel times and routable core, built once per window."""
    if window in _PLANNER_CACHE:
        return _PLANNER_CACHE[window]

    import sumolib

    from . import journey, simulate, trips
    from .network import network_path

    intervals = simulate.intervals_path(window)
    if not intervals.exists():
        raise SystemExit(
            "No interval travel times yet. Re-run:  otowi simulate"
        )

    log.info("loading network and travel times for the trip planner")
    net = sumolib.net.readNet(str(network_path()))
    times = journey.TravelTimes.load(intervals, net)
    core = trips.reachable_core(net)

    _PLANNER_CACHE[window] = (net, times, core)
    return _PLANNER_CACHE[window]


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the page from the package and the data from the cache."""

    data_path: Path
    walk_path: Path
    summary: dict
    window: tuple[int, int]

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.startswith("/data.geojson"):
            return self._send_file(self.data_path, "application/json")
        if self.path.startswith("/places.json"):
            from .config import PLACES

            return self._send_json([
                {"key": key, "name": place.name, "note": place.note}
                for key, place in PLACES.items()
            ])
        if self.path.startswith("/plan.json"):
            return self._plan()
        if self.path.startswith("/summary.json"):
            body = json.dumps(self.summary).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if self.path.startswith("/walk.json"):
            return self._send_file(self.walk_path, "application/json")
        if self.path.rstrip("/") in ("/walk", "/walk.html"):
            return self._send_file(STATIC_DIR / "walk.html", "text/html; charset=utf-8")
        if self.path in ("/", "/index.html"):
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        self.send_error(404)
        return None

    def _plan(self):
        """Route one trip across every candidate departure time.

        The network, travel times and routable core are loaded once and cached
        on the class: reading a 70 MB network per request would make the button
        take twenty seconds, and none of it changes between requests.
        """
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        origin = (query.get("from") or [""])[0]
        destination = (query.get("to") or [""])[0]

        try:
            from . import journey

            net, times, core = _planner(self.window)
            result = journey.plan(
                net, times, core, origin, destination, window=self.window
            )
        except SystemExit as exc:
            return self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # pragma: no cover - surfaced to the page
            log.exception("plan failed")
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

        return self._send_json(result)

    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None

    def _send_file(self, path: Path, content_type: str):
        try:
            payload = path.read_bytes()
        except OSError:
            self.send_error(404)
            return None
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        return None

    def log_message(self, fmt, *args):  # keep the terminal quiet
        log.debug(fmt, *args)


def serve(
    window: tuple[int, int] = AM_PEAK,
    *,
    port: int = 8814,
    open_browser: bool = True,
    force: bool = False,
) -> None:
    """Build the data if needed, then serve the map on localhost."""
    data_path, summary = build(window, force=force)
    # The walking page needs no simulation, so a failure to build it must not
    # take the driving map down with it -- but it must be visible, because a
    # tab that silently 404s is worse than one that is not there.
    try:
        walk_data, _ = build_walk(force=force)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("walking page unavailable: %s", exc)
        walk_data = CACHE_DIR / "walk.json"

    handler = type("Handler", (_Handler,),
                   {"data_path": data_path, "walk_path": walk_data,
                    "summary": summary, "window": window})

    # Without this a restart inside the TIME_WAIT window fails with "Address
    # already in use", which for a tool you stop and start constantly is the
    # difference between usable and annoying.
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"otowi map on {url}   (ctrl-C to stop)")
        print(f"  walking and cycling in Santa Fe: {url}walk")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")


# ------------------------------------------------------------ on foot, on a bike
#
# A second bundle, for a second question. The main map answers "how does
# driving here go wrong"; this one answers "what happens to the people who are
# not in a car", and it needs different data at a different scale -- crash
# points rather than link volumes, and the width of a street rather than the
# length of a corridor. Kept separate so the driving map does not pay for it.


#: Santa Fe, tightly. The walking page is a city instrument: 614 of the 758
#: pedestrian and cyclist crashes in the study area are inside this box, and
#: including the other 144 would zoom the map out to a region in which none of
#: the streets being discussed are legible.
SANTA_FE_BBOX = (-106.12, 35.57, -105.85, 35.76)

#: Lighting, collapsed to what a person walking would actually distinguish.
#: "Dark-Lighted" and "Dark-Not Lighted" are kept apart deliberately: they are
#: the same darkness and a different public works budget.
LIGHT_CODES = {
    "daylight": 0, "dark-lighted": 1, "dark-not lighted": 2,
    "dusk": 3, "dawn": 3,
}


def _light_code(lighting: str) -> int:
    return LIGHT_CODES.get(lighting.strip().lower(), 4)


def _in_box(lon: float, lat: float, box) -> bool:
    west, south, east, north = box
    return west <= lon <= east and south <= lat <= north


#: Vertices closer together than this are dropped when the geometry is written
#: for the page. The inventory records a sidewalk run as a survey track -- one
#: point every few metres, 28 of them for an average run -- and at the zoom a
#: city map is read at, twenty-five of those 28 land on the same pixel. Thinning
#: takes the bundle from 2.7 MB to under 1 and changes nothing anyone can see.
THIN_M = 12.0


def _thin(path: list, step_m: float = THIN_M) -> list:
    """Drop vertices that are not far enough from the last one kept.

    The endpoints always survive, so a thinned run still starts and stops where
    the real one does -- which is what matters for a line whose *extent* is the
    information.
    """
    if len(path) <= 2:
        return [[round(lon, 5), round(lat, 5)] for lon, lat in path]

    from .walking import _metres

    kept = [path[0]]
    for lon, lat in path[1:-1]:
        last_lon, last_lat = kept[-1]
        if _metres(last_lon, last_lat, lon, lat) >= step_m:
            kept.append((lon, lat))
    kept.append(path[-1])
    return [[round(lon, 5), round(lat, 5)] for lon, lat in kept]


def _asset_lines(assets, box, kinds=None) -> list:
    """Asset geometry inside the box, thinned, as flat coordinate paths.

    Written as bare arrays rather than GeoJSON features: there are eight
    thousand crossings and a wrapper object each would be most of the file.
    """
    out = []
    for asset in assets:
        if kinds is not None and asset.subtype not in kinds:
            continue
        for path in asset.paths:
            if any(_in_box(lon, lat, box) for lon, lat in path):
                out.append(_thin(path))
    return out


def walk_path() -> Path:
    return CACHE_DIR / "walk.json"


def build_walk(*, box=SANTA_FE_BBOX, force: bool = False) -> tuple[Path, dict]:
    """The pedestrian and cyclist bundle: crashes, corridors, and what is built.

    Everything the walking page draws, in one file, with the arrays kept
    positional. Nothing here depends on the simulation -- these are
    measurements, and the page is useful before a single vehicle has been
    routed.
    """
    from . import tru, vru, walking

    path = walk_path()
    if path.exists() and not force:
        return path, json.loads(path.read_text())

    crashes = vru.fetch(force=force)
    corridors = vru.fetch_corridors(force=force)
    assets = walking.fetch_all(force=force)
    exposures = walking.assess(crashes, assets)

    here = [e for e in exposures if _in_box(e.crash.lon, e.crash.lat, box)]

    def hours_for(rows) -> list[int]:
        counts = [0] * 24
        for exposure in rows:
            if exposure.crash.hour is not None:
                counts[exposure.crash.hour] += 1
        return counts

    walkers = [e for e in here if e.crash.pedestrian]
    riders = [e for e in here if e.crash.pedalcycle]

    # Crashes by hour and by what the light was doing. This is the claim "it is
    # when the light goes" made checkable rather than asserted: the evening
    # hours should fill with dark and dusk while the count is still high.
    by_light = [[0, 0, 0, 0, 0] for _ in range(24)]
    for exposure in here:
        if exposure.crash.hour is not None:
            by_light[exposure.crash.hour][_light_code(exposure.crash.lighting)] += 1

    data = {
        "box": list(box),
        "years": [min(e.crash.year for e in here), max(e.crash.year for e in here)],
        # lon, lat, hour, severity, mode, light, year, street, metres to the
        # nearest marked crossing (-1 where nothing is inventoried near by).
        "crashes": [[
            round(e.crash.lon, 5), round(e.crash.lat, 5),
            e.crash.hour if e.crash.hour is not None else -1,
            e.crash.severity,
            0 if e.crash.pedestrian and not e.crash.pedalcycle else 1,
            _light_code(e.crash.lighting),
            e.crash.year,
            e.crash.street,
            round(e.crosswalk_m) if e.crosswalk_m < 1e6 else -1,
        ] for e in here],
        "corridors": [{
            "name": c.name,
            "index": round(c.severity_index, 1),
            "vru": c.vru_crashes,
            "ped_ka": c.ped_ka,
            "ksi": c.ksi,
            "aadt": c.aadt,
            "speed": c.speed_limit,
            "lanes": c.lanes,
            "miles": round(c.length_mi, 2),
            "paths": [p for p in c.paths
                      if any(_in_box(lon, lat, box) for lon, lat in p)],
        } for c in corridors
            if any(_in_box(lon, lat, box)
                   for path in c.paths for lon, lat in path)],
        "assets": {
            "crosswalk": _asset_lines(assets["crosswalk"], box),
            "bike_lane": _asset_lines(assets["bike_lane"], box),
            "sidewalk": _asset_lines(assets["sidewalk"], box),
        },
        "hours": {
            "pedestrian": hours_for(walkers),
            "cyclist": hours_for(riders),
            "pedestrian_ksi": hours_for(
                [e for e in walkers if e.crash.killed_or_serious]),
            "cyclist_ksi": hours_for(
                [e for e in riders if e.crash.killed_or_serious]),
            "by_light": by_light,
        },
        "crossings": walking.crossings_and_crashes(
            exposures, corridors, assets["crosswalk"]),
        "by_corridor": walking.by_corridor(here, corridors)[:12],
        "infrastructure": walking.summarise(here),
        "summary": {
            "crashes": len(here),
            "pedestrians": sum(1 for e in here if e.crash.pedestrian),
            "cyclists": sum(1 for e in here if e.crash.pedalcycle),
            "killed": sum(1 for e in here if e.crash.severity == "K"),
            "killed_or_serious": sum(1 for e in here if e.crash.killed_or_serious),
            "after_dark": sum(1 for e in here if e.crash.is_dark),
            "killed_after_dark": sum(1 for e in here
                                     if e.crash.severity == "K" and e.crash.is_dark),
            "of_study_area": len(exposures),
        },
    }

    # The town's own vehicle-crash curve, so the page can say that the hour
    # pedestrians are hit is not the hour drivers crash. Optional: the walking
    # page must still build if the TRU reports cannot be read.
    try:
        hourly, _ = tru.fetch()
        towns = tru.names_for(("santa_fe_city",))
        data["town_crashes_by_hour"] = tru.profile(hourly, "all", places=towns)
    except Exception as exc:                                    # noqa: BLE001
        log.warning("TRU curve unavailable for the walking page: %s", exc)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")))
    log.info("wrote %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
    return path, data
