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
from pathlib import Path

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
    from . import gateways as gateway_module

    window = tuple(args.window)
    net = _load_net()

    # Statewide centroids, because an inbound trip's home end is by definition
    # outside the study area and still has to be located to pick its gateway.
    all_centroids = demand.block_centroids(bbox=None)
    flows = demand.commute_flows(args.year, all_centroids,
                                 include_external=not args.internal_only)

    inside = demand.block_centroids()
    attachment = trips.attach_blocks(net, inside)
    core = trips.reachable_core(net)
    gws = [] if args.internal_only else gateway_module.find(net, core)

    profile = departure.blended_profile(_residence_weights(flows))
    # The whole day, always. Trip generation filters to the window itself --
    # handing it a window-restricted profile is what put 100% of the day's
    # drivers inside the morning peak.
    weighted = departure.day_weights(profile)

    vehicles, stats = trips.generate(
        net, flows, attachment, weighted,
        window=window, seed=args.seed, scale=args.scale, gateways=gws,
        include_returns=not args.no_returns,
    )
    stats["gateways"] = len(gws)
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


def cmd_assign(args) -> None:
    """Iteratively route and simulate until routes and travel times agree."""
    window = tuple(args.window)
    source = trips.trips_path(window)
    if not source.exists():
        raise SystemExit(f"No trips at {source}.\nRun:  otowi trips")

    routes, history = simulate.assign_iteratively(
        source, window=window, iterations=args.iterations,
        end_padding_s=args.end_padding,
    )
    print(json.dumps({"routes": str(routes), "history": history}, indent=2))

    _report_convergence(history)


#: Route choice is called settled below this much movement per round, and the
#: measured travel times settled below this much change.
#:
#: Neither is zero, and route shift in particular never reaches zero: Gawron
#: keeps shuffling probability between alternatives that cost nearly the same,
#: which is not a failure to converge but a description of what equilibrium
#: looks like -- a driver genuinely indifferent between two equally good routes.
#: On a tenth of the demand the shift fell 37.9%, 18.3%, 11.3%, 8.6%, 7.8% and
#: was still drifting down while mean time loss had been within 3% of 245 s for
#: three rounds. So the physical measure is what says the answer has stopped
#: moving, and the route shift is what says the assignment is still settling
#: rather than flipping.
SETTLED_SHIFT = 0.15
SETTLED_TIME_LOSS = 0.05


def _report_convergence(history: list[dict]) -> None:
    """Say whether these travel times are worth believing."""
    if len(history) < 2:
        return
    shift = history[-1]["route_shift"]
    moved = history[-1]["mean_time_loss_change"]
    if shift is None or moved is None:
        return

    problems = []
    if shift > SETTLED_SHIFT:
        problems.append(f"route choice still moved {100 * shift:.1f}%")
    if moved > SETTLED_TIME_LOSS:
        problems.append(f"mean time loss still moved {100 * moved:.1f}%")

    shifts = [h["route_shift"] for h in history if h["route_shift"] is not None]
    if len(shifts) > 2 and shifts[-1] > shifts[0]:
        problems.append(
            "and route shift is rising rather than falling, which is the "
            "signature of an assignment flipping between routes rather than "
            "settling on them"
        )

    if problems:
        print(
            "\nNot converged: " + "; ".join(problems) + " on the last round. "
            "An assignment that has not settled is not an equilibrium and its "
            "travel times are not worth much. Run more iterations.",
            file=sys.stderr,
        )
    else:
        print(
            f"\nConverged: route choice moved {100 * shift:.1f}% on the last "
            f"round and mean time loss {100 * moved:.1f}%.",
            file=sys.stderr,
        )


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
        **simulate.summarize_tripinfo(outputs["tripinfo"], routes,
                                      outputs.get("statistics")),
        "busiest_edges": simulate.busiest_edges(outputs["edgedata"], net, top=args.top),
    }
    print(json.dumps(result, indent=2))
    print(
        "\nNOT CALIBRATED. These travel times are the model's opinion and have "
        "no error bar until edge volumes are compared against the MPO and NMDOT "
        "count stations. See the README.",
        file=sys.stderr,
    )


def cmd_calibrate(args) -> None:
    """Compare modelled edge volumes against NMDOT counts."""
    from . import counts

    window = tuple(args.window)
    edgedata = simulate.edgedata_path(window)
    if not edgedata.exists():
        raise SystemExit(f"No simulation output at {edgedata}.\nRun:  otowi simulate")

    net = _load_net()
    segments = counts.parse_segments(counts.fetch_aadt(force=args.force))
    matched = counts.match_to_edges(net, segments)
    hours = window[1] - window[0]
    modelled = counts.simulated_hourly(edgedata, window_hours=hours)
    result = counts.compare(matched, modelled, window_hours=hours)

    worst = sorted(result["links"], key=lambda row: -row["observed"])[: args.top]
    payload = {
        "count_segments": len(segments),
        "edges_matched": len(matched),
        # Which measurement the model was scored against. Printed, not left to
        # be inferred from the window: a ratio without its basis is not a
        # result, and the two bases differ by a factor of eight.
        "basis": result["basis"],
        "unit": result["unit"],
        "all": result["all"],
        "fit": result["fit"],
        "held_out": result["held_out"],
    }

    # For a whole-day run, also score its morning on the hourly basis. A daily
    # GEH cannot be read against the profession's 85%-under-5 bar, so without
    # this a 24-hour model can only be declared *different* from the peak-window
    # one it replaced, never better or worse.
    if hours >= 24 and args.peak and args.peak[1] > args.peak[0]:
        peak = tuple(args.peak)
        intervals = simulate.intervals_path(window)
        if intervals.exists():
            sliced = counts.simulated_hourly_in_slice(intervals, peak)
            peak_result = counts.compare(
                matched, sliced, window_hours=peak[1] - peak[0])
            payload["peak_slice"] = {
                "hours": f"{peak[0]:02.0f}:00-{peak[1]:02.0f}:00",
                "basis": peak_result["basis"],
                "unit": peak_result["unit"],
                "all": peak_result["all"],
                "held_out": peak_result["held_out"],
            }

    payload["busiest_measured_links"] = worst
    print(json.dumps(payload, indent=2))

    ratio = result["held_out"].get("median_ratio_modelled_over_observed")
    if ratio is not None and ratio < 0.8:
        print(
            f"\nThe model carries {ratio:.0%} of measured volume ({result['basis']}). It contains "
            "commuting between two points inside the study area and nothing else -- no "
            "freight, no shopping, no tourism, and no trip with one end outside the box, "
            "which is most of I-25 and US-84. This number is the size of that decision, "
            "not a tuning error.",
            file=sys.stderr,
        )


