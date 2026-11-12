"""The night sky: star data (S1), positions (astro/sky.py) and the sky screen (scenes/sky.py).

Reference positions in tests/fixtures/horizons_altaz.json are JPL Horizons' apparent,
refracted azimuth and elevation at the Wedge, queried 2026-11-04 (the query is in the file).
"""

import json
import math
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import cli, state
from photopainter.astro import sky as astro_sky
from photopainter.astro.sky import (
    bearing, galactic, load_constellations, load_milky_way, load_stars, milky_way_at, offset,
    sky_view, star_altaz,
)
from photopainter.config import default_config, load_config, save_config
from photopainter.render.draw import INK, InkCanvas
from photopainter.scenes import Scene, SceneContext, render_scene
from photopainter.scenes import sky
from photopainter.scenes.tides import load_weather, weather_icon

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
WEDGE = (33.593, -117.882)


def local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=LA)


def separation(az1, alt1, az2, alt2) -> float:
    """Angle between two sky directions, in degrees."""
    a1, h1, a2, h2 = map(math.radians, (az1, alt1, az2, alt2))
    c = math.sin(h1) * math.sin(h2) + math.cos(h1) * math.cos(h2) * math.cos(a1 - a2)
    return math.degrees(math.acos(min(1.0, max(-1.0, c))))


# --- S1: the data files --------------------------------------------------------------------


def test_star_file_matches_the_contract():
    data = json.loads((DATA / "stars.json").read_text())
    assert data["source"] and "CC BY-SA" in data["license"]
    hips = set()
    for row in data["stars"]:
        hip, ra, dec, mag, name, bv = row  # PRD §5 [hip, ra_deg, dec_deg, mag, name] + B−V
        assert isinstance(hip, int) and hip not in hips
        assert 0 <= ra < 360 and -90 <= dec <= 90 and -2 < mag < 7
        assert name is None or (isinstance(name, str) and name)
        assert bv is None or -0.5 < bv < 3
        hips.add(hip)


def test_every_line_endpoint_is_a_star_and_files_are_small():
    stars = {row[0] for row in json.loads((DATA / "stars.json").read_text())["stars"]}
    data = json.loads((DATA / "constellations.json").read_text())
    assert data["source"] and "CC BY-SA" in data["license"]
    cons = data["constellations"]
    assert len(cons) == 88 and len({c["abbr"] for c in cons}) == 88
    for c in cons:
        assert c["name"] and c["lines"]
        for a, b in c["lines"]:
            assert a in stars and b in stars, (c["abbr"], a, b)
    total = sum((DATA / n).stat().st_size for n in ("stars.json", "constellations.json", "milkyway.png",
                                                    "skyline.json"))
    assert total < 500_000


def test_bright_stars_are_all_there_and_named():
    cat = load_stars(DATA)
    assert (cat.mag <= 5.0).sum() > 1500  # Hipparcos stars to magnitude 5
    brightest = np.argsort(cat.mag)[:50]
    named = [cat.names[i] for i in brightest if cat.names[i]]
    assert len(named) >= 45
    assert {"Sirius", "Betelgeuse", "Rigel", "Vega", "Polaris", "Antares", "Fomalhaut"} <= set(cat.names)


def test_constellations_visible_from_the_wedge_have_lines():
    cat = load_stars(DATA)
    cons = {c.abbr: c for c in load_constellations(DATA)}
    for abbr in ("Ori", "UMa", "Cas", "Sco", "Sgr", "Peg", "Cyg", "Leo", "CMa", "Cen", "Cru"):
        assert cons[abbr].lines
    # every constellation with a star that ever rises at 33.6° N is in the file
    for c in cons.values():
        decs = [cat.dec_deg[cat.rows[h]] for pair in c.lines for h in pair]
        assert max(decs) > -90


def test_loaders(tmp_path):
    shutil.copy(FIXTURES / "stars_small.json", tmp_path / "stars.json")
    cat = load_stars(tmp_path)
    assert len(cat) == 25 and cat.rows[32349] == list(cat.hip).index(32349)
    sirius = cat.rows[32349]
    assert cat.names[sirius] == "Sirius" and cat.mag[sirius] == pytest.approx(-1.44)
    assert cat.bv[sirius] == pytest.approx(0.0, abs=0.05)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "stars.json").write_text('{"stars": [[1, "x"]]}')
    with pytest.raises(ValueError, match="star list"):
        load_stars(tmp_path / "bad")
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "stars.json").write_text('{"stars": [[1, 10.0, 20.0, 3.0, null]]}')  # no B−V
    assert np.isnan(load_stars(tmp_path / "old").bv[0])


# --- positions -----------------------------------------------------------------------------


