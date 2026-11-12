"""The buttons: pins, presses into actions, and what each action does to the frame."""

import json
import sys
import types
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from PIL import Image

from photopainter import buttons, cli, state
from photopainter.buttons import BACK, LOW_POWER, NEXT, PHOTOS, WIFI_SETUP, Decoder
from photopainter.config import ConfigError, QuietHours, Step, default_config, load_config
from photopainter.hardware.buttons import ButtonsUnavailable, FakeButtons, GpioButtons
from photopainter.render import battery_badge
from photopainter.render.draw import INK
from photopainter.schedule import Cycle, Overrides, Pin, decide, move_cycle
from photopainter.slideshow import save_order
from test_tick import SUN, Frame

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 4, 9, 20, tzinfo=LA)


# --- presses into actions ------------------------------------------------------------------


def play(steps: list[tuple[float, bool, bool]], decoder: Decoder | None = None) -> list[tuple[float, str]]:
    """Hold each (seconds, forward, back) in turn, sampled every 10 ms: the actions, with when."""
    decoder = decoder or Decoder()
    now, out = 0.0, []
    for seconds, forward, back in steps:
        end = now + seconds
        while now < end:
            action = decoder.feed(now, forward, back)
            if action:
                out.append((round(now, 2), action))
            now += 0.01
    return out


UP = (0.2, False, False)


def actions(*steps) -> list[str]:
    return [action for _, action in play(list(steps))]


def test_a_press_fires_when_released():
    assert actions(UP, (0.15, True, False), UP) == [NEXT]
    assert actions(UP, (0.15, False, True), UP) == [BACK]
    when, _ = play([(0.5, False, False), (0.3, True, False), UP])[0]
    assert when > 0.8  # not while it's still down


def test_two_presses_fire_twice():
    assert actions(UP, (0.1, True, False), UP, (0.1, True, False), UP, (0.1, False, True), UP) == [NEXT, NEXT, BACK]


def test_a_flicker_is_not_a_press():
    assert actions(UP, (0.02, True, False), UP) == []


def test_a_bouncing_contact_counts_once():
    bounce = [(0.01, True, False), (0.01, False, False)] * 3
    release = [(0.01, False, False), (0.01, True, False)] * 3
    assert actions(UP, *bounce, (0.1, True, False), *release, UP) == [NEXT]


def test_both_held_for_a_second_turns_low_power_over_once():
    when = play([UP, (2.5, True, True), UP])
    assert [a for _, a in when] == [LOW_POWER]
    assert when[0][0] == pytest.approx(0.2 + 0.04 + 1.0, abs=0.05)  # without waiting to be let go


def test_both_held_too_briefly_does_nothing():
    assert actions(UP, (0.5, True, True), UP) == []


def test_letting_go_of_both_one_at_a_time_fires_nothing_more():
    assert actions(UP, (1.5, True, True), (0.3, False, True), UP) == [LOW_POWER]


def test_buttons_pressed_a_moment_apart_still_count_as_both():
    assert actions(UP, (0.15, True, False), (1.3, True, True), UP) == [LOW_POWER]


def test_overlapping_presses_are_called_off():
    assert actions(UP, (0.1, True, False), (0.1, True, True), (0.1, False, True), UP) == []


def test_working_again_after_both():
    assert actions(UP, (1.5, True, True), UP, (0.1, True, False), UP) == [LOW_POWER, NEXT]


def test_forward_held_then_let_go_is_the_long_press():
    assert actions(UP, (1.3, True, False), UP) == [PHOTOS]
    assert actions(UP, (0.9, True, False), UP) == [NEXT]


def test_the_long_press_fires_when_let_go_not_before():
    when, action = play([UP, (2.0, True, False), UP])[0]
    assert action == PHOTOS and when > 2.2


def test_back_held_then_let_go_is_wifi_setup():
    assert actions(UP, (1.3, False, True), UP) == [WIFI_SETUP]


def test_back_held_too_briefly_is_still_back():
    assert actions(UP, (0.9, False, True), UP) == [BACK]


def test_something_leaning_on_a_button_is_not_a_press():
    assert actions(UP, (6.0, True, False), UP) == []
    assert actions(UP, (6.0, True, False), UP, (0.1, True, False), UP) == [NEXT]