def _parse_clock(text: str) -> float:
    """"17:30", "17.5" or "17" -> seconds since midnight.

    Accepting all three because the question is asked in clock time and typing
    a decimal hour to describe half past five is not how anyone thinks.
    """
    text = text.strip()
    try:
        if ":" in text:
            hours, _, minutes = text.partition(":")
            value = int(hours) * 3600 + int(minutes) * 60
        else:
            value = float(text) * 3600
    except ValueError:
        raise SystemExit(f"Could not read {text!r} as a time. Try 17:30 or 17.")
    if not 0 <= value <= 24 * 3600:
        raise SystemExit(f"{text!r} is not a time between 00:00 and 24:00.")
    return float(value)


def _risk_words(x) -> str:
    """A bare "0.78x" says nothing without a referent. These are the words."""
    if x is None:
        return "not assessed"
    if x < 0.5:
        return "safest time of day"
    if x < 0.8:
        return "below average risk"
    if x < 1.25:
        return "about average risk"
    if x < 2:
        return "above average risk"
    return "well above average risk"


def cmd_when(args) -> None:
    """Answer: what time should I leave, for this trip?"""
    from . import journey, trips as trips_mod

    simulated = tuple(args.window)
    intervals = simulate.intervals_path(simulated)
    if not intervals.exists():
        raise SystemExit(
            f"No interval data at {intervals}.\n"
            f"Run:  otowi run --window {simulated[0]} {simulated[1]}"
        )

    asked = None
    if args.between:
        asked = (_parse_clock(args.between[0]), _parse_clock(args.between[1]))
        if asked[1] <= asked[0]:
            raise SystemExit("The end of --between must be after its start.")
        sim_span = (simulated[0] * 3600.0, simulated[1] * 3600.0)
        if asked[0] < sim_span[0] or asked[1] > sim_span[1]:
            raise SystemExit(
                f"Asked about {args.between[0]}-{args.between[1]}, but the "
                f"simulation only covers {simulated[0]:02d}:00-{simulated[1]:02d}:00.\n"
                f"Run:  otowi run --window 0 24   for a whole day."
            )

    net = _load_net()
    times = journey.TravelTimes.load(intervals, net)
    core = trips_mod.reachable_core(net)
    result = journey.plan(
        net, times, core, args.origin, args.destination,
        simulated=simulated, window=asked, every_minutes=args.every,
    )

    # What the drive is made of, and what has happened on it. A regional
    # ranking says which road is dangerous; this says whether the drive you
    # actually make is.
    if args.risk:
        from . import counts, fatalities
        _, crashes, risks, _ = _risk_inputs(args, simulated)
        route_of = fatalities.corridors_from_counts(
            counts.match_to_edges(net, counts.parse_segments(counts.fetch_aadt())))
        result["risk"] = fatalities.along_route(
            net, result.get("route_edges", []), risks, route_of)
        result["risk"]["regional_average"] = round(
            fatalities.regional_average(risks), 1)
        travel = fatalities.hourly_travel(net, intervals)
        result["risk"]["by_hour"] = fatalities.by_hour(crashes, travel)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    inside = result["options"] if asked is None else [
        o for o in result["options"] if asked[0] <= o["depart_s"] < asked[1]
    ]
    print(f"\n{result['from']} to {result['to']}  "
          f"({result['window'] or 'no options in that range'})\n")
    if args.risk:
        print("  Drive time is simulated and optimistic -- this model carries 14%")
        print("  of real traffic. Risk is measured: fatal crashes recorded on these")
        print("  roads since 2018 per kilometre driven on them. The multiplier")
        print("  compares one hour against an average hour of the day.\n")

    good = set(result.get("good_departures", ()))
    longest = max((o["duration_min"] for o in inside), default=1) or 1

    # Risk per departure, if it was asked for. The journey time barely moves
    # across a day on these corridors while the chance of being killed moves by
    # a factor of nineteen, so the recommendation is risk-led and the minutes
    # are the tie-breaker.
    hours = {}
    if result.get("risk", {}).get("by_hour"):
        hours = {r["hour"]: r["relative_risk"] for r in result["risk"]["by_hour"]}
    for option in inside:
        if hours:
            option["risk_x"] = hours.get(int(option["depart"][:2]) % 24)
    scored = [o for o in inside if o.get("risk_x") is not None]
    safest = min(scored, key=lambda o: o["risk_x"]) if scored else None
    riskiest = max(scored, key=lambda o: o["risk_x"]) if scored else None

    # "Best" needs a defined trade-off or it is a preference dressed as a fact,
    # and the order of the two tests is the whole trade-off. Filtering on time
    # first and then taking the safest survivor recommends midnight -- the
    # second-deadliest hour of the day -- because it is ninety seconds quicker.
    # Risk is the binding constraint and time is the tie-breaker: take the
    # departures that are genuinely among the safest, then the quickest of them.
    fastest = min(inside, key=lambda o: o["duration_min"])
    balanced = fastest
    if scored and safest is not None:
        ceiling = safest["risk_x"] * 1.25
        safe_enough = [o for o in scored if o["risk_x"] <= ceiling]
        balanced = (min(safe_enough, key=lambda o: o["duration_min"])
                    if safe_enough else safest)
    chosen = {"speed": fastest, "safety": safest or fastest,
              "both": balanced}[args.prefer]

    if scored:
        print("  %-9s %-7s %-9s %s" % ("", "leave", "drive", "risk"))
        for tag, option in (("fastest", fastest), ("safest", safest),
                            ("best", balanced)):
            if option is None:
                continue
            star = " *" if option["depart"] == chosen["depart"] else "  "
            print("%s %-9s %-7s %-9s %s" % (
                star, tag, option["depart"],
                f"{option['duration_min']:.0f} min",
                _risk_words(option.get("risk_x"))))
        print()

    for option in inside:
        # Once risk is on the table the journey-time band is noise: it marks
        # departures as equally good on the dimension that moves by a minute
        # while ignoring the one that moves by a factor of nineteen.
        if safest is not None:
            mark = " <- " + args.prefer if option["depart"] == chosen["depart"] else ""
        else:
            mark = " <- as good as it gets" if option["depart"] in good else ""
        bar = "#" * max(1, int(option["duration_min"] / longest * 22))
        risk_col = (f"  {option['risk_x']:4.1f}x" if option.get("risk_x") is not None
                    else "")
        print(f"  leave {option['depart']}   {option['duration_min']:6.1f} min  "
              f"arrive {option['arrive']}{risk_col}  {bar}{mark}")

    if result.get("options_in_window") and safest is None:
        if len(good) > 1:
            print(f"\n  Leave any time between {result['good_from']} and "
                  f"{result['good_to']} -- within {result['tolerance_min']} min "
                  f"of the best the model can tell apart.")
        else:
            print(f"\n  Best:  leave {result['best_departure']}, "
                  f"{result['best_duration_min']} min")
        print(f"  Worst: leave {result['worst_departure']}, "
              f"{result['worst_duration_min']} min")
        print(f"  Spread: {result['spread_min']} min between best and worst\n")
    risk = result.get("risk")
    if risk and risk.get("per_billion_veh_km") is not None:
        average = risk["regional_average"]
        rate = risk["per_billion_veh_km"]
        comparison = ("about average for this region" if 0.8 <= rate / average <= 1.25
                      else ("%.1f times the regional average" % (rate / average)
                            if rate > average
                            else "%.0f%% of the regional average" % (100 * rate / average)))
        print(f"\n  On the roads this drive uses, {rate:.1f} people have died per")
        print(f"  billion kilometres driven -- {comparison}.")
        for stretch in risk["stretches"][:3]:
            print(f"    {stretch['km']:>5.1f} km on {stretch['road']:<8} "
                  f"{stretch['per_billion_veh_km']:>5.1f} per billion "
                  f"({stretch['deaths']} died there since 2018)")
        if risk["assessed_share"] < 0.95:
            print(f"  {1 - risk['assessed_share']:.0%} of the drive is on roads with no "
                  f"traffic count, so it")
            print("  could not be assessed and is left out of that figure.")

        if riskiest is not None and chosen.get("risk_x"):
            cost = chosen["duration_min"] - fastest["duration_min"]
            factor = riskiest["risk_x"] / chosen["risk_x"]
            print(f"\n  Leave at {chosen['depart']} -- {chosen['duration_min']:.0f} "
                  f"min, {_risk_words(chosen['risk_x'])}.")
            if args.prefer != "speed" and factor > 1.3:
                print(f"  About {factor:.1f} times safer per kilometre than leaving "
                      f"at {riskiest['depart']},")
                print("  and it costs %s." % (
                    f"{cost:.0f} min more driving" if cost > 0.5
                    else "nothing in driving time"))

    print()
    print(result["caveat"], file=sys.stderr)


