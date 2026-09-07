"""Routing the trips, running the simulation, and reporting what came out.

Two SUMO programs do the work and they answer different questions.

``duarouter`` turns each origin-destination-time trip into a path through the
network. It does this on free-flow travel times, which means every driver is
routed as though the road were empty. That is a real limitation and it is the
standard first pass: it produces the routes people would choose if there were
no congestion, and then the simulation shows what happens when all of them try
it at once. Iterating routing against the resulting congestion is what
``duaIterate`` does, and it is a later step here, not this one.

``sumo`` runs the microscopic simulation: every vehicle, every second,
car-following and lane-changing on the real geometry. The outputs worth having
are not the animation but the aggregate measures -- how long trips took, where
vehicles waited, and what crossed each edge.

Nothing here is calibrated. The travel times this produces are the model's
opinion, and until :mod:`otowi.counts` compares edge volumes against the MPO
and NMDOT count stations, that opinion has no error bar. The summary says so.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from xml.etree import ElementTree as etree

from .config import AM_PEAK, CACHE_DIR
from .network import find_tool, network_path

log = logging.getLogger(__name__)

#: Vehicles that cannot reach their destination are removed rather than
#: teleported. SUMO's default teleporting hides exactly the gridlock this
#: project exists to measure: a jammed vehicle vanishes, the jam clears, and
#: the model reports a travel time nobody experienced.
NO_TELEPORT = "-1"


def routes_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"routes-am-{window[0]:02d}{window[1]:02d}.rou.xml"


def tripinfo_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"tripinfo-am-{window[0]:02d}{window[1]:02d}.xml"


def edgedata_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"edgedata-am-{window[0]:02d}{window[1]:02d}.xml"


def build_routes(
    trips_file: Path,
    *,
    window: tuple[int, int] = AM_PEAK,
    force: bool = False,
) -> Path:
    """Run duarouter over the trips file."""
    out = routes_path(window)
    if out.exists() and not force:
        return out

    tool = find_tool("duarouter")
    if tool is None:
        raise RuntimeError("duarouter not found. pip install -r requirements.txt")

    cmd = [
        tool,
        "--net-file", str(network_path()),
        "--route-files", str(trips_file),
        "--output-file", str(out),
        # A trip whose endpoints cannot be connected is a data problem, not a
        # reason to abort 28,000 others. They are counted in the summary.
        "--ignore-errors",
        "--no-warnings",
        # Trips are already sorted by departure; saying so avoids duarouter
        # buffering the whole set to sort it again.
        "--unsorted-input", "false",
        "--routing-threads", "4",
    ]
    log.info("routing %s", trips_file.name)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    lost = routing_loss(trips_file, out)
    if lost["dropped"]:
        log.warning(
            "duarouter dropped %d of %d trips (%.1f%%) as unroutable. "
            "This is silent by design -- --ignore-errors turns an unroutable "
            "trip into a warning nobody reads -- and it shows up downstream as "
            "vehicles that never arrive, which reads as congestion. Check that "
            "trip endpoints are restricted to the routable core.",
            lost["dropped"], lost["trips"], 100 * lost["dropped_fraction"],
        )
    return out


def assign_iteratively(
    trips_file: Path,
    *,
    window: tuple[int, int] = AM_PEAK,
    iterations: int = 5,
    end_padding_s: int = 10800,
) -> tuple[Path, list[dict]]:
    """Route, simulate, re-route against the resulting congestion, repeat.

    Single-pass routing on free-flow times gives every driver the path that
    would be fastest on an empty road. They all choose the same one, it fills
    up, and the model produces a jam that real drivers avoid by spreading
    across alternatives. The symptom is severe: with the full demand, 45% of
    vehicles failed to finish, and adding traffic barely raised the volume the
    model carried because throughput had collapsed.

    The standard remedy is an iterative assignment approaching a user
    equilibrium -- the state where no driver can improve their own journey by
    switching route. Each round:

    1. route the trips using the previous round's *measured* edge travel times,
    2. simulate,
    3. keep the travel times that came out, and go again.

    Convergence is measured by how much total travel time moves between
    rounds. It is reported rather than assumed, because an assignment that has
    not converged is not an equilibrium and its travel times mean little.

    This is deliberately a simple fixed-point iteration rather than SUMO's
    ``duaIterate.py``. That script does the same thing with more options, but
    it wants to own the whole directory layout and its interface is a moving
    target across versions; the loop itself is twenty lines and doing it here
    keeps the file naming and the convergence report under our control.
    """
    duarouter = find_tool("duarouter")
    if duarouter is None:
        raise RuntimeError("duarouter not found. pip install -e '.[fast,dev]'")

    history: list[dict] = []
    weights: Path | None = None
    routes = routes_path(window)

    for step in range(iterations):
        cmd = [
            duarouter,
            "--net-file", str(network_path()),
            "--route-files", str(trips_file),
            "--output-file", str(routes),
            "--ignore-errors", "--no-warnings",
            "--routing-threads", "4",
        ]
        if weights is not None:
            # Route on what the last simulation actually measured.
            cmd += ["--weight-files", str(weights),
                    "--weight-attribute", "traveltime"]
        log.info("assignment round %d/%d: routing", step + 1, iterations)
        subprocess.run(cmd, check=True, capture_output=True, text=True)

        log.info("assignment round %d/%d: simulating", step + 1, iterations)
        outputs = run(routes, window=window, end_padding_s=end_padding_s)
        summary = summarize_tripinfo(outputs["tripinfo"], routes)
        weights = outputs["intervals"]

        total = summary.get("mean_duration_s", 0.0) * summary.get("vehicles_arrived", 0)
        if history:
            previous = history[-1]["total_travel_time_s"]
            change = abs(total - previous) / previous if previous else 1.0
        else:
            change = 1.0

        record = {
            "round": step + 1,
            "arrived": summary.get("vehicles_arrived", 0),
            "unfinished_fraction": summary.get("unfinished_fraction"),
            "mean_duration_s": summary.get("mean_duration_s"),
            "mean_time_loss_s": summary.get("mean_time_loss_s"),
            "total_travel_time_s": total,
            "relative_change": round(change, 4),
        }
        history.append(record)
        log.info(
            "round %d: %d arrived, %.1f%% unfinished, mean %.0f s, change %.1f%%",
            step + 1, record["arrived"],
            100 * (record["unfinished_fraction"] or 0),
            record["mean_duration_s"] or 0, 100 * change,
        )

    return routes, history


def routing_loss(trips_file: Path, routes_file: Path) -> dict:
    """How many trips duarouter silently discarded.

    Worth its own function because the failure it detects is invisible: SUMO
    reports success, the route file is valid, and the only symptom is a
    vehicle count that nobody compares against the input. A 30% loss here once
    presented itself as severe congestion that did not exist.
    """
    def count(path: Path, tag: str) -> int:
        total = 0
        for _, element in etree.iterparse(str(path), events=("end",)):
            if element.tag == tag:
                total += 1
            element.clear()
        return total

    trips_in = count(trips_file, "trip")
    routes_out = count(routes_file, "vehicle")
    dropped = max(0, trips_in - routes_out)
    return {
        "trips": trips_in,
        "routed": routes_out,
        "dropped": dropped,
        "dropped_fraction": round(dropped / trips_in, 4) if trips_in else 0.0,
    }


#: Length of each reporting interval, in seconds. Fifteen minutes is short
#: enough that the build and clearance of the peak are visible -- which is the
#: whole question when someone asks what time to leave -- and long enough that
#: an edge carrying a handful of vehicles still has a stable mean speed.
INTERVAL_S = 900


def intervals_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"intervals-am-{window[0]:02d}{window[1]:02d}.xml"


def _write_edgedata_config(path: Path, out_file: Path, window: tuple[int, int]) -> Path:
    """An additional-file asking SUMO for per-edge aggregates.

    Two collectors, because two different questions are being asked.

    The first is one interval spanning the window, which is what the
    count-station comparison wants: a single volume per edge to set beside a
    single measured volume.

    The second is fifteen-minute intervals, which is what "when should I
    leave?" wants. A whole-window average cannot answer it -- averaging 06:00
    and 08:00 together produces a road that is moderately busy all morning and
    never actually congested, so every departure time looks equally good and
    the model has nothing to say.
    """
    duration = (window[1] - window[0]) * 3600
    root = etree.Element("additional")
    etree.SubElement(
        root, "edgeData",
        id="edges",
        file=str(out_file),
        begin="0",
        end=str(duration),
        excludeEmpty="true",
    )
    etree.SubElement(
        root, "edgeData",
        id="intervals",
        file=str(intervals_path(window)),
        begin="0",
        end=str(duration),
        period=str(INTERVAL_S),
        excludeEmpty="true",
    )
    etree.ElementTree(root).write(str(path), encoding="utf-8", xml_declaration=True)
    return path


def run(
    routes: Path,
    *,
    window: tuple[int, int] = AM_PEAK,
    end_padding_s: int = 10800,
    step_length: float = 1.0,
) -> dict[str, Path]:
    """Run the microscopic simulation and collect its outputs.

    ``end_padding_s`` keeps the clock running after the last departure so
    vehicles still travelling are not cut off mid-trip; truncating them would
    remove the longest journeys from every average, which are exactly the ones
    congestion produces.

    The default is three hours rather than one because one was not enough: a
    run with 60 minutes of padding finished with 28% of vehicles still on the
    network, and their absence from the averages biased every reported travel
    time *downward* -- the model looked faster precisely where it was most
    congested. :func:`summarize_tripinfo` now reports the unfinished count so
    the same mistake is visible rather than inferred.
    """
    tool = find_tool("sumo")
    if tool is None:
        raise RuntimeError("sumo not found. pip install -r requirements.txt")

    tripinfo = tripinfo_path(window)
    edgedata = edgedata_path(window)
    additional = _write_edgedata_config(
        CACHE_DIR / "edgedata.add.xml", edgedata, window
    )

    duration = (window[1] - window[0]) * 3600 + end_padding_s
    cmd = [
        tool,
        "--net-file", str(network_path()),
        "--route-files", str(routes),
        "--additional-files", str(additional),
        "--tripinfo-output", str(tripinfo),
        "--begin", "0",
        "--end", str(duration),
        "--step-length", str(step_length),
        "--time-to-teleport", NO_TELEPORT,
        "--no-step-log",
        "--no-warnings",
        "--ignore-route-errors",
    ]
    log.info("simulating %d s of the %02d:00-%02d:00 window", duration, *window)
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return {"tripinfo": tripinfo, "edgedata": edgedata,
            "intervals": intervals_path(window)}


def summarize_tripinfo(path: Path, routes: Path | None = None) -> dict:
    """Aggregate travel times out of SUMO's per-vehicle output.

    ``timeLoss`` is the measure that matters: seconds lost relative to
    travelling the same route unobstructed at the speed limit. Total duration
    conflates a long trip with a delayed one, and this corridor's question is
    about delay.
    """
    durations: list[float] = []
    time_losses: list[float] = []
    waiting: list[float] = []

    for _, element in etree.iterparse(str(path), events=("end",)):
        if element.tag != "tripinfo":
            continue
        durations.append(float(element.get("duration", 0.0)))
        time_losses.append(float(element.get("timeLoss", 0.0)))
        waiting.append(float(element.get("waitingTime", 0.0)))
        element.clear()

    if not durations:
        return {"vehicles_arrived": 0}

    def percentile(values: list[float], fraction: float) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]

    summary = {
        "vehicles_arrived": len(durations),
        "mean_duration_s": round(sum(durations) / len(durations), 1),
        "median_duration_s": round(percentile(durations, 0.5), 1),
        "p90_duration_s": round(percentile(durations, 0.9), 1),
        "mean_time_loss_s": round(sum(time_losses) / len(time_losses), 1),
        "p90_time_loss_s": round(percentile(time_losses, 0.9), 1),
        "mean_waiting_s": round(sum(waiting) / len(waiting), 1),
    }

    # Vehicles still travelling when the clock stopped never appear in
    # tripinfo, so every average above is computed only over trips that
    # finished. That biases travel time *downward* exactly where congestion is
    # worst, which is the opposite of a conservative error, so the count is
    # reported rather than left to be inferred from a missing row.
    if routes is not None and routes.exists():
        loaded = 0
        for _, element in etree.iterparse(str(routes), events=("end",)):
            if element.tag == "vehicle":
                loaded += 1
            element.clear()
        unfinished = max(0, loaded - len(durations))
        summary["vehicles_loaded"] = loaded
        summary["vehicles_unfinished"] = unfinished
        summary["unfinished_fraction"] = round(unfinished / loaded, 4) if loaded else 0.0
        if unfinished:
            log.warning(
                "%d of %d vehicles (%.1f%%) had not arrived when the simulation "
                "ended. They are absent from every average above. Raise "
                "--end-padding or check for gridlock.",
                unfinished, loaded, 100 * unfinished / loaded,
            )

    return summary


def busiest_edges(path: Path, net, top: int = 15) -> list[dict]:
    """The edges carrying the most vehicles, named where OSM named them."""
    rows: list[dict] = []
    for _, element in etree.iterparse(str(path), events=("end",)):
        if element.tag != "edge":
            continue
        entered = element.get("entered")
        if entered is not None:
            rows.append(
                {
                    "edge": element.get("id"),
                    "vehicles": int(entered),
                    "speed_ms": float(element.get("speed", 0.0)),
                }
            )
        element.clear()

    rows.sort(key=lambda row: -row["vehicles"])
    named = []
    for row in rows[:top]:
        try:
            edge = net.getEdge(row["edge"])
            row["name"] = edge.getName() or "(unnamed)"
            row["limit_ms"] = round(edge.getSpeed(), 1)
        except KeyError:
            row["name"] = "(unknown)"
        named.append(row)
    return named
