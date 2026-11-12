"""schedule.decide(): table-driven, with fake sun times so every edge is exact."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from photopainter.astro.sun import SunEvents
from photopainter.config import QuietHours, Step, default_config
from photopainter.schedule import (
    Decision, Overrides, Pin, decide, next_day_night_change, resolve_until, show_now_until,
    upcoming_change,
)

LA = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc


def fake_sun(rise: str = "06:12", set_: str = "16:57"):
    """The same local sunrise and sunset every day."""
    r, s = time.fromisoformat(rise), time.fromisoformat(set_)

    def sun(day: date) -> SunEvents:
        sunrise = datetime.combine(day, r, tzinfo=LA)
        sunset = datetime.combine(day, s, tzinfo=LA)
        return SunEvents(sunrise, sunset, sunrise - timedelta(minutes=25), sunset + timedelta(minutes=25))

    return sun


SUN = fake_sun()


def t(text: str, fold: int = 0) -> datetime:
    """Local time at the Wedge, e.g. t("2026-11-04T14:10")."""
    return datetime.fromisoformat(text).replace(tzinfo=LA, fold=fold)


def photo_then_tides():
    """The schedule most of these tests are written against: a photo for the first 10 minutes
    of every hour, then tides, and the sky hourly at night (the default until 2026-11-08).
    The default itself is tested in "the default day"."""
    c = default_config()
    return replace(c, schedule=replace(c.schedule, day=(Step("photo", 10), Step("tides", 50)),
                                       night=(Step("sky", 60),)))


def config(day=None, night=None, **schedule):
    """`day` / `night`: a rotation as [(screen, minutes), ...]."""
    c = photo_then_tides()
    sched = c.schedule
    if day is not None:
        schedule["day"] = tuple(Step(*s) for s in day)
    if night is not None:
        schedule["night"] = tuple(Step(*s) for s in night)
    return replace(c, schedule=replace(sched, **schedule))


def run(now, cfg=None, overrides=Overrides(), sun=SUN) -> Decision:
    return decide(now, cfg or photo_then_tides(), overrides, sun)


# --- the default day -------------------------------------------------------------------

DEFAULT_DAY = [
    # now (local)            kind      key                         draw for             next change
    ("2026-11-04T05:30", "sky", "sky/2026-11-04T05:00", "2026-11-04T05:30", "2026-11-04T06:00"),  # night: the sky, hourly
    ("2026-11-04T06:12", "tides", "tides/2026-11-04T06:12", "2026-11-04T06:12", "2026-11-04T06:20"),  # sunrise, mid-step
    ("2026-11-04T14:00", "photo", "photo/2026-11-04T14:00", "2026-11-04T14:00", "2026-11-04T14:10"),  # a photo on the hour
    ("2026-11-04T14:10", "tides", "tides/2026-11-04T14:10", "2026-11-04T14:10", "2026-11-04T14:20"),
    ("2026-11-04T14:20", "harbor", "harbor/2026-11-04T14:20", "2026-11-04T14:20", "2026-11-04T14:30"),
    ("2026-11-04T14:30", "sky", "sky/2026-11-04T14:30", "2026-11-04T14:35", "2026-11-04T14:40"),  # its middle
    ("2026-11-04T14:40", "sea", "sea/2026-11-04T14:40", "2026-11-04T14:45", "2026-11-04T14:50"),  # its middle
    ("2026-11-04T14:50", "combo", "combo/2026-11-04T14:50", "2026-11-04T14:55", "2026-11-04T15:00"),  # all four last
    ("2026-11-04T15:00", "photo", "photo/2026-11-04T15:00", "2026-11-04T15:00", "2026-11-04T15:10"),
    ("2026-11-04T16:57", "sky", "sky/2026-11-04T16:57", "2026-11-04T16:58:30", "2026-11-04T17:00"),  # sunset
    ("2026-11-04T17:00", "sky", "sky/2026-11-04T17:00", "2026-11-04T17:30", "2026-11-04T18:00"),
]


@pytest.mark.parametrize("now, kind, key, draw_for, next_at", DEFAULT_DAY)
def test_default_day_shows_every_screen_each_hour(now, kind, key, draw_for, next_at):
    """By day each screen gets ten minutes an hour, a photo on the hour and the combo last;
    by night, the sky alone, redrawn hourly."""
    d = decide(t(now), default_config(), Overrides(), SUN)
    assert (d.scene.kind, d.scene.key, d.next_change_at, d.quiet) == (kind, key, t(next_at), False)
    assert d.scene.draw_for == t(draw_for)


def test_default_day_without_photos_gives_the_photos_time_to_tides():
    sched = default_config().schedule.without_photos()
    assert sched.day[0] == Step("tides", 20)
    assert sched.night == default_config().schedule.night


# --- a photo, then tides ----------------------------------------------------------------

DAY = [
    # now (local)            kind     key                        next change
    ("2026-11-04T05:30", "sky", "sky/2026-11-04T05:00", "2026-11-04T06:00"),  # night, hourly on the hour
    ("2026-11-04T06:00", "sky", "sky/2026-11-04T06:00", "2026-11-04T06:12"),  # no photos in the night rotation
    ("2026-11-04T06:11:59", "sky", "sky/2026-11-04T06:00", "2026-11-04T06:12"),
    ("2026-11-04T06:12", "tides", "tides/2026-11-04T06:12", "2026-11-04T07:00"),  # sunrise: day screen
    ("2026-11-04T06:59", "tides", "tides/2026-11-04T06:12", "2026-11-04T07:00"),
    ("2026-11-04T07:00", "photo", "photo/2026-11-04T07:00", "2026-11-04T07:10"),  # the photo step
    ("2026-11-04T07:09:59", "photo", "photo/2026-11-04T07:00", "2026-11-04T07:10"),
    ("2026-11-04T07:10", "tides", "tides/2026-11-04T07:10", "2026-11-04T08:00"),  # the step after it
    ("2026-11-04T14:10", "tides", "tides/2026-11-04T14:10", "2026-11-04T15:00"),
    ("2026-11-04T16:00", "photo", "photo/2026-11-04T16:00", "2026-11-04T16:10"),
    ("2026-11-04T16:56:59", "tides", "tides/2026-11-04T16:10", "2026-11-04T16:57"),
    ("2026-11-04T16:57", "sky", "sky/2026-11-04T16:57", "2026-11-04T17:00"),  # sunset: the sky, to the hour
    ("2026-11-04T17:00", "sky", "sky/2026-11-04T17:00", "2026-11-04T18:00"),  # no photos at night
    ("2026-11-04T23:59", "sky", "sky/2026-11-04T23:00", "2026-11-05T00:00"),
]


@pytest.mark.parametrize("now, kind, key, next_at", DAY)
def test_photo_then_tides_day(now, kind, key, next_at):
    d = run(t(now))
    assert (d.scene.kind, d.scene.key, d.next_change_at, d.quiet) == (kind, key, t(next_at), False)
    if kind == "sky":  # the sky is drawn for the middle of its bucket
        start = t(key.split("/")[1])
        assert d.scene.draw_for == start + (t(next_at) - start) / 2
    else:  # tides and photos depict the moment they're drawn
        assert d.scene.draw_for == t(now)
    assert d.next_change_at.tzinfo is not None


def test_night_sky_is_redrawn_every_step_length():
    cfg = config(night=[("sky", 30)], day=[("tides", 120)])
    d = run(t("2026-11-04T17:40"), cfg)
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("sky", "sky/2026-11-04T17:30",
                                                             t("2026-11-04T18:00"))
    assert d.scene.draw_for == t("2026-11-04T17:45")


@pytest.mark.parametrize("minutes_later", [1, 5, 30, 49])
def test_same_inputs_a_minute_later_give_the_same_key(minutes_later):
    start = run(t("2026-11-04T14:10"))
    later = run(t("2026-11-04T14:10") + timedelta(minutes=minutes_later))
    assert later.scene.key == start.scene.key
    assert later.next_change_at == start.next_change_at
    assert run(t("2026-11-04T14:10")) == start  # and the same moment gives the same decision


def test_naive_time_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        run(datetime(2026, 11, 4, 14, 10))


def test_other_time_zones_give_the_same_answer():
    here = run(t("2026-11-04T14:10"))
    there = run(t("2026-11-04T14:10").astimezone(UTC))
    assert there.scene.key == here.scene.key and there.next_change_at == here.next_change_at


# --- sunrise and sunset ------------------------------------------------------------------


@pytest.mark.parametrize("now, key", [
    ("2026-11-04T05:41:59", "sky/2026-11-04T05:00"),  # night: hourly on the hour
    ("2026-11-04T05:42", "tides/2026-11-04T05:42"),  # day starts at sunrise 06:12 - 30 min
    ("2026-11-04T17:26:59", "tides/2026-11-04T17:00"),  # day: hourly on the hour
    ("2026-11-04T17:27", "sky/2026-11-04T17:27"),  # night starts at sunset 16:57 + 30 min
])
def test_night_offset_moves_both_edges(now, key):
    cfg = config(night_offset_minutes=30, day=[("tides", 60)])
    assert run(t(now), cfg).scene.key == key


def test_negative_night_offset_starts_night_before_sunset():
    cfg = config(night_offset_minutes=-20)
    assert run(t("2026-11-04T16:37"), cfg).scene.key == "sky/2026-11-04T16:37"
    assert run(t("2026-11-04T16:36"), cfg).scene.key == "tides/2026-11-04T16:10"


def test_next_day_night_change():
    assert next_day_night_change(t("2026-11-04T14:00"), default_config(), SUN) == t("2026-11-04T16:57")
    assert next_day_night_change(t("2026-11-04T20:00"), default_config(), SUN) == t("2026-11-05T06:12")
    assert next_day_night_change(t("2026-11-04T03:00"), default_config(), SUN) == t("2026-11-04T06:12")


# --- photo steps ---------------------------------------------------------------------------


def test_photo_steps_at_night_only():
    cfg = config(day=[("tides", 60)], night=[("photo", 10), ("sky", 50)])
    assert run(t("2026-11-04T12:00"), cfg).scene.kind == "tides"
    d = run(t("2026-11-04T21:05"), cfg)
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("photo", "photo/2026-11-04T21:00",
                                                             t("2026-11-04T21:10"))
    # the night screen after a photo is redrawn from the photo's end
    d = run(t("2026-11-04T21:30"), cfg)
    assert (d.scene.key, d.next_change_at) == ("sky/2026-11-04T21:10", t("2026-11-04T22:00"))


def test_photo_steps_day_and_night():
    cfg = config(night=[("photo", 10), ("sky", 50)])
    assert run(t("2026-11-04T12:00"), cfg).scene.kind == "photo"
    assert run(t("2026-11-04T03:00"), cfg).scene.kind == "photo"


def test_only_photos_is_a_slideshow():
    cfg = config(day=[("photo", 60)], night=[("photo", 60)])
    for now, key, next_at in [("2026-11-04T12:30", "photo/2026-11-04T12:00", "2026-11-04T13:00"),
                              ("2026-11-04T13:00", "photo/2026-11-04T13:00", "2026-11-04T14:00"),
                              ("2026-11-04T21:59", "photo/2026-11-04T21:00", "2026-11-04T22:00")]:
        d = run(t(now), cfg)
        assert (d.scene.kind, d.scene.key, d.scene.photo, d.next_change_at) == ("photo", key, None, t(next_at))
    # sunset cuts a step, and the next photo starts there
    assert run(t("2026-11-04T16:57"), cfg).scene.key == "photo/2026-11-04T16:57"
    assert run(t("2026-11-04T16:30"), cfg).next_change_at == t("2026-11-04T16:57")


def test_two_photo_steps_in_a_row_are_two_photos():
    cfg = config(day=[("photo", 20), ("photo", 40)])
    assert run(t("2026-11-04T12:10"), cfg).scene.key == "photo/2026-11-04T12:00"
    assert run(t("2026-11-04T12:30"), cfg).scene.key == "photo/2026-11-04T12:20"


def test_no_photo_steps():
    cfg = config(day=[("tides", 60)])
    d = run(t("2026-11-04T12:00"), cfg)
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("tides", "tides/2026-11-04T12:00",
                                                             t("2026-11-04T13:00"))


def test_photo_step_inside_a_rotation():
    cfg = config(day=[("tides", 30), ("photo", 15), ("tides", 75)])  # 120 min from midnight
    assert run(t("2026-11-04T08:40"), cfg).scene.key == "photo/2026-11-04T08:30"
    assert run(t("2026-11-04T09:40"), cfg).scene.kind == "tides"
    assert run(t("2026-11-04T10:44"), cfg).next_change_at == t("2026-11-04T10:45")


def test_sunset_cuts_a_photo_short():
    d = run(t("2026-11-04T17:00"), sun=fake_sun(set_="17:05"))
    assert (d.scene.kind, d.next_change_at) == ("photo", t("2026-11-04T17:05"))
    assert run(t("2026-11-04T17:05"), sun=fake_sun(set_="17:05")).scene.key == "sky/2026-11-04T17:05"


def test_photo_cut_under_three_minutes_is_skipped():
    sun = fake_sun(set_="17:02")
    d = run(t("2026-11-04T17:00"), sun=sun)
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("tides", "tides/2026-11-04T16:10",
                                                             t("2026-11-04T17:02"))


def test_photo_starts_early_rather_than_after_a_short_first_bucket():
    # sunrise two minutes before the hour: no two-minute day screen before the photo
    d = run(t("2026-11-04T06:58"), sun=fake_sun(rise="06:58"))
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("photo", "photo/2026-11-04T06:58",
                                                             t("2026-11-04T07:10"))


def test_photo_runs_on_to_a_change_under_three_minutes_away():
    cfg = config(day=[("tides", 47), ("photo", 10), ("tides", 3)])
    d = run(t("2026-11-04T16:55"), cfg, sun=fake_sun(set_="16:58"))
    assert (d.scene.kind, d.next_change_at) == ("photo", t("2026-11-04T16:58"))


def test_short_last_bucket_is_merged_into_the_one_before():
    cfg = config(day=[("tides", 60)])
    sun = fake_sun(set_="17:02")  # buckets on the hour; the 17:00 one would last 2 min
    for now in ("2026-11-04T16:30", "2026-11-04T17:01"):
        d = run(t(now), cfg, sun=sun)
        assert (d.scene.key, d.next_change_at) == ("tides/2026-11-04T16:00", t("2026-11-04T17:02"))


# --- rotations ---------------------------------------------------------------------------------

ALTERNATING = config(day=[("tides", 30), ("sky", 30)], night=[("sky", 60)])
WITH_PHOTO = config(day=[("photo", 10), ("tides", 20), ("sky", 30)], night=[("sky", 60)])


@pytest.mark.parametrize("now, kind, key, next_at", [
    ("2026-11-04T06:12", "tides", "tides/2026-11-04T06:12", "2026-11-04T06:30"),  # sunrise, mid-step
    ("2026-11-04T06:30", "sky", "sky/2026-11-04T06:30", "2026-11-04T07:00"),
    ("2026-11-04T07:00", "photo", "photo/2026-11-04T07:00", "2026-11-04T07:10"),
    ("2026-11-04T07:10", "tides", "tides/2026-11-04T07:10", "2026-11-04T07:30"),  # the step after the photo
    ("2026-11-04T07:30", "sky", "sky/2026-11-04T07:30", "2026-11-04T08:00"),
    ("2026-11-04T16:55", "sky", "sky/2026-11-04T16:30", "2026-11-04T16:57"),  # cut by sunset
    ("2026-11-04T16:57", "sky", "sky/2026-11-04T16:57", "2026-11-04T17:00"),  # the night rotation, on the hour
])
def test_day_rotation_with_a_photo_changes_on_the_half_hour(now, kind, key, next_at):
    d = run(t(now), WITH_PHOTO)
    assert (d.scene.kind, d.scene.key, d.next_change_at) == (kind, key, t(next_at))
    if kind == "sky":  # a sky step is drawn for the middle of its bucket, by day too
        start = t(key.split("/")[1])
        assert d.scene.draw_for == start + (t(next_at) - start) / 2
    else:
        assert d.scene.draw_for == t(now)


def test_the_order_of_the_steps_sets_when_each_runs():
    first = config(day=[("photo", 10), ("tides", 50)])
    last = config(day=[("tides", 50), ("photo", 10)])
    assert run(t("2026-11-04T09:05"), first).scene.kind == "photo"
    assert run(t("2026-11-04T09:05"), last).scene.kind == "tides"
    assert run(t("2026-11-04T09:55"), first).scene.kind == "tides"
    assert run(t("2026-11-04T09:55"), last).scene.kind == "photo"


def test_rotation_is_anchored_at_local_midnight_not_sunrise():
    # a 50-minute cycle counts from 00:00, whatever sunrise is, and starts again at midnight
    cfg = config(day=[("tides", 50)], night=[("sky", 50)])
    assert run(t("2026-11-04T06:12"), cfg).scene.key == "tides/2026-11-04T06:12"
    d = run(t("2026-11-04T06:40"), cfg)  # 8 x 50 min after midnight
    assert (d.scene.key, d.next_change_at) == ("tides/2026-11-04T06:40", t("2026-11-04T07:30"))
    d = run(t("2026-11-04T23:50"), cfg)
    assert (d.scene.key, d.next_change_at) == ("sky/2026-11-04T23:20", t("2026-11-05T00:00"))
    assert run(t("2026-11-05T00:00"), cfg).scene.key == "sky/2026-11-05T00:00"


def test_short_first_bucket_starts_the_next_step_early():
    cfg = config(night=[("tides", 30), ("sky", 30)])
    d = run(t("2026-11-04T16:58"), cfg, sun=fake_sun(set_="16:58"))  # the sky step has 2 min left
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("tides", "tides/2026-11-04T16:58",
                                                             t("2026-11-04T17:30"))


def test_the_same_screen_on_either_side_of_a_photo():
    cfg = config(day=[("sky", 30), ("photo", 10), ("sky", 20)])
    d = run(t("2026-11-04T09:20"), cfg)
    assert (d.scene.key, d.next_change_at) == ("sky/2026-11-04T09:00", t("2026-11-04T09:30"))
    d = run(t("2026-11-04T09:45"), cfg)
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("sky", "sky/2026-11-04T09:40",
                                                             t("2026-11-04T10:00"))
    assert d.scene.draw_for == t("2026-11-04T09:50")


def test_refresh_minutes_for_pins():
    sched = ALTERNATING.schedule
    assert sched.refresh_minutes("sky", day=True) == 30
    assert sched.refresh_minutes("sky", day=False) == 60
    assert sched.refresh_minutes("tides", day=False) == 30  # no tides at night: the day's step
    assert photo_then_tides().schedule.refresh_minutes("tides", day=False) == 50  # [photo 10, tides 50]
    assert photo_then_tides().schedule.refresh_minutes("photo", day=False) == 10
    assert default_config().schedule.refresh_minutes("harbor", day=True) == 10  # every step is 10 min
    assert default_config().schedule.refresh_minutes("sky", day=False) == 60
    assert ALTERNATING.schedule.refresh_minutes("photo", day=True) == 60  # no photo steps: hourly
    # a tides pin at night redraws every 30 min with this day rotation
    d = run(t("2026-11-04T19:30"), ALTERNATING, pin("tides", until="2026-11-04T23:00"))
    assert (d.scene.key, d.next_change_at) == ("tides/2026-11-04T19:02", t("2026-11-04T19:32"))


def test_up_next_goes_first_and_skips_hidden():
    cfg = replace(default_config(), photos=replace(default_config().photos, hidden=("hidden.jpg",)))
    ov = Overrides(up_next=("hidden.jpg", "beach.jpg", "dog.jpg"))
    assert run(t("2026-11-04T12:00"), cfg, ov).scene.photo == "beach.jpg"
    assert run(t("2026-11-04T12:00")).scene.photo is None  # the rotation
    assert run(t("2026-11-04T12:20"), cfg, ov).scene.photo is None  # not a photo screen


# --- daylight saving -----------------------------------------------------------------------


def test_fall_back_day():
    # 2026-11-01: 02:00 PDT -> 01:00 PST. Night buckets stay an hour apart in real time.
    d = run(t("2026-11-01T00:30"))
    assert d.scene.key == "sky/2026-11-01T00:00"
    assert d.next_change_at.astimezone(UTC) == datetime(2026, 11, 1, 8, 0, tzinfo=UTC)  # 01:00 PDT
    first_one_thirty = run(t("2026-11-01T01:30", fold=0))
    second_one_thirty = run(t("2026-11-01T01:30", fold=1))
    assert first_one_thirty.next_change_at.astimezone(UTC) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert second_one_thirty.next_change_at.astimezone(UTC) == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    # the two 01:00 buckets get different keys, so the panel redraws for the second
    assert first_one_thirty.scene.key == "sky/2026-11-01T01:00"
    assert second_one_thirty.scene.key == "sky/2026-11-01T01:00-0800"
    # photos stay on the hour: :00 PST that afternoon
    d = run(t("2026-11-01T13:05"))
    assert (d.scene.key, d.next_change_at) == ("photo/2026-11-01T13:00", t("2026-11-01T13:10"))
    assert d.next_change_at.utcoffset() == timedelta(hours=-8)


def test_spring_forward_day():
    # 2026-03-08: 02:00 PST -> 03:00 PDT.
    d = run(t("2026-03-08T01:30"))
    assert d.scene.key == "sky/2026-03-08T01:00"
    assert d.next_change_at.astimezone(UTC) == datetime(2026, 3, 8, 10, 0, tzinfo=UTC)  # 03:00 PDT
    d = run(t("2026-03-08T03:30"))  # an hour after 01:30 PST
    assert (d.scene.key, d.next_change_at) == ("sky/2026-03-08T03:00", t("2026-03-08T04:00"))
    d = run(t("2026-03-08T12:05"))
    assert (d.scene.key, d.next_change_at) == ("photo/2026-03-08T12:00", t("2026-03-08T12:10"))
    assert d.next_change_at.utcoffset() == timedelta(hours=-7)


# --- pins -----------------------------------------------------------------------------------


def pin(kind, until=None, photo=None, set_at="2026-11-04T19:02"):
    return Overrides(pin=Pin(kind, None if until is None else t(until), photo,
                             None if set_at is None else t(set_at)))


def test_unexpired_tides_pin_replaces_the_schedule():
    d = run(t("2026-11-04T19:30"), overrides=pin("tides", until="2026-11-04T23:00"))
    assert (d.scene.kind, d.scene.key, d.next_change_at, d.quiet) == (
        "tides", "tides/2026-11-04T19:02", t("2026-11-04T19:52"), False)  # the tides step: 50 min


def test_pin_until_comes_before_the_next_bucket():
    d = run(t("2026-11-04T19:30"), overrides=pin("tides", until="2026-11-04T19:45"))
    assert d.next_change_at == t("2026-11-04T19:45")


def test_expired_pin_is_ignored():
    ov = pin("photo", until="2026-11-04T19:12", photo="beach.jpg")
    assert run(t("2026-11-04T19:11:59"), overrides=ov).scene.photo == "beach.jpg"
    d = run(t("2026-11-04T19:12"), overrides=ov)
    assert d == run(t("2026-11-04T19:12"))


def test_pin_until_cleared_never_expires():
    d = run(t("2027-01-01T12:00"), overrides=pin("tides", until=None))
    assert d.scene.kind == "tides" and d.next_change_at is not None  # still redrawn hourly


def test_show_now_pin():
    d = run(t("2026-11-04T19:05"), overrides=pin("photo", until="2026-11-04T19:12", photo="beach.jpg"))
    assert (d.scene.kind, d.scene.photo, d.scene.key, d.next_change_at) == (
        "photo", "beach.jpg", "photo/2026-11-04T19:02/beach.jpg", t("2026-11-04T19:12"))


def test_photo_mode_pin_rotates_and_uses_up_next():
    ov = replace(pin("photo", until=None), up_next=("dog.jpg",))
    d = run(t("2026-11-04T21:30"), overrides=ov)
    assert (d.scene.kind, d.scene.photo, d.scene.key, d.next_change_at) == (
        "photo", "dog.jpg", "photo/2026-11-04T21:22", t("2026-11-04T21:32"))  # the photo step: 10 min


def test_photo_mode_pin_with_no_photo_step_is_hourly():
    d = run(t("2026-11-04T21:30"), config(day=[("tides", 60)]), pin("photo", until=None))
    assert (d.scene.key, d.next_change_at) == ("photo/2026-11-04T21:02", t("2026-11-04T22:02"))


def test_sky_pin_shows_the_sky_by_day():
    d = run(t("2026-11-04T12:30"), config(day=[("tides", 60), ("sky", 20)]),
            pin("sky", until="2026-11-04T16:57", set_at="2026-11-04T12:02"))
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("sky", "sky/2026-11-04T12:22",
                                                             t("2026-11-04T12:42"))
    assert d.scene.draw_for == t("2026-11-04T12:32")


def test_combo_step_draws_its_window_for_the_middle_and_its_tide_for_the_start():
    d = run(t("2026-11-04T12:10"), config(day=[("combo", 60)]))
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("combo", "combo/2026-11-04T12:00", t("2026-11-04T13:00"))
    assert d.scene.draw_for == t("2026-11-04T12:30")
    assert d.scene.updated == t("2026-11-04T12:00")


def test_combo_pin_redraws_in_buckets_like_the_others():
    d = run(t("2026-11-04T19:30"), config(night=[("combo", 20)]), pin("combo", set_at="2026-11-04T19:02"))
    assert (d.scene.kind, d.scene.key, d.next_change_at) == ("combo", "combo/2026-11-04T19:22", t("2026-11-04T19:42"))
    assert d.scene.draw_for == t("2026-11-04T19:32")
    assert d.scene.updated == t("2026-11-04T19:22")


def test_pin_without_set_at_counts_from_midnight():
    d = run(t("2026-11-04T19:30"), config(day=[("tides", 60)]), pin("tides", set_at=None))
    assert d.scene.key == "tides/2026-11-04T19:00"


def test_overrides_from_file_data():
    data = {"pin": {"kind": "sky", "photo": None, "until": "2026-11-03T18:00:00-08:00",
                    "set_at": "2026-11-03T16:02:11-08:00"},
            "up_next": ["beach.jpg", "../etc/passwd", 7, "dog.jpg"],
            "redraw_at": "2026-11-03T17:30:00-08:00"}
    ov = Overrides.from_dict(data)
    assert ov.pin == Pin("sky", t("2026-11-03T18:00"), None, t("2026-11-03T16:02:11"))
    assert ov.up_next == ("beach.jpg", "dog.jpg")
    assert ov.redraw_at == t("2026-11-03T17:30")
    assert Overrides.from_dict(ov.to_dict()) == ov
    assert ov.redrawn().redraw_at is None and ov.redrawn().pin == ov.pin
    assert Overrides.from_dict({"redraw_at": "soon"}).redraw_at is None  # junk: no request
    assert Overrides().to_dict() == {"pin": None, "up_next": [], "redraw_at": None}


@pytest.mark.parametrize("bad_pin", [
    "sky", {"kind": "weather"}, {"kind": "sky", "until": "tomorrow"},
    {"kind": "sky", "until": "2026-11-03T18:00"},  # no UTC offset
    {"kind": "tides", "photo": "x.jpg"}, {"kind": "photo", "photo": "a/b.jpg"},
])
def test_unusable_pin_means_auto(bad_pin):
    assert Overrides.from_dict({"pin": bad_pin}).pin is None
    assert Overrides.from_dict(None) == Overrides() == Overrides.from_dict({"up_next": "x"})


def test_without_expired_and_consume():
    ov = replace(pin("tides", until="2026-11-04T20:00"), up_next=("a.jpg", "b.jpg", "a.jpg"))
    assert ov.without_expired(t("2026-11-04T19:59")).pin is not None
    assert ov.without_expired(t("2026-11-04T20:00")).pin is None
    assert ov.consume("a.jpg").up_next == ("b.jpg", "a.jpg")
    assert ov.consume("zzz.jpg") is ov


# --- quiet hours -------------------------------------------------------------------------------

OVERNIGHT = config(quiet_hours=QuietHours(time(22, 0), time(6, 0)))


@pytest.mark.parametrize("now, quiet, next_at", [
    ("2026-11-04T21:59", False, "2026-11-04T22:00"),
    ("2026-11-04T22:00", True, "2026-11-05T06:00"),
    ("2026-11-04T23:59", True, "2026-11-05T06:00"),
    ("2026-11-05T00:00", True, "2026-11-05T06:00"),
    ("2026-11-05T05:59", True, "2026-11-05T06:00"),
    ("2026-11-05T06:00", False, "2026-11-05T06:12"),
])
def test_quiet_hours_across_midnight(now, quiet, next_at):
    d = run(t(now), OVERNIGHT)
    assert (d.quiet, d.next_change_at) == (quiet, t(next_at))
    assert d.scene.key == run(t(now)).scene.key  # the decision itself is unchanged


def test_quiet_hours_within_a_day():
    cfg = config(quiet_hours=QuietHours(time(13, 0), time(14, 30)))
    assert run(t("2026-11-04T13:00"), cfg).quiet
    assert run(t("2026-11-04T14:29"), cfg).next_change_at == t("2026-11-04T14:30")
    assert not run(t("2026-11-04T14:30"), cfg).quiet
    assert not run(t("2026-11-04T12:59"), cfg).quiet


def test_pins_apply_in_quiet_hours():
    d = run(t("2026-11-04T23:00"), OVERNIGHT, pin("tides", until="2026-11-05T01:00"))
    assert (d.quiet, d.scene.key, d.next_change_at) == (False, "tides/2026-11-04T22:22",
                                                        t("2026-11-04T23:12"))


# --- next change for status.json -------------------------------------------------------------


def test_upcoming_change():
    assert upcoming_change(t("2026-11-04T14:10"), photo_then_tides(), Overrides(), SUN) == (
        t("2026-11-04T15:00"), "photo")
    assert upcoming_change(t("2026-11-04T15:03"), photo_then_tides(), Overrides(), SUN) == (
        t("2026-11-04T15:10"), "tides")
    assert upcoming_change(t("2026-11-04T14:10"), default_config(), Overrides(), SUN) == (
        t("2026-11-04T14:20"), "harbor")
    assert upcoming_change(t("2026-11-04T23:00"), OVERNIGHT, Overrides(), SUN) == (
        t("2026-11-05T06:00"), "sky")  # quiet hours end before sunrise (06:12)
    # a pin ending in quiet hours: the frame keeps it until they end
    ov = pin("photo", until="2026-11-04T23:10", photo="beach.jpg", set_at="2026-11-04T21:50")
    assert upcoming_change(t("2026-11-04T21:55"), OVERNIGHT, ov, SUN) == (t("2026-11-05T06:00"), "sky")
    assert upcoming_change(t("2026-11-04T21:55"), default_config(), Overrides(pin=Pin("photo", None, "x.jpg")),
                           SUN) is None


# --- helpers for the dashboard ---------------------------------------------------------------


def test_resolve_until():
    now, cfg = t("2026-11-04T14:00"), default_config()
    assert resolve_until(None, now, cfg, SUN) is None
    assert resolve_until("next_change", now, cfg, SUN) == t("2026-11-04T16:57")
    assert resolve_until("1h", now, cfg, SUN) == t("2026-11-04T15:00")
    assert resolve_until("30m", now, cfg, SUN) == t("2026-11-04T14:30")
    assert resolve_until("2026-11-04T18:00:00-08:00", now, cfg, SUN) == t("2026-11-04T18:00")
    for bad in ("2026-11-04T13:00:00-08:00", "2026-11-04T18:00", "0h", "soon", 5):
        with pytest.raises(ValueError):
            resolve_until(bad, now, cfg, SUN)


def test_show_now_until():
    assert show_now_until(t("2026-11-04T14:03"), default_config()) == t("2026-11-04T14:13")
    longer = replace(default_config(), photos=replace(default_config().photos, show_now_minutes=25))
    assert show_now_until(t("2026-11-04T14:03"), longer) == t("2026-11-04T14:28")
    # across the fall-back change: 10 real minutes
    end = show_now_until(t("2026-11-01T01:55", fold=0), default_config())
    assert end.astimezone(UTC) - t("2026-11-01T01:55").astimezone(UTC) == timedelta(minutes=10)


# --- a whole day with real sun times ---------------------------------------------------------


def test_keys_change_exactly_at_next_change(ephemeris):
    """Tick every minute for two days on the default schedule: the key changes iff
    `next_change_at` has passed, and the panel refreshes six times an hour by day and once
    an hour at night."""
    from photopainter.astro.sun import sun_events

    cfg = default_config()
    loc = cfg.location

    def sun(day):
        return sun_events(day, loc.lat, loc.lon, loc.tz)

    now = t("2026-11-04T00:00")
    prev = run(now, cfg, sun=sun)
    changes = {"day": 0, "night": 0}
    for _ in range(2 * 24 * 60):
        now += timedelta(minutes=1)
        d = run(now, cfg, sun=sun)
        assert (d.scene.key != prev.scene.key) == (prev.next_change_at <= now), now
        if d.scene.key != prev.scene.key:
            s = sun(now.date())
            changes["day" if s.sunrise <= now < s.sunset + timedelta(minutes=1) else "night"] += 1
        prev = d
    # 2 days: ~10.7 h of daylight a day at 6 an hour, ~13.3 h of night at 1 an hour
    assert 124 <= changes["day"] <= 136, changes
    assert 24 <= changes["night"] <= 30, changes