def test_a_pin_held_down_for_good_does_nothing():
    assert actions((10.0, True, False)) == []


# --- watching ------------------------------------------------------------------------------


def run_watch(samples, *, busy=False):
    """Watch `samples` (one a poll) with a clock that follows the polls."""
    samples = list(samples)
    fake, seen, ticks = FakeButtons(samples), [], [0]

    def sleep(_):
        ticks[0] += 1

    buttons.watch(fake, seen.append, busy=lambda: busy, stop=lambda: ticks[0] > len(samples) + 5,
                  clock=lambda: ticks[0] * buttons.POLL_S, sleep=sleep)
    return seen


PRESS = [(False, False)] * 3 + [(True, False)] * 5 + [(False, False)] * 5


def test_watch_reports_a_press():
    assert run_watch(PRESS) == [NEXT]


def test_watch_ignores_a_press_while_busy():
    assert run_watch(PRESS, busy=True) == []


class Ticks:
    def __init__(self, running=False, queued=False):
        self.state = {"running": running, "queued": queued}

    def snapshot(self):
        return self.state


@pytest.mark.parametrize("ticks, going_up", [
    (Ticks(), False),
    (Ticks(running=True), True),
    (Ticks(queued=True), True),
])
def test_a_press_is_still_going_up_until_it_has_reached_the_wall(ticks, going_up):
    assert buttons._going_up(ticks) is going_up


# --- the pins ------------------------------------------------------------------------------


@pytest.fixture
def fake_lgpio(monkeypatch):
    mod = types.ModuleType("lgpio")
    mod.SET_PULL_UP = 32
    mod.error = RuntimeError
    mod.claims, mod.levels, mod.closed = {}, {14: 1, 15: 1}, []
    mod.gpiochip_open = lambda chip: 7
    mod.gpio_claim_input = lambda handle, pin, flags: mod.claims.__setitem__(pin, flags)
    mod.gpio_read = lambda handle, pin: mod.levels[pin]
    mod.gpiochip_close = mod.closed.append
    monkeypatch.setitem(sys.modules, "lgpio", mod)
    return mod


def test_pins_are_rxd_and_txd_pulled_up_and_read_low_when_pressed(fake_lgpio):
    pins = GpioButtons()
    assert fake_lgpio.claims == {15: 32, 14: 32}
    assert pins.read() == (False, False)
    fake_lgpio.levels[15] = 0
    assert pins.read() == (True, False)  # RXD is forward
    fake_lgpio.levels[15], fake_lgpio.levels[14] = 1, 0
    assert pins.read() == (False, True)  # TXD is back
    pins.close()
    pins.close()
    assert fake_lgpio.closed == [7]


def test_pins_in_use_say_so(fake_lgpio):
    def busy(handle, pin, flags):
        raise fake_lgpio.error("GPIO busy")

    fake_lgpio.gpio_claim_input = busy
    with pytest.raises(ButtonsUnavailable, match="GPIO busy"):
        GpioButtons()
    assert fake_lgpio.closed == [7]


def test_buttons_command_without_lgpio_explains_itself(monkeypatch):
    monkeypatch.setitem(sys.modules, "lgpio", None)  # import raises ImportError
    assert cli.main(["buttons"]) == 2


# --- which photo ---------------------------------------------------------------------------


@pytest.fixture
def frame(tmp_path, monkeypatch):
    return Frame(tmp_path, monkeypatch)


def press(frame, action, at=NOW):
    config = load_config()[0]
    return buttons.step_photo(action, at, config, frame.dir)


def pin_of(frame) -> Pin | None:
    return Overrides.from_dict(frame.overrides()).pin


def last_shown(frame):
    return json.loads((frame.dir / state.SLIDESHOW).read_text()).get(str(frame.photos.resolve()))


def test_forward_with_no_photo_up_starts_the_rotation(frame):
    assert press(frame, NEXT) == "a.jpg"
    pin = pin_of(frame)
    assert (pin.kind, pin.photo, pin.set_at) == ("photo", "a.jpg", NOW)
    assert pin.until == NOW + timedelta(minutes=default_config().photos.show_now_minutes)
    assert last_shown(frame) == "a.jpg"  # the schedule's next photo carries on from here


