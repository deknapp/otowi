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
        if segment is None and volume < min_veh_per_h:
            continue

        properties = {
            "id": edge_id,
            "name": edge.getName() or "",
            "modelled": round(volume, 1),
            "limit_kmh": round(edge.getSpeed() * 3.6),
        }
        if segment is not None:
            observed = segment.peak_hour_directional
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
    modelled = counts.simulated_hourly(edgedata, window_hours=window[1] - window[0])
    segments = counts.parse_segments(counts.fetch_aadt())
    matched = counts.match_to_edges(net, segments)
    comparison = counts.compare(matched, modelled)

    data = build_geojson(net, modelled, matched)

    summary = {
        "window": f"{window[0]:02d}:00-{window[1]:02d}:00",
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


class _Handler(http.server.SimpleHTTPRequestHandler):
    """Serves the page from the package and the data from the cache."""

    data_path: Path
    summary: dict

    def do_GET(self):  # noqa: N802 - stdlib naming
        if self.path.startswith("/data.geojson"):
            return self._send_file(self.data_path, "application/json")
        if self.path.startswith("/summary.json"):
            body = json.dumps(self.summary).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return None
        if self.path in ("/", "/index.html"):
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        self.send_error(404)
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

    handler = type("Handler", (_Handler,), {"data_path": data_path, "summary": summary})

    # Without this a restart inside the TIME_WAIT window fails with "Address
    # already in use", which for a tool you stop and start constantly is the
    # difference between usable and annoying.
    socketserver.TCPServer.allow_reuse_address = True

    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/"
        print(f"otowi map on {url}   (ctrl-C to stop)")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
