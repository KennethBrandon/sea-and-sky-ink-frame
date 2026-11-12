import json
import random
from datetime import time
from pathlib import Path

import pytest

from photopainter import state
from photopainter.config import (
    Config,
    ConfigError,
    QuietHours,
    Step,
    default_config,
    load_config,
    save_config,
    validate,
)

REPO = Path(__file__).resolve().parents[1]


def test_example_file_is_the_defaults():
    example = json.loads((REPO / "config.example.json").read_text())
    assert example == default_config().to_dict()
    assert validate(example) == default_config()


def test_defaults_match_prd():
    c = default_config()
    assert (c.location.name, c.location.lat, c.location.lon) == ("The Wedge", 33.593, -117.882)
    assert c.location.tz.key == "America/Los_Angeles"
    assert c.photos.show_now_minutes == 10
    # every screen ten minutes an hour by day, a photo on the hour; the sky hourly at night
    assert c.schedule.day == (Step("photo", 10), Step("tides", 10), Step("harbor", 10),
                              Step("sky", 10), Step("sea", 10), Step("combo", 10))
    assert c.schedule.night == (Step("sky", 60),)
    assert c.schedule.quiet_hours is None
    assert (c.tides.station, c.tides.datum) == ("9410580", "MLLW")
    assert (c.tides.chart_days_behind, c.tides.chart_days_ahead) == (1, 2)
    assert (c.tides.label_tides, c.tides.label_times, c.tides.label_heights) == ("all", True, "extremes")
    assert c.tides.flip_arrows is True  # the Viewer faces south
    assert (c.sky.center_azimuth_deg, c.sky.field_of_view_deg, c.sky.max_altitude_deg) == (190, 170, 90)
    assert c.photos.path == Path.home() / "photos"
    assert c.photos.hidden == ()


def test_round_trip():
    data = default_config().to_dict()
    data["schedule"]["quiet_hours"] = {"start": "23:30", "end": "05:00"}
    data["photos"]["hidden"] = ["a.jpg", "b.png"]
    data["display"]["portrait"] = True
    c = validate(data)
    assert c.schedule.quiet_hours == QuietHours(time(23, 30), time(5, 0))
    assert c.photos.hidden == ("a.jpg", "b.png")
    assert validate(c.to_dict()) == c
    assert json.loads(json.dumps(c.to_dict())) == c.to_dict()


def test_battery_setting():
    b = default_config().battery
    assert (b.low_volts, b.indicator_volts) == (3.3, 3.6)
    assert validate({"battery": {"low_volts": 3.6}}).battery.low_volts == 3.6
    for bad in (2.9, 4.2, "low"):
        with pytest.raises(ConfigError, match="battery.low_volts"):
            validate({"battery": {"low_volts": bad}})
    assert validate({"battery": {"indicator_volts": 4.2}}).battery.indicator_volts == 4.2  # always, on battery
    for bad in (2.9, 4.3, "soon"):
        with pytest.raises(ConfigError, match="battery.indicator_volts"):
            validate({"battery": {"indicator_volts": bad}})
    both = validate({"battery": {"indicator_volts": 3.1, "low_volts": 3.5}}).battery  # used as given
    assert (both.indicator_volts, both.low_volts) == (3.1, 3.5)
    assert validate({"battery": {"low_volts": 3.5}}).to_dict()["battery"]["indicator_volts"] == 3.6  # an old file


def test_tide_chart_settings_round_trip():
    c = validate({"tides": {"chart_days_behind": 0.5, "chart_days_ahead": 7, "label_times": False,
                            "label_heights": "all", "flip_arrows": False, "label_tides": "daily"}})
    assert c.tides.flip_arrows is False
    assert c.tides.label_tides == "daily"
    assert (c.tides.chart_days_behind, c.tides.chart_days_ahead) == (0.5, 7.0)
    assert (c.tides.label_times, c.tides.label_heights) == (False, "all")
    assert validate(c.to_dict()) == c
    assert validate({"tides": {"chart_days_behind": 0, "chart_days_ahead": 0.25}}).tides.chart_days_behind == 0