def test_forward_and_back_walk_the_photos_and_wrap(frame):
    assert [press(frame, NEXT) for _ in range(4)] == ["a.jpg", "b.jpg", "c.jpg", "a.jpg"]
    assert [press(frame, BACK) for _ in range(2)] == ["c.jpg", "b.jpg"]
    assert press(frame, NEXT) == "c.jpg"
    assert last_shown(frame) == "c.jpg"


def test_a_photo_up_from_the_schedule_is_where_they_start(frame):
    state.write_json(frame.dir / state.STATUS, {"showing": {"kind": "photo", "photo": "b.jpg", "key": "x"}})
    assert press(frame, NEXT) == "c.jpg"
    state.write_json(frame.dir / state.OVERRIDES, Overrides().to_dict())
    assert press(frame, BACK) == "a.jpg"


def test_back_from_another_screen_is_the_photo_shown_last(frame):
    state.write_json(frame.dir / state.STATUS, {"showing": {"kind": "tides", "photo": None, "key": "x"}})
    state.write_json(frame.dir / state.SLIDESHOW, {str(frame.photos.resolve()): "b.jpg"})
    assert press(frame, BACK) == "b.jpg"


def test_forward_from_another_screen_is_the_next_in_the_rotation(frame):
    state.write_json(frame.dir / state.STATUS, {"showing": {"kind": "tides", "photo": None, "key": "x"}})
    state.write_json(frame.dir / state.SLIDESHOW, {str(frame.photos.resolve()): "b.jpg"})
    assert press(frame, NEXT) == "c.jpg"


def test_they_follow_the_dashboards_order(frame):
    save_order(frame.photos, ["c.jpg", "a.jpg", "b.jpg"])
    assert [press(frame, NEXT) for _ in range(2)] == ["c.jpg", "a.jpg"]
    assert press(frame, BACK) == "c.jpg"
    assert press(frame, BACK) == "b.jpg"


def test_hidden_photos_are_skipped(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, hidden=("b.jpg",))
    assert [press(frame, NEXT) for _ in range(2)] == ["a.jpg", "c.jpg"]
    assert press(frame, BACK) == "a.jpg"


def test_forward_takes_up_next_first_and_uses_it_up(frame):
    frame.overrides({"pin": None, "up_next": ["c.jpg", "b.jpg"], "redraw_at": None})
    assert press(frame, NEXT) == "c.jpg"
    assert frame.overrides()["up_next"] == ["b.jpg"]
    assert press(frame, BACK) == "b.jpg"  # seen now, by either button
    assert frame.overrides()["up_next"] == []


def test_forward_does_not_stick_on_a_queued_photo_that_is_already_up(frame):
    pin = Pin("photo", until=NOW + timedelta(hours=1), photo="b.jpg", set_at=NOW)
    frame.overrides(Overrides(pin=pin, up_next=("b.jpg",)).to_dict())
    assert press(frame, NEXT) == "c.jpg"


def test_a_photo_that_has_gone_keeps_its_place(frame):
    frame.overrides(Overrides(pin=Pin("photo", until=NOW + timedelta(hours=1), photo="b.jpg", set_at=NOW)).to_dict())
    (frame.photos / "b.jpg").unlink()
    assert press(frame, BACK) == "a.jpg"
    frame.overrides(Overrides(pin=Pin("photo", until=NOW + timedelta(hours=1), photo="b.jpg", set_at=NOW)).to_dict())
    assert press(frame, NEXT) == "c.jpg"


def test_an_expired_pin_is_not_where_they_start(frame):
    frame.overrides(Overrides(pin=Pin("photo", until=NOW - timedelta(minutes=1), photo="b.jpg")).to_dict())
    assert press(frame, NEXT) == "a.jpg"


def test_nothing_to_do_with_one_photo_up_or_none(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, photos=("only.jpg",))
    assert press(frame, NEXT) == "only.jpg"
    assert press(frame, NEXT) is None and press(frame, BACK) is None
    (frame.photos / "only.jpg").unlink()
    assert press(frame, NEXT) is None


