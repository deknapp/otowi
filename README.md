# otowi

[![tests](https://github.com/deknapp/otowi/actions/workflows/tests.yml/badge.svg)](https://github.com/deknapp/otowi/actions/workflows/tests.yml)

Traffic simulation for northern New Mexico: Santa Fe, the Los Alamos commute
over Otowi Bridge, and US-84 north to Abiquiu Lake.

Named for the bridge. NM-502 crosses the Rio Grande at Otowi and climbs to the
mesa, and there is no redundant route — which is why a small change in demand
produces a large change in delay, and why the commute is worth simulating
rather than estimating.

> **Status: it runs end to end, it is validated against real counts, and it
> fails them.** `otowi run` goes from an empty checkout to a finished morning
> peak compared against 2,152 NMDOT-measured links. On held-out segments the
> median GEH is 5.92 and **the model carries 16% of measured peak-hour volume**.
> That number is reported rather than tuned away, because it is not a tuning
> error — see [What the calibration says](#what-the-calibration-says).

![The modelled morning peak](docs/map-modelled.png)

*`otowi web` — the modelled 06:00–09:00 peak. The bright corridor is US-84/285
north out of Santa Fe, branching west over Otowi Bridge to Los Alamos. Both
map images on this page were captured before the gateway and assignment
changes and show the 8% model; `otowi web` regenerates them from the current
run.*

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
- **Validation comes from NMDOT's published counts.** Their
  [AADT layer](https://services.arcgis.com/hOpd7wfnKm16p9D9/arcgis/rest/services/Annual_Average_Daily_Traffic_2026/FeatureServer)
  gives annual average daily traffic per highway segment with the K and D
  factors needed to turn it into a directional peak hour — 7,149 segments
  intersect the study area, with 2025 counts. It is open, and no key or request
  form is needed, despite the Data Management Bureau's page offering only an
  emailed PDF request form.
  The [Santa Fe MPO](https://santafempo.org/resources/traffic-counts/)'s 17
  permanent stations are **not** wired in yet; they would add in-town coverage
  where NMDOT's state-highway focus is thinnest.
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
otowi calibrate   # compare against NMDOT counts                (~1 min)
otowi web         # interactive map on localhost                (instant)

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

## What the calibration says

`otowi calibrate` compares modelled edge volumes against NMDOT's published
AADT, converted to a directional peak hour with the K and D factors NMDOT
publishes alongside it. 7,149 count segments intersect the study area; 4,904
match an edge; 2,152 of those carry modelled traffic to compare.

On the held-out half of the segments — split by a hash of the station ID, so it
is the same split on every run and cannot be reshuffled until it flatters:

| | held out | the other half |
|---|---|---|
| Links | 1,058 | 1,094 |
| Median GEH | 5.92 | 5.93 |
| Links with GEH < 5 | 37.8% | 36.8% |
| **Median modelled ÷ observed** | **0.163** | 0.157 |

The conventional bar is 85% of links under GEH 5. This is nowhere near it.

The two columns agree to within half a percent, which is the least interesting
result here and the one most worth stating: nothing was fitted, so there was
nothing to overfit. The held-out half exists to make that checkable rather than
asserted.

![Where the model is missing traffic](docs/map-ratio.png)

*The ratio layer. Red is where the model carries far less than NMDOT measured;
grey would be a match. Almost nothing is grey.*

**The 16% is the finding, and no constant will fix it.** The model contains
commuting and nothing else: no freight, no shopping, no school runs, no
tourism. Those are absent by construction, not by accident, and they are most
of what is on the road at 07:30.

The shortfall is close to flat across the size of the road, which was not true
before trips with one end outside the study area were added back. On links
measuring 1,000 veh/h or more the median ratio is 0.151; on links under
300 veh/h it is 0.159. A model that was missing *highway* traffic specifically
would not look like that.

The exception is the through corridor, and it is the exception that makes
sense. I-25 sits at 0.054 against the 0.16 overall, because almost everything
on it is going to Albuquerque or beyond — past the study area rather than into
it. NM-14, which is a Santa Fe commuter road, is at 0.361. That ordering is a
map of what to build next, which is what a calibration is supposed to
produce.

### What is wrong with the current numbers, specifically

The pipeline runs. That is not the same as the answers being right, and these
are the three reasons no travel time from it is quoted here:

1. **The model carries 16% of measured volume**, for the structural reason
   above. Travel times from a network loaded to a sixth of reality are not the
   travel times of that network.
2. **The network is over-saturated even at 16% of real demand.** Routing is
   now iterative — `otowi run` reaches equilibrium rather than handing every
   driver the same free-flow path — and the run still needed **2,635 jam
   teleports**, SUMO's rescue for a deadlocked vehicle, and still had 7.0% of
   vehicles unfinished when the clock stopped. Mean time loss is 31 minutes per
   vehicle and the 90th percentile trip takes 2.6 hours, against a median of
   25 minutes. A quarter-hour commute and a two-and-a-half-hour tail cannot
   both be right. The plausible causes are junction control and lane
   connectivity at the interchanges, not demand, since there is six times less
   demand here than reality carries without deadlocking.
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
