"""The Wi-Fi watchdog (watchdog.py): its rules, what it reads on the Pi, and the tick around it."""

import socket
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from photopainter import state, tick, watchdog
from photopainter.config import Step, default_config, save_config
from photopainter.hardware.battery import FakeBattery
from photopainter.hardware.fake import FakePanel
from photopainter.astro.sun import SunEvents
from photopainter.watchdog import Check, decide, fault
from photopainter.web.wifi import Network

LA = ZoneInfo("America/Los_Angeles")
START = datetime(2026, 11, 12, 5, 8, tzinfo=LA)
UP = 3600.0  # an hour since boot: a restart is allowed
OFF_ROUTER = Check(link=True, dns=False, internet=False, router=False)  # Wi-Fi up, nothing gets through
HOUSE_OUT = Check(link=True, dns=True, internet=False, router=True)  # the router answers, the internet doesn't
GOOD = Check(link=True, dns=True, internet=True, address="203.0.113.7")


def at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def never_heard() -> bool:
    raise AssertionError("scanned when it didn't need to")


def run(prev, check, minutes, *, uptime_s=UP, enabled=True, heard=never_heard):
    return decide(prev, check, at(minutes), uptime_s=uptime_s, enabled=enabled, heard_saved=heard)


def through(check, minutes, prev=None, **kw):
    """Tick once a minute over `minutes`; the state at the end and the steps taken, by minute."""
    steps = {}
    for m in minutes:
        prev, step = run(prev, check, m, **kw)
        if step is not None:
            steps[m] = step.kind
    return prev, steps


# --- the rules ------------------------------------------------------------------------------


def test_the_internet_answering_clears_an_outage_and_remembers_the_address():
    prev = {"offline_since": at(0), "reset_at": at(20), "reboot_at": at(-600), "addresses": ["198.51.100.1"]}
    new, step = run(prev, GOOD, 30)
    assert step is None
    assert new == {"offline_since": None, "reset_at": None, "reboot_at": at(-600),
                   "addresses": ["203.0.113.7", "198.51.100.1"]}


def test_remembered_addresses_stay_few_and_newest_first():
    prev = {"addresses": ["a", "b", "c", "d"]}
    new, _ = run(prev, replace(GOOD, address="c"), 0)
    assert new["addresses"] == ["c", "a", "b", "d"]
    new, _ = run(new, replace(GOOD, address="e"), 1)
    assert new["addresses"] == ["e", "c", "a", "b"]


def test_nothing_to_go_on_changes_nothing():
    prev = {"offline_since": at(0), "reset_at": None, "reboot_at": None, "addresses": []}
    assert run(prev, Check(), 60) == (prev, None)


def test_a_short_outage_does_nothing():
    new, steps = through(OFF_ROUTER, range(20))
    assert steps == {} and new["offline_since"] == at(0)
    assert run(new, GOOD, 20)[0]["offline_since"] is None


def test_wifi_is_reset_after_20_minutes_then_every_2_hours():
    _, steps = through(HOUSE_OUT, range(0, 400))
    assert steps == {20: "reset", 140: "reset", 260: "reset", 380: "reset"}


def test_a_stuck_connection_is_reset_then_restarted():
    new, steps = through(OFF_ROUTER, range(0, 100))
    assert steps == {20: "reset", 90: "reboot"}
    assert new["reboot_at"] == at(90) and new["reset_at"] == at(20)


def test_the_house_internet_out_never_restarts_the_frame():
    _, steps = through(HOUSE_OUT, range(0, 24 * 60))
    assert set(steps.values()) == {"reset"}


def test_names_that_dont_resolve_while_the_way_out_works_restart_it():
    dns_broken = Check(link=True, dns=False, internet=False, route=True, router=True)
    _, steps = through(dns_broken, range(0, 100))
    assert steps == {20: "reset", 90: "reboot"}
    way_out_dead = replace(dns_broken, route=False)
    _, steps = through(way_out_dead, range(0, 100))
    assert steps == {20: "reset"}


def test_on_no_network_it_restarts_only_with_a_saved_one_in_range():
    lost = Check(link=False, internet=False)
    _, steps = through(lost, range(0, 100), heard=lambda: True)
    assert steps == {20: "reset", 90: "reboot"}
    _, steps = through(lost, range(0, 100), heard=lambda: False)  # the router's off
    assert steps == {20: "reset"}