def test_moon_and_planets_match_jpl_horizons(ephemeris):
    ref = json.loads((FIXTURES / "horizons_altaz.json").read_text())
    cat = load_stars(DATA)
    checked: dict[str, set[str]] = {}
    for row in ref["rows"]:
        if row["alt"] < 3:  # Horizons refracts far below the horizon; we don't draw there
            continue
        at = datetime.fromisoformat(row["utc"].replace("Z", "+00:00"))
        v = sky_view(at, *WEDGE, cat)
        body = {b.name: b for b in (*v.planets, v.moon)}[row["body"]]
        assert separation(body.az, body.alt, row["az"], row["alt"]) < 0.02, row  # PRD: within 0.5°
        checked.setdefault(row["utc"], set()).add(row["body"])
    assert len(checked) == 2  # two dates…
    for bodies in checked.values():  # …each with the Moon and at least two planets
        assert "Moon" in bodies and len(bodies - {"Moon"}) >= 2


def test_star_positions(ephemeris):
    cat = load_stars(DATA)
    polaris = cat.rows[11767]
    v = sky_view(local("2026-11-04T21:30"), *WEDGE, cat)
    assert v.star_alt[polaris] == pytest.approx(WEDGE[0], abs=1.0)  # Polaris sits at your latitude
    assert v.star_az[polaris] == pytest.approx(0.0, abs=1.5) or v.star_az[polaris] > 358.5
    # Sirius due south at its highest: 90 - 33.593 - 16.72 = 39.69°, plus 0.02° of refraction
    sirius = cat.ra_deg[cat.rows[32349]], cat.dec_deg[cat.rows[32349]]
    best = max((star_altaz(local(f"2027-01-0{d}T{h:02d}:{m:02d}"), *WEDGE, np.array([sirius[0]]),
                           np.array([sirius[1]])) for d in (5,) for h in (23, 0) for m in range(0, 60, 2)),
               key=lambda p: p[0][0])
    assert best[0][0] == pytest.approx(39.71, abs=0.05) and best[1][0] == pytest.approx(180, abs=1.5)


def test_bright_limb_faces_the_sun(ephemeris):
    cat = load_stars(DATA)
    # 2026-11-16 first quarter: evening Moon in the south, the Sun set in the west (on the right)
    v = sky_view(local("2026-11-16T18:00"), *WEDGE, cat)
    assert v.moon.phase.name == "First quarter" and 45 < v.moon.bright_limb_deg < 135
    # 2026-11-01 last quarter: morning Moon, the Sun rising in the east (on the left)
    v = sky_view(local("2026-11-01T06:00"), *WEDGE, cat)
    assert v.moon.phase.name == "Last quarter" and 225 < v.moon.bright_limb_deg < 315
    # the angle points at the Sun: a step along it gets closer
    near = offset(v.moon.alt, v.moon.az, v.moon.bright_limb_deg, 5.0)
    assert separation(near[1], near[0], v.sun.az, v.sun.alt) < separation(v.moon.az, v.moon.alt,
                                                                          v.sun.az, v.sun.alt)


def test_bearing_and_offset():
    assert bearing(0, 180, 10, 180) == pytest.approx(0)  # straight up
    assert bearing(0, 180, 0, 190) == pytest.approx(90)  # facing south, west is on the right
    assert bearing(20, 180, 10, 180) == pytest.approx(180)
    assert bearing(0, 180, 0, 170) == pytest.approx(270)
    alt, az = offset(30.0, 200.0, 70.0, 12.0)
    assert separation(az, alt, 200.0, 30.0) == pytest.approx(12.0)
    assert bearing(30.0, 200.0, alt, az) == pytest.approx(70.0)


def test_galactic_coordinates_and_milky_way(ephemeris):
    at = datetime(2027, 7, 4, 5, 30, tzinfo=UTC)
    alt, az = star_altaz(at, *WEDGE, np.array([266.405, 192.859]), np.array([-29.008, 27.128]))
    l, b = galactic(at, *WEDGE, alt, az)  # Sagittarius A* and the north galactic pole
    assert abs((l[0] + 180) % 360 - 180) < 0.1 and abs(b[0]) < 0.1
    assert b[1] == pytest.approx(90, abs=0.1)
    grid = load_milky_way(DATA)
    assert grid.shape == (300, 600)
    core, pole, anticenter = milky_way_at(grid, np.array([0.0, 0.0, 180.0]), np.array([0.0, 90.0, 0.0]))
    assert core > 0.8 and pole < 0.3 and pole < anticenter < core


def test_naive_time_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        sky_view(datetime(2026, 11, 4, 21, 30), *WEDGE, load_stars(DATA))


# --- the projection ------------------------------------------------------------------------