# --- low power -----------------------------------------------------------------------------


def test_low_power_turns_over_keeping_everything_else_and_asks_for_a_redraw(frame):
    before = json.loads(state.config_path().read_text())
    assert before["battery"]["low_power"] is False
    assert buttons.toggle_low_power(NOW, frame.dir) is True
    after = json.loads(state.config_path().read_text())
    assert after["battery"].pop("low_power") is True
    before["battery"].pop("low_power")
    assert after == before
    assert frame.overrides()["redraw_at"] == state.to_iso(NOW)
    assert buttons.toggle_low_power(NOW, frame.dir) is False


def test_a_bad_settings_file_is_left_alone(frame):
    state.config_path().write_text("{not json")
    with pytest.raises(ConfigError):
        buttons.toggle_low_power(NOW, frame.dir)
    assert state.config_path().read_text() == "{not json"
    assert buttons.act(LOW_POWER, NOW) is False  # and a press just logs it


def test_a_press_that_goes_wrong_does_not_stop_the_watching(frame, monkeypatch):
    def broken(*_):
        raise OSError("disk full")

    monkeypatch.setattr(buttons, "step", broken)
    assert buttons.act(NEXT, NOW, SUN) is False


# --- on the frame --------------------------------------------------------------------------


def show_low_power(frame):
    assert buttons.act(LOW_POWER, frame.now) is True


def test_a_pressed_photo_goes_up_and_the_schedule_comes_back(frame):
    frame.tick("2026-11-04T09:20")
    assert frame.status["showing"]["kind"] == "tides"
    frame.now = frame.now + timedelta(minutes=2)
    assert buttons.act(NEXT, frame.now, SUN) is True
    frame.tick()
    assert frame.status["showing"]["photo"] == "a.jpg"
    frame.tick("2026-11-04T09:45")  # the photo step's 10 minutes are up: tides follows it
    assert frame.status["showing"]["kind"] == "tides"


def test_the_mark_shows_while_low_power_is_on_and_goes_when_it_is_off(frame):
    def corner():
        png = np.asarray(Image.open(frame.dir / state.CURRENT_PNG).convert("RGB"))
        return png[-30:, -90:].copy()

    frame.tick("2026-11-04T09:20")
    plain = corner()
    frame.now += timedelta(minutes=2)
    show_low_power(frame)
    frame.tick()
    marked = corner()
    assert frame.panel.refreshes == 2 and (marked != plain).any()
    frame.now += timedelta(minutes=2)
    show_low_power(frame)
    frame.tick()
    assert frame.panel.refreshes == 3 and (corner() == plain).all()


def test_redrawing_a_rotation_photo_keeps_that_photo(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch)
    frame.tick("2026-11-04T09:01")  # the default day rotation starts on a photo
    first = frame.status["showing"]
    assert first["kind"] == "photo"
    frame.now += timedelta(minutes=2)
    show_low_power(frame)
    frame.tick()
    assert frame.panel.refreshes == 2
    assert frame.status["showing"]["photo"] == first["photo"]
    assert frame.status["showing"]["key"] == first["key"]


def test_press_command_runs_a_tick(frame, capsys):
    assert cli.main(["press", "next", "--fake-panel", "--at", "2026-11-04T09:20"]) == 0
    assert state.read_json(frame.dir / state.STATUS)["showing"]["photo"] == "a.jpg"


# --- moving through the schedule -----------------------------------------------------------


DAY = (Step("photo", 10), Step("tides", 10), Step("sky", 10), Step("tides", 20))
NIGHT = (Step("sky", 30), Step("tides", 30))


def config_with(day=DAY, night=NIGHT):
    c = default_config()
    return replace(c, schedule=replace(c.schedule, day=day, night=night))


def moved(at, by, overrides=None, config=None):
    return move_cycle(at, config or config_with(), overrides or Overrides(), SUN, by)


def kind_at(at, cycle, config=None):
    return decide(at, config or config_with(), Overrides(cycle=cycle), SUN).scene.kind