def test_a_restart_needs_a_reset_first():
    prev = {"offline_since": at(0), "reset_at": None, "reboot_at": None, "addresses": []}
    _, step = run(prev, OFF_ROUTER, 95)
    assert step.kind == "reset"  # the reset comes first, however long it has been


def test_no_restart_soon_after_boot():
    prev, _ = through(OFF_ROUTER, range(0, 90), uptime_s=600)
    assert prev["reboot_at"] is None
    _, step = run(prev, OFF_ROUTER, 90, uptime_s=None)
    assert step is None


def test_restarts_are_at_least_6_hours_apart_across_outages():
    prev, steps = through(OFF_ROUTER, range(0, 100))
    assert steps[90] == "reboot"
    prev, _ = run(prev, GOOD, 101)  # back after the restart…
    prev, steps = through(OFF_ROUTER, range(110, 600), prev=prev)  # …and lost again
    assert [m for m, kind in steps.items() if kind == "reboot"] == [450]  # 90 + 6 h


def test_a_restart_time_in_the_future_blocks_one():
    prev = {"offline_since": at(0), "reset_at": at(20), "reboot_at": at(200), "addresses": []}
    _, step = run(prev, OFF_ROUTER, 95)
    assert step is None


def test_turned_off_it_keeps_track_but_takes_no_steps():
    new, steps = through(OFF_ROUTER, range(0, 200), enabled=False)
    assert steps == {} and new["offline_since"] == at(0)


def test_a_clock_set_back_starts_the_outage_again():
    prev = {"offline_since": at(30), "reset_at": at(50), "reboot_at": None, "addresses": []}
    new, step = run(prev, OFF_ROUTER, 10)
    assert new["offline_since"] == at(10) and new["reset_at"] is None and step is None


def test_a_reset_from_an_earlier_outage_doesnt_count():
    prev = {"offline_since": at(0), "reset_at": at(-300), "reboot_at": None, "addresses": []}
    new, step = run(prev, OFF_ROUTER, 21)
    assert step.kind == "reset" and new["reset_at"] == at(21)


def test_bad_state_is_read_as_none():
    new, step = run({"offline_since": "soon", "reset_at": 3, "reboot_at": [], "addresses": "x"}, OFF_ROUTER, 0)
    assert new == {"offline_since": at(0), "reset_at": None, "reboot_at": None, "addresses": []} and step is None


def test_pausing_ends_an_outage_but_keeps_the_restart_and_addresses():
    prev = {"offline_since": at(0), "reset_at": at(20), "reboot_at": at(-30), "addresses": ["a", 4]}
    assert watchdog.pause(prev) == {"offline_since": None, "reset_at": None, "reboot_at": at(-30),
                                    "addresses": ["a"]}
    assert watchdog.pause(None) == {"offline_since": None, "reset_at": None, "reboot_at": None, "addresses": []}


@pytest.mark.parametrize("check, ours", [
    (OFF_ROUTER, "the router doesn't answer"),
    (Check(link=True, dns=True, internet=False, router=None), "no route to a router"),
    (HOUSE_OUT, None),
    (Check(link=True, dns=False, internet=False, route=None, router=True), None),
    (GOOD, None),
    (Check(), None),
])
def test_whose_fault(check, ours):
    assert fault(check, never_heard) == ours


# --- the log --------------------------------------------------------------------------------


def test_a_log_line():
    radio = watchdog.Radio("Beach, House", "02:00:00:00:00:02", -65.0)
    line = watchdog.log_line(at(0), radio, OFF_ROUTER, "reset (no internet): ok")
    assert line == "2026-11-12T05:08:00-08:00,Beach  House,02:00:00:00:00:02,-65,1,0,0,,0,reset (no internet): ok"
    assert len(line.split(",")) == len(watchdog.LOG_HEADER.split(","))
    assert watchdog.log_line(at(0), watchdog.Radio(), Check(), "paused: low power").endswith(",,,,,,,,,paused: low power")


def test_the_log_is_trimmed(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "LOG_MAX_LINES", 5)
    monkeypatch.setattr(watchdog, "LOG_KEEP_LINES", 3)
    for m in range(40):
        watchdog.append_log(tmp_path, watchdog.log_line(at(m), watchdog.Radio(), GOOD, ""))
    lines = (tmp_path / watchdog.LOG).read_text().splitlines()
    assert lines[0] == watchdog.LOG_HEADER and 3 <= len(lines) - 1 <= 5
    assert lines[-1].startswith("2026-11-12T05:47")