def test_stereographic_projection():
    proj = sky.Stereographic.fit(190, 170, 90, 800, 422)
    x, y, ok = proj.project(np.zeros(5), np.array([120.0, 150.0, 190.0, 230.0, 260.0]))
    assert ok.all() and np.allclose(y, 422)  # the horizon is straight along the bottom
    assert list(np.argsort(x)) == [0, 1, 2, 3, 4]  # east on the left, west on the right
    assert x[2] == pytest.approx(400)
    assert proj.horizon_x(150) == pytest.approx(x[1])
    zx, zy, _ = proj.point(90, 123)  # the zenith, from any azimuth
    assert (zx, zy) == (pytest.approx(400), pytest.approx(0, abs=1e-6))
    assert proj.half_fov(800) >= 85  # at least the field of view fits…
    assert proj.point(90, 0)[1] >= -1e-6  # …and the altitude
    # conformal: a small circle stays round anywhere in the view
    for alt, az in ((10, 130), (50, 250)):
        pts = [offset(alt, az, b, 1.0) for b in range(0, 360, 30)]
        xs, ys, _ = proj.project(np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
        cx, cy, _ = proj.point(alt, az)
        r = np.hypot(xs - cx, ys - cy)
        assert r.max() / r.min() < 1.02  # circles stay circles (their centers shift a little)


# --- the screen ----------------------------------------------------------------------------


def ctx(config=None, data_dir=DATA, canvas=(800, 480)):
    return SceneContext(config or default_config(), canvas, state.state_dir(), data_dir)


NIGHTS = ["2026-11-04T21:30", "2026-11-05T05:30", "2026-11-18T19:30", "2027-07-03T22:30"]


@pytest.mark.parametrize("when", NIGHTS)
def test_renders_every_kind_of_night(ephemeris, when):
    out = render_scene(Scene("sky", f"sky/{when}", local(when)), ctx())
    assert out.shape == (480, 800) and out.dtype == np.uint8 and out.max() <= 5
    assert (out == INK["black"]).mean() > 0.4  # a black sky
    assert (out == INK["white"]).any() and (out == INK["green"]).any()


def test_rendering_is_repeatable(ephemeris):
    scene = Scene("sky", "sky/x", local("2026-11-05T05:30"))
    assert np.array_equal(render_scene(scene, ctx()), render_scene(scene, ctx()))


def test_settings_change_what_is_drawn(ephemeris):
    scene = Scene("sky", "sky/x", local("2026-11-04T21:30"))
    full = render_scene(scene, ctx())
    cfg = default_config()
    bare = replace(cfg, sky=replace(cfg.sky, constellation_lines=False, labels=False, magnitude_limit=2.0))
    plain = render_scene(scene, ctx(bare))
    assert not (plain == INK["green"]).any()  # no lines, no names
    assert (plain == INK["white"]).sum() < (full == INK["white"]).sum()
    turned = replace(cfg, sky=replace(cfg.sky, center_azimuth_deg=0))  # north: no Catalina, no sea path
    assert not np.array_equal(render_scene(scene, ctx(turned)), full)


def test_renders_without_optional_data_and_in_portrait(ephemeris, tmp_path):
    for name in ("stars.json", "constellations.json", "de421.bsp"):
        (tmp_path / name).symlink_to(DATA / name)
    shutil.copytree(DATA / "fonts", tmp_path / "fonts")
    scene = Scene("sky", "sky/x", local("2027-07-03T22:30"))
    without = render_scene(scene, ctx(data_dir=tmp_path))  # no Milky Way, no skyline
    assert without.shape == (480, 800)
    assert not np.array_equal(without, render_scene(scene, ctx()))
    tall = render_scene(scene, ctx(canvas=(480, 800)))
    assert tall.shape == (800, 480)


def test_planets_on_screen_get_no_off_view_marker(ephemeris):
    # 2026-11-04 02:56 (on the panel that night): Jupiter is 50° up at azimuth 102°, past
    # the horizon's left edge (105°) but on screen, since the view reaches further
    # sideways higher up. It was drawn and also got "◂ Jupiter" at the edge.
    m = sky.sky_model(local("2026-11-04T02:56:11"), ctx())
    proj = sky.Stereographic.fit(190, 170, 90, 800, 480 - sky.SkyStyle().footer_h)
    jupiter = next(p for p in m.view.planets if p.name == "Jupiter")
    x, y, ok = proj.point(jupiter.alt, jupiter.az)
    assert ok and 0 <= x < 800 and 0 <= y < 422  # drawn…
    assert abs((jupiter.az - 190 + 180) % 360 - 180) > proj.half_fov(800)  # …beyond the horizon's edge
    marked = {body.name: left for body, left in sky.off_view(proj, m, 800, 422, moon_up=False)}
    assert "Jupiter" not in marked and "Mars" not in marked
    assert marked.get("Moon") is True  # rising in the east, below the left edge's reach


def test_moon_just_past_the_edge_gets_a_marker_not_a_sliver(ephemeris):
    # 2026-11-04 03:26 (on the panel): the crescent's center was 6 px past the left edge,
    # so only a sliver showed, with no name.
    m = sky.sky_model(local("2026-11-04T03:26:37"), ctx())
    proj = sky.Stereographic.fit(190, 170, 90, 800, 480 - sky.SkyStyle().footer_h)
    x, _, _ = proj.point(m.view.moon.alt, m.view.moon.az)
    assert -sky.SkyStyle().moon_radius < x < 0 and m.view.moon.alt > 0
    assert not sky.disc_on_screen(proj, m.view.moon, 800, 422, 20)
    marked = {body.name: left for body, left in sky.off_view(proj, m, 800, 422, moon_up=False)}
    assert marked["Moon"] is True
    # an hour later it's well inside the view: drawn, no marker
    later = sky.sky_model(local("2026-11-04T04:30"), ctx())
    assert sky.disc_on_screen(proj, later.view.moon, 800, 422, 20)


def test_skyline_is_catalina_and_only_from_near_the_wedge(ephemeris):
    sk = sky.load_skyline(DATA)
    assert sk.name == "Santa Catalina Island" and sk.observer == WEDGE
    assert 225 < sk.az.min() < sk.az.max() < 265
    assert 0.2 < sk.height.max() < 0.8  # it really is under a degree tall
    far = replace(default_config(), location=replace(default_config().location, lat=34.5))
    assert sky.sky_model(local("2026-11-04T21:30"), ctx(far)).skyline is None
    assert sky.sky_model(local("2026-11-04T21:30"), ctx()).skyline is sk


def test_plain_band_instead_of_the_beach(ephemeris):
    m = sky.sky_model(local("2026-11-04T21:30"), ctx())
    c = InkCanvas((800, 480), "black")
    sky.draw(c, m, default_config(), sky.SkyStyle(beach=False))
    band = c.indices()[440:, :]
    assert not (band == INK["yellow"]).any() and (band == INK["blue"]).mean() > 0.8


def test_star_colors():
    cat = load_stars(DATA)
    color = {n: sky.star_color(float(cat.bv[cat.rows[h]]), "dither")
             for n, h in (("Betelgeuse", 27989), ("Aldebaran", 21421), ("Capella", 24608),
                          ("Rigel", 24436), ("Sirius", 32349), ("Arcturus", 69673))}
    assert color == {"Betelgeuse": "red-orange", "Aldebaran": "orange", "Capella": "yellow",
                     "Rigel": "blue-white", "Sirius": "white", "Arcturus": "orange"}
    assert sky.star_color(1.85, "solid") == "red" and sky.star_color(-0.2, "solid") == "white"
    assert sky.star_color(float("nan"), "dither") == "white" and sky.star_color(1.8, "off") == "white"
    c = InkCanvas((40, 40), "black")
    sky.stamp_colored(c, 20, 20, 8, "orange")
    inks = set(np.unique(c.indices()).tolist())
    assert inks == {INK["black"], INK["yellow"], INK["red"]}


@pytest.mark.parametrize("lit", [0.0, 0.25, 0.5, 0.9, 1.0])
def test_moon_disc_is_lit_by_its_phase(lit):
    c = InkCanvas((60, 60), "black")
    sky.phase_disc(c, 30, 30, 20, lit, 90.0, INK["white"], INK["blue"])
    out = c.indices()
    white, blue = (out == INK["white"]).sum(), (out == INK["blue"]).sum()
    assert white / (white + blue) == pytest.approx(lit, abs=0.05)
    if 0 < lit < 1:  # lit on the right (90°)
        assert (out[:, 31:] == INK["white"]).sum() > (out[:, :30] == INK["white"]).sum()


def test_tick_shows_the_sky_at_night(ephemeris, tmp_path):
    save_config(replace(default_config(), photos=replace(default_config().photos, folder=str(tmp_path / "none"))))
    assert cli.main(["tick", "--fake-panel", "--at", "2026-11-04T21:30"]) == 0
    s = state.read_json(state.state_dir() / state.STATUS)
    assert s["showing"]["kind"] == "sky" and s["showing"]["key"] == "sky/2026-11-04T21:00"  # hourly from midnight
    assert s["showing"]["drawn_for"].startswith("2026-11-04T21:30")  # the middle of 21:00-22:00
    assert (state.state_dir() / "current.png").is_file()


# --- the sky by daylight ----------------------------------------------------------------------

K, B, W, Y, R = range(5)  # columns of sky.sky_shares, as sky.SKY_INKS


def test_sky_inks_are_the_ones_the_shares_name():
    assert [int(i) for i in sky.SKY_INKS] == [INK[n] for n in ("black", "blue", "white", "yellow", "red")]


def test_unproject_undoes_project():
    proj = sky.Stereographic.fit(190, 170, 90, 800, 422)
    alt = np.array([0.0, 10.0, 45.0, 80.0, 30.0])
    az = np.array([190.0, 120.0, 250.0, 200.0, 175.0])
    x, y, _ = proj.project(alt, az)
    back_alt, back_az = proj.unproject(x, y)
    assert back_alt == pytest.approx(alt, abs=1e-6)
    assert back_az % 360 == pytest.approx(az, abs=1e-6)


def test_night_is_left_exactly_as_approved():
    style = sky.SkyStyle()
    assert sky.lit_style(style, -15.0) is style
    assert sky.lit_style(style, -40.0) is style
    assert sky.star_limit(-15.0, 4.5, 2.0) == 4.5


def test_the_sky_loses_stars_as_it_gets_light():
    limits = [sky.star_limit(alt, 4.5, 2.0) for alt in (-20, -15, -12, -8, -4, 0, 40)]
    assert limits == sorted(limits, reverse=True)
    assert limits[0] == 4.5 and limits[-1] == 2.0
    assert sky.star_limit(40.0, 1.0, 2.0) == 1.0  # a lower night limit isn't raised by day
    assert sky.star_limit(40.0, 4.5, 6.0) == 4.5  # and the day never shows more than the night


@pytest.mark.parametrize("sun_alt", [-14.0, -9.0, -6.0, -3.0, 0.0, 3.0, 8.0, 40.0, 79.0])
def test_sky_shares_sum_to_one(sun_alt):
    alt, az = np.meshgrid(np.linspace(0, 90, 10), np.linspace(0, 350, 12))
    shares = sky.sky_shares(alt, az, sun_alt, 200.0, sky.SkyStyle())
    assert shares.shape == (*alt.shape, 5)
    assert shares.sum(axis=-1) == pytest.approx(np.ones(alt.shape))
    assert shares.min() >= -1e-9


def test_day_sky_is_blue_overhead_and_paler_low_and_near_the_sun():
    style = sky.SkyStyle()

    def at(alt, az):
        return sky.sky_shares(np.array(alt, dtype=float), np.array(az, dtype=float), 40.0, 188.0, style)

    zenith, low, near_sun = at(90, 0), at(2, 40), at(38, 190)
    assert zenith[B] > 0.7 and zenith[K] == 0 and zenith[Y] == 0 and zenith[R] == 0
    assert low[W] > zenith[W] + 0.3
    assert near_sun[W] > at(38, 20)[W] + 0.2  # the same height, away from the Sun


def test_the_sunset_is_on_the_suns_side_and_blue_never_meets_yellow():
    style = sky.SkyStyle()

    def at(alt, az, sun_alt=0.0):
        return sky.sky_shares(np.array(alt, dtype=float), np.array(az, dtype=float), sun_alt, 250.0, style)

    toward, away = at(1, 250), at(1, 70)
    assert toward[Y] + toward[R] > 0.8
    assert away[Y] + away[R] < toward[Y] + toward[R] - 0.4
    for alt in range(0, 60, 2):
        for az in (250, 200, 150, 70):
            s = at(alt, az)
            assert min(s[B], s[Y]) < 0.25  # blue and yellow dots together would look grey-green
    night_side = at(90, 0, -3.0)
    assert night_side[Y] + night_side[R] < 0.01


def test_sun_track_and_next_events(ephemeris):
    config = default_config()
    noon = local("2026-11-04T12:00")
    track = sky.sun_track(noon, config)
    assert len(track) == 145
    assert track[0][0] == local("2026-11-04T00:00") and track[-1][0] == local("2026-11-05T00:00")
    highest = max(track, key=lambda p: p[1])
    assert highest[0].hour in (11, 12)
    view = sky_view(noon, *WEDGE, load_stars(DATA))
    at_noon = next(p for p in track if p[0] == noon)
    assert at_noon[1] == pytest.approx(view.sun.alt, abs=0.01) and at_noon[2] == pytest.approx(view.sun.az, abs=0.01)

    assert sky.next_sun_event(local("2026-11-04T05:00"), config)[0] == "rises"
    what, when = sky.next_sun_event(noon, config)
    assert what == "sets" and when.date() == noon.date()
    what, when = sky.next_sun_event(local("2026-11-04T20:00"), config)
    assert what == "rises" and when.date() == local("2026-11-05T12:00").date()


def test_only_a_lit_sky_works_out_the_suns_track(ephemeris):
    night = sky.sky_model(local("2026-11-04T21:30"), ctx())
    assert night.sun_next[0] == "rises" and night.sun_track == ()  # the sunrise shows all night
    day = sky.sky_model(local("2026-11-04T12:00"), ctx())
    assert day.sun_next[0] == "sets" and len(day.sun_track) == 145


DAYS = ["2026-11-04T06:00", "2026-11-04T06:30", "2026-11-04T09:00", "2026-11-04T12:00", "2026-11-04T16:30",
        "2026-11-04T16:55", "2026-11-04T17:30", "2027-06-21T08:00", "2027-06-21T12:30", "2027-06-21T19:45"]


@pytest.mark.parametrize("when", DAYS)
def test_renders_every_kind_of_day(ephemeris, when):
    out = render_scene(Scene("sky", f"sky/{when}", local(when)), ctx())
    assert out.shape == (480, 800) and out.dtype == np.uint8 and out.max() <= 5


def test_noon_is_a_blue_sky_with_the_sun_in_it(ephemeris):
    # At noon on 2026-11-04 the Sun is 41° up, near the middle of the view.
    scene = Scene("sky", "sky/x", local("2026-11-04T12:00"))
    out = render_scene(scene, ctx())
    sky_part = out[:422]
    assert (sky_part == INK["blue"]).mean() > 0.4 and (sky_part == INK["white"]).mean() > 0.2
    assert (sky_part == INK["black"]).mean() < 0.02  # labels only
    assert not (out == INK["green"]).any()  # by day the lines are white and their names black
    proj = sky.Stereographic.fit(190, 170, 90, 800, 422)
    m = sky.sky_model(scene.draw_for, ctx())
    x, y, _ = proj.point(m.view.sun.alt, m.view.sun.az)
    disc = out[round(y) - 10:round(y) + 10, round(x) - 10:round(x) + 10]
    assert (disc == INK["yellow"]).all()


def test_day_sand_is_bright_and_night_sand_is_dark(ephemeris):
    day = render_scene(Scene("sky", "sky/x", local("2026-11-04T12:00")), ctx())[-20:]
    night = render_scene(Scene("sky", "sky/x", local("2026-11-04T21:30")), ctx())[-20:]
    assert (day == INK["yellow"]).mean() > 0.4 and (day == INK["black"]).mean() < 0.15
    assert (night == INK["black"]).mean() > 0.3


def with_sky(**changes):
    cfg = default_config()
    return replace(cfg, sky=replace(cfg.sky, **changes))


NOON, NINE_PM = local("2026-11-04T12:00"), local("2026-11-04T21:30")


def rendered(when, config=None):
    return render_scene(Scene("sky", "sky/x", when), ctx(config))


def test_day_lines_setting(ephemeris):
    lines = rendered(NOON)
    no_day_lines = rendered(NOON, with_sky(day_constellation_lines=False))
    assert not np.array_equal(lines, no_day_lines)
    # Off altogether is the same picture, so the main switch still rules by day.
    assert np.array_equal(rendered(NOON, with_sky(constellation_lines=False)), no_day_lines)
    assert np.array_equal(rendered(NINE_PM), rendered(NINE_PM, with_sky(day_constellation_lines=False)))


def test_sun_arc_setting(ephemeris):
    assert not np.array_equal(rendered(NOON), rendered(NOON, with_sky(sun_arc=False)))
    assert np.array_equal(rendered(NINE_PM), rendered(NINE_PM, with_sky(sun_arc=False)))


def test_day_magnitude_setting(ephemeris):
    bright_only = with_sky(day_magnitude_limit=-2.0, day_constellation_lines=False, labels=False)
    more = with_sky(day_magnitude_limit=3.0, day_constellation_lines=False, labels=False)
    assert not np.array_equal(rendered(NOON, bright_only), rendered(NOON, more))
    assert np.array_equal(rendered(NINE_PM), rendered(NINE_PM, with_sky(day_magnitude_limit=-2.0)))


def test_daytime_settings_are_in_the_config_and_the_dashboard():
    d = default_config().to_dict()["sky"]
    assert d["day_magnitude_limit"] == 2.0 and d["day_constellation_lines"] is True and d["sun_arc"] is True
    page = (REPO / "src/photopainter/web/static/app.js").read_text(encoding="utf-8")
    for key in ("day_magnitude_limit", "day_constellation_lines", "sun_arc"):
        assert f'"sky.{key}"' in page  # a label, not just the key's name


@pytest.mark.parametrize(("key", "bad"), [("day_magnitude_limit", 99), ("day_magnitude_limit", "lots"),
                                          ("day_constellation_lines", "yes"), ("sun_arc", 1)])
def test_bad_daytime_settings_fall_back(tmp_path, monkeypatch, key, bad):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"sky": {key: bad}}), encoding="utf-8")
    monkeypatch.setenv("PHOTOPAINTER_CONFIG", str(path))
    config, error = load_config()
    assert getattr(config.sky, key) == getattr(default_config().sky, key)
    assert f"sky.{key}" in error