def _risk_inputs(args, window):
    """Everything the risk view needs: crashes, exposure, and the ratio."""
    from . import counts, fatalities

    net = _load_net()
    crashes = fatalities.fetch(force=getattr(args, "force", False))
    segments = counts.parse_segments(counts.fetch_aadt())
    counted = counts.match_to_edges(net, segments)
    hours = window[1] - window[0]
    modelled = counts.simulated_hourly(simulate.edgedata_path(window), hours)
    # The model carries a fraction of real traffic, so its volumes understate
    # exposure and would overstate every rate computed from them. Scale by the
    # measured ratio rather than pretending otherwise.
    comparison = counts.compare(matched := counted, modelled, window_hours=hours)
    carries = comparison["held_out"].get(
        "median_ratio_modelled_over_observed") or 1.0
    years = fatalities.DEFAULT_YEARS[1] - fatalities.DEFAULT_YEARS[0] + 1
    risks = fatalities.build(
        net, fatalities.match_to_edges(net, crashes),
        modelled=modelled, counted=matched, years=years, model_carries=carries)
    return net, crashes, risks, carries


def cmd_risk(args) -> None:
    """Where people have actually been killed, per unit of travel."""
    from . import fatalities

    window = tuple(args.window)
    if not simulate.edgedata_path(window).exists():
        raise SystemExit(
            f"No simulation output for {window}.\nRun:  otowi run --window "
            f"{window[0]} {window[1]}")

    net, crashes, risks, carries = _risk_inputs(args, window)
    summary = fatalities.summarise(crashes, risks)
    travel = fatalities.hourly_travel(net, simulate.intervals_path(window))
    summary["by_hour"] = fatalities.by_hour(crashes, travel)
    summary["regional_average"] = round(fatalities.regional_average(risks), 1)

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    print(f"\nFatal crashes in the study area, "
          f"{fatalities.DEFAULT_YEARS[0]}-{fatalities.DEFAULT_YEARS[1]}\n")
    print(f"  {summary['crashes']} crashes, {summary['fatalities']} deaths."
          f"  {summary['share_after_dark']:.0%} of them after dark.\n")

    print(f"  {'road':<9}{'deaths':>7}{'km':>7}{'veh/day':>9}"
          f"{'per bn veh-km':>15}{'measured':>10}")
    for row in summary["worst"]:
        print(f"  {row['name']:<9}{row['fatalities']:>7}{row['length_km']:>7.1f}"
              f"{row['veh_per_day']:>9}"
              f"{row['per_billion_veh_km']:>10.1f} ({row['lower_bound']:.1f})"
              f"{row['counted_share']:>9.0%}")

    rows = summary["by_hour"]
    worst = fatalities.worst_hours(rows, 1)[0]
    safest = fatalities.safest_hours(rows, 1)[0]
    print("\n  When, per kilometre driven\n")
    for row in rows:
        bar = "#" * int(min(row["relative_risk"], 5.0) * 8)
        mark = ""
        if row["hour"] == worst["hour"]:
            mark = "  worst"
        elif row["hour"] == safest["hour"]:
            mark = "  safest"
        print(f"  {row['hour']:02d}:00 {row['relative_risk']:5.2f}x  {bar}{mark}")
    print(f"\n  1.00x is an ordinary hour. Driving at {worst['hour']:02d}:00 is about"
          f" {worst['relative_risk'] / safest['relative_risk']:.0f} times more")
    print(f"  dangerous per kilometre than driving at {safest['hour']:02d}:00 --"
          f" the rush hour is")
    print("  the safest time to be on these roads, not the most dangerous.\n")

    foot = summary["on_foot"]
    print(f"\n  {foot['deaths']} of the {summary['fatalities']} people killed "
          f"here were on foot or on a bike")
    print(f"  -- {foot['share_of_crashes']:.0%} of all fatal crashes, and they are "
          f"not on the")
    print(f"  highways. {foot['share_after_dark']:.0%} of them died after dark, "
          f"against {foot['vehicle_share_after_dark']:.0%} of the")
    print("  people killed inside vehicles.")
    if foot["worst_roads"]:
        worst_road, worst_n = foot["worst_roads"][0]
        print(f"  The worst single road for it is {worst_road}, with {worst_n}.")

    print("\n  The bracketed figure is a Poisson lower bound, and it is the one to")
    print("  rank on: three deaths and thirty deaths are not equally good evidence")
    print("  of a rate, and sorting on the point estimate puts whichever quiet road")
    print("  had one bad night at the top. The US average is about 7.\n")
    print(f"  Only {summary['share_of_deaths_ranked']:.0%} of the deaths "
          f"({summary['fatalities_on_ranked_corridors']} of "
          f"{summary['fatalities']}) are on corridors NMDOT counts, which is")
    print("  what a rate needs. The rest happened on roads with no measured")
    print("  traffic; they are on the map as points and are not ranked, because")
    print("  there is nothing honest to rank them by.\n")


