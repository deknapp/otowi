"""Where traffic enters and leaves the study area.

Half the commuting this region does has one end outside the box. Someone lives
in Santa Fe and works in Albuquerque; someone lives in Rio Rancho and works at
LANL. Dropping those trips -- which is what the first version of the demand
model did -- removes 47,528 workers against the 52,197 it keeps, and it is the
main reason the model carried 8% of measured traffic.

The fix is not a bigger study area. Enlarging the box would mean simulating
Albuquerque to get Santa Fe right, and the box would still have an edge
somewhere. Instead a trip with one end outside is attached to a **gateway**:
the place where it crosses the boundary. The vehicle appears on the right
highway at roughly the right time and drives the part of its journey that is
actually inside the study area, which is the part we claim to model.

**Finding the gateways.** A clipped road is a road that stops. So an entry
gateway is an edge with no predecessor at all -- nothing inside the network
feeds it, so anything on it must have come from outside -- from which the
routable core is reachable. An exit gateway is the mirror image.

The reachability test has to be a search rather than a look at the immediate
neighbours. I-25 enters the box and runs through several edges before it joins
anything strongly connected, so its first edge has no core neighbour and an
adjacency test misses the single most important gateway in the region. That
mistake is why this is a graph search and not a one-liner.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass

from .config import BBOX

log = logging.getLogger(__name__)

#: Only real through-routes are gateways. A residential street clipped at the
#: boundary carries a handful of vehicles and attaching regional demand to it
#: would invent a rat run through somebody's neighbourhood.
GATEWAY_CLASSES = (
    "motorway", "motorway_link",
    "trunk", "trunk_link",
    "primary", "primary_link",
    "secondary",
)

#: How far to search for the core before giving up on an edge being a gateway.
#: I-25 needs about a dozen; the limit exists so a long severed fragment does
#: not get promoted to a regional entry point.
MAX_HOPS_TO_CORE = 60

#: Assumed speed on the road outside the study area, in metres per second
#: (~90 km/h). Used only to estimate when a vehicle that left an external home
#: reaches the boundary. It is a highway average, and it is an assumption --
#: but a stated one, and far better than pretending everyone materialises at
#: the boundary at the moment they left home.
EXTERNAL_SPEED_MS = 25.0


@dataclass(frozen=True)
class Gateway:
    edge_id: str
    lon: float
    lat: float
    kind: str
    direction: str  # "in" or "out"


def _endpoint_lonlat(net, edge, index: int) -> tuple[float, float]:
    x, y = edge.getShape()[index]
    return net.convertXY2LonLat(x, y)


def _reaches_core(edge, core: set[str], *, forward: bool, max_hops: int) -> bool:
    """Whether the strongly-connected core can be reached from this edge."""
    seen = {edge.getID()}
    queue = deque([(edge, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth >= max_hops:
            continue
        neighbours = current.getOutgoing() if forward else current.getIncoming()
        for nxt in neighbours:
            nxt_id = nxt.getID()
            if nxt_id in seen:
                continue
            if nxt_id in core:
                return True
            seen.add(nxt_id)
            queue.append((nxt, depth + 1))
    return False


def find(net, core: set[str], *, max_hops: int = MAX_HOPS_TO_CORE) -> list[Gateway]:
    """Every point at which the modelled network meets the outside world."""
    found: list[Gateway] = []

    for edge in net.getEdges():
        if edge.isSpecial() or not edge.allows("passenger"):
            continue
        kind = edge.getType().split(".")[-1]
        if kind not in GATEWAY_CLASSES:
            continue

        if not edge.getIncoming() and _reaches_core(
            edge, core, forward=True, max_hops=max_hops
        ):
            lon, lat = _endpoint_lonlat(net, edge, 0)
            found.append(Gateway(edge.getID(), lon, lat, kind, "in"))

        if not edge.getOutgoing() and _reaches_core(
            edge, core, forward=False, max_hops=max_hops
        ):
            lon, lat = _endpoint_lonlat(net, edge, -1)
            found.append(Gateway(edge.getID(), lon, lat, kind, "out"))

    entries = sum(1 for g in found if g.direction == "in")
    log.info("%d gateways: %d entry, %d exit", len(found), entries, len(found) - entries)
    return found


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres."""
    radius = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def choose(
    gateways: list[Gateway],
    external_lon: float,
    external_lat: float,
    internal_lon: float,
    internal_lat: float,
    direction: str,
) -> tuple[Gateway, float] | None:
    """The gateway a trip between these two points would actually use.

    Chosen by minimising the whole journey -- outside distance plus inside
    distance -- rather than by proximity to either end. Picking the gateway
    nearest the external point sends an Albuquerque-to-Los Alamos trip in via
    whichever boundary road happens to be closest to Albuquerque, which can be
    the wrong side of the study area entirely.

    Returns the gateway and the estimated external leg distance in metres, which
    trip generation needs to work out when the vehicle reaches the boundary.
    """
    best = None
    for gateway in gateways:
        if gateway.direction != direction:
            continue
        outside = haversine_m(external_lon, external_lat, gateway.lon, gateway.lat)
        inside = haversine_m(gateway.lon, gateway.lat, internal_lon, internal_lat)
        total = outside + inside
        if best is None or total < best[0]:
            best = (total, gateway, outside)

    if best is None:
        return None
    return best[1], best[2]


def external_travel_s(distance_m: float, speed_ms: float = EXTERNAL_SPEED_MS) -> float:
    """How long the unmodelled part of the journey takes.

    Used to shift a departure time to a boundary-arrival time: someone leaving
    Albuquerque at 06:30 does not appear on I-25 at the study-area boundary
    until roughly 07:00, and putting them there at 06:30 would move the whole
    inbound peak half an hour early.
    """
    return distance_m / speed_ms


def inside_bbox(lon: float, lat: float, bbox: tuple[float, float, float, float] = BBOX) -> bool:
    west, south, east, north = bbox
    return west <= lon <= east and south <= lat <= north