def test_sun_gets_a_marker_off_the_side_and_none_when_drawn(ephemeris):
    proj = sky.Stereographic.fit(190, 170, 90, 800, 422)
    summer = sky.sky_model(local("2027-06-21T08:00"), ctx())  # up in the northeast
    marked = {body.name: left for body, left in sky.off_view(proj, summer, 800, 422, moon_up=False)}
    assert marked["Sun"] is True
    assert not sky.disc_on_screen(proj, summer.view.sun, 800, 422, 22)
    noon = sky.sky_model(local("2026-11-04T12:00"), ctx())
    assert "Sun" not in {body.name for body, _ in sky.off_view(proj, noon, 800, 422, moon_up=True)}
    assert sky.disc_on_screen(proj, noon.view.sun, 800, 422, 22)
    night = sky.sky_model(local("2026-11-04T21:30"), ctx())
    assert "Sun" not in {body.name for body, _ in sky.off_view(proj, night, 800, 422, moon_up=False)}


def test_the_sun_altaz_helper_agrees_with_the_sky_view(ephemeris):
    at = local("2026-11-04T09:00")
    alt, az = astro_sky.sun_altaz([at], *WEDGE)
    view = sky_view(at, *WEDGE, load_stars(DATA))
    assert float(alt[0]) == pytest.approx(view.sun.alt, abs=1e-6)
    assert float(az[0]) == pytest.approx(view.sun.az, abs=1e-6)


