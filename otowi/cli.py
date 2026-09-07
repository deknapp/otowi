"""Command line: one subcommand per stage of the pipeline.

The stages are separate commands rather than one ``run everything`` because
they cost wildly different amounts and fail for different reasons. Fetching
OpenStreetMap hits a donated service and should happen once ever. Building the
network is local and cheap. Downloading LODES is one request. Routing and
simulating take minutes and are the parts worth re-running while changing
assumptions.

``otowi run`` chains them for the common case, skipping any stage whose output
is already on disk.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter

from . import demand, departure, network, simulate, trips
from .config import AM_PEAK, CORRIDORS, PLACES

log = logging.getLogger(__name__)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _load_net():
    """Read the built network, with a useful error if it is not there yet."""
    path = network.network_path()
    if not path.exists():
        raise SystemExit(
            f"No network at {path}.\nRun:  otowi network"
        )
    try:
        import sumolib
    except ImportError as exc:  # pragma: no cover - dependency is in requirements
        raise SystemExit(
            "sumolib is not installed. Run:  pip install -r requirements.txt"
        ) from exc
    return sumolib.net.readNet(str(path))


def _residence_weights(flows: list[demand.Flow]) -> dict[str, int]:
    """Workers by county of residence, for weighting the departure profile.

    Residence rather than workplace because B08302 asks when the respondent
    left *home*. The first five digits of a census block GEOID are the state
    and county FIPS.
    """
    counter: Counter[str] = Counter()
    for flow in flows:
        counter[flow.home_block[:5]] += flow.jobs
    return {fips: counter.get(fips, 0) for fips in departure.COUNTIES}


# --------------------------------------------------------------------- stages


def cmd_places(args) -> None:
    """Print the study area, so the geography can be checked without running anything."""
    for key, place in PLACES.items():
        print(f"{key:14s} {place.latitude:8.4f} {place.longitude:10.4f}  {place.note}")
    print("\nCorridors:")
    for key, description in CORRIDORS.items():
        print(f"  {key:20s} {description}")


def cmd_network(args) -> None:
    """Fetch OSM and build the SUMO network."""
    if not network.netconvert_available():
        raise SystemExit(
            "netconvert not found. Run:  pip install -r requirements.txt"
        )
    path = network.build_network(force=args.force)
    net = _load_net()
    print(json.dumps({
        "network": str(path),
        "edges": len(net.getEdges()),
        "junctions": len(net.getNodes()),
        "traffic_lights": len(net.getTrafficLights()),
    }, indent=2))


def cmd_demand(args) -> None:
    """Download LODES and ACS, and report the demand without simulating it."""
    centroids = demand.block_centroids()
    flows = demand.commute_flows(args.year, centroids)
    weights = _residence_weights(flows)
    profile = departure.blended_profile(weights)

    print(json.dumps({
        "lodes_year": args.year,
        "blocks_in_study_area": len(centroids),
        **demand.summarize(flows),
        "workers_by_county_of_residence": {
            departure.COUNTIES[fips]: count for fips, count in weights.items()
        },
        "departure": departure.summarize(profile, tuple(args.window)),
    }, indent=2))


def cmd_trips(args) -> None:
    """Generate the vehicles: origin edge, destination edge, departure second."""
    window = tuple(args.window)
    net = _load_net()
    centroids = demand.block_centroids()
    flows = demand.commute_flows(args.year, centroids)
    attachment = trips.attach_blocks(net, centroids)
    profile = departure.blended_profile(_residence_weights(flows))
    weighted = departure.morning_weights(profile, window)

    vehicles, stats = trips.generate(
        net, flows, attachment, weighted,
        window=window, seed=args.seed, scale=args.scale,
    )
    path = trips.write_trips(vehicles, window=window)

    print(json.dumps({
        "trips_file": str(path),
        "attachment": attachment.summary(),
        **stats,
    }, indent=2))


def cmd_route(args) -> None:
    """Turn trips into routes with duarouter."""
    window = tuple(args.window)
    source = trips.trips_path(window)
    if not source.exists():
        raise SystemExit(f"No trips at {source}.\nRun:  otowi trips")
    path = simulate.build_routes(source, window=window, force=args.force)
    print(json.dumps({"routes": str(path), "bytes": path.stat().st_size}, indent=2))


def cmd_simulate(args) -> None:
    """Run SUMO and summarize what happened."""
    window = tuple(args.window)
    routes = simulate.routes_path(window)
    if not routes.exists():
        raise SystemExit(f"No routes at {routes}.\nRun:  otowi route")

    outputs = simulate.run(routes, window=window, end_padding_s=args.end_padding)
    net = _load_net()
    result = {
        "window": f"{window[0]:02d}:00-{window[1]:02d}:00",
        **simulate.summarize_tripinfo(outputs["tripinfo"], routes),
        "busiest_edges": simulate.busiest_edges(outputs["edgedata"], net, top=args.top),
    }
    print(json.dumps(result, indent=2))
    print(
        "\nNOT CALIBRATED. These travel times are the model's opinion and have "
        "no error bar until edge volumes are compared against the MPO and NMDOT "
        "count stations. See the README.",
        file=sys.stderr,
    )


def cmd_run(args) -> None:
    """Everything, skipping stages whose output already exists."""
    window = tuple(args.window)
    if not network.network_path().exists():
        cmd_network(args)
    if not trips.trips_path(window).exists():
        cmd_trips(args)
    if not simulate.routes_path(window).exists():
        cmd_route(args)
    cmd_simulate(args)


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="otowi",
        description="Traffic simulation for northern New Mexico.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    add("places", cmd_places, "Print the study area and corridors.")

    network_parser = add("network", cmd_network, "Fetch OSM and build the SUMO network.")
    network_parser.add_argument("--force", action="store_true",
                                help="Rebuild even if the network exists.")

    for name, handler, help_text in [
        ("demand", cmd_demand, "Report the commute demand without simulating."),
        ("trips", cmd_trips, "Generate vehicles from the demand."),
        ("route", cmd_route, "Route the trips with duarouter."),
        ("simulate", cmd_simulate, "Run SUMO on the routes."),
        ("run", cmd_run, "Do every stage that has not been done."),
    ]:
        sub = add(name, handler, help_text)
        sub.add_argument("--year", type=int, default=demand.DEFAULT_YEAR,
                         help="LODES vintage.")
        sub.add_argument("--window", type=int, nargs=2, default=list(AM_PEAK),
                         metavar=("START", "END"),
                         help="Simulation window in local hours.")
        sub.add_argument("--seed", type=int, default=0)
        sub.add_argument("--scale", type=float, default=1.0,
                         help="Multiply demand. Anything but 1.0 must be reported: "
                              "delay is not linear in demand.")
        sub.add_argument("--force", action="store_true")
        sub.add_argument("--top", type=int, default=15,
                         help="How many busiest edges to report.")
        sub.add_argument("--end-padding", type=int, default=10800,
                         help="Seconds to keep simulating after the last departure, "
                              "so long trips are not cut off and dropped from the averages.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