# --- what it reads on the Pi ----------------------------------------------------------------


IW_LINK = """Connected to 02:00:00:00:00:03 (on wlan0)
\tSSID: Home Net
\tfreq: 2462.0
\tRX: 1644786 bytes (6994 packets)
\tsignal: -65 dBm
\trx bitrate: 39.0 MBit/s
"""


def test_reads_iw_link():
    assert watchdog.parse_iw_link(IW_LINK) == watchdog.Radio("Home Net", "02:00:00:00:00:03", -65.0)
    assert watchdog.parse_iw_link("Not connected.\n") == watchdog.Radio()


PROC_ROUTE = """Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT
tailscale0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0
wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\t0\t0\t0
wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0
"""


def test_finds_the_router():
    assert watchdog.gateway(PROC_ROUTE) == "192.168.1.1"
    assert watchdog.gateway(PROC_ROUTE.splitlines()[0]) is None
    assert watchdog.gateway(PROC_ROUTE, iface="eth0") is None


def net(ssid: str, uuid: str = "u") -> Network:
    return Network(ssid, ssid, False, False, uuid + ssid)


def test_the_strongest_saved_network_in_range():
    scan = [{"ssid": "Neighbor", "signal": 80}, {"ssid": "Attic", "signal": 60}, {"ssid": "Home", "signal": 50}]
    assert watchdog.best_saved(scan, [net("Home"), net("Attic")]).ssid == "Attic"
    assert watchdog.best_saved(scan, [net("Garage")]) is None


def resolver(table: dict):
    def resolve(host, port, type=None):
        if host not in table:
            raise socket.gaierror("no such name")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in table[host]]
    return resolve


def test_reach_by_name():
    hosts = (("a.example", 443), ("b.example", 443))
    tried = []

    def connect(address, timeout):
        tried.append(address)
        return address[0] == "203.0.113.9"

    found = watchdog.reach(hosts, ["198.51.100.1"], resolve=resolver({"a.example": ["203.0.113.8"],
                                                                     "b.example": ["203.0.113.9"]}), connect=connect)
    assert found == (True, True, None, "203.0.113.9")
    assert tried == [("203.0.113.8", 443), ("203.0.113.9", 443)]


def test_reach_with_names_failing_tries_the_remembered_addresses():
    hosts = (("a.example", 443),)
    up = watchdog.reach(hosts, ["198.51.100.1"], resolve=resolver({}), connect=lambda a, t: True)
    assert up == (False, False, True, None)
    down = watchdog.reach(hosts, ["198.51.100.1"], resolve=resolver({}), connect=lambda a, t: False)
    assert down == (False, False, False, None)
    assert watchdog.reach(hosts, [], resolve=resolver({}), connect=lambda a, t: True) == (False, False, None, None)


def test_reach_with_names_resolving_but_no_way_out():
    found = watchdog.reach((("a.example", 443),), ["198.51.100.1"], resolve=resolver({"a.example": ["203.0.113.8"]}),
                           connect=lambda a, t: False)
    assert found == (True, False, None, None)


def test_a_hung_check_gives_up_in_time():
    import threading

    gate = threading.Event()
    assert watchdog._within(0.05, lambda: gate.wait(5), "gave up") == "gave up"
    gate.set()
    assert watchdog._within(1, lambda: "done", "gave up") == "done"


class FakeRun:
    """subprocess.run for Pi: records commands; nmcli answers from `scan` and `saved`."""

    def __init__(self, *, fail=(), scan="", saved=()):
        self.calls: list[list[str]] = []
        self.fail = fail
        self.scan, self.saved = scan, saved

    def __call__(self, argv, timeout=30, **kw):
        self.calls.append(list(argv))
        line = " ".join(argv)
        out, code = "", 0
        if any(f in line for f in self.fail):
            code = 1
        elif "device wifi list" in line:
            out = self.scan
        elif "-f NAME,UUID,TYPE,ACTIVE" in line:
            out = "".join(f"{n}:{n}-uuid:802-11-wireless:no\n" for n in self.saved)
        elif "802-11-wireless.ssid" in line:
            out = f"802-11-wireless.ssid:{argv[-1].removesuffix('-uuid')}\n802-11-wireless.hidden:no\n"
        return subprocess.CompletedProcess(argv, code, out, "nope" if code else "")

    def ran(self, text: str) -> bool:
        return any(text in " ".join(c) for c in self.calls)