def test_moving_starts_the_next_step_now_and_the_rest_follow_in_order():
    config = config_with()
    at = NOW  # 09:20 on the grid from midnight: 50-minute cycles, so 09:10 photo, 09:20 tides, 09:30 sky
    assert decide(at, config, Overrides(), SUN).scene.kind == "tides"
    cycle = moved(at, 1)
    assert cycle.step == 2 and cycle.anchor == NOW
    assert [kind_at(at + timedelta(minutes=m), cycle) for m in (0, 9, 10, 29, 30, 39, 40, 49, 50)] == \
        ["sky", "sky", "tides", "tides", "photo", "photo", "tides", "tides", "sky"]


def test_moving_back_and_wrapping_around():
    assert moved(NOW, -1).step == 0
    photo_step = NOW - timedelta(minutes=10)  # 09:10 is the photo step
    assert moved(photo_step, -1).step == len(DAY) - 1
    last = NOW + timedelta(minutes=20)  # 09:40: the second tides, its last
    assert moved(last, 1).step == 0


def test_moves_add_up_from_where_the_last_one_put_it():
    first = moved(NOW, 1)
    second = moved(NOW + timedelta(minutes=2), 1, Overrides(cycle=first))
    assert (first.step, second.step) == (2, 3)
    assert moved(NOW + timedelta(minutes=2), -1, Overrides(cycle=first)).step == 1


def test_a_screen_twice_in_the_rotation_keeps_its_place():
    second_tides = moved(NOW + timedelta(minutes=10), 1)  # 09:30 is sky; next is the second tides
    assert second_tides.step == 3
    assert moved(NOW + timedelta(minutes=11), 1, Overrides(cycle=second_tides)).step == 0


def test_a_move_starts_on_the_minute_so_its_changes_fall_on_the_ticks():
    cycle = moved(NOW + timedelta(seconds=37), 1)
    assert cycle.anchor == NOW
    decision = decide(NOW + timedelta(seconds=37), config_with(), Overrides(cycle=cycle), SUN)
    assert decision.next_change_at == NOW + timedelta(minutes=10)


def test_a_move_lasts_until_the_period_ends_then_the_clock_has_it_again():
    at = datetime(2026, 11, 4, 16, 30, tzinfo=LA)  # sunset is 16:57 in these tests
    cycle = moved(at, 1)
    assert cycle.until == datetime(2026, 11, 4, 16, 57, tzinfo=LA)
    assert kind_at(datetime(2026, 11, 4, 17, 0, tzinfo=LA), cycle) == "sky"  # the night's first
    assert not cycle.active(datetime(2026, 11, 4, 16, 57, tzinfo=LA))


def test_a_move_at_night_lasts_until_midnight_where_the_grid_starts_again():
    at = datetime(2026, 11, 4, 23, 5, tzinfo=LA)
    assert moved(at, 1).until == datetime(2026, 11, 5, 0, 0, tzinfo=LA)


def test_only_one_step_has_nowhere_to_move():
    assert moved(NOW, 1, config=config_with(day=(Step("photo", 60),))) is None


def test_a_pin_comes_first_and_the_move_carries_on_after_it():
    cycle = moved(NOW, 1)
    pin = Pin("tides", until=NOW + timedelta(minutes=5), set_at=NOW)
    overrides = Overrides(pin=pin, cycle=cycle)
    assert decide(NOW + timedelta(minutes=1), config_with(), overrides, SUN).scene.kind == "tides"
    assert decide(NOW + timedelta(minutes=6), config_with(), overrides, SUN).scene.kind == "sky"


def test_a_shorter_rotation_after_a_settings_change_drops_the_move():
    cycle = Cycle(NOW, 3, NOW + timedelta(hours=2))
    config = config_with(day=(Step("photo", 10), Step("tides", 40)))
    assert decide(NOW, config, Overrides(cycle=cycle), SUN).scene.kind == decide(NOW, config, Overrides(), SUN).scene.kind


def test_the_move_goes_through_the_file_and_only_when_there_is_one():
    assert "cycle" not in Overrides().to_dict()
    cycle = moved(NOW, 1)
    assert Overrides.from_dict(Overrides(cycle=cycle).to_dict()).cycle == cycle
    assert Overrides(cycle=cycle).without_expired(cycle.until).cycle is None
    assert Overrides(cycle=cycle).without_expired(NOW).cycle == cycle


