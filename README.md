# otowi

[![tests](https://github.com/deknapp/otowi/actions/workflows/tests.yml/badge.svg)](https://github.com/deknapp/otowi/actions/workflows/tests.yml)

Traffic simulation for northern New Mexico: Santa Fe, the Los Alamos commute
over Otowi Bridge, and US-84 north to Abiquiu Lake.

Named for the bridge. NM-502 crosses the Rio Grande at Otowi and climbs to the
mesa, and there is no redundant route — which is why a small change in demand
produces a large change in delay, and why the commute is worth simulating
rather than estimating.

> **Status: it runs end to end, and it is not calibrated.** Network, demand,
> departure times, routing and simulation are all built — `otowi run` goes from
> an empty checkout to a finished morning peak. What is missing is the part
> that would make the output believable: no edge volume has yet been compared
> against a count station, so the travel times are the model's opinion with no
> error bar. See [Running it](#running-it) and [Honesty](#honesty).

## What it is for

Two questions, one model:

1. **The Los Alamos commute.** Roughly the same population drives the same
   corridor at the same time every weekday into a destination with clustered
   start times and one river crossing. That is a textbook tidal-flow problem
   with a hard bottleneck, and it is the daily reality for thousands of people.
2. **Getting across Santa Fe.** Cerrillos, St. Francis, Old Pecos Trail,
   St. Michael's. Less dramatic, more useful: when to leave, and which way.

## The part that matters

Most traffic simulations built outside of transportation agencies are a road
network from OpenStreetMap plus randomly generated trips. They animate
beautifully and mean nothing, because the demand is invented.

The demand here is not invented, and the model is not trusted on its own word:

- **Origin–destination flows come from [LEHD LODES](https://lehd.ces.census.gov/data/)**
  (LODES8, 2022 vintage) — census-block-level counts of where workers live and
  where they work, built from unemployment-insurance wage records covering
  roughly 95% of private employment. LODES lags by a couple of years, so this
  is not a current-conditions model and the provenance says which year it is.
- **Departure times come from [ACS table B08302](https://data.census.gov/table/ACSDT5Y2022.B08302)**,
  the survey question asking what time people left home for work. LODES has no
  time in it at all, and a fabricated departure curve would move every
  congestion number while looking entirely plausible.
- **Calibration and validation come from real count stations.** The
  [Santa Fe MPO](https://santafempo.org/resources/traffic-counts/) operates 17
  permanent stations inside its planning area, and the
  [NMDOT Data Management Bureau](https://www.dot.nm.gov/planning-research-multimodal-and-safety/planning-division/data-management-bureau/)
  publishes volume, classification, and speed for the state highway segments
  beyond it.
- **Validation is against held-out stations.** The model is fit on some of them
  and its error is reported on the rest. A simulation that reports its own
  error against ground truth it did not see is a different object from one that
  simply runs.

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[fast,dev]'
```

That installs the SUMO toolchain from PyPI — `netconvert`, `duarouter` and
`sumo` land in `.venv/bin`, so there is no Homebrew tap to trust and no
XQuartz needed for the command-line tools.

Then, in order. Each stage writes to `data/cache/` and is skipped if its output
is already there, so re-running is cheap.

```bash
otowi places      # the study area and corridors — checks nothing has to download
otowi network     # OpenStreetMap → SUMO network        (~5 min, once, ever)
otowi demand      # LODES + ACS, reported without simulating   (~1 min)
otowi trips       # commute flows → individual vehicles         (~30 s)
otowi route       # duarouter: trips → paths                    (~30 s)
otowi simulate    # SUMO: the microscopic run                   (~5 min)

otowi run         # all of the above, skipping what is done
```

`otowi network` is the slow one and it hits Overpass, a donated service. It
happens once; the extract is cached forever.

### What a run currently produces

On the 06:00–09:00 window, LODES 2022:

| | |
|---|---|
| Census blocks in the study area | 5,372 |
| Commute pairs with both ends inside it | 39,697 |
| Workers on those pairs | 52,052 |
| Blocks attached to a routable road | 5,076 (94.5%, median 98 m) |
| Vehicles generated | 28,442 |
| Trips lost in routing | 0 |

Useful flags:

- `--scale 0.1` runs a tenth of the demand. Much faster, and **it does not
  reproduce congestion** — delay is not linear in demand, which is the whole
  reason this corridor is worth simulating. Use it for development, never for
  a result.
- `--window 15 18` runs the afternoon peak instead.
- `--seed N` — trip generation is Poisson, so a different seed is a different
  morning.
- `--end-padding N` — seconds to keep simulating after the last departure.

## Built on

[Eclipse SUMO](https://eclipse.dev/sumo/) — microscopic, open source (EPL-2.0),
maintained by the German Aerospace Center. SUMO supplies the traffic model and
the network format. Everything specific to northern New Mexico — the study
area, the demand, the calibration, the reporting — lives in this repository.

## Study area

| | |
|---|---|
| **Santa Fe** | the southern anchor and the in-town network |
| **Pojoaque** | where Los Alamos traffic leaves US-84/285 onto NM-502 |
| **Otowi Bridge** | the Rio Grande crossing, then the climb to the mesa |
| **Los Alamos** | the destination that makes the morning peak tidal |
| **Española** | US-84/285 splits toward Abiquiu and Taos |
| **Abiquiu Lake** | weekend recreational demand — a different regime entirely |

Roughly 65 km east–west by 83 km north–south. Simulation is scoped to the
morning and afternoon peak windows rather than a full day: a 24-hour
microscopic run over this area is mostly empty and the questions worth asking
do not live in it.

## Honesty

Things this will say about itself, and keep saying:

- **A calibrated model is still a model.** It reproduces count-station volumes
  within a stated error. That is not the same as predicting your Tuesday.
- **LODES is commute flows, and not all traffic is commuting.** Recreational
  travel to Abiquiu, freight, and tourism are not in it, and where they matter
  they will be estimated separately and labelled as estimates.
- **Uncalibrated output is not a result.** Until the validation error is
  reported, this repository will say so at the top.

### What is wrong with the current numbers, specifically

The pipeline runs. That is not the same as the answers being right, and these
are the three reasons no travel time from it is quoted here:

1. **Nothing is calibrated.** No edge volume has been compared against an MPO
   or NMDOT count station. This is the next piece of work and the one that
   turns output into a result.
2. **Routing is single-pass on free-flow times.** `duarouter` gives every
   driver the path that would be fastest on an empty road, so all of them
   choose the same one and the busiest corridors are overloaded in a way real
   drivers avoid by spreading out. The fix is iterative assignment
   (`duaIterate`), which is not wired up yet. Until it is, congestion on the
   single best path is overstated and congestion on the alternatives is
   understated.
3. **Some vehicles do not finish.** Any that are still travelling when the
   clock stops are absent from every average, which biases travel times
   *downward* exactly where the network is worst. The run reports
   `vehicles_unfinished` for this reason rather than leaving it to be inferred
   from a missing row.

Two bugs found by building this are worth recording, because both reported
success while being wrong:

- **netconvert built 911 edges from 11,710 ways** and called it a success — no
  I-25, no US-84/285, no traffic lights — because the tile merge interleaved
  nodes and ways. Fixed; the network is now 33,138 usable edges.
- **duarouter silently discarded 30% of trips.** `--ignore-errors` turns an
  unroutable trip into a warning nobody reads, and the missing vehicles looked
  exactly like congestion. The cause was that only 23,209 of 33,138 edges are
  mutually reachable — a bounding box severs frontage roads and one-way stubs —
  and trips were being attached to fragments. Trip endpoints are now restricted
  to the largest strongly-connected component, and every run compares input
  trips against output routes so this cannot be silent again.

## License

MIT
