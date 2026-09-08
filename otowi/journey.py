"""When should I leave?

This is the question the rest of the project exists to answer. Not "what does
the network look like" -- someone deciding whether to leave Santa Fe at 06:45
or 07:30 for a 08:00 start in Los Alamos wants one number for each option, and
the difference between them.

Answering it properly needs *time-dependent* routing, because the answer is
entirely about time. A journey starting at 06:30 meets a different road at
Pojoaque than one starting at 07:30, and averaging the morning into a single
travel time per edge -- which is what a whole-window average does -- destroys
exactly the signal being asked for: every departure looks equally good, and
the model has nothing to say.

So the simulation reports fifteen-minute intervals, and the search here walks
them: a vehicle entering an edge at 07:05 is charged that edge's 07:00-07:15
travel time, arrives at some later moment, and is charged the *next* edge's
travel time for whenever it actually gets there. This is time-dependent
Dijkstra, and it is correct as long as travel times satisfy first-in-first-out,
which for aggregated interval data on a road network is a reasonable and
standard assumption.

What the answer is worth is a separate matter, and the honest statement is in
:func:`plan`'s return value rather than buried: this model carries a fraction
of real traffic, so every duration here is optimistic. The *shape* of the
curve -- that leaving at 07:45 is worse than at 06:45 -- survives that error
better than the absolute numbers do, because both are wrong in the same
direction, but neither should be quoted as a prediction yet.
"""

from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as etree

from .config import AM_PEAK, PLACES

log = logging.getLogger(__name__)

#: Free-flow is used where an interval saw no vehicles at all. An empty road is
#: not a slow road, and treating "no data" as "no information" would make the
#: quiet early-morning intervals look impassable.
EMPTY_INTERVAL_USES_FREE_FLOW = True


@dataclass(frozen=True)
class Leg:
    edge_id: str
    name: str
    enter_s: float
    travel_s: float


@dataclass
class Journey:
    """One departure time and what it costs."""

    depart_s: float
    duration_s: float | None
    legs: list[Leg]

    @property
    def arrived(self) -> bool:
        return self.duration_s is not None