def test_the_harbor_has_its_own_tide_chart_settings():
    c = validate({"harbor": {"chart_days_behind": 3, "chart_days_ahead": 7, "label_tides": "daily",
                             "label_times": False, "label_heights": "none"}})
    assert (c.harbor.chart_days_behind, c.harbor.chart_days_ahead) == (3, 7)
    assert (c.harbor.label_tides, c.harbor.label_times, c.harbor.label_heights) == ("daily", False, "none")
    assert (c.tides.chart_days_behind, c.tides.chart_days_ahead) == (1, 2)  # the day screen's are its own
    assert validate(c.to_dict()) == c
    assert c.chart_days_behind == 3  # the caches reach back as far as either chart
    assert validate({"tides": {"chart_days_behind": 5}}).chart_days_behind == 5


@pytest.mark.parametrize("data, fragment", [
    ({"chart_days_behind": 8}, "harbor.chart_days_behind"),
    ({"chart_days_ahead": 15}, "harbor.chart_days_ahead"),
    ({"chart_days_behind": 0, "chart_days_ahead": 0.1}, "harbor.chart_days_ahead: days behind and ahead"),
    ({"label_tides": "some"}, "harbor.label_tides"),
    ({"label_heights": "some"}, "harbor.label_heights"),
    ({"label_times": "yes"}, "harbor.label_times"),
])
def test_bad_harbor_chart_settings_are_rejected(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        validate({"harbor": data})


def test_rotations_round_trip():
    c = validate({"schedule": {"day": [{"screen": "tides", "minutes": 30}, {"screen": "sky", "minutes": 30}],
                               "night": [{"screen": "sky", "minutes": 45}]}})
    assert c.schedule.day == (Step("tides", 30), Step("sky", 30))
    assert c.schedule.night == (Step("sky", 45),)
    assert validate(c.to_dict()) == c
    assert c.to_dict()["schedule"]["day"] == [{"screen": "tides", "minutes": 30}, {"screen": "sky", "minutes": 30}]
    assert "tides_refresh_minutes" not in c.to_dict()["schedule"]


def test_refresh_minutes_before_rotations_still_load():
    # the Pi's config.json on 2026-11-04: one screen a period, redrawn every so often
    c = validate({"schedule": {"tides_refresh_minutes": 30, "sky_refresh_minutes": 45}})
    assert c.schedule.day == (Step("tides", 30),) and c.schedule.night == (Step("sky", 45),)
    assert validate({"schedule": {"tides_refresh_minutes": 30}}).schedule.night == (Step("sky", 60),)
    # a rotation beside the old key: the rotation counts
    c = validate({"schedule": {"tides_refresh_minutes": 30, "day": [{"screen": "sky", "minutes": 20}]}})
    assert c.schedule.day == (Step("sky", 20),)
    assert c.schedule.refresh_minutes("sky", day=True) == 20
    assert c.schedule.refresh_minutes("tides", day=True) == 60  # not in either rotation: hourly


# --- schedule.photo, from before a photo was a rotation step ------------------------------


def rotation(*steps):
    return [{"screen": screen, "minutes": minutes} for screen, minutes in steps]


def steps(*pairs):
    return tuple(Step(screen, minutes) for screen, minutes in pairs)


def old(photo=None, **schedule):
    rule = {"enabled": True, "at_minute": 0, "every_minutes": 60, "show_minutes": 10, "during": "day"}
    return validate({"schedule": {"photo": {**rule, **(photo or {})}, **schedule}})


def test_photo_step_round_trips():
    c = validate({"schedule": {"day": rotation(("photo", 60)), "night": rotation(("sky", 30), ("photo", 5))}})
    assert c.schedule.day == steps(("photo", 60))
    assert c.schedule.night == steps(("sky", 30), ("photo", 5))
    assert validate(c.to_dict()) == c


@pytest.mark.parametrize("before, every, show, after", [
    # the cycle divides `every`: the steps repeat up to it, the first one shortened
    ([("tides", 30)], 60, 10, [("photo", 10), ("tides", 20), ("tides", 30)]),  # the Pi's config
    ([("tides", 60)], 60, 10, [("photo", 10), ("tides", 50)]),
    ([("tides", 30), ("sky", 30)], 60, 10, [("photo", 10), ("tides", 20), ("sky", 30)]),
    ([("tides", 60)], 120, 15, [("photo", 15), ("tides", 45), ("tides", 60)]),
    # ... and what's left of a step under 3 min goes to the next, so the cycle stays `every`
    ([("tides", 12), ("sky", 48)], 60, 10, [("photo", 10), ("sky", 50)]),
    ([("tides", 12)], 12, 10, [("photo", 12)]),
    ([("tides", 5), ("sky", 5)], 20, 10, [("photo", 10), ("tides", 5), ("sky", 5)]),
    # otherwise: a photo first, the first step shortened or dropped
    ([("tides", 45)], 60, 10, [("photo", 10), ("tides", 35)]),
    ([("tides", 12), ("sky", 33)], 60, 10, [("photo", 10), ("sky", 33)]),
    ([("tides", 40)], 30, 10, [("photo", 10), ("tides", 30)]),
])
def test_old_photo_rule_folds_into_the_rotation(before, every, show, after):
    c = old({"every_minutes": every, "show_minutes": show}, day=rotation(*before))
    assert c.schedule.day == steps(*after)
    assert c.schedule.night == steps(("sky", 60))  # `during` was day
    assert c.photos.show_now_minutes == show
    assert validate(c.to_dict()) == c


def test_old_photo_rule_keeps_to_the_step_limit():
    c = old(day=rotation(("tides", 3)))
    assert len(c.schedule.day) == 12 and c.schedule.day[0] == Step("photo", 10)
    assert sum(s.minutes for s in c.schedule.day) == 60
    assert validate(c.to_dict()) == c
    c = old({"every_minutes": 50}, day=rotation(*[("tides", 7)] * 12))  # the cycle doesn't divide 50
    assert len(c.schedule.day) == 12 and c.schedule.day[0] == Step("photo", 10)
    assert validate(c.to_dict()) == c


def test_old_photo_rule_follows_during():
    c = old({"during": "night"}, day=rotation(("tides", 30)))
    assert c.schedule.day == steps(("tides", 30))
    assert c.schedule.night == steps(("photo", 10), ("sky", 50))
    c = old({"during": "always"}, day=rotation(("tides", 60)), night=rotation(("sky", 30)))
    assert c.schedule.day == steps(("photo", 10), ("tides", 50))
    assert c.schedule.night == steps(("photo", 10), ("sky", 20), ("sky", 30))


def test_old_photo_rule_disabled_leaves_the_rotations_alone():
    c = old({"enabled": False, "show_minutes": 25}, day=rotation(("tides", 30)))
    assert c.schedule.day == steps(("tides", 30)) and c.schedule.night == steps(("sky", 60))
    assert c.photos.show_now_minutes == 25
    # no day rotation in the file: the old default, not today's with its photo
    c = old({"enabled": False})
    assert c.schedule.day == steps(("tides", 60))


def test_old_photo_rule_without_a_rotation_gives_what_it_meant():
    # a photo on the hour and tides the rest, not today's default with every screen in it
    c = old()
    assert c.schedule.day == steps(("photo", 10), ("tides", 50)) and c.schedule.night == steps(("sky", 60))
    c = validate({"schedule": {"photo": {"show_minutes": 5}}})  # keys left out: the old defaults
    assert c.schedule.day == steps(("photo", 5), ("tides", 55)) and c.photos.show_now_minutes == 5


def test_old_photo_rule_ignores_at_minute():
    assert old({"at_minute": 30}, day=rotation(("tides", 30))) == old(day=rotation(("tides", 30)))


def test_old_photo_rule_with_the_older_refresh_keys():
    c = old(tides_refresh_minutes=30, sky_refresh_minutes=45)
    assert c.schedule.day == steps(("photo", 10), ("tides", 20), ("tides", 30))
    assert c.schedule.night == steps(("sky", 45))
    c = old({"during": "always"}, tides_refresh_minutes=30, sky_refresh_minutes=45)
    assert c.schedule.night == steps(("photo", 10), ("sky", 35))


def test_old_photo_rule_too_short_to_draw_adds_no_step():
    c = old({"show_minutes": 2}, day=rotation(("tides", 30)))
    assert c.schedule.day == steps(("tides", 30)) and c.photos.show_now_minutes == 2


def test_old_photo_rule_leaves_a_photo_step_alone():
    c = old(day=rotation(("photo", 20), ("tides", 40)))
    assert c.schedule.day == steps(("photo", 20), ("tides", 40))


def test_show_now_minutes_in_the_file_wins_over_the_old_rule():
    data = {"schedule": {"photo": {"show_minutes": 25}}, "photos": {"show_now_minutes": 7}}
    assert validate(data).photos.show_now_minutes == 7


def test_old_photo_rule_is_not_written_back():
    data = old(day=rotation(("tides", 30))).to_dict()
    assert set(data["schedule"]) == {"day", "night", "night_offset_minutes", "quiet_hours"}
    assert data["photos"]["show_now_minutes"] == 10


def test_old_photo_rule_with_whole_floats():
    c = old({"every_minutes": 60.0, "show_minutes": 10.0}, day=rotation(("tides", 30)))
    assert c.schedule.day == steps(("photo", 10), ("tides", 20), ("tides", 30))


@pytest.mark.parametrize("junk", [
    "x", 7, [], True,
    {"every_minutes": "often"}, {"every_minutes": 0}, {"every_minutes": 100000}, {"show_minutes": -1},
    {"show_minutes": True}, {"show_minutes": 1.5}, {"enabled": "yes"}, {"during": "dusk"},
    {"show_minutes": 60, "every_minutes": 60},  # never a valid rule
])
def test_old_photo_rule_that_cant_be_read_is_ignored(junk, caplog):
    c = validate({"schedule": {"photo": junk}})
    assert c == default_config()
    assert "schedule.photo" in caplog.text


def test_old_photo_rule_with_unknown_keys_or_null_still_loads():
    assert validate({"schedule": {"photo": {"at_mintue": 5}}}).schedule == old().schedule
    assert validate({"schedule": {"photo": None}}) == default_config()


def test_refresh_minutes_of_a_photo_step():
    day_photo = validate({"schedule": {"day": rotation(("tides", 20), ("photo", 15))}}).schedule
    assert day_photo.refresh_minutes("photo", day=True) == 15
    assert day_photo.refresh_minutes("photo", day=False) == 15  # the other period's
    none = validate({"schedule": {"day": rotation(("tides", 20))}}).schedule
    assert none.refresh_minutes("photo", day=True) == 60


@pytest.mark.parametrize("before, after", [
    ([("photo", 10), ("tides", 50)], [("tides", 60)]),
    ([("tides", 50), ("photo", 10)], [("tides", 60)]),
    ([("tides", 20), ("photo", 10), ("sky", 30)], [("tides", 30), ("sky", 30)]),
    ([("photo", 5), ("photo", 5), ("sky", 50)], [("sky", 60)]),
    ([("photo", 10), ("tides", 20), ("tides", 30)], [("tides", 30), ("tides", 30)]),
    ([("tides", 60)], [("tides", 60)]),
    ([("photo", 60)], [("tides", 60)]),  # only photos: the period's own screen
])
def test_without_photos_keeps_the_cycle(before, after):
    sched = validate({"schedule": {"day": rotation(*before), "night": rotation(("photo", 45))}}).schedule
    assert sched.has_photos()
    bare = sched.without_photos()
    assert bare.day == steps(*after)
    assert bare.night == steps(("sky", 45))
    assert not bare.has_photos()


def test_missing_keys_take_defaults():
    c = validate({"photos": {"show_now_minutes": 5}, "sky": {"center_azimuth_deg": 165}})
    assert c.photos.show_now_minutes == 5
    assert c.photos.fit == "auto"
    assert c.sky.center_azimuth_deg == 165.0
    assert c.location == default_config().location
    assert validate({}) == default_config()


def test_config_is_immutable_and_hashable():
    c = default_config()
    with pytest.raises(AttributeError):
        c.location.lat = 0  # type: ignore[misc]
    assert hash(c) == hash(default_config())


@pytest.mark.parametrize(
    "data, fragment",
    [
        ({"locaton": {}}, "locaton: unknown setting"),
        ({"location": {"lat": 91}}, "location.lat: must be a number from -90 up to 90"),
        ({"location": {"lat": "33.6"}}, "location.lat"),
        ({"location": {"lat": float("nan")}}, "location.lat"),
        ({"location": {"timezone": "Pacific/Wedge"}}, "location.timezone: unknown time zone"),
        ({"location": {"timezone": "../etc/passwd"}}, "location.timezone"),
        ({"location": {"name": ""}}, "location.name: must be non-empty text"),
        ({"display": {"portrait": 1}}, "display.portrait: must be true or false"),
        ({"photos": {"show_now_minutes": 0}}, "photos.show_now_minutes: must be a whole number from 1 to 1440"),
        ({"photos": {"show_now_minutes": True}}, "photos.show_now_minutes"),
        ({"photos": {"show_now_minutes": 1.5}}, "photos.show_now_minutes"),
        ({"schedule": {"tides_refresh_minutes": 2}}, "schedule.tides_refresh_minutes: must be a whole number from 3"),
        ({"schedule": {"day": []}}, "schedule.day: must have at least one screen"),
        ({"schedule": {"day": "tides"}}, "schedule.day: must be a list of screens"),
        ({"schedule": {"day": [{"screen": "moon", "minutes": 30}]}}, "schedule.day[0].screen: must be one of tides, sky, sea, photo, harbor, combo"),
        ({"schedule": {"day": [{"screen": "sky", "minutes": 30}, {"screen": "moon", "minutes": 30}]}},
         "schedule.day[1].screen"),
        ({"schedule": {"night": [{"screen": "sky", "minutes": 2}]}},
         "schedule.night[0].minutes: must be a whole number from 3 to 1440"),
        ({"schedule": {"night": [{"screen": "sky"}]}}, "schedule.night[0].minutes"),
        ({"schedule": {"day": [{"minutes": 30}]}}, "schedule.day[0].screen"),
        ({"schedule": {"day": [{"screen": "sky", "minutes": 30, "color": "red"}]}}, "schedule.day[0].color: unknown setting"),
        ({"schedule": {"day": [{"screen": "sky", "minutes": 30}] * 13}}, "schedule.day: at most 12 screens"),
        ({"schedule": {"night_offset_minutes": 500}}, "schedule.night_offset_minutes"),
        ({"schedule": {"quiet_hours": {"start": "24:00", "end": "05:00"}}}, "schedule.quiet_hours.start"),
        ({"schedule": {"quiet_hours": {"start": "1:00", "end": "05:00"}}}, "schedule.quiet_hours.start"),
        ({"schedule": {"quiet_hours": {"start": "05:00", "end": "05:00"}}}, "start and end must differ"),
        ({"schedule": {"quiet_hours": "00:00-05:00"}}, "schedule.quiet_hours: must be an object"),
        ({"tides": {"station": "941058"}}, "tides.station: must be a 7-digit NOAA station id"),
        ({"tides": {"datum": "mllw"}}, "tides.datum: must be one of"),
        ({"tides": {"chart_days_behind": -0.5}}, "tides.chart_days_behind: must be a number from 0 up to 7"),
        ({"tides": {"chart_days_behind": 7.5}}, "tides.chart_days_behind: must be a number from 0 up to 7"),
        ({"tides": {"chart_days_ahead": 15}}, "tides.chart_days_ahead: must be a number from 0 up to 14"),
        ({"tides": {"chart_days_ahead": "2"}}, "tides.chart_days_ahead: must be a number"),
        ({"tides": {"chart_days_behind": 0, "chart_days_ahead": 0.2}}, "must add up to at least 0.25"),
        ({"tides": {"label_tides": "weekly"}}, "tides.label_tides: must be one of"),
        ({"tides": {"label_times": "no"}}, "tides.label_times: must be true or false"),
        ({"tides": {"flip_arrows": 1}}, "tides.flip_arrows: must be true or false"),
        ({"tides": {"label_heights": "some"}}, "tides.label_heights: must be one of"),
        ({"sky": {"center_azimuth_deg": 360}}, "sky.center_azimuth_deg: must be a number from 0 below 360"),
        ({"sky": {"max_altitude_deg": 95}}, "sky.max_altitude_deg"),
        ({"photos": {"fit": "stretch"}}, "photos.fit: must be one of auto, fill, fit"),
        ({"photos": {"hidden": ["../x.jpg"]}}, "photos.hidden: must be a list of file names"),
        ({"photos": {"hidden": "a.jpg"}}, "photos.hidden"),
        (["not", "an", "object"], "config: must be an object"),
    ],
)
def test_bad_values_are_rejected_with_a_readable_message(data, fragment):
    with pytest.raises(ConfigError) as info:
        validate(data)
    assert fragment in str(info.value)


def test_every_problem_is_reported():
    with pytest.raises(ConfigError) as info:
        validate({"location": {"lat": 100, "lon": 200}, "sky": {"labels": "yes"}})
    assert len(info.value.errors) == 3
    assert "location.lat" in str(info.value) and "location.lon" in str(info.value)


def test_whole_floats_are_accepted_as_integers():
    assert validate({"photos": {"show_now_minutes": 5.0}}).photos.show_now_minutes == 5


def test_hidden_duplicates_dropped():
    assert validate({"photos": {"hidden": ["a.jpg", "a.jpg", "b.jpg"]}}).photos.hidden == ("a.jpg", "b.jpg")


# --- load_config / save_config -----------------------------------------------------


def test_load_missing_file_gives_defaults_and_no_error(tmp_path):
    assert load_config(tmp_path / "nope.json") == (default_config(), None)


def test_load_uses_env_path_by_default(isolated_paths):
    path = state.config_path()
    assert path == isolated_paths / "config" / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"display": {"portrait": True}}))
    config, error = load_config()
    assert error is None and config.display.portrait is True