# --- the corners: the weather top left, the notes top right (S3) ---------------------------

WEATHER_NIGHT = "2026-11-03T21:30"  # the weather fixture was fetched that evening


def weather_fixture() -> dict:
    return json.loads((FIXTURES / "weather.json").read_text())


def with_weather(age=timedelta(minutes=20), now=None) -> Path:
    """The weather fixture in the state dir, fetched `age` before `now`."""
    now = local(WEATHER_NIGHT) if now is None else now
    data = weather_fixture()
    data["fetched_at"] = state.to_iso(now - age)
    folder = state.state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "weather.json").write_text(json.dumps(data))
    return folder


def test_sky_model_takes_the_weather_from_the_day_screens_cache(ephemeris):
    at = local(WEATHER_NIGHT)
    assert sky.sky_model(at, ctx()).weather is None  # no cache
    with_weather()
    w = sky.sky_model(at, ctx()).weather
    assert w is not None and w.temp == 64.5 and w.high == 74.3 and w.low == 57.8 and not w.stale
    with_weather(age=timedelta(hours=3))
    assert sky.sky_model(at, ctx()).weather.stale  # old: shown with "as of"
    with_weather(age=timedelta(hours=13))
    assert sky.sky_model(at, ctx()).weather is None  # too old to show
    with_weather()
    cfg = default_config()
    off = replace(cfg, weather=replace(cfg.weather, enabled=False))
    assert sky.sky_model(at, ctx(off)).weather is None