def test_reset_turns_the_radio_off_and_on_and_waits_for_a_network():
    run = FakeRun()
    links = iter([False, False, True])
    pi = watchdog.Pi(run=run, sleep=lambda s: None, link=lambda: next(links))
    note = pi.reset()
    assert note.startswith("radio off and on, back on")
    assert run.calls[0] == ["sudo", "-n", "nmcli", "radio", "wifi", "off"]
    assert run.calls[1] == ["sudo", "-n", "nmcli", "radio", "wifi", "on"]
    assert not run.ran("connection up")


def test_reset_joins_the_strongest_saved_network_when_nothing_comes_back():
    run = FakeRun(scan="Neighbor:90:WPA2\nAttic:60:WPA2\nHome:50:WPA2\n", saved=("Home", "Attic"))
    pi = watchdog.Pi(run=run, sleep=lambda s: None, link=lambda: False)
    assert pi.reset() == "radio off and on, then joined Attic"
    assert run.calls[-1][:4] == ["sudo", "-n", "nmcli", "--wait"] and run.calls[-1][-1] == "Attic-uuid"


def test_reset_with_no_saved_network_in_range_says_so():
    run = FakeRun(scan="Neighbor:90:WPA2\n", saved=("Home",))
    pi = watchdog.Pi(run=run, sleep=lambda s: None, link=lambda: False)
    assert pi.reset() == "radio off and on, still off; no saved network in range"
    assert not run.ran("connection up")


def test_reset_stops_when_the_radio_wont_switch():
    run = FakeRun(fail=("radio wifi off",))
    pi = watchdog.Pi(run=run, sleep=lambda s: None, link=lambda: True)
    assert pi.reset() == "radio off failed: nope"
    assert len(run.calls) == 1


def test_on_no_network_the_check_goes_no_further():
    pi = watchdog.Pi(run=FakeRun(), link=lambda: False)
    assert pi.check(["198.51.100.1"]) == Check(link=False, internet=False)
    assert watchdog.Pi(run=FakeRun(), link=lambda: None).check([]) == Check()


def test_with_no_internet_the_check_pings_the_router(monkeypatch, tmp_path):
    route = tmp_path / "route"
    route.write_text(PROC_ROUTE)
    monkeypatch.setattr(watchdog, "PROC_ROUTE", route)
    monkeypatch.setattr(watchdog, "reach", lambda hosts, remembered: (True, False, None, None))
    run = FakeRun(fail=("ping",))
    check = watchdog.Pi(run=run, link=lambda: True).check([])
    assert check == Check(link=True, dns=True, internet=False, route=None, router=False)
    assert run.calls == [["ping", "-n", "-q", "-c", "1", "-W", "2", "192.168.1.1"]]


def test_with_the_internet_up_nothing_is_pinged(monkeypatch):
    monkeypatch.setattr(watchdog, "reach", lambda hosts, remembered: (True, True, None, "203.0.113.7"))
    run = FakeRun()
    assert watchdog.Pi(run=run, link=lambda: True).check([]).internet is True
    assert run.calls == []


# --- the tick -------------------------------------------------------------------------------


def SUN(day) -> SunEvents:
    rise = datetime(day.year, day.month, day.day, 6, 12, tzinfo=LA)
    set_ = datetime(day.year, day.month, day.day, 16, 57, tzinfo=LA)
    return SunEvents(rise, set_, rise - timedelta(minutes=25), set_ + timedelta(minutes=25))


class Frame:
    """A tick with the fake panel and the fake watchdog (fake_wifi.json), at minutes from START."""

    def __init__(self, monkeypatch):
        c = default_config()
        save_config(replace(c, schedule=replace(c.schedule, day=(Step("tides", 60),), night=(Step("tides", 60),))))
        self.dir = state.state_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.panel = FakePanel(self.dir)
        self.watch = watchdog.Fake(self.dir)
        self.fetches = 0
        monkeypatch.setattr(tick, "render_scene", lambda scene, ctx: np.full((480, 800), 1, np.uint8))

        def refresh(t: tick._Tick) -> None:  # counts the ticks that would fetch
            if t.fetch and t.clock_ok and t.network_ok:
                self.fetches += 1
        monkeypatch.setattr(tick._Tick, "_refresh_data", refresh)

    def wifi(self, **fields) -> None:
        state.write_json(self.dir / "fake_wifi.json", {**state.read_json(self.dir / "fake_wifi.json", {}), **fields})

    def tick(self, minutes: float, **kw) -> int:
        kw.setdefault("watch", self.watch)
        return tick.run(at(minutes), self.panel, sun=SUN, wall=lambda: at(minutes), fetch=True, **kw)

    @property
    def status(self) -> dict:
        return state.read_json(self.dir / state.STATUS)

    @property
    def log(self) -> list[list[str]]:
        return [line.split(",") for line in (self.dir / watchdog.LOG).read_text().splitlines()[1:]]

    @property
    def fake(self) -> dict:
        return state.read_json(self.dir / "fake_wifi.json", {})


