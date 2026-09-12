"""Numbers read off the bars of a chart, and the two ways that went wrong.

The state's crash counts reach the public as a PDF of bar charts. The values
wanted are the data labels printed above the bars, which is fine -- each label
sits over its own bar, so sorting the numeric words in the plot area by x
position puts them back in hour order. Everything that is hard about it is at
the edges of that plot area, and both edges bit:

* **The left edge.** The y-axis tick labels are numbers too, and they are the
  only other numbers in the frame. They are excluded by being left of where the
  hour labels start -- but the chart's footnote starts further left still, and
  taking *its* margin as the plot edge quietly readmits every tick as a bar.
* **The bottom edge.** The footnote is the missing-hour count, and it is the
  only thing that makes the parse checkable: bars plus missing has to equal the
  year's total in the report's own Table 1. Stopping the band at the footnote
  to keep it out of the bars threw away the check instead.

The guard against both is not the tests here but `tru.check`, which compares
every chart to the table on page 3 of the same report. What the tests pin is
that a short read is a dropped series rather than a shifted one: a chart read
as 23 bars would move every hour after the gap into its neighbour, and the
totals would still agree.
"""

from __future__ import annotations

import pytest

from otowi import tru
from otowi.tru import HourlyCrashes, SeverityYear


def word(text: str, x0: float, top: float) -> dict:
    """One word as pdfplumber reports it. Heights are uniform in these reports."""
    return {"text": text, "x0": x0, "x1": x0 + 6.0 * len(text),
            "top": top, "bottom": top + 8.0}


def chart(values: list[int], *, missing: int = 0, ticks=(24, 16, 8, 0)) -> list[dict]:
    """A by-hour chart laid out the way the reports lay one out.

    Bars across the plot from x=80 to x=540, a tick column at x=54 to the left
    of them, the hour axis underneath, and the footnote below that at the page
    margin -- which is further left than anything else on the page.
    """
    words: list[dict] = []
    for index, tick in enumerate(ticks):
        words.append(word(str(tick), 54.0, 474.0 + index * 62.0))
    for index, value in enumerate(values):
        # Taller bars put their label higher up the page; the parse must not
        # depend on that, only on x.
        words.append(word(str(value), 81.0 + index * 19.7, 660.0 - value * 4.0))
    for index in range(0, 24, 2):
        hour = 12 if index % 12 == 0 else index % 12
        words.append(word(str(hour), 78.0 + index * 19.7, 674.4))
        words.append(word("a.m." if index < 12 else "p.m.", 92.0 + index * 19.7, 674.4))
    words.append(word(f"* In 2021, Santa Fe County had {missing} crashes for which "
                      f"hour data were missing.", 36.0, 690.0))
    return words


class TestReadingTheBars:
    def test_hours_come_back_in_x_order_not_page_order(self):
        values = list(range(24))
        counts, _ = tru._chart_values(chart(values), top=400.0, bottom=792.0)
        assert counts == values

    def test_the_y_axis_ticks_are_not_bars(self):
        # 24 bars plus four tick labels. The ticks are at x=54, left of the
        # first hour label; the footnote below is at x=36, left of everything.
        counts, _ = tru._chart_values(chart([1] * 24), top=400.0, bottom=792.0)
        assert counts == [1] * 24

    def test_the_footnote_is_read_not_excluded(self):
        _, missing = tru._chart_values(chart([1] * 24, missing=2),
                                       top=400.0, bottom=792.0)
        assert missing == 2

    def test_a_chart_with_no_hour_axis_yields_nothing(self):
        words = [word("42", 100.0, 500.0), word("17", 200.0, 500.0)]
        assert tru._chart_values(words, top=400.0, bottom=792.0) == ([], 0)