@pytest.mark.parametrize("bad", ["soon", {"step": 1}, {"step": "1", "anchor": "x", "until": "y"},
                                 {"step": -1, "anchor": "2026-11-04T09:20:00-08:00", "until": "2026-11-04T10:00:00-08:00"},
                                 {"step": True, "anchor": "2026-11-04T09:20:00-08:00", "until": "2026-11-04T10:00:00-08:00"},
                                 {"step": 1, "anchor": "never", "until": "2026-11-04T10:00:00-08:00"}])
def test_a_move_that_cannot_be_read_is_no_move(bad):
    assert Overrides.from_dict({"cycle": bad}).cycle is None


@pytest.fixture
def cycling(tmp_path, monkeypatch):
    return Frame(tmp_path, monkeypatch, day=DAY, night=NIGHT)


def showing(frame):
    s = frame.status["showing"]
    return s["kind"], s["photo"]


def go(frame, action, at):
    frame.now = datetime.fromisoformat(at).replace(tzinfo=LA)
    assert buttons.act(action, frame.now, SUN) is True
    assert frame.tick() == 0


def test_forward_and_back_walk_the_schedule_on_the_wall(cycling):
    cycling.tick("2026-11-04T09:20")
    assert showing(cycling) == ("tides", None)
    go(cycling, NEXT, "2026-11-04T09:22")
    assert showing(cycling) == ("sky", None)
    go(cycling, NEXT, "2026-11-04T09:24")
    assert showing(cycling) == ("tides", None)
    go(cycling, NEXT, "2026-11-04T09:26")  # a photo step: the rotation's next photo
    assert showing(cycling) == ("photo", "a.jpg")
    go(cycling, BACK, "2026-11-04T09:28")
    assert showing(cycling) == ("tides", None)
    assert cycling.panel.refreshes == 5


def test_a_photo_step_reached_by_a_press_moves_the_photos_on(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, BACK, "2026-11-04T09:22")
    assert showing(cycling) == ("photo", "a.jpg")
    go(cycling, NEXT, "2026-11-04T09:24")
    go(cycling, BACK, "2026-11-04T09:26")
    assert showing(cycling) == ("photo", "b.jpg")


def test_the_schedule_carries_on_from_a_press(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, NEXT, "2026-11-04T09:22")
    cycling.tick("2026-11-04T09:31")  # sky's 10 minutes were up at 09:32: not yet
    assert showing(cycling) == ("sky", None)
    cycling.tick("2026-11-04T09:32")
    assert showing(cycling) == ("tides", None)
    cycling.tick("2026-11-04T09:52")
    assert showing(cycling) == ("photo", "a.jpg")


def test_a_press_over_show_now_ends_it(cycling):
    cycling.overrides(Overrides(pin=Pin("photo", until=NOW + timedelta(hours=1), photo="c.jpg", set_at=NOW)).to_dict())
    cycling.tick("2026-11-04T09:20")
    assert showing(cycling) == ("photo", "c.jpg")
    go(cycling, NEXT, "2026-11-04T09:22")
    assert showing(cycling) == ("sky", None)
    assert cycling.overrides()["pin"] is None


def test_a_press_reaches_the_wall_in_quiet_hours(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, day=DAY, night=NIGHT,
                  quiet_hours=QuietHours(start=time(9, 0), end=time(10, 0)))
    frame.tick("2026-11-04T09:20")
    go(frame, NEXT, "2026-11-04T09:22")
    assert showing(frame) == ("sky", None)


def test_a_press_right_after_a_refresh_goes_up_at_once(cycling):
    cycling.tick("2026-11-04T09:20")
    cycling.now += timedelta(seconds=20)
    assert buttons.act(NEXT, cycling.now, SUN) is True
    cycling.tick()
    assert showing(cycling) == ("sky", None)


def test_with_no_photos_the_photo_step_is_the_other_screen(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, photos=(), day=DAY, night=NIGHT)
    frame.tick("2026-11-04T09:20")
    go(frame, BACK, "2026-11-04T09:22")
    assert showing(frame) == ("tides", None)