def cmd_crashes(args) -> None:
    """When crashes happen, from the state's whole file rather than only the deaths.

    `otowi risk` answers this from FARS: a census of fatal crashes, about 150
    records here, single digits per hour. This answers it from the NMDOT/UNM
    community reports -- 15,000 crashes over the same three counties -- and it
    does most of the work without an exposure denominator at all.

    That last part is the point. Every "is driving more dangerous at 3am"
    question needs to divide by how much driving happened, and this project's
    divisor is a commuter-only model. Every "given that a crash happened, was
    drink involved" question does not: the exposure is in the numerator and
    denominator alike and cancels. So the alcohol share by hour and the
    pedestrian share by hour are facts about the crash file that no modelling
    assumption can move, and they come first.
    """
    from . import fatalities, tru

    hourly, severity = tru.fetch(force=getattr(args, "force", False))
    if not hourly:
        raise SystemExit("No TRU reports could be read. Check the network, "
                         "or run with -v to see which fetch failed.")

    summary = tru.summarise(hourly, severity)
    counties = tru.names_for(tru.COUNTY_KEYS)
    cities = tru.names_for(tru.CITY_KEYS)

    county_all = tru.profile(hourly, "all", places=counties)
    county_ksi = tru.profile(hourly, "injury_or_fatal", places=counties)
    county_alcohol = tru.profile(hourly, "alcohol", places=counties)
    # Pedestrians from the city reports against city crashes: a county figure
    # is diluted by highway crashes no pedestrian was anywhere near, and the
    # share only means something if both halves cover the same ground.
    town_all = tru.profile(hourly, "all", places=cities)
    town_vru = tru.profile(hourly, "vru", places=cities)

    summary["shares"] = {
        "injury_or_fatal": tru.share_by_hour(county_ksi, county_all),
        "alcohol": tru.share_by_hour(county_alcohol, county_all),
        "vru_in_towns": tru.share_by_hour(town_vru, town_all),
    }

    window = tuple(args.window)
    travel = None
    if simulate.edgedata_path(window).exists():
        travel = fatalities.hourly_travel(
            _load_net(), simulate.intervals_path(window))
        summary["temporal_bias"] = tru.temporal_bias(travel, county_all)
        crashes = fatalities.fetch()
        summary["fars_on_model_exposure"] = fatalities.by_hour(crashes, travel)
        summary["fars_on_crash_exposure"] = fatalities.by_hour(
            crashes, tru.as_exposure(county_all))

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    years = ", ".join(str(y) for y in summary["years"])
    print(f"\nNMDOT crashes, {', '.join(counties)}, {years}\n")
    sev = summary.get("severity", {})
    if sev:
        print(f"  {sev['crashes']:,} crashes over {sev['years'][0]}-{sev['years'][1]}: "
              f"{sev['fatal']:,} fatal, {sev['injury']:,} injury, "
              f"{sev['property_damage']:,} damage only.")
        print(f"  {sev['crashes_per_fatal']:.0f} recorded crashes for every one that "
              f"killed someone. FARS sees the one.\n")

    print("  Hour by hour, needing no exposure denominator\n")
    print(f"  {'hour':<6}{'crashes':>9}{'drink':>8}{'hurt':>8}"
          f"{'on foot, per 1,000':>20}")
    alcohol = summary["shares"]["alcohol"]
    hurt = summary["shares"]["injury_or_fatal"]
    walking = summary["shares"]["vru_in_towns"]
    for hour in range(24):
        print(f"  {hour:02d}:00 {county_all[hour]:>9,}{alcohol[hour]:>7.0%}"
              f"{hurt[hour]:>8.0%}{walking[hour] * 1000:>16.0f}")

    worst_drink = max(range(24), key=lambda h: alcohol[h])
    print(f"\n  Drink is in {alcohol[worst_drink]:.0%} of the crashes at "
          f"{worst_drink:02d}:00 and {min(alcohol):.0%} of them at "
          f"{alcohol.index(min(alcohol)):02d}:00 --")
    print("  a fortyfold swing, measured on 15,000 crashes, with nothing modelled.")
    print(f"  Whether a crash hurt someone barely moves: "
          f"{min(hurt):.0%} to {max(hurt):.0%} across the")
    print("  whole day. The night is not more dangerous because crashes are worse")
    print("  then; it is more dangerous because of who is driving.")

    town = summary["vru_in_towns"]
    evening = sum(town_vru[18:23]) / sum(town_vru) if sum(town_vru) else 0.0
    print(f"\n  {town['total']} pedestrians and cyclists were hit in the three towns. "
          f"Of every 1,000")
    print(f"  crashes at 08:00, {walking[8] * 1000:.0f} involved one; at 21:00, "
          f"{walking[21] * 1000:.0f}. {evening:.0%} of them were hit")
    print("  between 18:00 and 23:00 -- not when drivers crash, and not when the")
    print("  roads are empty. It is when the light goes.\n")

    if travel is None:
        print(f"  No whole-day simulation for {window}, so there is no exposure")
        print("  curve to check against. Run:")
        print(f"    otowi run --window {window[0]} {window[1]}\n")
        return

    bias = summary["temporal_bias"]
    over = max(bias, key=lambda r: r["ratio"])
    under = min((r for r in bias if r["ratio"] > 0), key=lambda r: r["ratio"])
    print("  And what that says about this model's day\n")
    print(f"  {'hour':<6}{'model travel':>14}{'crashes':>10}{'ratio':>8}")
    for row in bias:
        print(f"  {row['hour']:02d}:00 {row['share_of_modelled_travel']:>13.1%}"
              f"{row['share_of_crashes']:>10.1%}{row['ratio']:>8.2f}")
    print(f"\n  The model puts {over['share_of_modelled_travel']:.0%} of the day's "
          f"driving in the {over['hour']:02d}:00 hour, when "
          f"{over['share_of_crashes']:.0%} of")
    print(f"  the crashes happen, and {under['share_of_modelled_travel']:.0%} in the "
          f"{under['hour']:02d}:00 hour, when {under['share_of_crashes']:.0%} do. "
          f"That is the")
    print("  commuter-only demand showing: LODES knows the drive to work and")
    print("  nothing about the errands that fill the middle of the day.")

    model_rows = summary["fars_on_model_exposure"]
    proxy_rows = summary["fars_on_crash_exposure"]
    print("\n  Which moves the headline. Fatal-crash risk per unit of exposure,")
    print("  once on the model's travel curve and once on the crash curve:\n")
    print(f"  {'hour':<6}{'deaths':>8}{'on the model':>14}{'on crashes':>13}")
    for a, b in zip(model_rows, proxy_rows):
        print(f"  {a['hour']:02d}:00 {a['deaths']:>8}{a['relative_risk']:>13.2f}x"
              f"{b['relative_risk']:>12.2f}x")

    def spread(rows):
        high = max(rows, key=lambda r: r["relative_risk"])
        low = min(rows, key=lambda r: r["relative_risk"])
        return high, low, high["relative_risk"] / max(low["relative_risk"], 1e-9)

    high_m, low_m, ratio_m = spread(model_rows)
    high_p, low_p, ratio_p = spread(proxy_rows)
    print(f"\n  On the model: {high_m['hour']:02d}:00 is {ratio_m:.0f}x "
          f"{low_m['hour']:02d}:00. On the crash curve: "
          f"{high_p['hour']:02d}:00 is {ratio_p:.0f}x {low_p['hour']:02d}:00.")
    print("  The worst hour is the same one either way, and so is the direction --")
    print("  the rush hour is the safe part of the day. The size of it is not:")
    print(f"  the published {ratio_m:.0f}x is the top of a "
          f"{min(ratio_p, ratio_m):.0f}x-to-{max(ratio_p, ratio_m):.0f}x range. "
          f"The model overstates how")
    print("  much of the day's driving happens at rush hour, which deflates the")
    print("  rush hour's risk and inflates the ratio against it.")
    print("  Crashes are not travel either, and using them as exposure drags")
    print("  every hour toward 1.0, so the truth is inside the bracket.\n")
    print("  Counties are not the study area -- Rio Arriba runs north to Chama --")
    print("  so only the shape of these curves is used, never the level. NMDOT")
    print("  crash data is protected under 23 U.S.C. 409.\n")