def test_the_weather_is_drawn_top_left_in_place_of_the_clock(ephemeris):
    scene = Scene("sky", "sky/x", local(WEATHER_NIGHT))
    bare = render_scene(scene, ctx())
    with_weather()
    shown = render_scene(scene, ctx())
    corner = (slice(6, 110), slice(6, 260))
    assert (shown[corner] == INK["white"]).sum() > (bare[corner] == INK["white"]).sum() + 500
    w = sky.sky_model(local(WEATHER_NIGHT), ctx()).weather
    x0, y0, x1, y1 = sky.weather_block(InkCanvas((800, 480)), w, False, sky.SkyStyle(), paint_it=False)
    block = shown[y0:y1, x0:x1]
    # Cloudy: no yellow Sun or crescent. The black is the cloud's outline.
    assert set(np.unique(block).tolist()) == {INK["blue"], INK["black"], INK["white"]}
    for when in ("2026-11-03T17:35", "2026-11-03T16:30"):  # dusk, and the Sun up
        out = render_scene(Scene("sky", f"sky/{when}", local(when)), ctx())
        assert out.shape == (480, 800) and (out[corner] == INK["white"]).sum() > 500


def test_names_go_on_boxes_only_while_the_sky_is_light():
    style = sky.SkyStyle()
    for alt in (0.0, 40.0, -1.9):
        assert sky.lit_style(style, alt).label_box
    for alt in (-2.0, -10.0, -15.0, -40.0):
        assert not sky.lit_style(style, alt).label_box