def test_one_step_has_nowhere_to_go(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, day=(Step("sky", 60),), night=NIGHT)
    frame.tick("2026-11-04T09:20")
    assert buttons.act(NEXT, frame.now, SUN) is False


def test_a_slideshow_goes_through_its_photos(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, day=(Step("photo", 60),), night=NIGHT)
    frame.tick("2026-11-04T09:20")
    assert showing(frame) == ("photo", "a.jpg")
    go(frame, NEXT, "2026-11-04T09:22")
    assert showing(frame) == ("photo", "b.jpg")
    go(frame, BACK, "2026-11-04T09:24")
    assert showing(frame) == ("photo", "a.jpg")


# --- photos only ---------------------------------------------------------------------------


def test_the_long_press_goes_to_photos_and_stays_there(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, PHOTOS, "2026-11-04T09:22")
    assert showing(cycling) == ("photo", "a.jpg")
    pin = Overrides.from_dict(cycling.overrides()).pin
    assert (pin.kind, pin.photo, pin.until) == ("photo", None, None)  # the dashboard's "until you change it"
    cycling.tick("2026-11-04T15:00")
    assert showing(cycling)[0] == "photo"
    cycling.tick("2026-11-04T23:00")  # into the night too
    assert showing(cycling)[0] == "photo"


def test_forward_and_back_walk_the_photos_in_photos_only(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, PHOTOS, "2026-11-04T09:22")
    go(cycling, NEXT, "2026-11-04T09:24")
    assert showing(cycling) == ("photo", "b.jpg")
    go(cycling, NEXT, "2026-11-04T09:26")
    assert showing(cycling) == ("photo", "c.jpg")
    go(cycling, BACK, "2026-11-04T09:28")
    assert showing(cycling) == ("photo", "b.jpg")
    pin = Overrides.from_dict(cycling.overrides()).pin
    assert (pin.kind, pin.photo, pin.until) == ("photo", None, None)  # still in it
    assert cycling.overrides()["up_next"] == []  # the tick took it off


def test_the_slideshow_carries_on_from_a_press(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, PHOTOS, "2026-11-04T09:22")
    go(cycling, NEXT, "2026-11-04T09:24")
    assert showing(cycling) == ("photo", "b.jpg")
    cycling.tick("2026-11-04T09:34")  # a photo step's length later
    assert showing(cycling) == ("photo", "c.jpg")


def test_the_long_press_comes_back_to_the_schedule_as_set(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, NEXT, "2026-11-04T09:22")  # moved along
    go(cycling, PHOTOS, "2026-11-04T09:24")
    assert Overrides.from_dict(cycling.overrides()).cycle is None
    go(cycling, PHOTOS, "2026-11-04T09:26")
    overrides = Overrides.from_dict(cycling.overrides())
    assert overrides.pin is None and overrides.cycle is None
    assert showing(cycling) == ("tides", None)  # 09:26 by the clock


def test_up_next_still_comes_first_in_photos_only(cycling):
    cycling.tick("2026-11-04T09:20")
    go(cycling, PHOTOS, "2026-11-04T09:22")
    cycling.overrides({**cycling.overrides(), "up_next": ["c.jpg"]})
    go(cycling, NEXT, "2026-11-04T09:24")
    assert showing(cycling) == ("photo", "c.jpg") and cycling.overrides()["up_next"] == []


def test_the_dashboards_photos_mode_counts_as_photos_only_and_keeps_its_end(cycling):
    until = NOW + timedelta(hours=2)
    cycling.overrides(Overrides(pin=Pin("photo", until=until, set_at=NOW)).to_dict())
    cycling.tick("2026-11-04T09:20")
    go(cycling, NEXT, "2026-11-04T09:22")
    assert showing(cycling) == ("photo", "b.jpg")
    assert Overrides.from_dict(cycling.overrides()).pin.until == until


def test_no_photos_no_photos_only(tmp_path, monkeypatch):
    frame = Frame(tmp_path, monkeypatch, photos=(), day=DAY, night=NIGHT)
    frame.tick("2026-11-04T09:20")
    assert buttons.act(PHOTOS, frame.now, SUN) is False
    assert frame.overrides() in (None, Overrides().to_dict())


