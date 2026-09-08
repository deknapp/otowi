# otowi

[![tests](https://github.com/deknapp/otowi/actions/workflows/tests.yml/badge.svg)](https://github.com/deknapp/otowi/actions/workflows/tests.yml)

Traffic simulation for northern New Mexico: Santa Fe, the Los Alamos commute
over Otowi Bridge, and US-84 north to Abiquiu Lake.

Named for the bridge. NM-502 crosses the Rio Grande at Otowi and climbs to the
mesa, and there is no redundant route — which is why a small change in demand
produces a large change in delay, and why the commute is worth simulating
rather than estimating.

> **Status: it runs end to end, it is validated against real counts, and it
> fails them.** `otowi run --window 0 24` goes from an empty checkout to a
> converged 24-hour model compared against 2,567 NMDOT-measured links. On
> held-out segments **the model carries 14% of measured daily volume**. That
> number is reported rather than tuned away, because it is not a tuning error —
> see [What the calibration says](#what-the-calibration-says).
>
> It used to say 16%. That figure was inflated: trip generation placed every
> commuter's whole-day vehicle inside the 06:00–09:00 window, when 70.5% of
> measured departures happen then. The correction is in the numbers below.

![The modelled morning peak](docs/map-modelled.png)

*`otowi web` — the bright corridor is US-84/285 north out of Santa Fe,
branching west over Otowi Bridge to Los Alamos. Both map images on this page
were captured from an earlier run and are out of date; `otowi web` regenerates
them from the current one.*

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

On the whole day (`--window 0 24`), LODES 2022:

| | |
|---|---|
| Census blocks in the study area | 5,372 |
| Commute pairs touching it | 85,717 (39,697 internal) |
| Workers on those pairs | 99,580 |
| Blocks attached to a routable road | 4,953 (92.2%, median 95 m) |
| Vehicles generated | 107,901 (53,952 to work, 53,949 home) |
| Trips lost in routing | 0 |
| Assignment | converged at round 9 of 9 |
| Jam teleports | 166 |

Every vehicle appears twice because every commuter drives home. LODES measures
home-to-work flows and nothing else, so a model built from it alone has a
morning peak and fifteen empty hours; see
[the drive home](#the-drive-home-is-an-assumption).

Useful flags:

- `--scale 0.1` runs a tenth of the demand. Much faster, and **it does not
  reproduce congestion** — delay is not linear in demand, which is the whole
  reason this corridor is worth simulating. Use it for development, never for
  a result.
- `--window 0 24` runs a whole day; `--window 15 18` the afternoon peak. Any
  window works, and trips are kept or dropped by whether their departure falls
  inside it rather than by squeezing the day into it.
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
AADT. 7,149 count segments intersect the study area; 4,775 match an edge; 2,567
of those carry modelled traffic to compare.

**Which AADT figure depends on the window, and it is not a detail.** A
peak-window run is scored against `AADT × K × D`, the directional design-hour
volume NMDOT's own factors produce. A whole-day run is scored against `AADT / 2`
— the measured daily total, halved because AADT is two-way and the split really
is close to even over a day, by conservation. That drops both K and D, which
are factors published *alongside* the count rather than the count itself, so a
24-hour model is checked against the measurement instead of something derived
from it.

On the held-out half of the segments — split by a hash of the station ID, so it
is the same split on every run and cannot be reshuffled until it flatters:

| | held out | the other half |
|---|---|---|
| Links | 1,253 | 1,314 |
| **Median modelled ÷ observed** | **0.142** | 0.139 |
| Median GEH | 16.14 | 16.45 |

The two columns agree to within a third of a percent, which is the least
interesting result here and the one most worth stating: nothing was fitted, so
there was nothing to overfit. The held-out half exists to make that checkable
rather than asserted.

**GEH does not mean what it usually means in that table, and the report says
so.** GEH is defined for hourly flows, and the conventional bar — 85% of links
under 5 — is calibrated for them. The statistic scales with the size of the
numbers: multiply modelled and observed by *k* and GEH scales by √*k*. Daily
volumes here are about eight times hourly ones, so the *same model* scores
roughly 2.9× worse on a daily basis without having changed. Printing "5.6% of
links under GEH 5" next to that bar would be a units error dressed as a failing
grade, so a whole-day comparison omits the figure and says why.

The way to get a number that can be read against the bar is to score the
morning out of the whole-day run, which `otowi calibrate` does automatically:

| scored on | links | median GEH | GEH < 5 | ratio |
|---|---|---|---|---|
| this run, 06:00–09:00 slice | 1,108 | **5.93** | 35.6% | 0.130 |
| the previous morning-only model | 1,234 | 5.92 | 37.8% | 0.163 |
| this run, whole day | 1,253 | 16.14 | *n/a* | 0.142 |

Per-link accuracy is unchanged — 5.93 against 5.92 — while the model now covers
24 hours rather than three and carries an honest vehicle count instead of one
inflated 1.42× by placing every commuter in the morning peak. The lower ratio
is that correction showing up where it should. **It is still nowhere near the
85% bar.**

![Where the model is missing traffic](docs/map-ratio.png)

*The ratio layer. Red is where the model carries far less than NMDOT measured;
grey would be a match. Almost nothing is grey.*

**The 14% is the finding, and no constant will fix it.** The model contains
commuting and nothing else: no freight, no shopping, no school runs, no
tourism. Those are absent by construction, not by accident, and they are most
of what is on the road at 07:30 — and even more of it at 11:00.

The shortfall has only a shallow gradient in road size. On the busiest measured
links the median ratio is 0.171 and on the quietest it is 0.134; on the morning
slice, 0.183 against 0.126. A model missing *highway* traffic specifically
would look the other way round, and one missing local traffic would show a much
steeper slope than this.

The ordering by corridor is the useful part. I-25 sits at 0.126, because almost
everything on it is going to Albuquerque or beyond — past the study area rather
than into it. NM-14, a Santa Fe commuter road, is at 0.672. The model is
closest to reality exactly where the traffic is commuting and furthest where it
is through traffic, which is what a commute-only model should look like, and it
is a map of what to build next.

### What the model says about the commute

This is the question the project exists for, and until 2026-09-08 the model
could not answer it: the Santa Fe to Los Alamos trip came out at a median of
116 minutes for a 63 km drive that really takes about 45. The cause was not the
highway. LODES reports jobs by census block, the laboratory sits in a couple of
blocks, and each block was attached to exactly one edge -- so 6,080 of the
9,387 vehicles arriving in Los Alamos were delivered to 6th Street, a
residential street. It deadlocked and the queue propagated back down East Road
and Diamond Drive onto NM-502. Arrivals are now spread across the roads that
actually serve the site, weighted by capacity.

`otowi when santa_fe los_alamos --window 0 24`, on the converged assignment:

| leave | duration | | leave | duration |
|---|---|---|---|---|
| 00:00 | 36.3 min | | 12:00 | 38.6 min |
| 04:00 | 37.4 min | | 16:00 | 39.2 min |
| 08:00 | **41.5 min** | | 20:00 | 39.0 min |

**The spread is 5.2 minutes across a whole day.** That is the answer worth
having, and it is not the one a commute model is expected to give: on this
corridor, at this demand, when you leave barely matters. A tool that only ever
emitted a single recommended departure could not say that, which is why the
planner reports every option and the gap between best and worst rather than a
recommendation.

Treat the durations as optimistic — see the coverage figure above — and the
shape of the curve as the more trustworthy part.

### Asking about a time of day

The planner takes a range, so the question can be the one you actually have.
Someone working a hospital shift in Española and driving from Santa Fe does not
want to know about the morning peak:

```console
$ otowi when santa_fe espanola --window 0 24 --between 17:00 23:00

  leave 17:00   30.9 min   arrive 17:30
  leave 19:00   30.3 min   arrive 19:30
  leave 22:00   29.9 min   arrive 22:29

  Leave any time between 17:00 and 22:00 -- within 1.5 min of the best
  the model can tell apart.
  Spread: 1.0 min between best and worst
```

The recommendation is a **band**, never a minute. This model carries a fraction
of real traffic, so every duration is optimistic; the shape survives that
better than the absolute numbers do, but not well enough to distinguish 06:15
from 06:30 when they differ by forty seconds. Claiming otherwise would be
inventing precision.

The whole 24-hour curve is computed once and shipped in `plans.json` — 96
departure times for each of 30 origin–destination pairs, 180 KB — so the
dashboard answers any range by slicing what is already in the browser. Nothing
is computed per visitor and nothing could be: a run of this model takes hours.

### The drive home is an assumption

LODES measures home-to-work flows and nothing else. Every vehicle it produces
is driving *to* work, so a 24-hour run built from it alone gives a morning peak
and fifteen empty hours. The return trip has to be generated, and no free
source crosses return time with an origin–destination pair — ACS publishes
departure time to work (B08302) and travel time to work (B08303), and nothing
at all about the way back.

So each vehicle's return departs a sampled **time away from home** after its
outbound departure: mean 9.6 h, sd 1.8 h, clipped to 4–14 h. Folding the
commute into that quantity is deliberate — one assumption instead of two, and
the outbound duration is a model output that is not known when trips are
generated.

**This is the largest unmeasured input in a whole-day run, and the timing of
the evening peak is almost entirely determined by it.** It lives in
`config.TIME_AWAY_*` as a constant that can be varied and reported as a
sensitivity, rather than buried in a function, because that is the only
defensible way to use a number nobody measured.

Two further limits on the evening specifically. ACS's departure bins are half
an hour wide through the morning and then 12:00–16:00 and 16:00–24:00 — four
and eight hours — so an evening departure is placed in roughly the right part
of the day and smeared flat within it. And the question asks what time you
*usually* leave in a typical week, which under-represents shift work by
construction. Both push the modelled evening toward being flatter than reality.

### Convergence

Assignment iterates until routes and travel times agree. Route shift is the
share of drivers who changed path since the previous round:

| round | unfinished | mean duration | mean delay | route shift | jam teleports |
|---|---|---|---|---|---|
| 1 | 11.2% | 63 min | 45 min | — | 17,218 |
| 2 | 0.0% | 41 min | 24 min | 42.3% | 6,209 |
| 3 | 0.0% | 24 min | 6 min | 21.9% | 752 |
| 5 | 0.0% | 22 min | 4 min | 8.9% | 223 |
| 7 | 0.0% | 21 min | 3 min | 6.4% | — |
| 9 | 0.0% | 21 min | 3 min | 5.3% | 166 |

Round 1 is single-pass free-flow routing — every driver handed the same
fastest-on-an-empty-road path — so it is the worst case by construction rather
than a result, and its 17,218 jam teleports are what that costs. The
assignment is the thing that removes them.

**Five rounds was not enough and the tool said so.** At round 5 route choice
had settled to 8.9% but mean time loss was still moving 6.4% between rounds,
and the run reported `Not converged`. Route choice settling while travel times
have not is not an equilibrium — it means drivers have stopped switching but
the network they are switching on is still changing under them. Nine rounds
gets both under the threshold: 5.3% and 1.9%.

A run whose route shift is still large, *or whose time loss is still moving*,
has not converged and its travel times are not worth quoting. The planner
refuses to be interesting about a model in that state, and it should.

### What is wrong with the current numbers, specifically

The pipeline runs and converges. These are the reasons to read its travel times
with care:

1. **The model carries 14% of measured volume**, for the structural reason
   above. A network loaded to a sixth of reality is a network with less
   competition for space than the real one, so every duration here is
   optimistic. This is the honest limit on the whole model and no amount of
   assignment fixes it.
2. **A small amount of deadlock remains.** The converged run needed 166 jam
   teleports — SUMO's rescue for a stuck vehicle — for 107,901 vehicles, down
   from 17,218 on the free-flow first round. Small enough not to distort the
   averages, not small enough to call the network right.
3. **The evening's shape rests on an assumption**, not a measurement. See
   [the drive home](#the-drive-home-is-an-assumption). The morning is measured;
   the evening is inferred from it.
4. **Every vehicle finishes**, which is worth stating because it did not
   always: `vehicles_unfinished` is reported rather than left to be inferred
   from a missing row, and unfinished trips bias travel times *downward*
   exactly where the network is worst.

Five bugs found by building this are worth recording, because every one of them
reported success while being wrong. That is the failure mode this project keeps
producing, and the reason so much of the code asserts its own output:

- **netconvert built 911 edges from 11,710 ways** and called it a success — no
  I-25, no US-84/285, no traffic lights — because the tile merge interleaved
  nodes and ways. Fixed; the network is now 31,909 usable edges.
- **duarouter silently discarded 30% of trips.** `--ignore-errors` turns an
  unroutable trip into a warning nobody reads, and the missing vehicles looked
  exactly like congestion. The cause was that only 22,782 of 31,909 edges are
  mutually reachable — a bounding box severs frontage roads and one-way stubs —
  and trips were being attached to fragments. Trip endpoints are now restricted
  to the largest strongly-connected component, and every run compares input
  trips against output routes so this cannot be silent again.
- **The morning peak was 1.42× too big.** The departure profile was
  renormalised to the simulation window while the vehicle count stayed a whole
  day's, so 100% of a flow's drivers departed between 06:00 and 09:00 when
  70.5% of measured departures do. No output could reveal it: the vehicle total
  was exactly the number it was meant to be, and the shape within the window
  was right. The window is now a filter over a whole-day profile.
- **The map drew a road that does not exist.** FR 289 Dome Road is a dirt
  Forest Service track over the Jemez, tagged `residential` with
  `surface=dirt`, one segment `4wd_only=yes` and another `access=private`.
  netconvert reads none of that, gave it a lane at 50 km/h, and the assignment
  found a Cochiti-to-Los Alamos shortcut of 55 km against the ~97 km the drive
  really takes — carrying 400 to 835 vehicles an hour over a washed-out track.
  It was the *only* path between the two in the network, because the road that
  actually serves Cochiti crosses the southern edge of the bounding box.
  Unsealed *and* restricted ways are now dropped; Cochiti drops out with them,
  which is the truth.
- **Return trips went the wrong way through gateways.** An entry gateway is an
  edge with no incoming edges, so nothing can arrive there; an exit gateway has
  no outgoing edges, so nothing placed on it can move. The drive home reused
  whichever gateway the outbound leg had chosen, and duarouter discarded a
  quarter of the demand without an error.
- **`otowi run` skipped the assignment whenever a routes file existed** — and
  `otowi route` writes the same filename as `otowi assign`. So running the
  single-pass router once by hand, for any reason, silently disarmed iterative
  assignment for every run afterwards: 26,495 teleports against 356, because
  every driver held the empty-network path. The check now asks whether an
  assignment *finished*, using the alternatives file that only the assignment
  writes.

## License

MIT