class TestCheckingAgainstTheReportsOwnTable:
    def table(self, **kw) -> SeverityYear:
        base = dict(place="Santa Fe County", scope="county", year=2021,
                    fatal=19, injury=925, property_damage=1590, total=2534,
                    alcohol_fatal=6, alcohol_injury=58,
                    alcohol_property_damage=68, alcohol_total=132)
        base.update(kw)
        return SeverityYear(**base)

    def series(self, counts, **kw) -> HourlyCrashes:
        base = dict(place="Santa Fe County", scope="county", year=2021,
                    kind="all", counts=counts, missing=0)
        base.update(kw)
        return HourlyCrashes(**base)

    def test_bars_plus_missing_must_equal_the_table(self):
        # The real 2021 Santa Fe County chart: 2,532 on the bars, 2 hours
        # unrecorded, 2,534 in Table 1.
        bars = [0] * 23 + [2532]
        assert tru.check([self.series(bars, missing=2)], [self.table()]) == []

    def test_a_dropped_hour_is_reported(self):
        bars = [0] * 23 + [2500]
        problems = tru.check([self.series(bars, missing=2)], [self.table()])
        assert len(problems) == 1 and "2502" in problems[0]

    def test_fatal_and_injury_is_checked_against_fatal_plus_injury(self):
        bars = [0] * 23 + [944]
        assert tru.check([self.series(bars, kind="injury_or_fatal")],
                         [self.table()]) == []

    def test_a_series_with_no_table_row_is_not_a_problem(self):
        # Pedestrian and DWI charts have nothing in Table 1 to check against.
        assert tru.check([self.series([1] * 24, kind="vru")], [self.table()]) == []


class TestCombining:
    def series(self, place, kind, counts, year=2021) -> HourlyCrashes:
        scope = "county" if place.endswith("County") else "city"
        return HourlyCrashes(place=place, scope=scope, year=year, kind=kind,
                             counts=counts, missing=0)

    def test_counties_add_because_they_do_not_overlap(self):
        hourly = [self.series("Santa Fe County", "all", [1] * 24),
                  self.series("Rio Arriba County", "all", [2] * 24),
                  self.series("Los Alamos County", "all", [3] * 24)]
        counties = tru.names_for(tru.COUNTY_KEYS)
        assert tru.profile(hourly, "all", places=counties) == [6] * 24

    def test_a_city_inside_a_county_is_not_added_to_it(self):
        hourly = [self.series("Santa Fe County", "all", [10] * 24),
                  self.series("Santa Fe", "all", [7] * 24)]
        counties = tru.names_for(tru.COUNTY_KEYS)
        assert tru.profile(hourly, "all", places=counties) == [10] * 24

    def test_years_are_summed_unless_asked_for_one(self):
        hourly = [self.series("Santa Fe County", "all", [1] * 24, year=2019),
                  self.series("Santa Fe County", "all", [1] * 24, year=2021)]
        assert tru.profile(hourly, "all")[0] == 2
        assert tru.profile(hourly, "all", years=(2021,))[0] == 1


class TestRelativeRisk:
    def test_an_hour_with_its_share_of_both_is_an_ordinary_hour(self):
        counts = [100] * 24
        travel = {hour: 1000.0 for hour in range(24)}
        rows = tru.relative_risk(counts, travel)
        assert all(row["relative_risk"] == pytest.approx(1.0) for row in rows)

    def test_crashes_without_traffic_is_what_the_number_is_for(self):
        # One hour with a twentieth of the driving and a tenth of the crashes.
        counts = [10] * 24
        counts[3] = 10
        travel = {hour: 100.0 for hour in range(24)}
        travel[3] = 10.0
        rows = tru.relative_risk(counts, travel)
        assert rows[3]["relative_risk"] > 5.0
        assert rows[12]["relative_risk"] < 1.0

    def test_an_hour_the_model_never_drives_is_not_infinitely_dangerous(self):
        counts = [1] * 24
        travel = {hour: 100.0 for hour in range(24)}
        travel[4] = 0.0
        assert tru.relative_risk(counts, travel)[4]["relative_risk"] == 0.0


class TestUrls:
    def test_2016_uses_an_underscore_and_the_rest_a_hyphen(self):
        county = tru.COMMUNITIES["santa_fe_county"]
        assert tru.report_url(county, 2021).endswith("counties/county_santafe-2021.pdf")
        assert tru.report_url(county, 2016).endswith("counties/county_santafe_2016.pdf")

    def test_cities_and_counties_live_in_different_folders(self):
        city = tru.COMMUNITIES["espanola_city"]
        assert "/cities/city_espanola-2021.pdf" in tru.report_url(city, 2021)


class TestKinds:
    def test_the_caption_wording_changed_and_both_mean_the_same_thing(self):
        # 2019 said "Pedestrian and Pedalcyclist"; 2020 onward says "All
        # Pedestrian and Pedalcycle". Keyed on the caption, the series would
        # have silently split in two.
        assert tru.KINDS["pedestrian and pedalcyclist crashes"] == "vru"
        assert tru.KINDS["all pedestrian and pedalcycle crashes"] == "vru"