@pytest.fixture
def frame(monkeypatch):
    return Frame(monkeypatch)


def test_a_good_connection_is_logged_and_fetches(frame):
    frame.wifi(connected=True)
    assert frame.tick(0) == 0
    assert frame.status["wifi"]["internet"] is True
    assert frame.status["wifi"]["watchdog"]["addresses"] == ["192.0.2.1"]
    assert frame.log == [["2026-11-12T05:08:00-08:00", "Home", "02:00:00:00:00:01", "-60", "1", "1", "1", "", "", ""]]
    assert frame.fetches == 1


def test_no_radio_means_no_watchdog(frame):
    frame.tick(0)  # no fake_wifi.json: like the Mac
    assert not (frame.dir / watchdog.LOG).exists()
    assert "internet" not in (frame.status["wifi"] or {})


def test_an_outage_is_reset_then_restarted_after_status_is_saved(frame, monkeypatch):
    frame.wifi(connected=True, internet=False, router=False)
    saved_at_restart = {}
    real_reboot = frame.watch.reboot

    def reboot():
        saved_at_restart.update(state.read_json(frame.dir / state.STATUS)["wifi"]["watchdog"])
        real_reboot()
    monkeypatch.setattr(frame.watch, "reboot", reboot)

    for m in range(0, 91):
        frame.tick(m)
    assert frame.fetches == 0  # no internet: no fetching
    assert frame.fake["resets"] == 1 and "rebooted_at" in frame.fake
    notes = {line[0][11:16]: line[-1] for line in frame.log if line[-1]}
    assert notes == {"05:28": "reset (no internet since 05:08): radio off and on (simulated)",
                     "06:38": "restart: the router doesn't answer"}
    assert saved_at_restart["reboot_at"] == "2026-11-12T06:38:00-08:00"


def test_a_reset_that_works_ends_the_outage(frame):
    frame.wifi(connected=True, internet=False, router=False, reset_fixes=True)
    for m in range(0, 25):
        frame.tick(m)
    assert frame.fake["resets"] == 1
    assert frame.status["wifi"]["internet"] is True and frame.status["wifi"]["watchdog"]["offline_since"] is None


def test_the_watchdog_pauses_in_low_power(frame):
    data = state.read_json(state.config_path())
    data["battery"]["low_power"] = True
    state.write_json(state.config_path(), data)
    battery = FakeBattery(frame.dir)
    battery.set(3.9, -200)
    frame.wifi(connected=True, internet=False, router=False)
    for m in range(0, 40):
        frame.tick(m, battery=battery)
    assert "resets" not in frame.fake
    assert frame.log[-1][-1] == "paused: low power"
    assert frame.status["wifi"]["watchdog"]["offline_since"] is None


def test_a_tick_without_the_watchdog_keeps_its_restart_time(frame):
    frame.wifi(connected=True)
    frame.tick(0)
    data = state.read_json(frame.dir / state.STATUS)
    data["wifi"]["watchdog"]["reboot_at"] = "2026-11-12T01:00:00-08:00"
    state.write_json(frame.dir / state.STATUS, data)
    from photopainter.hotspot import FakeNetwork

    frame.tick(1, watch=None, network=FakeNetwork(frame.dir))
    assert frame.status["wifi"]["watchdog"]["reboot_at"] == "2026-11-12T01:00:00-08:00"


def test_a_simulated_moment_on_the_frame_has_no_watchdog(monkeypatch):
    seen = {}
    monkeypatch.setattr(tick, "run", lambda *a, **kw: seen.update(kw) or 0)
    monkeypatch.setattr(tick, "PanelDisplay", lambda **kw: None)
    tick.main(at=at(0))
    assert seen["watch"] is None
    tick.main(fake_panel=True, at=at(0))
    assert isinstance(seen["watch"], watchdog.Fake)
    tick.main()
    assert isinstance(seen["watch"], watchdog.Pi)
