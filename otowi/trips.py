"""Turning measured flows into vehicles on specific roads.

Three problems live here, and they are the ones that decide whether the
simulation is about northern New Mexico or about a plausible-looking graph.

**Attaching a block to a road.** LODES gives a census block centroid. SUMO
needs an edge. The centroid of a rural Rio Arriba block can sit two kilometres
from any modelled road, and the nearest edge to a Santa Fe block might be the
interstate rather than the residential street the trip actually starts on. So
attachment is bounded by a radius, prefers roads a trip would plausibly start
on, and *reports what it could not place* instead of quietly dropping it.

**Sampling integers from fractional demand.** The median flow is one or two
workers. Multiplying by drive share and trip rate and rounding gives zero, and
rounding forty thousand small flows independently would delete most of the
demand. Vehicles are drawn from a Poisson with the fractional expectation as
its mean, so small flows contribute in proportion to how often they should.

**Placing departures in time.** The count for a flow says how many; the ACS
profile says when. Each vehicle draws a bin according to the measured shares,
then a uniform second inside that bin -- uniform because ACS publishes no
within-bin structure, and pretending to know it would be invention.

The output is a SUMO trips file: origin edge, destination edge, departure
second. Routing is a separate step, because the route a driver takes is a
model output and not an input.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as etree

from .config import AM_PEAK, CACHE_DIR
from . import gateways as gateway_module
from .demand import Flow, vehicles_on

log = logging.getLogger(__name__)

#: How far a block centroid may be from a road before the trip is abandoned.
#: Generous, because rural blocks here are genuinely large -- but finite, so a
#: block in the middle of the Santa Fe National Forest does not get attached to
#: a highway ten kilometres away and invent a long trip.
MAX_ATTACH_M = 2000.0

#: Road classes a trip may start or end on, best first. A commute begins on a
#: local street, not on the shoulder of I-25, and attaching origins to
#: motorways puts vehicles onto the fast network without the local delay of
#: getting there -- which flatters every travel time in the model.
ORIGIN_PREFERENCE = (
    "residential", "unclassified", "tertiary", "tertiary_link",
    "secondary", "secondary_link", "primary", "primary_link",
)

#: Classes never used as an endpoint at all, at any distance.
NEVER_ENDPOINT = ("motorway", "motorway_link", "trunk_link")

#: How many edges a block's *destinations* may be spread over.
#:
#: Origins do not need this and do not get it: homes are spread across
#: thousands of blocks already, and ORIGIN_PREFERENCE deliberately starts a
#: driver on a local street so the journey pays the cost of reaching the
#: arterial. Workplaces are the opposite shape. LODES reports jobs by census
#: block, one block can hold an entire national laboratory, and attaching that
#: block to a single edge delivers the whole workforce to one street. In this
#: model that was 6th Street in Los Alamos, which received 6,080 of the 9,387
#: vehicles arriving in the town -- 65% of them, onto a residential street --
#: and deadlocked, backing traffic down East Road and Diamond Drive and out
#: onto NM-502. The commute the project exists to measure was the worst-served
#: trip in it as a result.
MAX_DESTINATION_EDGES = 8


#: Arriving traffic is split between a block's edges in proportion to what each
#: can carry, so a laboratory is reached mostly by the arterials that serve it
#: -- in Los Alamos, West Jemez Road, Diamond Drive and Canyon Road -- and only
#: incidentally by the side streets. Lanes times speed limit is a crude
#: capacity, and crude is the right precision here: the claim being made is
#: only that a four-lane road takes more of the morning than a cul-de-sac.
def _capacity(edge) -> float:
    return max(1.0, edge.getLaneNumber() * edge.getSpeed())


def reachable_core(net) -> set[str]:
    """Edge IDs in the largest mutually-reachable part of the network.

    This exists because of a silent 30% loss. Attaching trips to the nearest
    plausible edge produced 2,877 vehicles, of which duarouter loaded 2,012 --
    it had quietly dropped every trip whose endpoints could not be connected,
    because ``--ignore-errors`` turns an unroutable trip into a warning nobody
    reads. The missing 30% looked like congestion in the results and was not.

    OSM extracts always contain fragments that a bounding box severed: a
    frontage road whose only junction lies outside the box, a residential loop
    reachable solely by a track that was filtered out, a one-way stub. Any trip
    ending on one of those is unroutable no matter how close the block centroid
    sits.

    So: compute the largest strongly-connected component over the edge graph
    once, and refuse to attach anything outside it. Strongly connected, not
    merely connected, because a trip needs a path *both* ways -- an edge you
    can drive into but not out of is exactly as useless as an isolated one.

    Kosaraju's algorithm, iterative because the recursion depth on 33,000 edges
    exceeds Python's default limit.
    """
    edges = [edge for edge in net.getEdges() if _usable(edge)]
    ids = {edge.getID() for edge in edges}

    def outgoing(edge_id: str) -> list[str]:
        try:
            edge = net.getEdge(edge_id)
        except KeyError:
            return []
        return [nxt.getID() for nxt in edge.getOutgoing() if nxt.getID() in ids]

    def incoming(edge_id: str) -> list[str]:
        try:
            edge = net.getEdge(edge_id)
        except KeyError:
            return []
        return [prv.getID() for prv in edge.getIncoming() if prv.getID() in ids]

    # Pass one: order edges by finishing time in the forward graph.
    visited: set[str] = set()
    order: list[str] = []
    for start in ids:
        if start in visited:
            continue
        stack = [(start, iter(outgoing(start)))]
        visited.add(start)
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child not in visited:
                    visited.add(child)
                    stack.append((child, iter(outgoing(child))))
                    advanced = True
                    break
            if not advanced:
                order.append(node)
                stack.pop()

    # Pass two: components in the reverse graph, taken in reverse finish order.
    assigned: set[str] = set()
    largest: set[str] = set()
    for start in reversed(order):
        if start in assigned:
            continue
        component = {start}
        assigned.add(start)
        stack = [start]
        while stack:
            node = stack.pop()
            for parent in incoming(node):
                if parent not in assigned:
                    assigned.add(parent)
                    component.add(parent)
                    stack.append(parent)
        if len(component) > len(largest):
            largest = component

    log.info(
        "routable core: %d of %d usable edges are mutually reachable",
        len(largest), len(ids),
    )
    return largest


@dataclass
class Attachment:
    """The block-to-edge mapping, and an account of what failed."""

    edge_by_block: dict[str, str] = field(default_factory=dict)
    #: block -> [(edge id, weight)], for trip *ends* only. See
    #: :data:`MAX_DESTINATION_EDGES`.
    destinations_by_block: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    unplaced: list[str] = field(default_factory=list)
    distances: list[float] = field(default_factory=list)

    def choose_destination(self, block: str, rng: random.Random) -> str | None:
        """One arrival edge for one vehicle, weighted by road capacity.

        Falls back to the single attached edge when a block has no spread --
        which is the case for anything constructed by hand, and for a block
        whose only usable road is one edge.
        """
        options = self.destinations_by_block.get(block)
        if not options:
            return self.edge_by_block.get(block)
        total = sum(weight for _, weight in options)
        draw = rng.random() * total
        for edge_id, weight in options:
            draw -= weight
            if draw <= 0:
                return edge_id
        return options[-1][0]

    def summary(self) -> dict:
        placed = len(self.edge_by_block)
        total = placed + len(self.unplaced)
        median = 0.0
        if self.distances:
            ordered = sorted(self.distances)
            median = ordered[len(ordered) // 2]
        return {
            "blocks_placed": placed,
            "blocks_unplaced": len(self.unplaced),
            "placement_rate": round(placed / total, 3) if total else 0.0,
            "median_attach_distance_m": round(median, 1),
            "max_attach_distance_m": round(max(self.distances), 1) if self.distances else 0.0,
        }


def _usable(edge) -> bool:
    """Whether an edge can carry a car and can hold a trip endpoint."""
    if edge.isSpecial():  # internal junction edges have no length to speak of
        return False
    if not edge.allows("passenger"):
        return False
    return edge.getType().split(".")[-1] not in NEVER_ENDPOINT


def _rank(edge, distance: float) -> tuple[int, float]:
    """Sort key: preferred road class first, then proximity.

    Class beats distance deliberately. A residential street 400 m away is a
    better trip origin than a primary arterial 50 m away, because the arterial
    is what the driver joins *after* the first block of the trip, and starting
    them on it removes that delay from every journey.
    """
    kind = edge.getType().split(".")[-1]
    try:
        preference = ORIGIN_PREFERENCE.index(kind)
    except ValueError:
        preference = len(ORIGIN_PREFERENCE)
    return (preference, distance)


def attach_blocks(
    net,
    centroids: dict[str, tuple[float, float]],
    *,
    max_distance_m: float = MAX_ATTACH_M,
    core: set[str] | None = None,
) -> Attachment:
    """Map each census block centroid to the edge a trip there would use.

    Only edges in the routable core are eligible; see :func:`reachable_core`.
    A block whose nearest road is a severed fragment is attached to the nearest
    *routable* road instead, and if there is none within ``max_distance_m`` it
    is reported unplaced rather than turned into a trip that will be discarded
    later without anyone noticing.
    """
    core = core if core is not None else reachable_core(net)
    result = Attachment()

    for block, (lat, lon) in centroids.items():
        x, y = net.convertLonLat2XY(lon, lat)
        candidates = [
            (edge, distance)
            for edge, distance in net.getNeighboringEdges(x, y, max_distance_m)
            if _usable(edge) and edge.getID() in core
        ]
        if not candidates:
            result.unplaced.append(block)
            continue
        edge, distance = min(candidates, key=lambda pair: _rank(*pair))
        result.edge_by_block[block] = edge.getID()
        result.distances.append(distance)

        # Destinations get the whole neighbourhood, not just the best-ranked
        # edge, and they are not filtered by ORIGIN_PREFERENCE: someone driving
        # to work arrives *on* the arterial that serves the site, which is the
        # opposite of how they left home.
        nearby = sorted(candidates, key=lambda pair: pair[1])[:MAX_DESTINATION_EDGES]
        result.destinations_by_block[block] = [
            (candidate.getID(), _capacity(candidate)) for candidate, _ in nearby
        ]

    log.info(
        "attached %d of %d blocks to edges (%d unplaced)",
        len(result.edge_by_block), len(centroids), len(result.unplaced),
    )
    return result


def _sample_departures(
    count: int,
    weighted_bins: list[tuple[object, float]],
    window: tuple[int, int],
    rng: random.Random,
) -> list[float]:
    """Draw ``count`` departure seconds from the measured profile."""
    bins = [item for item, _ in weighted_bins]
    weights = [weight for _, weight in weighted_bins]
    window_start_s = window[0] * 3600

    seconds = []
    for chosen in rng.choices(bins, weights=weights, k=count):
        # Clip to the window: a bin straddling the edge contributes only its
        # overlapping part, and a departure must land inside what we simulate.
        low = max(chosen.start_hour, window[0]) * 3600
        high = min(chosen.end_hour, window[1]) * 3600
        seconds.append(rng.uniform(low, high) - window_start_s)
    return sorted(seconds)


def generate(
    net,
    flows: list[Flow],
    attachment: Attachment,
    weighted_bins: list[tuple[object, float]],
    *,
    window: tuple[int, int] = AM_PEAK,
    seed: int = 0,
    scale: float = 1.0,
    gateways: list | None = None,
) -> tuple[list[dict], dict]:
    """Expand flows into individual vehicles with an origin, destination and time.

    ``scale`` multiplies the expected vehicle count. It exists for running a
    cheap 10% simulation while developing; anything other than 1.0 must be
    reported alongside the results, because a scaled run does not reproduce
    congestion -- delay is not linear in demand, which is the entire reason
    this corridor is worth simulating.
    """
    rng = random.Random(seed)
    trips: list[dict] = []
    dropped_unplaced = 0
    dropped_same_edge = 0
    dropped_no_gateway = 0
    expected_total = 0.0
    by_kind = {"internal": 0, "inbound": 0, "outbound": 0}
    destination_edges: set[str] = set()

    for flow in flows:
        # Where the trip enters and leaves the roads we actually model.
        external_delay_s = 0.0

        # ``destination_block`` is set when the trip ends at a workplace inside
        # the study area. It is resolved to an edge once per *vehicle* below
        # rather than once per flow, because a flow can be hundreds of jobs at
        # one laboratory and they do not all arrive on the same street.
        destination_block = None

        if flow.kind == "internal":
            origin = attachment.edge_by_block.get(flow.home_block)
            destination = attachment.edge_by_block.get(flow.work_block)
            destination_block = flow.work_block

        elif flow.kind == "inbound":
            # Lives outside, works inside: appears at a gateway.
            destination = attachment.edge_by_block.get(flow.work_block)
            destination_block = flow.work_block
            origin = None
            if destination is not None and gateways:
                chosen = gateway_module.choose(
                    gateways, flow.home_lon, flow.home_lat,
                    flow.work_lon, flow.work_lat, "in",
                )
                if chosen is not None:
                    gateway, outside_m = chosen
                    origin = gateway.edge_id
                    # Someone leaving Albuquerque at 06:30 does not reach the
                    # boundary until roughly 07:00. Putting them on the gateway
                    # at 06:30 would move the whole inbound peak early.
                    external_delay_s = gateway_module.external_travel_s(outside_m)

        else:  # outbound -- lives inside, works outside
            origin = attachment.edge_by_block.get(flow.home_block)
            destination = None
            if origin is not None and gateways:
                chosen = gateway_module.choose(
                    gateways, flow.work_lon, flow.work_lat,
                    flow.home_lon, flow.home_lat, "out",
                )
                if chosen is not None:
                    destination = chosen[0].edge_id

        if flow.kind != "internal" and (origin is None or destination is None):
            if attachment.edge_by_block.get(
                flow.work_block if flow.kind == "inbound" else flow.home_block
            ) is not None:
                dropped_no_gateway += flow.jobs
                continue

        if origin is None or destination is None:
            dropped_unplaced += flow.jobs
            continue
        if origin == destination:
            # Both ends on the same edge is a trip with no network extent.
            dropped_same_edge += flow.jobs
            continue

        expected = vehicles_on(flow) * scale
        expected_total += expected
        count = _poisson(expected, rng)
        if count == 0:
            continue

        by_kind[flow.kind] += count
        for second in _sample_departures(count, weighted_bins, window, rng):
            # An inbound vehicle is placed when it reaches the boundary, not
            # when it left home. Departures pushed past the window are kept at
            # its end rather than dropped, which slightly over-fills the last
            # interval and is preferable to deleting long-distance commuters.
            depart = min(second + external_delay_s,
                         (window[1] - window[0]) * 3600 - 1)

            arrival = destination
            if destination_block is not None:
                spread = attachment.choose_destination(destination_block, rng)
                if spread is not None:
                    arrival = spread
            if arrival == origin:
                # Spreading can land a vehicle back on the edge it started on.
                # Counted, not silently kept as a trip with no network extent.
                dropped_same_edge += 1
                continue

            trips.append({"from": origin, "to": arrival, "depart": depart})
            destination_edges.add(arrival)

    trips.sort(key=lambda trip: trip["depart"])
    stats = {
        "vehicles": len(trips),
        "vehicles_by_kind": by_kind,
        "expected_vehicles": round(expected_total, 1),
        "workers_dropped_unplaced_block": dropped_unplaced,
        "workers_dropped_same_edge": dropped_same_edge,
        "workers_dropped_no_gateway": dropped_no_gateway,
        # A funnel is invisible in the vehicle count and obvious here.
        "distinct_destination_edges": len(destination_edges),
        "scale": scale,
        "seed": seed,
        "window": f"{window[0]:02d}:00-{window[1]:02d}:00",
    }
    log.info("generated %d vehicles (expected %.0f)", len(trips), expected_total)
    return trips, stats


def _poisson(mean: float, rng: random.Random) -> int:
    """Knuth's algorithm. Small means only, which is all we have here.

    Poisson rather than rounding because the demand is a rate: a flow of 1.3
    expected vehicles should produce one vehicle most mornings and two
    sometimes, and rounding it to one loses the variance that makes a peak a
    peak.
    """
    if mean <= 0:
        return 0
    if mean > 30:  # would underflow; not reached by real flows, but be safe
        return int(round(mean))
    limit = 2.718281828459045 ** -mean
    count, product = 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def trips_path(window: tuple[int, int] = AM_PEAK) -> Path:
    return CACHE_DIR / f"trips-am-{window[0]:02d}{window[1]:02d}.trips.xml"


def write_trips(trips: list[dict], path: Path | None = None,
                window: tuple[int, int] = AM_PEAK) -> Path:
    """Write a SUMO trips file, sorted by departure as SUMO requires."""
    path = path or trips_path(window)
    path.parent.mkdir(parents=True, exist_ok=True)

    root = etree.Element("routes")
    etree.SubElement(root, "vType", id="car", vClass="passenger")
    for index, trip in enumerate(trips):
        etree.SubElement(
            root, "trip",
            id=f"c{index}",
            type="car",
            depart=f"{trip['depart']:.2f}",
            **{"from": trip["from"], "to": trip["to"]},
        )

    tmp = path.with_suffix(".partial")
    etree.ElementTree(root).write(str(tmp), encoding="utf-8", xml_declaration=True)
    tmp.replace(path)
    log.info("wrote %d trips to %s", len(trips), path)
    return path