def test_the_corners_are_the_same_whatever_the_sun_is_doing():
    for alt in (40.0, 0.0, -1.9, -2.0, -10.0, -15.0, -40.0):
        style = sky.lit_style(sky.SkyStyle(), alt)
        assert style.corner_box == INK["blue"] and style.text == INK["white"]


def test_a_name_on_a_box_covers_what_is_behind_it():
    c = InkCanvas((200, 60), "green")
    boxed = replace(sky.SkyStyle(), label_box=True, label_halo=INK["white"])
    sky.name_label(c, boxed, (20, 30), "VEGA", 15, INK["black"])
    x0, y0, x1, y1 = sky.text_box(c, (20, 30), "VEGA", 15, "lm")
    out = c.indices()
    assert set(np.unique(out[y0:y1 + 1, x0:x1 + 1]).tolist()) == {INK["black"], INK["white"]}
    assert out[y0 - 1, x0] == INK["green"] and out[y0, x0 - 1] == INK["green"]  # nothing beyond the box


@pytest.mark.parametrize("when", ["2026-11-03T15:30", "2026-11-03T17:20", "2026-11-03T22:30"])
def test_both_corners_are_solid_blue_with_no_line_around_them(ephemeris, when):
    """By day, at dusk and at night."""
    at = local(when)
    with_weather(now=at)
    out = render_scene(Scene("sky", "sky/x", at), ctx())
    blue, white = INK["blue"], INK["white"]
    m = sky.sky_model(at, ctx())
    style = sky.lit_style(sky.SkyStyle(), m.view.sun.alt)
    _, _, x1, y1 = sky.weather_block(InkCanvas((800, 480)), m.weather, m.view.sun.alt > 0, style, paint_it=False)
    assert (out[0, :x1] == blue).all() and (out[:y1, 0] == blue).all()  # against the screen's edges
    r = sky.CORNER_RADIUS
    assert (out[y1, :x1 - r] == blue).all() and (out[:y1 - r, x1] == blue).all()  # its other two sides: no line
    assert (out[:y1, :x1] == blue).mean() > 0.6 and (out[:y1, :x1] == white).sum() > 500
    x0, _, _, y = sky.note_rows(m, 800)[1]
    assert (out[0, x0 + r:] == blue).all() and (out[:y, -1] == blue).all()
    assert (out[y, x0 + r:] == blue).all() and (out[:y - r, x0] == blue).all()
    assert (out[:y, x0:] == blue).mean() > 0.6 and (out[:y, x0:] == white).sum() > 200


