"""Routing the trips, running the simulation, and reporting what came out.

Two SUMO programs do the work and they answer different questions.

``duarouter`` turns each origin-destination-time trip into a path through the
network. It does this on free-flow travel times, which means every driver is
routed as though the road were empty. That is a real limitation and it is the
standard first pass: it produces the routes people would choose if there were
no congestion, and then the simulation shows what happens when all of them try
it at once. Iterating routing against the resulting congestion, so that drivers
spread across alternatives the way real ones do, is
:func:`assign_iteratively` -- and getting that iteration to converge rather
than oscillate is the subtle part. Its docstring is where that story lives.

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
import zlib
from pathlib import Path
from xml.etree import ElementTree as etree

from .config import AM_PEAK, CACHE_DIR
from .network import find_tool, network_path

log = logging.getLogger(__name__)

#: Seconds a vehicle may be stuck before SUMO moves it past the blockage.
#:
#: This was ``-1`` -- teleporting disabled entirely -- on the reasoning that
#: teleporting hides the gridlock this project exists to measure. That concern
#: was right and the fix was wrong, and it cost a lot of wrong conclusions.
#:
#: With teleporting off, a vehicle that cannot move never recovers. Jams became
#: permanent, cascaded into the junctions feeding them, and eventually blocked
#: insertion so far upstream that vehicles queued two hours to get onto the
#: network at all. Measured: 45.9% of vehicles failed to finish with ``-1``,
#: against 23.1% at the value below. Nearly half the shortfall was an artifact
#: of the setting.
#:
#: The honest treatment is to let SUMO resolve deadlock the way real traffic
#: does, and then *report the teleports*, because a jam teleport is precisely
#: the gridlock signal worth having. :func:`summarize_tripinfo` surfaces the
#: count rather than letting it disappear into the log.
TELEPORT_S = "300"


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


#: Gawron's route-choice parameters, at SUMO's defaults.
#:
#: ``beta`` is the share of a driver's route probability that may move in one
#: round, and it is the damping: at 0.3 a route losing badly still keeps most
#: of its traffic this round and sheds it over the next few. Raising it makes
#: the assignment converge faster right up until it does not converge at all.
GAWRON_BETA = "0.3"
GAWRON_A = "0.05"

#: How many paths a driver keeps in play. Beyond about five the extra
#: alternatives are minor variations that never carry traffic, and they cost
#: memory on every round for 54,000 vehicles.
MAX_ALTERNATIVES = "5"


def alternatives_path(window: tuple[int, int] = AM_PEAK) -> Path:
    """Route *alternatives*, carried from one assignment round to the next.

    Distinct from the routes file, and the distinction is the entire mechanism
    described in :func:`assign_iteratively`: the routes file says which path
    each driver takes, the alternatives file says which paths each driver is
    choosing *between* and with what probability. Iterating on the second is
    what stops the assignment flipping.
    """
    return CACHE_DIR / f"alts-am-{window[0]:02d}{window[1]:02d}.rou.alt.xml"


def _route_distributions(alts_file: Path) -> dict[str, dict[int, float]]:
    """Each vehicle's probability over its alternative paths.

    Keyed by a CRC32 of the edge list rather than the list itself: at 54,000
    vehicles with up to five alternatives apiece, holding the paths twice to
    compare two rounds is hundreds of megabytes and nothing here needs to read
    them, only to tell one from another.
    """
    dists: dict[str, dict[int, float]] = {}
    current: str | None = None
    for event, element in etree.iterparse(str(alts_file), events=("start", "end")):
        if event == "start":
            if element.tag == "vehicle":
                current = element.get("id")
                dists[current] = {}
            continue
        if element.tag == "route" and current is not None:
            key = zlib.crc32((element.get("edges") or "").encode())
            dists[current][key] = float(element.get("probability") or 0.0)
        elif element.tag == "vehicle":
            current = None
            element.clear()
    return dists


def _route_shift(previous: dict[str, dict[int, float]],
                 current: dict[str, dict[int, float]]) -> float | None:
    """How far route choice moved between rounds, in [0, 1].

    Total-variation distance between each vehicle's probability distribution
    over its paths, averaged across vehicles. Zero means nobody's route choice
    moved at all, which is the equilibrium being iterated toward.

    **Measure the distribution, not the route that got written.** The obvious
    version of this metric compares the chosen path in each round's route file
    and counts how many drivers changed, and it is wrong in a way that looks
    convincing. duarouter *samples* the route it writes out of the
    distribution, so a driver holding four alternatives at 0.25 apiece has a
    75% chance of appearing to "change route" every round no matter how
    completely the assignment has settled. Measured on a tenth of the demand,
    that version reported 37.8%, then 45.3%, then 49.1% -- rising steadily
    toward the coin-flip it actually was -- across exactly the rounds where
    mean time loss fell 583 s to 290 to 248 to 246 and jam teleports fell from
    138 to 1. It would have reported divergence at the moment of convergence.

    The distributions are what Gawron updates, so they are what settles.
    """
    if not previous:
        return None
    shared = [v for v in current if v in previous]
    if not shared:
        return None
    total = 0.0
    for v in shared:
        before, after = previous[v], current[v]
        keys = set(before) | set(after)
        total += 0.5 * sum(abs(after.get(k, 0.0) - before.get(k, 0.0)) for k in keys)
    return total / len(shared)


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

    The remedy is an iterative assignment approaching a user equilibrium -- the
    state where no driver can improve their own journey by switching route.

    **The first attempt at this oscillated, and the reason is worth keeping.**
    Each round re-ran duarouter over the raw trips with the last round's
    measured travel times and replaced every driver's route with the new
    shortest path. That is all-or-nothing reassignment, and it cannot converge:
    the whole population moves onto whatever was fast last round, which jams
    it, so next round the whole population moves back. Measured over three
    rounds the unfinished fraction went 23.1%, then 11.1%, then 34.9% -- not
    noise around a converging value, a flip. Reporting a "relative change" over
    that and calling a small number convergence would have been reporting the
    moment the swing crossed zero.

    What fixes it is moving *some* traffic rather than all of it. Each driver
    keeps a set of alternative paths with probabilities, and after each
    simulation those probabilities shift toward the routes that turned out
    faster -- by a bounded fraction, set by :data:`GAWRON_BETA`. A route that
    is losing sheds traffic over several rounds instead of emptying at once, so
    the flow that arrives on the alternative is small enough not to jam it in
    turn. This is Gawron's method, it is what ``duaIterate.py`` uses, and
    duarouter implements it directly given an alternatives file to iterate on.

    So the loop is:

    1. route on the previous round's *measured* edge travel times, shifting
       route probabilities rather than replacing routes,
    2. simulate,
    3. keep the travel times that came out, and go again.

    Convergence is reported as **route shift**: how far drivers' probability
    distributions over their paths moved since the last round, averaged across
    drivers. Falling shift is an assignment settling; shift that stays high is
    one that has not, and its travel times should not be believed. See
    :func:`_route_shift` -- both for why this replaced the travel-time
    comparison that was here before, and for the more subtle metric that also
    had to be thrown away.

    This stays a hand-rolled loop rather than a call to ``duaIterate.py``. That
    script does the same thing with more options, but it wants to own the whole
    directory layout and its interface moves between versions; the loop is
    forty lines and keeping it here keeps the file naming and the convergence
    report under our control.
    """
    duarouter = find_tool("duarouter")
    if duarouter is None:
        raise RuntimeError("duarouter not found. pip install -e '.[fast,dev]'")

    history: list[dict] = []
    weights: Path | None = None
    routes = routes_path(window)
    alts = alternatives_path(window)
    # duarouter streams its input, so it cannot read and write the same
    # alternatives file in one pass. Write beside it and swap.
    alts_next = alts.with_suffix(".next.xml")
    previous_dists: dict[str, dict[int, float]] = {}

    for step in range(iterations):
        first = step == 0
        cmd = [
            duarouter,
            "--net-file", str(network_path()),
            # Round one starts from the trips. Every round after starts from
            # the alternatives, which is what carries each driver's route
            # probabilities forward -- restarting from the trips each time is
            # exactly the all-or-nothing behaviour this replaced.
            "--route-files", str(trips_file if first else alts),
            "--output-file", str(routes),
            "--alternatives-output", str(alts_next),
            "--ignore-errors", "--no-warnings",
            "--routing-threads", "4",
        ]
        if weights is not None:
            cmd += [
                "--weight-files", str(weights),
                "--weight-attribute", "traveltime",
                # The measured intervals end when the last vehicle departs,
                # but vehicles departing near the end are still driving for an
                # hour afterwards. Without this their second half is routed
                # over free-flow roads.
                "--weights.expand",
                "--route-choice-method", "gawron",
                "--gawron.beta", GAWRON_BETA,
                "--gawron.a", GAWRON_A,
                "--max-alternatives", MAX_ALTERNATIVES,
            ]
        log.info("assignment round %d/%d: routing", step + 1, iterations)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        alts_next.replace(alts)

        distributions = _route_distributions(alts)
        shift = _route_shift(previous_dists, distributions)
        previous_dists = distributions

        log.info("assignment round %d/%d: simulating", step + 1, iterations)
        outputs = run(routes, window=window, end_padding_s=end_padding_s)
        summary = summarize_tripinfo(outputs["tripinfo"], routes,
                                     outputs.get("statistics"))
        weights = outputs["intervals"]

        record = {
            "round": step + 1,
            "arrived": summary.get("vehicles_arrived", 0),
            "unfinished_fraction": summary.get("unfinished_fraction"),
            "mean_duration_s": summary.get("mean_duration_s"),
            "mean_time_loss_s": summary.get("mean_time_loss_s"),
            "teleports_jam": summary.get("teleports_jam"),
            # None on the first round: there is nothing to have moved from.
            "route_shift": None if shift is None else round(shift, 4),
            "mean_time_loss_change": (
                None if not history or not history[-1]["mean_time_loss_s"]
                else round(abs((summary.get("mean_time_loss_s") or 0)
                               - history[-1]["mean_time_loss_s"])
                           / history[-1]["mean_time_loss_s"], 4)
            ),
        }
        history.append(record)
        log.info(
            "round %d: %d arrived, %.1f%% unfinished, mean %.0f s, "
            "time loss %.0f s, route shift %s",
            step + 1, record["arrived"],
            100 * (record["unfinished_fraction"] or 0),
            record["mean_duration_s"] or 0,
            record["mean_time_loss_s"] or 0,
            "n/a" if shift is None else f"{100 * shift:.1f}%",
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


def stats_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"stats-am-{window[0]:02d}{window[1]:02d}.xml"


def read_teleports(path: Path) -> dict:
    """Teleport counts from SUMO's statistics file.

    A teleport is SUMO moving a vehicle past a blockage it could not clear.
    ``jam`` is the one that matters: it means the network genuinely deadlocked
    there and had to be rescued. A model with many jam teleports is telling you
    it is over-saturated, and that is a finding rather than a nuisance -- so
    these are reported next to the travel times rather than left in a file
    nobody opens.
    """
    if not path.exists():
        return {}
    try:
        root = etree.parse(str(path)).getroot()
    except etree.ParseError:
        return {}

    result: dict = {}
    vehicles = root.find("vehicles")
    if vehicles is not None:
        result["loaded"] = int(vehicles.get("loaded", 0))
        result["inserted"] = int(vehicles.get("inserted", 0))
        result["running_at_end"] = int(vehicles.get("running", 0))
        result["never_inserted"] = int(vehicles.get("waiting", 0))

    teleports = root.find("teleports")
    if teleports is not None:
        result["teleports_total"] = int(teleports.get("total", 0))
        result["teleports_jam"] = int(teleports.get("jam", 0))
        result["teleports_yield"] = int(teleports.get("yield", 0))
        result["teleports_wrong_lane"] = int(teleports.get("wrongLane", 0))
    return result


def _write_edgedata_config(path: Path, out_file: Path, window: tuple[int, int],
                           end_padding_s: int = 0) -> Path:
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

    The interval collector runs past the last departure by ``end_padding_s``,
    and the whole-window one does not. That asymmetry is deliberate. The
    whole-window figure exists to sit beside a count station's three-hour
    total, so it must cover the same three hours and no more. The intervals
    feed the next round of :func:`assign_iteratively`, and a driver leaving at
    08:59 is still on the road at 09:40; cutting the measurement at 09:00 makes
    the tail of the peak invisible to routing, which then sends the next
    round's traffic into it.
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
        end=str(duration + end_padding_s),
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
    statistics = stats_path(window)
    additional = _write_edgedata_config(
        CACHE_DIR / "edgedata.add.xml", edgedata, window,
        end_padding_s=end_padding_s,
    )

    duration = (window[1] - window[0]) * 3600 + end_padding_s
    cmd = [
        tool,
        "--net-file", str(network_path()),
        "--route-files", str(routes),
        "--additional-files", str(additional),
        "--tripinfo-output", str(tripinfo),
        # Teleports live only here. Without this file the single most useful
        # gridlock diagnostic the simulation produces is thrown away.
        "--statistic-output", str(statistics),
        "--begin", "0",
        "--end", str(duration),
        "--step-length", str(step_length),
        "--time-to-teleport", TELEPORT_S,
        "--no-step-log",
        "--no-warnings",
        "--ignore-route-errors",
    ]
    log.info("simulating %d s of the %02d:00-%02d:00 window", duration, *window)
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return {"tripinfo": tripinfo, "edgedata": edgedata,
            "intervals": intervals_path(window), "statistics": statistics}


def summarize_tripinfo(path: Path, routes: Path | None = None,
                       statistics: Path | None = None) -> dict:
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

    if statistics is not None:
        teleports = read_teleports(statistics)
        summary.update(teleports)
        jam = teleports.get("teleports_jam", 0)
        if jam:
            log.warning(
                "%d jam teleports: the network deadlocked that many times and "
                "SUMO had to rescue a vehicle. This is the gridlock signal, not "
                "a nuisance -- a model needing many of them is over-saturated.",
                jam,
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