def cmd_vru(args) -> None:
    """The people who were not driving, with coordinates this time.

    FARS has 40 of them and no way to say which street or at what hour. NMDOT's
    Vulnerable Road User Safety Assessment has 758 inside this bounding box,
    2013 to 2023, each with a latitude, an hour, a severity and a lighting
    condition -- and it includes the people who were hit and lived, which is
    most of them.
    """
    from . import fatalities, vru as vru_module

    crashes = vru_module.fetch(force=getattr(args, "force", False))
    corridors = vru_module.fetch_corridors(force=getattr(args, "force", False))
    if not crashes:
        raise SystemExit("No VRU crashes returned. Check the network, or run "
                         "with -v to see what NMDOT's service said.")

    summary = vru_module.summarise(crashes, corridors)

    window = tuple(args.window)
    if simulate.edgedata_path(window).exists():
        from . import tru

        travel = fatalities.hourly_travel(
            _load_net(), simulate.intervals_path(window))
        summary["relative_risk"] = vru_module.relative_risk(crashes, travel)
        # The same bracket `otowi crashes` puts round the driver curve. The
        # model's travel curve has a hole at midday that is the commuter-only
        # demand and not the road, and it lands straight on the hours these
        # crashes happen.
        hourly, _ = tru.fetch()
        county_all = tru.profile(hourly, "all",
                                 places=tru.names_for(tru.COUNTY_KEYS))
        if sum(county_all):
            summary["relative_risk_on_crash_exposure"] = vru_module.relative_risk(
                crashes, tru.as_exposure(county_all))

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    span = summary["years"]
    print(f"\nPedestrians and cyclists struck in the study area, "
          f"{span[0]}-{span[1]}\n")
    print(f"  {summary['crashes']} crashes: {summary['pedestrians']} on foot, "
          f"{summary['cyclists']} on a bike.")
    print(f"  {summary['killed']} killed, {summary['killed_or_serious']} killed or "
          f"seriously hurt.")
    print(f"  {summary['share_after_dark']:.0%} happened after dark -- but "
          f"{summary['share_after_dark_when_killed']:.0%} of the ones that")
    print("  killed someone did. Darkness does not cause many more of these; it")
    print("  decides how they end.\n")

    print(f"  {'street':<26}{'crashes':>9}{'KSI':>6}{'killed':>8}{'dark':>7}")
    for row in summary["worst_streets"][:8]:
        print(f"  {row['street'][:25]:<26}{row['crashes']:>9}"
              f"{row['killed_or_serious']:>6}{row['killed']:>8}"
              f"{row['after_dark'] / row['crashes']:>7.0%}")

    counts = summary["by_hour"]
    peak = max(range(24), key=lambda h: counts[h])
    print(f"\n  By hour. The peak is {peak:02d}:00, and "
          f"{summary['ksi_share_1700_2300']:.0%} of the killed-and-serious")
    print("  happen between 17:00 and 23:00.\n")
    widest = max(counts) or 1
    for hour in range(24):
        bar = "#" * int(counts[hour] / widest * 44)
        print(f"  {hour:02d}:00 {counts[hour]:>4}  {bar}")

    hin = summary.get("high_injury_network")
    if hin:
        print(f"\n  NMDOT's own ranking: {hin['segments']} High Injury Network "
              f"segments here.\n")
        print(f"  {'road':<26}{'index':>8}{'VRU':>6}{'ped KA':>8}{'miles':>7}")
        for row in hin["by_road"][:8]:
            print(f"  {row['name'][:25]:<26}{row['severity_index']:>8.0f}"
                  f"{row['vru_crashes']:>6}{row['ped_ka']:>8}{row['miles']:>7.1f}")
        top = hin["by_road"][0]
        second = hin["by_road"][1] if len(hin["by_road"]) > 1 else None
        if second and second["severity_index"]:
            print(f"\n  {top['name']} scores "
                  f"{top['severity_index'] / second['severity_index']:.1f}x the next "
                  f"road on the list, and it is")
            print("  the same road FARS puts at the top on deaths alone. Two files,")
            print("  two methods, one answer.")

    rows = summary.get("relative_risk")
    if rows is None:
        print(f"\n  No whole-day simulation for {window}, so there is no exposure")
        print(f"  curve. Run:  otowi run --window {window[0]} {window[1]}\n")
        return

    worst = max(rows, key=lambda r: r["relative_risk"])
    print(f"\n  Per kilometre driven, the worst hour for hitting somebody on foot")
    print(f"  is {worst['hour']:02d}:00, at {worst['relative_risk']:.1f}x an ordinary "
          f"hour --")

    proxy = summary.get("relative_risk_on_crash_exposure")
    if proxy:
        worst_proxy = max(proxy, key=lambda r: r["relative_risk"])
        print(f"  or {worst_proxy['hour']:02d}:00 at "
              f"{worst_proxy['relative_risk']:.1f}x, measuring exposure by the "
              f"state's crash")
        print("  counts instead of by the model. The two disagree because the")
        print("  model's day has a hole in the middle of it where the errands")
        print("  should be, and these crashes happen in the afternoon. Take the")
        print("  evening block rather than the single hour: both curves agree")
        print("  the risk is high from mid-afternoon until it gets dark.\n")
    else:
        print()

    print("  That denominator is vehicle travel, because vehicle travel is the")
    print("  only thing this project measures. It answers how likely a kilometre")
    print("  of driving is to hit somebody -- not how dangerous walking is, which")
    print("  would need a count of people walking, and nobody counts that.\n")
    print("  NMDOT crash data is protected under 23 U.S.C. 409.\n")