def test_press_command_goes_in_and_out_of_photos_only(frame):
    assert cli.main(["press", "photos", "--fake-panel", "--at", "2026-11-04T09:20"]) == 0
    assert state.read_json(frame.dir / state.STATUS)["showing"]["kind"] == "photo"
    assert cli.main(["press", "photos", "--fake-panel", "--at", "2026-11-04T09:22"]) == 0
    assert state.read_json(frame.dir / state.OVERRIDES)["pin"] is None


# --- the mark ------------------------------------------------------------------------------


def test_mark_sits_beside_the_batterys_place_on_a_white_box_ruled_in_black():
    screen = np.full((480, 800), INK["blue"], np.uint8)
    out = battery_badge.draw_low_power(screen)
    rows, cols = np.nonzero(out != screen)
    width, height = battery_badge.box_size()
    assert rows.max() == 480 - battery_badge.MARGIN - 1 and rows.max() - rows.min() + 1 == height
    assert cols.max() == 800 - battery_badge.MARGIN - width - battery_badge.MARK_GAP - 1
    assert cols.max() - cols.min() + 1 == height  # square
    box = out[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    assert (box[0] == INK["black"]).all() and (box[-1] == INK["black"]).all()
    assert set(np.unique(box)) == {INK["black"], INK["white"]}
    both = battery_badge.draw(out, 50)
    assert (both[rows.min():rows.max() + 1, cols.min():cols.max() + 1] == box).all()  # the battery leaves it be


def test_mark_in_a_left_corner_and_a_bad_corner():
    screen = np.full((480, 800), INK["white"], np.uint8)
    _, cols = np.nonzero(battery_badge.draw_low_power(screen, "top-left") != screen)
    assert cols.min() >= battery_badge.MARGIN + battery_badge.box_size()[0]
    with pytest.raises(ValueError, match="unknown corner"):
        battery_badge.draw_low_power(screen, "middle")


def test_scene_command_draws_the_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTOPAINTER_STATE_DIR", str(tmp_path))
    plain, marked = tmp_path / "plain.png", tmp_path / "marked.png"
    assert cli.main(["scene", "message", "--message", "x", "--at", "2026-11-03T14:10", "-o", str(plain)]) == 0
    assert cli.main(["scene", "message", "--message", "x", "--at", "2026-11-03T14:10", "-o", str(marked),
                     "--low-power-mark"]) == 0
    a, b = np.asarray(Image.open(plain).convert("RGB")), np.asarray(Image.open(marked).convert("RGB"))
    assert (a != b).any()


def test_the_service_file_runs_the_command():
    unit = Path(__file__).parents[1] / "deploy" / "systemd" / "photopainter-buttons.service"
    text = unit.read_text()
    assert "ExecStart=/usr/local/bin/photopainter buttons" in text and "KillMode=mixed" in text


# --- Wi-Fi setup -----------------------------------------------------------------------------


def test_holding_back_asks_for_the_hotspot(frame):
    from photopainter import hotspot

    assert buttons.act(WIFI_SETUP, frame.now, sun=SUN) is True
    assert hotspot.load(state.state_dir())["asked_at"] is not None


@pytest.mark.parametrize("action", [NEXT, BACK, PHOTOS, WIFI_SETUP])
def test_any_press_calls_off_a_hotspot_thats_up(frame, action):
    from photopainter import hotspot

    folder = state.state_dir()
    state.write_json(folder / hotspot.FILE, {**hotspot.EMPTY, "state": "on"})
    before = state.read_json(folder / state.OVERRIDES, {})
    assert buttons.act(action, frame.now, sun=SUN) is True
    data = hotspot.load(folder)
    assert data["cancel_at"] is not None and data["asked_at"] is None
    assert state.read_json(folder / state.OVERRIDES, {}) == before  # the press did nothing else


def test_low_power_still_turns_over_with_the_hotspot_up(frame):
    from photopainter import hotspot
    from photopainter.config import load_config

    folder = state.state_dir()
    state.write_json(folder / hotspot.FILE, {**hotspot.EMPTY, "state": "on"})
    assert buttons.act(LOW_POWER, frame.now, sun=SUN) is True
    assert load_config()[0].battery.low_power is True
    assert hotspot.load(folder)["cancel_at"] is None
