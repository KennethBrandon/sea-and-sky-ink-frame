"""Eclipses on the sky screen: finding them (astro/eclipse.py) and drawing them (scenes/sky.py).

Lunar contact times are the published ones (NASA's eclipse catalog), to the minute, in UTC.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import state
from photopainter.astro import eclipse as astro_eclipse
from photopainter.astro.eclipse import LunarEclipse, SolarEclipse, disc_overlap, find_eclipse
from photopainter.config import default_config
from photopainter.render.draw import INK, InkCanvas
from photopainter.scenes import Scene, SceneContext, render_scene, sky

DATA = Path(__file__).resolve().parent.parent / "data"
LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
WEDGE = (33.593, -117.882)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LA)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def at_wedge(text: str):
    return find_eclipse(local(text), *WEDGE)


def lunar(**changes) -> LunarEclipse:
    t = local("2026-03-03T03:00")
    values = dict(kind="total", seen=True, greatest=t + timedelta(minutes=90), stage="partial", begins=t,
                  ends=t + timedelta(hours=3), total_begins=t + timedelta(hours=1), total_ends=t + timedelta(hours=2),
                  distance=2.75, bearing_deg=90.0, umbra=2.75, penumbra=4.9)
    return LunarEclipse(**(values | changes))


def solar(**changes) -> SolarEclipse:
    t = local("2029-01-14T07:00")
    values = dict(kind="partial", here="partial", greatest=t + timedelta(minutes=90), stage="partial", begins=t,
                  ends=t + timedelta(hours=3), total_begins=None, total_ends=None, distance=1.0, bearing_deg=90.0,
                  moon_size=1.0)
    return SolarEclipse(**(values | changes))


# --- finding them --------------------------------------------------------------------------


# Every eclipse of 2024-2027 (NASA's catalogs): greatest eclipse in UTC, and its kind.
NASA = [
    ("2024-03-25T07:13", "penumbral"), ("2024-04-08T18:17", "total"), ("2024-09-18T02:44", "partial"),
    ("2024-10-02T18:45", "annular"), ("2025-03-14T06:58", "total"), ("2025-03-29T10:47", "partial"),
    ("2025-09-07T18:11", "total"), ("2025-09-21T19:41", "partial"), ("2026-02-17T12:12", "annular"),
    ("2026-03-03T11:33", "total"), ("2026-08-12T17:46", "total"), ("2026-08-28T04:12", "partial"),
    ("2027-02-06T16:00", "annular"), ("2027-02-20T23:13", "penumbral"), ("2027-07-18T16:03", "penumbral"),
    ("2027-08-02T10:07", "total"), ("2027-08-17T07:13", "penumbral"),
]


@pytest.mark.parametrize("greatest, kind", NASA)
def test_every_eclipse_is_found_and_called_what_it_is_anywhere(ephemeris, greatest, kind):
    e = find_eclipse(utc(greatest) - timedelta(hours=1), *WEDGE)
    assert e is not None and e.kind == kind
    assert abs(e.greatest - utc(greatest)) < timedelta(minutes=2)


@pytest.mark.parametrize("seen_at, contacts", [
    ("2025-03-13T23:00", ("2025-03-14T05:09", "2025-03-14T06:26", "2025-03-14T07:31", "2025-03-14T08:47")),
    ("2026-03-03T03:33", ("2026-03-03T09:50", "2026-03-03T11:04", "2026-03-03T12:03", "2026-03-03T13:17")),
])
def test_a_lunar_eclipses_times_match_the_published_ones(ephemeris, seen_at, contacts):
    e = at_wedge(seen_at)
    assert isinstance(e, LunarEclipse) and e.kind == "total"
    for found, published in zip((e.begins, e.total_begins, e.total_ends, e.ends), contacts):
        assert abs(found - utc(published)) < timedelta(seconds=90)


def test_how_deep_it_is_follows_the_clock(ephemeris):
    moments = {"2026-03-03T01:50": "penumbral", "2026-03-03T02:20": "partial", "2026-03-03T03:33": "total",
               "2026-03-03T04:40": "partial", "2026-03-03T06:00": "penumbral"}
    for when, stage in moments.items():
        assert at_wedge(when).stage == stage, when
    assert at_wedge("2026-03-03T01:50").covered == 0.0
    assert 0.0 < at_wedge("2026-03-03T02:20").covered < 1.0
    assert at_wedge("2026-03-03T03:33").covered == 1.0


def test_it_is_mentioned_from_twelve_hours_ahead_and_not_before(ephemeris):
    assert at_wedge("2026-03-02T13:40") is None
    soon = at_wedge("2026-03-02T14:00")
    assert soon.kind == "total" and soon.stage is None
    assert at_wedge("2026-03-03T07:30") is None  # and it's over


def test_the_shadow_is_where_the_geometry_puts_it(ephemeris):
    # From where the Moon is on the meridian, so up is north and left is east. In March it's near
    # the equinox point, moving east and down the ecliptic, 28° off level (23° + the Moon's 5°),
    # so at greatest eclipse the shadow's center is square to that: below and to the right when
    # the Moon passes north of it (2025, gamma +0.35), above and to the left when south (2026, -0.38).
    assert find_eclipse(utc("2025-03-14T06:58"), 33.0, -100.0).bearing_deg == pytest.approx(180.0 - 28.0, abs=8.0)
    assert find_eclipse(utc("2026-03-03T11:33"), 33.0, -170.0).bearing_deg == pytest.approx(360.0 - 28.0, abs=8.0)
    # Before that the Moon is heading for it, on the left; after, it's left behind, on the right.
    assert 180.0 < find_eclipse(utc("2026-03-03T10:20"), 33.0, -170.0).bearing_deg < 300.0
    assert 30.0 < find_eclipse(utc("2026-03-03T12:45"), 33.0, -170.0).bearing_deg < 120.0


def test_the_kinds_of_lunar_eclipse(ephemeris):
    assert at_wedge("2028-01-11T20:13").kind == "partial"
    assert at_wedge("2027-08-17T00:13").kind == "penumbral"
    assert at_wedge("2029-06-25T20:22").stage == "total"


def test_a_solar_eclipse_is_worked_out_from_where_you_stand(ephemeris):
    e = at_wedge("2024-04-08T11:12")
    assert isinstance(e, SolarEclipse) and e.stage == "partial"
    assert e.kind == "total" and e.here == "partial"  # total in Texas, partial here
    assert abs(e.begins - local("2024-04-08T10:06")) < timedelta(seconds=90)
    assert abs(e.ends - local("2024-04-08T12:22")) < timedelta(seconds=90)
    assert e.covered == pytest.approx(0.5, abs=0.05)
    # The Moon crosses from the west (on the right, facing the Sun in the south) to the east.
    assert 0.0 < at_wedge("2024-04-08T10:30").bearing_deg < 180.0
    assert 180.0 < at_wedge("2024-04-08T12:00").bearing_deg < 360.0


def test_total_and_annular_from_where_they_were(ephemeris):
    dallas = find_eclipse(utc("2024-04-08T18:42"), 32.78, -96.80)
    assert dallas.here == "total" and dallas.stage == "total" and dallas.covered == 1.0 and dallas.moon_size > 1.0
    albuquerque = find_eclipse(utc("2023-10-14T16:37"), 35.08, -106.65)
    assert albuquerque.here == "annular" and albuquerque.stage == "annular" and albuquerque.moon_size < 1.0
    assert 0.8 < albuquerque.covered < 1.0


def test_eclipses_not_seen_from_here_are_still_found(ephemeris):
    lunar_ = at_wedge("2025-09-07T11:11")  # the Moon on the other side of the Earth
    assert lunar_.kind == "total" and not lunar_.seen
    solar_ = at_wedge("2026-08-12T10:45")  # in Spain
    assert solar_.kind == "total" and solar_.here is None and not solar_.seen and solar_.stage is None
    assert abs(solar_.begins - utc("2026-08-12T15:34")) < timedelta(minutes=3)  # anywhere on Earth
    assert abs(solar_.ends - utc("2026-08-12T19:58")) < timedelta(minutes=3)
    assert at_wedge("2026-08-12T13:30") is None  # over everywhere


def test_a_solar_eclipse_over_here_is_still_on_elsewhere(ephemeris):
    e = at_wedge("2024-04-08T12:40")
    assert e.seen and e.stage is None and sky.eclipse_notes(e, local("2024-04-08T12:40")) == []


@pytest.mark.parametrize("when", ["2026-11-24T02:30", "2026-12-08T17:30"])
def test_most_full_and_new_moons_are_no_eclipse(ephemeris, when):
    assert at_wedge(when) is None


def test_away_from_a_full_or_new_moon_nothing_is_searched(ephemeris, monkeypatch):
    def fail(*_):
        raise AssertionError("searched")

    monkeypatch.setattr(astro_eclipse, "_lunar", fail)
    monkeypatch.setattr(astro_eclipse, "_solar", fail)
    assert at_wedge("2026-11-15T21:30") is None
    assert find_eclipse(local("2026-11-15T21:30"), *WEDGE, phase_deg=90.0) is None


def test_naive_time_is_refused():
    with pytest.raises(ValueError):
        find_eclipse(datetime(2026, 3, 3, 3, 33), *WEDGE)


@pytest.mark.parametrize("distance, radius", [(0.5, 1.0), (1.0, 1.0), (1.5, 2.75), (2.5, 2.75), (0.3, 0.5), (1.2, 0.5)])
def test_disc_overlap_matches_counting(distance, radius):
    yy, xx = np.mgrid[-1:1:800j, -1:1:800j]
    inside = xx ** 2 + yy ** 2 <= 1
    counted = (inside & ((xx - distance) ** 2 + yy ** 2 <= radius ** 2)).sum() / inside.sum()
    assert disc_overlap(distance, radius) == pytest.approx(counted, abs=0.005)


def test_disc_overlap_at_the_ends():
    assert disc_overlap(3.0, 1.0) == 0.0
    assert disc_overlap(0.5, 2.75) == 1.0
    assert disc_overlap(0.0, 0.5) == 0.25


# --- drawing them --------------------------------------------------------------------------


def drawn_moon(e: LunarEclipse, toward: float = 90.0) -> np.ndarray:
    c = InkCanvas((60, 60), "blue")  # no ink a Moon uses
    sky.eclipsed_moon(c, 30, 30, 20, e, toward)
    return c.indices()


def test_the_shadow_falls_on_the_side_it_is_on():
    out = drawn_moon(lunar(distance=2.75))  # the umbra's edge through the center, on the right
    right, left = out[:, 31:], out[:, :30]
    red = (INK["red"], INK["yellow"])
    assert np.isin(right, red).sum() > 400 and not np.isin(left, red).any()
    assert (left == INK["white"]).sum() > 250
    assert not (out[20:41, 34:] == INK["white"]).any()  # the edge curves away, toward the limb


def test_a_totally_eclipsed_moon_is_red_and_darkest_toward_the_shadows_center():
    out = drawn_moon(lunar(stage="total", distance=1.4))
    assert not (out == INK["white"]).any()
    assert (out == INK["red"]).sum() > 700
    assert (out[:, 31:] == INK["black"]).sum() > (out[:, :30] == INK["black"]).sum()  # the center's to the right
    assert (out[:, :30] == INK["yellow"]).sum() > (out[:, 31:] == INK["yellow"]).sum()  # and the edge's to the left


def test_in_the_penumbra_the_moon_only_dims():
    out = drawn_moon(lunar(stage="penumbral", distance=4.2))
    assert (out == INK["white"]).sum() > 800 and (out == INK["black"]).any()
    assert not (out == INK["red"]).any()
    assert (out[:, 31:] == INK["black"]).sum() > (out[:, :30] == INK["black"]).sum()
    far = drawn_moon(lunar(stage=None, distance=30.0))
    assert (far == INK["white"]).sum() > 1200 and not (far == INK["black"]).any()


def drawn_sun(e: SolarEclipse) -> np.ndarray:
    c = InkCanvas((60, 60), "blue")
    sky.stamp_colored(c, 30, 30, 41, "yellow")
    sky.moon_across_sun(c, 30, 30, 41, e, 90.0)
    return c.indices()


def test_the_moon_bites_the_sun_on_its_side_and_only_on_the_sun():
    out = drawn_sun(solar(distance=1.0))
    assert (out[:, 31:] == INK["black"]).sum() > 400 and not (out[:, :30] == INK["black"]).any()
    assert (out[:, 52:] == INK["blue"]).all()  # off the Sun it can't be seen
    black, yellow = (out == INK["black"]).sum(), (out == INK["yellow"]).sum()
    assert black / (black + yellow) == pytest.approx(solar(distance=1.0).covered, abs=0.03)


def test_total_and_annular_suns():
    total = drawn_sun(solar(distance=0.0, moon_size=1.05))
    assert not (total == INK["yellow"]).any()
    ring = drawn_sun(solar(distance=0.0, moon_size=0.9))
    assert (ring[30, 30] == INK["black"]) and (ring[30, 49] == INK["yellow"])


@pytest.mark.parametrize("now, words", [
    ("2026-03-03T02:00", "Begins 3:00 am"),
    ("2026-03-03T03:30", "Total at 4:00 am"),
    ("2026-03-03T04:30", "Total until 5:00 am"),
    ("2026-03-03T05:30", "Ends 6:00 am"),
])
def test_the_note_names_it_then_says_what_comes_next(now, words):
    assert sky.eclipse_notes(lunar(), local(now)) == ["Total lunar eclipse", words]


def test_the_notes_other_kinds():
    assert sky.eclipse_notes(lunar(), local("2026-03-03T06:30")) == []  # over, though still in the penumbra
    partial = lunar(kind="partial", total_begins=None, total_ends=None)
    assert sky.eclipse_notes(partial, local("2026-03-03T04:00")) == ["Partial lunar eclipse", "Ends 6:00 am"]
    faint = lunar(kind="penumbral", total_begins=None, total_ends=None)
    assert sky.eclipse_notes(faint, local("2026-03-03T02:00")) == ["Faint lunar eclipse", "Begins 3:00 am"]
    assert sky.eclipse_notes(faint, local("2026-03-03T04:00")) == ["Faint lunar eclipse", "Ends 6:00 am"]
    t = local("2023-10-14T10:00")
    annular = solar(kind="annular", here="annular", begins=t, ends=t + timedelta(hours=3),
                    total_begins=t + timedelta(hours=1), total_ends=t + timedelta(hours=2))
    assert sky.eclipse_notes(annular, t + timedelta(minutes=90)) == ["Annular solar eclipse", "Annular until 12:00 pm"]


def test_a_total_eclipse_is_called_total_though_it_is_partial_here():
    e = solar(kind="total", here="partial")
    assert sky.eclipse_notes(e, local("2029-01-14T06:00")) == ["Total solar eclipse", "Partial here from 7:00 am"]
    assert sky.eclipse_notes(e, local("2029-01-14T08:00")) == ["Total solar eclipse", "Partial here until 10:00 am"]
    assert sky.eclipse_notes(solar(), local("2029-01-14T08:00")) == ["Partial solar eclipse", "Ends 10:00 am"]


def test_an_eclipse_not_seen_here_gets_its_time_and_says_so():
    away = solar(kind="total", here=None, stage=None)
    assert sky.eclipse_notes(away, local("2029-01-14T06:00")) == ["Total solar eclipse", "8:30 am, not seen here"]
    moon_down = lunar(seen=False)
    assert sky.eclipse_notes(moon_down, local("2026-03-03T01:00")) == ["Total lunar eclipse", "4:30 am, not seen here"]


# --- the screen ----------------------------------------------------------------------------


def ctx(tmp_path=None) -> SceneContext:
    return SceneContext(default_config(), (800, 480), state.state_dir(), DATA)


def model(when: str) -> sky.SkyModel:
    return sky.sky_model(local(when), ctx())


def on_screen(m: sky.SkyModel, body) -> tuple[int, int]:
    config = default_config()
    proj = sky.Stereographic.fit(config.sky.center_azimuth_deg, config.sky.field_of_view_deg, config.sky.max_altitude_deg,
                                 800, 480 - sky.SkyStyle().footer_h)
    x, y, _ = proj.point(body.alt, body.az)
    return int(round(x)), int(round(y))


def drawn(m: sky.SkyModel) -> np.ndarray:
    c = InkCanvas((800, 480), "black")
    sky.draw(c, m, default_config())
    return c.indices()


def around(out: np.ndarray, xy: tuple[int, int], r: int) -> np.ndarray:
    x, y = xy
    return out[max(0, y - r):y + r + 1, max(0, x - r):x + r + 1]


def test_total_eclipse_on_the_screen(ephemeris):
    m = model("2026-03-03T03:33")
    assert m.eclipse.stage == "total"
    out = drawn(m)
    moon = around(out, on_screen(m, m.view.moon), 15)
    assert (moon == INK["red"]).mean() > 0.6 and not (moon == INK["white"]).any()
    # And without the eclipse, the same moment has a white Moon and no shadow around it.
    plain = around(drawn(sky.replace(m, eclipse=None)), on_screen(m, m.view.moon), 15)
    assert (plain == INK["white"]).mean() > 0.9 and not (plain == INK["red"]).any()
    assert [(what, s) for what, s, _ in sky.top_notes(m)][:2] == [
        ("eclipse", "Total lunar eclipse"), ("eclipse_detail", "Total until 4:02 am")]


def test_an_eclipsed_moon_goes_red_on_the_water(ephemeris):
    m = model("2026-03-03T03:33")
    ocean = sky.SkyStyle().ocean_h
    horizon = 480 - sky.SkyStyle().footer_h
    water = drawn(m)[horizon + 2:horizon + ocean - 2]
    assert (water == INK["red"]).any()
    plain = drawn(sky.replace(m, eclipse=None))[horizon + 2:horizon + ocean - 2]
    assert not (plain == INK["red"]).any()


def test_solar_eclipse_on_the_screen(ephemeris):
    m = model("2024-04-08T11:12")
    assert isinstance(m.eclipse, SolarEclipse)
    out = drawn(m)
    sun = around(out, on_screen(m, m.view.sun), 20)
    assert (sun == INK["black"]).sum() > 300 and (sun == INK["yellow"]).sum() > 300
    plain = around(drawn(sky.replace(m, eclipse=None)), on_screen(m, m.view.sun), 20)
    assert not (plain == INK["black"]).any()
    assert [s for _, s, _ in sky.top_notes(m)][:2] == ["Total solar eclipse", "Partial here until 12:22 pm"]


def test_an_eclipse_not_seen_from_here_is_named_but_not_drawn(ephemeris):
    m = model("2025-09-06T23:30")  # the night before, with the Moon up and not yet eclipsed
    assert [s for _, s, _ in sky.top_notes(m)][:2] == ["Total lunar eclipse", "11:11 am, not seen here"]
    moon = on_screen(m, m.view.moon)
    assert np.array_equal(around(drawn(m), moon, 30), around(drawn(sky.replace(m, eclipse=None)), moon, 30))
    horizon = 480 - sky.SkyStyle().footer_h
    assert np.array_equal(drawn(m)[horizon:], drawn(sky.replace(m, eclipse=None))[horizon:])


def test_a_new_moon_is_not_drawn_beside_the_sun(ephemeris):
    m = model("2026-12-08T12:00")
    assert m.eclipse is None
    style = sky.SkyStyle()
    assert sky.moon_lost_in_sun(*on_screen(m, m.view.moon), *on_screen(m, m.view.sun), style)
    far_side = model("2026-11-23T21:30")
    assert not sky.moon_lost_in_sun(*on_screen(far_side, far_side.view.moon), 700, 50, style)


def test_most_nights_have_no_eclipse_on_them(ephemeris):
    m = model("2026-11-23T21:30")
    assert m.eclipse is None
    assert [what for what, _, _ in sky.top_notes(m)] == ["sun"]


@pytest.mark.parametrize("kind", ["sky", "combo"])
@pytest.mark.parametrize("when", ["2026-03-03T02:30", "2026-03-03T03:33", "2024-04-08T11:12", "2029-06-25T20:22",
                                  "2028-01-11T20:13", "2027-08-17T00:13", "2026-03-02T22:00", "2025-09-06T23:30",
                                  "2026-08-12T05:30"])
def test_every_kind_of_eclipse_renders(ephemeris, kind, when):
    out = render_scene(Scene(kind, f"{kind}/x", local(when)), ctx())
    assert out.shape == (480, 800) and out.max() <= 5