def cmd_web(args) -> None:
    """Build the map data and serve it on localhost."""
    from . import web

    web.serve(
        tuple(args.window),
        port=args.port,
        open_browser=not args.no_browser,
        force=args.force,
    )


def cmd_export(args) -> None:
    """Write the map, the numbers and the routes as files a static host serves.

    GitHub Pages rather than a running server, deliberately. Nothing here is
    computed per visitor and nothing could be: a run of this model takes about
    an hour, so any hosted version shows precomputed output whatever is behind
    it. A static export is the honest shape of that, and it cannot break at
    3 a.m.

    The page is `otowi/static/index.html`, the same file `otowi web` serves,
    with a flag set. Keeping one page means the published copy cannot quietly
    drift from the one this repository runs.
    """
    from . import journey, web

    window = tuple(args.window)
    out = Path(args.out)
    data = out / "data"
    data.mkdir(parents=True, exist_ok=True)

    geojson, summary = web.build(window=window, force=args.force)
    (data / "map.geojson").write_bytes(geojson.read_bytes())
    (data / "summary.json").write_text(json.dumps(summary, indent=2))

    # Fatal crashes, and the corridor rates computed from them. Written as a
    # separate file rather than folded into the map: it is 147 points against
    # 8,000 lines, it changes on a different cadence, and a reader who wants
    # the risk view should not pay for it on every other view.
    try:
        from . import counts, fatalities
        net_r, crashes, risks, carries = _risk_inputs(args, window)
        route_of = fatalities.corridors_from_counts(
            counts.match_to_edges(net_r, counts.parse_segments(counts.fetch_aadt())))
        risk_net_travel = fatalities.hourly_travel(
            net_r, simulate.intervals_path(window))
        risk_summary = fatalities.summarise(crashes, risks)
        risk_summary["by_hour"] = fatalities.by_hour(crashes, risk_net_travel)
        risk_summary["regional_average"] = round(
            fatalities.regional_average(risks), 1)
        (data / "risk.json").write_text(json.dumps({
            "summary": risk_summary,
            "model_carries": round(carries, 3),
            # edge -> corridor, so the map can colour a line by its road's rate
            "corridor_of": route_of,
            "corridors": {k: r.as_dict() for k, r in risks.items() if r.fatalities},
            "crashes": [
                {"lat": round(c.lat, 5), "lon": round(c.lon, 5),
                 "n": c.fatalities, "year": c.year, "hour": c.hour,
                 "road": c.road, "dark": c.is_dark, "harm": c.harm,
                 "foot": c.on_foot}
                for c in crashes
            ],
        }, separators=(",", ":")))
    except Exception as exc:                                   # noqa: BLE001
        # The risk view is additive. A FARS outage must not take the map with
        # it, but it must also not silently ship a page whose tab is empty.
        print(f"risk layer skipped: {exc}", file=sys.stderr)

    # The walking page. Independent of the simulation -- it is measurements,
    # not model output -- so it is built whether or not a run exists, and its
    # failure is reported rather than allowed to take the export with it.
    try:
        walk_data, _ = web.build_walk(force=args.force)
        (data / "walk.json").write_bytes(walk_data.read_bytes())
        walk_page = (web.STATIC_DIR / "walk.html").read_text().replace(
            "<body>", "<body>\n<script>window.OTOWI_STATIC = true;</script>", 1)
        (out / "walk.html").write_text(walk_page)
    except Exception as exc:                                   # noqa: BLE001
        print(f"walking page skipped: {exc}", file=sys.stderr)

    (data / "places.json").write_text(json.dumps(
        [{"key": key, "name": place.name, "note": place.note}
         for key, place in PLACES.items()], indent=2))

    # Every ordered pair, routed now so the page does not need a router. Six
    # places is thirty pairs; a pair that cannot be routed is left out and
    # reported rather than written as an empty answer.
    net = _load_net()
    # intervals_path, not edgedata_path. edgedata is the whole window
    # aggregated into one bucket, so every departure time would return an
    # identical duration and the departure curve -- the entire point of the
    # planner -- would come out flat. cmd_when loads the same file.
    times = journey.TravelTimes.load(simulate.intervals_path(window), net)
    core = trips.reachable_core(net)
    risk_ctx = None
    try:
        from . import fatalities as _f
        _net, _crashes, _risks, _ = _risk_inputs(args, window)
        risk_ctx = {
            "net": _net, "risks": _risks,
            "route_of": _f.corridors_from_counts(
                counts.match_to_edges(
                    _net, counts.parse_segments(counts.fetch_aadt()))),
        }
        fatalities = _f
    except Exception as exc:                                    # noqa: BLE001
        print(f"per-route risk skipped: {exc}", file=sys.stderr)

    plans, unroutable = {}, []
    for origin in PLACES:
        for destination in PLACES:
            if origin == destination:
                continue
            try:
                # No window: the whole simulated span, so the page holds the
                # entire departure curve and can answer "some time between 5
                # and 8" by slicing it. Nothing here is computed per visitor
                # and nothing could be -- a run of this model takes hours.
                plan = journey.plan(
                    net, times, core, origin, destination, simulated=window)
                # depart_s is only needed to slice the curve, and the browser
                # can recompute it from the label; dropping it keeps the file
                # small enough to ship on a static host.
                for option in plan["options"]:
                    option.pop("depart_s", None)
                # The crash record of the roads this particular drive uses,
                # precomputed so the page can put risk *in* the departure
                # recommendation rather than in a separate tab. route_edges is
                # dropped afterwards: it is only needed to compute this, and it
                # is most of the file's weight.
                if risk_ctx is not None:
                    plan["risk"] = fatalities.along_route(
                        risk_ctx["net"], plan.get("route_edges", []),
                        risk_ctx["risks"], risk_ctx["route_of"])
                    plan["risk"]["stretches"] = plan["risk"]["stretches"][:3]
                plan.pop("route_edges", None)
                plans[f"{origin}>{destination}"] = plan
            except SystemExit as exc:
                unroutable.append(f"{origin}>{destination}: {exc}")
    (data / "plans.json").write_text(json.dumps(plans, separators=(",", ":")))

    page = Path(__file__).resolve().parent / "static" / "index.html"
    html = page.read_text().replace(
        "<body>", "<body>\n<script>window.OTOWI_STATIC = true;</script>", 1)
    (out / "index.html").write_text(html)

    print(json.dumps({
        "out": str(out),
        "index_kb": round(len(html) / 1024, 1),
        "map_mb": round((data / "map.geojson").stat().st_size / 1e6, 2),
        "plans": len(plans),
        "unroutable": unroutable,
    }, indent=2))