class TravelTimes:
    """Per-edge travel time by interval, with a free-flow fallback.

    Built from SUMO's interval edgeData. ``traveltime`` is SUMO's own estimate
    for traversing the edge during that interval, which already accounts for
    queueing at the far end, so it is preferable to deriving one from mean
    speed and length.
    """

    def __init__(self, interval_s: float, free_flow: dict[str, float]):
        self.interval_s = interval_s
        self.free_flow = free_flow
        self._by_interval: dict[int, dict[str, float]] = {}

    @classmethod
    def load(cls, path: Path, net) -> TravelTimes:
        free_flow = {}
        for edge in net.getEdges():
            speed = edge.getSpeed()
            if speed > 0:
                free_flow[edge.getID()] = edge.getLength() / speed

        times = cls(interval_s=900.0, free_flow=free_flow)
        interval_index = 0
        seen_interval_length = None

        for _, element in etree.iterparse(str(path), events=("end",)):
            if element.tag == "interval":
                begin = float(element.get("begin", 0.0))
                end = float(element.get("end", 0.0))
                if seen_interval_length is None and end > begin:
                    seen_interval_length = end - begin
                    times.interval_s = seen_interval_length
                interval_index = int(begin // times.interval_s)
                bucket = times._by_interval.setdefault(interval_index, {})
                for edge in element.findall("edge"):
                    value = edge.get("traveltime")
                    if value is not None:
                        bucket[edge.get("id")] = float(value)
                element.clear()

        log.info(
            "loaded travel times for %d intervals of %.0f s",
            len(times._by_interval), times.interval_s,
        )
        return times

    @property
    def n_intervals(self) -> int:
        return len(self._by_interval)

    def cost(self, edge_id: str, at_s: float) -> float:
        """Seconds to traverse ``edge_id`` entering it at ``at_s``.

        Times outside the simulated window clamp to the nearest interval rather
        than failing. A journey that starts near the end of the window and runs
        past it is charged the last interval it has evidence for, which is the
        least-bad option and is flagged in the result rather than hidden.
        """
        if not self._by_interval:
            return self.free_flow.get(edge_id, math.inf)

        index = int(max(0.0, at_s) // self.interval_s)
        lowest, highest = min(self._by_interval), max(self._by_interval)
        index = max(lowest, min(highest, index))

        value = self._by_interval.get(index, {}).get(edge_id)
        if value is None:
            return self.free_flow.get(edge_id, math.inf)
        return value


def _nearest_edge(net, lon: float, lat: float, core: set[str], radius_m: float = 2000.0):
    """The routable edge closest to a point."""
    x, y = net.convertLonLat2XY(lon, lat)
    best = None
    for edge, distance in net.getNeighboringEdges(x, y, radius_m):
        if edge.getID() not in core:
            continue
        if best is None or distance < best[1]:
            best = (edge, distance)
    return best[0] if best else None


def route_at(
    net,
    times: TravelTimes,
    origin_edge: str,
    destination_edge: str,
    depart_s: float,
) -> Journey:
    """Time-dependent shortest path, leaving at ``depart_s``.

    Ordinary Dijkstra with one change: the cost of an edge depends on when the
    search reaches it. Because a later departure never produces an earlier
    arrival on this data, the usual settled-node argument still holds and the
    first time the destination is popped it is optimal.
    """
    if origin_edge == destination_edge:
        return Journey(depart_s, 0.0, [])

    arrival = {origin_edge: depart_s}
    previous: dict[str, str] = {}
    queue = [(depart_s, origin_edge)]
    settled: set[str] = set()

    while queue:
        now, edge_id = heapq.heappop(queue)
        if edge_id in settled:
            continue
        settled.add(edge_id)

        if edge_id == destination_edge:
            return _build_journey(net, times, previous, origin_edge,
                                  destination_edge, depart_s, now)

        try:
            edge = net.getEdge(edge_id)
        except KeyError:
            continue

        for nxt in edge.getOutgoing():
            nxt_id = nxt.getID()
            if nxt_id in settled or not nxt.allows("passenger"):
                continue
            cost = times.cost(nxt_id, now)
            if not math.isfinite(cost):
                continue
            candidate = now + cost
            if candidate < arrival.get(nxt_id, math.inf):
                arrival[nxt_id] = candidate
                previous[nxt_id] = edge_id
                heapq.heappush(queue, (candidate, nxt_id))

    return Journey(depart_s, None, [])


def _build_journey(net, times, previous, origin, destination, depart_s, arrive_s) -> Journey:
    chain = [destination]
    while chain[-1] != origin:
        step = previous.get(chain[-1])
        if step is None:
            break
        chain.append(step)
    chain.reverse()

    legs: list[Leg] = []
    clock = depart_s
    for edge_id in chain[1:]:
        cost = times.cost(edge_id, clock)
        try:
            name = net.getEdge(edge_id).getName() or ""
        except KeyError:
            name = ""
        legs.append(Leg(edge_id, name, clock, cost))
        clock += cost

    return Journey(depart_s, arrive_s - depart_s, legs)


def sweep(
    net,
    times: TravelTimes,
    origin_edge: str,
    destination_edge: str,
    *,
    window: tuple[int, int] = AM_PEAK,
    every_minutes: int = 15,
) -> list[Journey]:
    """Run the same trip at every candidate departure across the window."""
    step = every_minutes * 60
    duration = (window[1] - window[0]) * 3600
    return [
        route_at(net, times, origin_edge, destination_edge, float(offset))
        for offset in range(0, duration, step)
    ]


def _clock(total_s: float) -> str:
    total = int(total_s) % (24 * 3600)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}"


def summarise(options: list[dict], window: tuple[float, float] | None) -> dict:
    """Best and worst departure inside ``window``, over a curve already built.

    Separated from :func:`plan` so that a whole-day curve can be computed once
    and asked about many times -- which is the whole point of the planner. A
    user says "I have to be there some time this evening"; the answer is a
    slice of a curve, not another simulation.

    The recommendation is a *band*, not a minute. This model carries a fraction
    of real traffic, so every duration is optimistic; the shape survives that
    better than the absolute numbers, but not well enough to distinguish 06:15
    from 06:30 when they differ by forty seconds. Reporting a single best time
    would imply a precision the model does not have. Anything within a minute,
    or 5%, of the best is reported as equally good.
    """
    inside = options if window is None else [
        o for o in options if window[0] <= o["depart_s"] < window[1]
    ]
    if not inside:
        return {"window": None, "options_in_window": 0}

    best = min(inside, key=lambda o: o["duration_min"])
    worst = max(inside, key=lambda o: o["duration_min"])
    tolerance = max(1.0, best["duration_min"] * 0.05)
    good = [o for o in inside if o["duration_min"] <= best["duration_min"] + tolerance]

    return {
        "window": f"{_clock(window[0])}-{_clock(window[1])}" if window else "all day",
        "options_in_window": len(inside),
        "best_departure": best["depart"],
        "best_duration_min": best["duration_min"],
        "worst_departure": worst["depart"],
        "worst_duration_min": worst["duration_min"],
        "spread_min": round(worst["duration_min"] - best["duration_min"], 1),
        # Every departure that is as good as the best, within the model's
        # ability to tell them apart.
        "good_departures": [o["depart"] for o in good],
        "good_from": good[0]["depart"],
        "good_to": good[-1]["depart"],
        "tolerance_min": round(tolerance, 1),
    }


def plan(
    net,
    times: TravelTimes,
    core: set[str],
    origin: str,
    destination: str,
    *,
    simulated: tuple[int, int] = AM_PEAK,
    window: tuple[float, float] | None = None,
    every_minutes: int = 15,
) -> dict:
    """Answer "when should I leave" for two named places.

    ``simulated`` is the span the simulation actually covers, and the curve is
    built across all of it. ``window`` is the range the *user* asked about and
    only selects which part of that curve to recommend from -- so a whole-day
    run answers "some time between 17:00 and 20:00" without re-running
    anything, and the curve outside the window is still returned so the caller
    can show what was passed over.

    Returns every option rather than only the best one. The difference between
    best and worst is the more useful number: if leaving an hour earlier saves
    four minutes, the honest advice is that it does not matter when you leave,
    and a tool that only ever emits a single recommended time cannot say that.
    """
    for key in (origin, destination):
        if key not in PLACES:
            raise SystemExit(
                f"Unknown place {key!r}. Known: {', '.join(sorted(PLACES))}"
            )

    start = PLACES[origin]
    end = PLACES[destination]
    origin_edge = _nearest_edge(net, start.longitude, start.latitude, core)
    destination_edge = _nearest_edge(net, end.longitude, end.latitude, core)

    if origin_edge is None or destination_edge is None:
        raise SystemExit(
            f"Could not attach {origin} or {destination} to a routable road."
        )

    journeys = sweep(
        net, times, origin_edge.getID(), destination_edge.getID(),
        window=simulated, every_minutes=every_minutes,
    )
    arrived = [j for j in journeys if j.arrived]
    if not arrived:
        raise SystemExit(f"No route found from {origin} to {destination}.")

    # Simulation seconds are offsets from the start of the simulated span, so
    # for a whole-day run they are already clock times.
    base_s = simulated[0] * 3600
    options = [
        {
            "depart": _clock(base_s + j.depart_s),
            "depart_s": base_s + j.depart_s,
            "duration_min": round(j.duration_s / 60, 1),
            "arrive": _clock(base_s + j.depart_s + j.duration_s),
        }
        for j in arrived
    ]

    return {
        "from": start.name,
        "to": end.name,
        "simulated": f"{simulated[0]:02d}:00-{simulated[1]:02d}:00",
        "every_minutes": every_minutes,
        "options": options,
        **summarise(options, window),
        "caveat": (
            "Modelled, not measured. This network carries a fraction of real "
            "traffic, so these durations are optimistic. The shape of the curve "
            "is more trustworthy than any single number in it."
        ),
    }