def test_load_valid_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"schedule": {"night": [{"screen": "photo", "minutes": 60}]}}))
    config, error = load_config(path)
    assert error is None
    assert config.schedule.night == (Step("photo", 60),)


@pytest.mark.parametrize(
    "content, fragment",
    [
        ("{not json", "not valid JSON"),
        ("", "not valid JSON"),
        ("[]", "must be an object"),
        ("null", "must be an object"),
        ('{"location": {"lat": 1000}}', "location.lat"),
        ('{"photos": {"hidden": [1, 2]}}', "photos.hidden"),
    ],
)
def test_load_bad_file_gives_defaults_and_error(tmp_path, content, fragment):
    path = tmp_path / "config.json"
    path.write_text(content)
    config, error = load_config(path)
    assert config == default_config()
    assert error is not None and fragment in error and str(path) in error


def test_load_unreadable_file_never_raises(tmp_path):
    folder = tmp_path / "config.json"
    folder.mkdir()  # a directory where the file should be
    config, error = load_config(folder)
    assert config == default_config() and "can't read" in error
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe\x00garbage")
    config, error = load_config(binary)
    assert config == default_config() and error


def test_load_config_never_raises_on_random_json(tmp_path):
    rng = random.Random(7)
    leaves = [None, True, 0, -1, 1.5, 1e308, "", "x", "../a", [], {}, ["a"], {"a": 1}]
    keys = ["location", "lat", "schedule", "photo", "quiet_hours", "start", "sky", "photos",
            "hidden", "timezone", "during", "station", "datum", "tides"]

    def junk(depth=0):
        if depth > 3 or rng.random() < 0.3:
            return rng.choice(leaves)
        return {rng.choice(keys): junk(depth + 1) for _ in range(rng.randint(0, 4))}

    path = tmp_path / "config.json"
    for _ in range(300):
        path.write_text(json.dumps(junk()))
        config, error = load_config(path)
        assert isinstance(config, Config)
        assert error is None or isinstance(error, str)


def test_save_then_load(tmp_path):
    path = tmp_path / "sub" / "config.json"
    c = validate({"schedule": {"quiet_hours": {"start": "00:00", "end": "05:00"}}})
    save_config(c, path)
    assert load_config(path) == (c, None)
    assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]