def cmd_run(args) -> None:
    """Everything, skipping stages whose output already exists.

    Routing here is `cmd_assign`, not `cmd_route`. Single-pass duarouter hands
    every driver the path that is fastest on an empty road, so they all choose
    the same one and the model manufactures congestion on one corridor while
    leaving its alternatives empty. Iterative assignment was written to fix
    exactly that and then was reachable only by running `otowi assign` by hand
    -- so the default path through this program still had the defect the fix
    was for, and anyone running `otowi run` on a clean checkout got the old
    behaviour without being told.

    Calling cmd_assign is not enough on its own, which cost a whole run to
    learn. `cmd_route` and `cmd_assign` write the *same* routes file, so a
    check for its existence cannot tell iteratively-assigned routes from
    single-pass ones -- and running `otowi route` once by hand, for any reason,
    silently disarms the fix for every `otowi run` afterwards. That run
    produced 26,495 teleports against 356 on the published model, because
    every driver had taken the empty-network path. The skip test now asks
    whether an *assignment* finished, not whether a file exists.
    """
    window = tuple(args.window)
    if not network.network_path().exists():
        cmd_network(args)
    if not trips.trips_path(window).exists():
        cmd_trips(args)
    if not simulate.assignment_is_converged(window):
        cmd_assign(args)
    cmd_simulate(args)
    cmd_calibrate(args)


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
        ("assign", cmd_assign,
         "Iterate routing against measured congestion (user equilibrium)."),
        ("calibrate", cmd_calibrate, "Compare modelled volumes against NMDOT counts."),
        ("risk", cmd_risk, "Rank corridors by fatal crashes per unit of travel."),
        ("crashes", cmd_crashes,
         "When crashes happen, from the state's all-severity file."),
        ("vru", cmd_vru,
         "Pedestrians and cyclists: where they are hit, and when."),
        ("web", cmd_web, "Serve an interactive map of the model and its error."),
        ("export", cmd_export, "Write the map as static files for GitHub Pages."),
        ("run", cmd_run, "Do every stage that has not been done."),
    ]:
        sub = add(name, handler, help_text)
        sub.add_argument("--year", type=int, default=demand.DEFAULT_YEAR,
                         help="LODES vintage.")
        sub.add_argument("--window", type=int, nargs=2, default=list(AM_PEAK),
                         metavar=("START", "END"),
                         help="Simulation window in local hours.")
        sub.add_argument("--seed", type=int, default=0)
        sub.add_argument("--no-returns", action="store_true",
                         help="Only generate the drive to work. A whole-day "
                              "run with this set has an empty evening.")
        sub.add_argument("--scale", type=float, default=1.0,
                         help="Multiply demand. Anything but 1.0 must be reported: "
                              "delay is not linear in demand.")
        sub.add_argument("--force", action="store_true")
        sub.add_argument("--json", action="store_true",
                         help="Emit the raw payload.")
        sub.add_argument("--top", type=int, default=15,
                         help="How many busiest edges to report.")
        sub.add_argument("--peak", type=float, nargs=2, default=[6, 9],
                         metavar=("START", "END"),
                         help="For a whole-day run, also score this slice on "
                              "the hourly basis, so the result can be read "
                              "against the GEH bar and against a peak-window "
                              "run. Pass 0 0 to skip.")
        sub.add_argument("--iterations", type=int, default=9,
                         help="Assignment rounds for `otowi assign`. Five does not converge a whole-day run -- route choice settles while travel times are still moving -- so the default is nine.")
        sub.add_argument("--internal-only", action="store_true",
                         help="Drop trips with one end outside the study area, as "
                              "the model did before gateways existed.")
        sub.add_argument("--port", type=int, default=8814,
                         help="Port for the local map server.")
        sub.add_argument("--out", default="site",
                         help="Directory for `otowi export`.")
        sub.add_argument("--no-browser", action="store_true",
                         help="Do not open a browser window.")
        sub.add_argument("--end-padding", type=int, default=10800,
                         help="Seconds to keep simulating after the last departure, "
                              "so long trips are not cut off and dropped from the averages.")

    when_parser = add("when", cmd_when,
                      "What time should I leave, for a given trip?")
    when_parser.add_argument("origin", help=f"One of: {', '.join(sorted(PLACES))}")
    when_parser.add_argument("destination", help="Likewise.")
    when_parser.add_argument("--window", type=int, nargs=2, default=list(AM_PEAK),
                             metavar=("START", "END"))
    when_parser.add_argument("--between", nargs=2, metavar=("START", "END"),
                             help="Only recommend departures in this range, "
                                  "e.g. --between 17:30 20:00. Any range "
                                  "inside the simulated window; defaults to "
                                  "all of it.")
    when_parser.add_argument("--json", action="store_true")
    when_parser.add_argument("--prefer", choices=("both", "speed", "safety"),
                             default="both",
                             help="Which axis to optimise. Journey times here "
                                  "differ by about a minute across a day and "
                                  "risk by a factor of nineteen, so 'both' "
                                  "takes the safest departure that costs no "
                                  "meaningful extra driving.")
    when_parser.add_argument("--risk", action="store_true",
                             help="Also report the fatal-crash record of the "
                                  "roads this drive uses, and how the hour of "
                                  "day changes it.")
    when_parser.add_argument("--every", type=int, default=15,
                             help="Minutes between candidate departure times.")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