def test_a_corner_box_rounds_only_the_corner_facing_the_middle():
    green, blue = INK["green"], INK["blue"]
    c = InkCanvas((200, 100), "green")
    sky.corner_panel(c, (0, 0, 60, 40), blue)
    sky.corner_panel(c, (140, 0, 200, 40), blue)
    out = c.indices()
    assert out[0, 0] == blue and out[40, 0] == blue and out[0, 60] == blue  # the left box: square at the edges
    assert out[40, 60] == green and out[28, 60] == blue and out[40, 48] == blue  # rounded inside
    assert out[0, 199] == blue and out[40, 199] == blue and out[0, 140] == blue  # the right box, mirrored
    assert out[40, 140] == green and out[28, 140] == blue and out[40, 152] == blue
    assert out[41, 30] == green and out[20, 100] == green  # nothing beyond them


def test_weather_block_stays_inside_the_box_it_reserves():
    w = load_weather(weather_fixture(), local("2026-11-03T23:10"))
    assert w is not None
    for case in (w, replace(w, wind=None), replace(w, stale=True), replace(w, wind_dir=None, high=None, low=None),
                 replace(w, code=0, temp=102.4, gust=55.0)):
        c = InkCanvas((800, 480), "black")
        box = sky.weather_block(c, case, False, sky.SkyStyle(), paint_it=False)
        assert not (c.indices() != INK["black"]).any()  # measuring draws nothing
        assert sky.weather_block(c, case, False, sky.SkyStyle()) == box
        ys, xs = np.nonzero(c.indices() != INK["black"])
        assert box[0] <= xs.min() and xs.max() <= box[2] and box[1] <= ys.min() and ys.max() <= box[3]
        assert box[0] == 0 and box[1] == 0
        # The text stays inside the blue: nothing but the box's own blue reaches its far edge.
        assert (c.indices()[1:box[3] - sky.CORNER_RADIUS, box[2] - 3:box[2] + 1] == INK["blue"]).all()


def test_wind_words():
    w = load_weather(weather_fixture(), local("2026-11-03T23:10"))
    assert sky.wind_words(w) == "NW 5–11 mph"
    assert sky.wind_words(replace(w, gust=None)) == "NW 5 mph"
    assert sky.wind_words(replace(w, gust=4.0)) == "NW 5 mph"  # gusts no higher than the wind
    assert sky.wind_words(replace(w, wind_dir=None)) == "– 5–11 mph"


def drawn_on_blue(kind: str) -> set[int]:
    c = InkCanvas((80, 80), "blue")
    sky.sky_weather_icon(c, 40, 40, 40, kind, INK["white"])
    return set(np.unique(c.indices()).tolist()) - {INK["blue"]}


def test_night_weather_icons_are_stars_not_a_crescent():
    assert drawn_on_blue("moon") == {INK["white"]}
    assert INK["yellow"] not in drawn_on_blue("moon_cloud")


@pytest.mark.parametrize("kind, inks", [
    ("sun", {"yellow"}),  # no black rays
    ("sun_cloud", {"yellow", "white", "black"}),
    ("cloud", {"white", "black"}),
    ("rain", {"white", "black"}),
    ("snow", {"white", "black"}),
    ("fog", {"white"}),
    ("thunder", {"white", "black", "yellow"}),
])
def test_every_weather_icon_shows_on_the_blue_corner(kind, inks):
    """The day screen's blue rain and snow and black fog would be lost on the blue."""
    assert drawn_on_blue(kind) == {INK[n] for n in inks}


def test_the_rain_and_fog_only_change_on_a_dark_background():
    """The day screen draws its icons on white, as before."""
    def drawn(kind: str, **kw) -> set[int]:
        c = InkCanvas((80, 80), "white")
        weather_icon(c, 40, 40, 40, kind, **kw)
        return set(np.unique(c.indices()).tolist())

    assert INK["blue"] in drawn("rain") and INK["blue"] in drawn("snow") and INK["black"] in drawn("fog")
    assert INK["blue"] not in drawn("rain", on_dark=True)
    c, d = InkCanvas((80, 80), "white"), InkCanvas((80, 80), "white")
    weather_icon(c, 40, 40, 40, "thunder")
    weather_icon(d, 40, 40, 40, "thunder", on_dark=True)
    assert (c.indices() == INK["black"]).sum() > (d.indices() == INK["black"]).sum()  # a white cloud on dark


def test_the_notes_top_right_show_the_next_sunrise_all_night(ephemeris):
    night = sky.sky_model(local("2026-11-04T21:30"), ctx())
    notes = sky.top_notes(night)
    assert [n[0] for n in notes] == ["moon", "sun"]  # soonest first
    assert notes[0][1].startswith("Moon rises ") and notes[1][1].startswith("Sun rises 6:14 am")
    assert notes[0][2] < notes[1][2]
    day = sky.sky_model(local("2026-11-04T12:00"), ctx())
    assert sky.top_notes(day)[0][1].startswith("Sun sets 4:56 pm")
