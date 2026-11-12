"""power.py: the battery log's cycles and time-left guess, and the low-power decisions."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from photopainter import power
from photopainter.config import default_config
from photopainter.hardware.battery import Reading
from photopainter.power import Cycle, Sample, decide, estimate, last_cycle, read_log

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 11, 6, 9, 0, tzinfo=LA)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def samples(spec: list[tuple[int, float, float, str]]) -> list[Sample]:
    return [Sample(at(m), v, ma, s) for m, v, ma, s in spec]


def write_log(path: Path, rows: list[Sample]) -> None:
    lines = ["time,volts,milliamps,state"] + [f"{s.t.isoformat(timespec='seconds')},{s.volts:.3f},{s.milliamps:.1f},{s.state}"
                                             for s in rows]
    path.write_text("\n".join(lines) + "\n")


# --- the log and its cycles -------------------------------------------------------------


def test_read_log_skips_the_header_and_broken_lines(tmp_path):
    path = tmp_path / "battery.csv"
    path.write_text("time,volts,milliamps,state\n"
                    "2026-11-06T09:00:00-08:00,4.100,300.0,charging\n"
                    "2026-11-06T09:01:00-08:00,3.9\n"  # cut short by a power cut
                    "not a line\n"
                    "2026-11-06T09:02:00-08:00,3.950,-200.0,discharging\n")
    rows = read_log(path)
    assert [(r.t.minute, r.volts, r.milliamps, r.state) for r in rows] == [(0, 4.1, 300.0, "charging"),
                                                                            (2, 3.95, -200.0, "discharging")]
    assert read_log(tmp_path / "missing.csv") == []


def test_last_cycle_is_the_latest_run_of_discharging_readings():
    rows = samples([(0, 4.1, 300, "charging"), (1, 4.0, -200, "discharging"), (2, 3.99, -210, "discharging"),
                    (3, 4.1, 400, "charging"), (4, 4.1, 50, "idle"),
                    (5, 4.05, -190, "discharging"), (6, 4.04, -200, "discharging")])
    c = last_cycle(rows)
    assert c is not None and c.ongoing and c.ended_at is None
    assert c.started_at == at(5) and len(c.samples) == 2
    finished = last_cycle(rows[:5])
    assert finished is not None and not finished.ongoing
    assert (finished.started_at, finished.ended_at, len(finished.samples)) == (at(1), at(3), 2)
    assert last_cycle(samples([(0, 4.1, 300, "charging")])) is None
    assert last_cycle([]) is None


def test_used_mah_integrates_the_current_and_ignores_long_gaps():
    rows = samples([(0, 4.0, -200, "discharging"), (6, 3.9, -200, "discharging"),  # 200 mA for 6 min
                    (9, 3.85, -100, "discharging"),  # then 150 mA average for 3 min
                    (300, 3.7, -100, "discharging")])  # a gap of hours: a reboot or a lost log, not a draw
    c = last_cycle(rows)
    assert c.used_mah() == pytest.approx(200 * 6 / 60 + 150 * 3 / 60, abs=0.01)


def test_cycle_to_dict_thins_the_readings_and_keeps_the_last():
    rows = samples([(m, 4.0 - m / 1000, -200, "discharging") for m in range(0, 1000)])
    d = last_cycle(rows).to_dict(points=100)
    assert 100 <= len(d["readings"]) <= 101
    assert d["readings"][0]["t"] == at(0) and d["readings"][-1]["t"] == at(999)
    assert d["ongoing"] is True and d["ended_at"] is None and d["used_mah"] == round(200 * 999 / 60)


def test_history_averages_buckets_and_breaks_the_line_at_gaps():
    # 4 hours in 16 buckets of 15 min: an hour on USB, the log stops for 40 min, then the battery
    rows = samples([(m, 4.1, 0, "idle") for m in range(0, 60)]
                   + [(m, 4.0 - (m - 100) / 1000, -200, "discharging") for m in range(100, 240)])
    h = power.history(rows, at(240), days=4 / 24, points=16)
    assert h["from"] == at(0) and h["to"] == at(240) and h["step_s"] == 900
    first = h["readings"][0]
    assert (first["t"], first["volts"], first["milliamps"], first["state"], first["after_gap"]) == (at(7), 4.1, 0.0, "idle", False)
    times = [r["t"] for r in h["readings"]]
    assert len(times) == 4 + 10  # the 60-100 gap empties two buckets and part of a third
    after = [r for r in h["readings"] if r["after_gap"]]
    assert len(after) == 1 and after[0]["t"] == at(102) and after[0]["state"] == "discharging"
    assert all(r["t"].microsecond == 0 for r in h["readings"])


def test_history_splits_a_bucket_where_the_state_changes():
    rows = samples([(0, 4.1, 300, "charging"), (1, 4.1, 300, "charging"), (2, 3.9, -200, "discharging"),
                    (3, 0.0, 0, "none"), (4, 3.9, -220, "discharging")])
    h = power.history(rows, at(5), days=5 / 1440, points=1)
    assert [(r["t"], r["state"], r["milliamps"], r["after_gap"]) for r in h["readings"]] == [
        (at(0.5), "charging", 300.0, False), (at(3), "discharging", -210.0, False)]


def test_history_spans_follow_the_state_at_full_detail():
    rows = samples([(0, 4.1, 0, "idle"), (1, 4.1, 0, "idle"),
                    (2, 3.95, -200, "discharging"), (3, 3.94, -200, "discharging"),  # a short run on the cell
                    (4, 4.15, 900, "charging"),
                    (30, 4.15, 900, "charging"), (31, 4.1, 0, "idle"),  # after a 26 min gap (the Pi off)
                    (32, 0.0, 0, "none")])
    h = power.history(rows, at(32.5), days=1, points=96)
    assert [(s["state"], s["start"], s["end"]) for s in h["spans"]] == [
        ("idle", at(0), at(2)), ("discharging", at(2), at(4)), ("charging", at(4), at(5)),
        ("charging", at(30), at(31)), ("idle", at(31), at(32)), ("none", at(32), at(32.5))]
    assert all(r["state"] != "none" for r in h["readings"])  # no cell: no point on the line


def test_history_leaves_out_what_is_older_than_its_days():
    rows = samples([(m, 4.1, 0, "idle") for m in range(0, 7 * 1440, 60)])
    h = power.history(rows, at(7 * 1440))
    assert h["from"] == at(2 * 1440) and h["spans"][0]["start"] == at(2 * 1440)
    assert len(h["readings"]) == 5 * 24


def test_history_across_a_clock_change_buckets_by_real_time():
    # Five days back from 4 Nov crosses the end of daylight time (1 Nov); the log's lines carry
    # fixed offsets, as read back from battery.csv.
    now = datetime(2026, 11, 4, 10, 20, tzinfo=LA)
    rows = [Sample((now - timedelta(minutes=m)).astimezone(timezone(timedelta(hours=-8))), 3.8, -200, "discharging")
            for m in range(89, -1, -1)]
    h = power.history(rows, now)
    assert h["from"] == datetime(2026, 10, 30, 11, 20, tzinfo=LA)  # 5 x 24 real hours back: 11:20 daylight time
    assert [r["t"].strftime("%H:%M") for r in h["readings"]] == ["08:57", "09:12", "09:27", "09:42", "09:57", "10:12"]


def test_the_tick_keeps_what_the_history_shows():
    from photopainter import tick
    assert tick.BATTERY_LOG_KEEP_LINES >= power.HISTORY_DAYS * 24 * 60
    assert tick.BATTERY_LOG_MAX_LINES > tick.BATTERY_LOG_KEEP_LINES


# --- the time-left guess ------------------------------------------------------------------


def test_estimate_from_the_curve_and_the_recent_draw():
    # Steady 3.8 V (48 % on the curve) at 200 mA on a 10,000 mAh pack: 4,800 mAh left = 24 h
    rows = samples([(m, 3.8, -200, "discharging") for m in range(0, 120)])
    c = last_cycle(rows)
    e = estimate(c, 10000, 3.5, at(119))
    assert e["avg_ma"] == 200 and e["percent"] == 48 and e["left_mah"] == 4800
    assert e["hours_left"] == pytest.approx(24.0)
    assert e["runs_out_at"] == at(119) + timedelta(hours=24)
    # 3.5 V is 15 % on the curve, so 33 % of the pack lies between here and the notice
    assert e["hours_to_low"] == pytest.approx(16.5) and e["low_at"] == at(119) + timedelta(hours=16.5)


def test_estimate_averages_the_last_half_hour_and_smooths_the_voltage():
    rows = samples([(m, 3.9, -400, "discharging") for m in range(0, 60)]
                   + [(m, 3.9 if m % 2 else 3.0, -200, "discharging") for m in range(60, 90)])  # a noisy reading
    e = estimate(last_cycle(rows), 2000, 3.5, at(89))
    assert e["avg_ma"] == 200  # the 400 mA hour is older than the window
    assert e["volts"] == 3.9  # the median shrugs off the 3.0 V glitches


def test_no_estimate_for_a_finished_or_tiny_cycle():
    rows = samples([(0, 3.9, -200, "discharging"), (1, 3.9, -200, "discharging"), (2, 4.1, 300, "charging")])
    assert estimate(last_cycle(rows), 2000, 3.5, at(2)) is None
    assert estimate(last_cycle(rows[:1]), 2000, 3.5, at(0)) is None


def test_summary_reads_the_state_dir(tmp_path):
    write_log(tmp_path / "battery.csv", samples([(m, 3.8, -200, "discharging") for m in range(0, 40)]))
    config = default_config()
    config = replace(config, battery=replace(config.battery, capacity_mah=5000))
    s = power.summary(tmp_path, config, at(39), {"mode": "normal"})
    assert s["capacity_mah"] == 5000 and s["low_volts"] == 3.3 and s["indicator_volts"] == 3.6 and s["power"] == {"mode": "normal"}
    assert s["cycle"]["ongoing"] is True and len(s["cycle"]["readings"]) == 40
    assert s["estimate"]["hours_left"] == pytest.approx(12.0)
    assert s["history"]["to"] == at(39) and len(s["history"]["readings"]) == 3
    assert s["history"]["spans"] == [{"state": "discharging", "start": at(0), "end": at(39)}]
    empty = power.summary(tmp_path / "none", config, at(0), None)
    assert empty["cycle"] is None and empty["estimate"] is None
    assert empty["history"]["readings"] == [] and empty["history"]["spans"] == []


# --- low power ----------------------------------------------------------------------------


def cfg(**battery):
    c = default_config()
    return replace(c, battery=replace(c.battery, low_power=True, **battery))


ON_BATTERY = Reading(3.9, -200.0)
CHARGING = Reading(4.1, 300.0)


def test_low_power_stays_off_unless_enabled():
    new, actions = decide(None, ON_BATTERY, at(0), at(10), default_config())
    assert new["mode"] == "normal" and new["wifi"] == "on" and actions == []


def test_low_power_starts_after_the_delay_and_schedules_a_data_window():
    c = cfg(low_power_after_minutes=1, data_every_hours=2)
    new, actions = decide(None, ON_BATTERY, at(0), at(0), c)
    assert new["mode"] == "normal" and actions == []  # not a minute yet
    new, actions = decide(new, ON_BATTERY, at(0), at(1), c)
    assert new["mode"] == "low" and new["wifi"] == "off" and new["since"] == at(1)
    assert new["next_window_at"] == at(121) and new["window_until"] is None
    assert actions == ["web_stop", "wifi_off"]
    later, actions = decide(new, ON_BATTERY, at(0), at(60), c)
    assert later == new and actions == []


def test_data_window_opens_for_a_few_minutes_then_closes():
    c = cfg(data_every_hours=2)
    low = {"mode": "low", "wifi": "off", "since": at(1), "window_until": None, "next_window_at": at(121)}
    open_, actions = decide(low, ON_BATTERY, at(0), at(121), c)
    assert actions == ["wifi_on"] and open_["wifi"] == "on"
    assert open_["window_until"] == at(121 + power.WINDOW_MINUTES) and open_["since"] == at(1)
    still, actions = decide(open_, ON_BATTERY, at(0), at(123), c)
    assert still == open_ and actions == []
    closed, actions = decide(open_, ON_BATTERY, at(0), at(121 + power.WINDOW_MINUTES), c)
    assert actions == ["wifi_off"] and closed["wifi"] == "off" and closed["window_until"] is None
    assert closed["next_window_at"] == at(121 + power.WINDOW_MINUTES + 120)


def test_usb_power_back_restores_wifi_and_the_dashboard():
    c = cfg()
    low = {"mode": "low", "wifi": "off", "since": at(1), "window_until": None, "next_window_at": at(121)}
    new, actions = decide(low, CHARGING, None, at(30), c)
    assert new["mode"] == "normal" and new["wifi"] == "on" and new["since"] == at(30)
    assert actions == ["wifi_on", "web_start"]
    # from an open data window the Wi-Fi is already on
    window = {**low, "wifi": "on", "window_until": at(125)}
    assert decide(window, CHARGING, None, at(123), c)[1] == ["web_start"]
    # a reading of nothing at all (no chip) also means normal
    assert decide(low, None, None, at(30), c)[1] == ["wifi_on", "web_start"]


def test_switching_the_setting_off_or_the_stay_awake_file_restores_normal():
    low = {"mode": "low", "wifi": "off", "since": at(1), "window_until": None, "next_window_at": at(121)}
    new, actions = decide(low, ON_BATTERY, at(0), at(30), default_config())
    assert new["mode"] == "normal" and actions == ["wifi_on", "web_start"]
    new, actions = decide(low, ON_BATTERY, at(0), at(30), cfg(), stay_awake=True)
    assert new["mode"] == "normal" and actions == ["wifi_on", "web_start"]
    assert decide(None, ON_BATTERY, at(0), at(30), cfg(), stay_awake=True)[1] == []


def test_decide_tolerates_a_garbled_previous_state():
    new, actions = decide({"mode": "weird", "wifi": 3, "since": "yesterday", "next_window_at": 12}, ON_BATTERY,
                          at(0), at(5), cfg())
    assert new["mode"] == "low" and actions == ["web_stop", "wifi_off"]


def test_apply_runs_actions_in_order_and_reports_failures():
    ran = []

    def runner(action):
        ran.append(action)
        if action == "wifi_off":
            raise RuntimeError("rfkill said no")

    errors = power.apply(["web_stop", "wifi_off", "wifi_on"], runner)
    assert ran == ["web_stop", "wifi_off", "wifi_on"]
    assert errors == ["wifi_off: rfkill said no"]


def test_the_real_runner_uses_sudo_without_a_password():
    for action in power.ACTIONS:
        assert power.COMMANDS[action][:2] == ["sudo", "-n"]
    assert power.COMMANDS["web_stop"][-1] == "photopainter-web.service"
    power.log_only("wifi_off")  # the Mac's runner does nothing
