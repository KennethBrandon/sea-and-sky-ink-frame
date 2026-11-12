"""Tides, weather, surf and gauge sources (PRD.md T1), against recorded API responses.

No test touches the network: `urllib.request.urlopen` and socket connections are replaced
for every test here, and `FakeAPI` serves the files in tests/fixtures/:

- `noaa_hilo.json`, `noaa_curve.json`: NOAA CO-OPS predictions for 9410580 (MLLW, GMT),
  `interval=hilo` and `interval=30`, from 2026-11-02 00:00 PST for 44 days
- `openmeteo_forecast.json`, `openmeteo_marine.json`: Open-Meteo forecast and marine
- `noaa_gauge_water_level.json`, `noaa_gauge_predictions.json`: Los Angeles's six-minute
  readings and table (more in test_gauge.py)
- `noaa_error.json`: NOAA's answer for station 9999999
- `openmeteo_error.json`: Open-Meteo's HTTP 400 body for an unknown variable

All were recorded at RECORDED (below) with exactly the parameters the modules send
(`request_params`), so re-record them the same way if the requests change.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest

from photopainter import state
from photopainter.config import (
    MAX_CHART_DAYS_AHEAD,
    ConfigError,
    Gauge,
    Surf,
    Tides,
    Weather,
    default_config,
    validate,
)
from photopainter.sources import common, ensure_all_fresh, gauge, surf, tides, weather
from photopainter.scenes.tides import STALE_AFTER
from photopainter.sources.common import FetchError
from photopainter.web.health import SHOW_AGE_AFTER

FIXTURES = Path(__file__).parent / "fixtures"
LA = ZoneInfo("America/Los_Angeles")
RECORDED = datetime(2026, 11, 3, 23, 18, 3, tzinfo=LA)
CONFIG = default_config()
MODULES = {"tides": tides, "weather": weather, "surf": surf, "gauge": gauge}

ROUTES = {
    "api.tidesandcurrents.noaa.gov": None,  # by interval, see FakeAPI.body_for
    "api.open-meteo.com": "openmeteo_forecast.json",
    "marine-api.open-meteo.com": "openmeteo_marine.json",
}


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeAPI:
    """Stands in for urllib.request.urlopen; serves the recorded responses."""

    def __init__(self) -> None:
        self.requests: list[tuple[urllib.request.Request, float]] = []
        self.fail: BaseException | None = None  # raised instead of answering
        self.bodies: dict[str, bytes] = {}  # fixture name -> body to send instead of it

    def body_for(self, url: str) -> bytes:
        parts = urlsplit(url)
        name = ROUTES[parts.hostname or ""]
        if name is None:
            query = parse_qs(parts.query)
            if query["product"] == ["water_level"]:
                name = "noaa_gauge_water_level.json"
            else:
                name = {"hilo": "noaa_hilo.json", "6": "noaa_gauge_predictions.json"}.get(
                    query["interval"][0], "noaa_curve.json")
        return self.bodies[name] if name in self.bodies else (FIXTURES / name).read_bytes()

    def __call__(self, request, timeout=None):
        assert isinstance(request, urllib.request.Request)
        self.requests.append((request, timeout))
        if self.fail is not None:
            raise self.fail
        return FakeResponse(self.body_for(request.full_url))

    def hosts(self) -> list[str]:
        return [urlsplit(r.full_url).hostname for r, _ in self.requests]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not use the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def api(monkeypatch) -> FakeAPI:
    fake = FakeAPI()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


@pytest.fixture
def sdir() -> Path:
    return state.state_dir()  # the temp state dir conftest sets up


def cache_bytes(module) -> bytes | None:
    path = common.cache_path(module.FILE)
    return path.read_bytes() if path.exists() else None


def run(name: str, now: datetime = RECORDED, config=CONFIG, **kw):
    return MODULES[name].ensure_fresh(config, now, **kw)


# --- parsing the recorded responses ----------------------------------------------------


def test_tides_cache_matches_contract():
    cache = tides.parse(fixture("noaa_hilo.json"), fixture("noaa_curve.json"), CONFIG, RECORDED)
    assert list(cache) == ["station", "datum", "units", "fetched_at", "hilo", "curve"]
    assert (cache["station"], cache["datum"], cache["units"]) == ("9410580", "MLLW", "ft")
    assert cache["fetched_at"] == "2026-11-03T23:18:03-08:00"
    # NOAA's 13:27 UTC high on 2026-11-03 (PRD.md §5's example) at local time
    assert {"t": "2026-11-03T05:27:00-08:00", "v": 4.711, "type": "H"} in cache["hilo"]
    for row in cache["hilo"]:
        assert list(row) == ["t", "v", "type"] and row["type"] in ("H", "L")
        assert isinstance(row["v"], float)
    times = [state.from_iso(r["t"]) for r in cache["curve"]]
    assert all(list(r) == ["t", "v"] and isinstance(r["v"], float) for r in cache["curve"])
    assert times == sorted(times)
    assert max(b - a for a, b in zip(times, times[1:])) <= timedelta(minutes=30)
    assert times[0] == datetime(2026, 11, 2, tzinfo=LA)  # from local midnight yesterday
    assert all(t.utcoffset() == timedelta(hours=-8) for t in times)
    until = tides.covers_until(cache)
    assert until >= RECORDED + tides.MIN_AHEAD
    assert until.date().isoformat() == "2026-12-15"
    assert tides.stale_reason(cache, CONFIG, RECORDED) is None


def test_tides_highs_and_lows_alternate():
    cache = tides.parse(fixture("noaa_hilo.json"), fixture("noaa_curve.json"), CONFIG, RECORDED)
    kinds = [r["type"] for r in cache["hilo"]]
    assert all(a != b for a, b in zip(kinds, kinds[1:]))
    assert 3 <= sum(1 for r in cache["hilo"] if r["t"].startswith("2026-11-03")) <= 4


def test_tides_request_params():
    p = tides.request_params(CONFIG, RECORDED, "hilo")
    assert p["application"] == "photopainter"
    assert (p["station"], p["datum"], p["units"], p["time_zone"]) == ("9410580", "MLLW", "english", "gmt")
    assert p["begin_date"] == "20261102 08:00"  # local midnight 2026-11-02 in UTC
    assert p["end_date"] == "20261216 08:00"
    assert tides.request_params(CONFIG, RECORDED, "30")["interval"] == "30"


def test_tides_times_across_the_dst_change():
    """GMT in, local out: the repeated 01:00 hour on 2026-11-01 stays in order."""
    rows = [{"t": f"2026-11-01 {h}", "v": "1.0"} for h in ("07:30", "08:00", "08:30", "09:00", "09:30")]
    hilo = {"predictions": [{"t": "2026-11-01 08:10", "v": "2.0", "type": "H"}]}
    cache = tides.parse(hilo, {"predictions": rows}, CONFIG, RECORDED)
    assert [r["t"] for r in cache["curve"]] == [
        "2026-11-01T00:30:00-07:00", "2026-11-01T01:00:00-07:00", "2026-11-01T01:30:00-07:00",
        "2026-11-01T01:00:00-08:00", "2026-11-01T01:30:00-08:00",
    ]


@pytest.mark.parametrize("hilo, curve, message", [
    ({"error": {"message": "No Predictions data was found."}}, {"predictions": []}, "NOAA: No Predictions"),
    ({"predictions": []}, {"predictions": []}, "no highs and lows"),
    ({"predictions": [{"t": "2026-11-03 05:27", "v": "4.7", "type": "X"}]}, None, "type 'X'"),
    ({"predictions": [{"t": "yesterday", "v": "4.7", "type": "H"}]}, None, "bad time"),
    ({"predictions": [{"t": "2026-11-03 05:27", "v": "", "type": "H"}]}, None, "bad height"),
    (None, {"predictions": [{"t": "2026-11-03 00:00", "v": "1"}, {"t": "2026-11-03 01:00", "v": "1"}]}, "gap"),
])
def test_tides_bad_responses_raise_fetch_error(hilo, curve, message):
    hilo = hilo if hilo is not None else fixture("noaa_hilo.json")
    curve = curve if curve is not None else fixture("noaa_curve.json")
    with pytest.raises(FetchError, match=message):
        tides.parse(hilo, curve, CONFIG, RECORDED)


def test_noaa_error_fixture():
    with pytest.raises(FetchError, match="NOAA: No Predictions data was found"):
        tides.parse(fixture("noaa_error.json"), fixture("noaa_curve.json"), CONFIG, RECORDED)


def test_weather_cache_matches_contract():
    raw = fixture("openmeteo_forecast.json")
    cache = weather.parse(raw, CONFIG, RECORDED)
    assert list(cache) == ["source", "fetched_at", "lat", "lon", "units", "current", "hourly", "daily"]
    assert cache["source"] == "open-meteo"
    assert (cache["lat"], cache["lon"]) == (33.593, -117.882)
    assert cache["units"] == {"temp": "F", "wind": "mph"}
    cur = cache["current"]
    assert list(cur) == ["t", "temp", "code", "wind", "gust", "wind_dir"]
    assert state.from_iso(cur["t"]) == datetime.fromtimestamp(raw["current"]["time"], LA)
    assert cur["temp"] == raw["current"]["temperature_2m"]
    assert cur["code"] == raw["current"]["weather_code"]
    assert cur["gust"] == raw["current"]["wind_gusts_10m"]
    assert cur["wind_dir"] == raw["current"]["wind_direction_10m"]
    hourly = cache["hourly"]
    assert all(list(h) == ["t", "temp", "code", "precip_chance", "wind", "wind_dir"] for h in hourly)
    assert hourly[0]["t"] == "2026-11-03T00:00:00-08:00"
    assert state.from_iso(hourly[-1]["t"]) >= RECORDED + timedelta(hours=48)
    assert [h["temp"] for h in hourly] == raw["hourly"]["temperature_2m"]
    daily = cache["daily"]
    assert all(list(d) == ["date", "high", "low", "code", "precip_chance"] for d in daily)
    assert [d["date"] for d in daily[:3]] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert daily[0]["high"] == raw["daily"]["temperature_2m_max"][0]
    assert daily[0]["low"] == raw["daily"]["temperature_2m_min"][0]
    assert daily[0]["precip_chance"] == raw["daily"]["precipitation_probability_max"][0]
    assert weather.stale_reason(cache, CONFIG, RECORDED) is None


def test_weather_nulls_pass_through():
    raw = fixture("openmeteo_forecast.json")
    raw["hourly"]["precipitation_probability"][5] = None
    raw["current"]["wind_gusts_10m"] = None
    cache = weather.parse(raw, CONFIG, RECORDED)
    assert cache["hourly"][5]["precip_chance"] is None
    assert cache["current"]["gust"] is None


@pytest.mark.parametrize("change, message", [
    (lambda r: r["current_units"].update(temperature_2m="°C"), "temperature_2m is '°C'"),
    (lambda r: r["hourly_units"].update(wind_speed_10m="km/h"), "wind_speed_10m is 'km/h'"),
    (lambda r: r["hourly"]["temperature_2m"].pop(), "ragged"),
    (lambda r: r.pop("daily"), "no daily data"),
    (lambda r: r.pop("current"), "no current conditions"),
    (lambda r: r["hourly"]["weather_code"].__setitem__(0, "cloudy"), "expected a number"),
])
def test_weather_bad_responses_raise_fetch_error(change, message):
    raw = fixture("openmeteo_forecast.json")
    change(raw)
    with pytest.raises(FetchError, match=message):
        weather.parse(raw, CONFIG, RECORDED)


def test_surf_cache_matches_contract():
    raw = fixture("openmeteo_marine.json")
    cache = surf.parse(raw, CONFIG, RECORDED)
    assert list(cache) == ["source", "fetched_at", "lat", "lon", "units", "hourly"]
    assert cache["source"] == "open-meteo-marine"
    assert (cache["lat"], cache["lon"]) == (33.625, -117.875)  # the model cell, not the Wedge
    assert cache["units"] == {"height": "ft", "period": "s", "water_temp": "F"}
    keys = ["t", "wave_height", "wave_period", "wave_dir", "swell_height", "swell_period",
            "swell_dir", "water_temp"]
    assert all(list(h) == keys for h in cache["hourly"])
    first, h = raw["hourly"], cache["hourly"][0]
    assert h["t"] == "2026-11-03T00:00:00-08:00"
    assert h["wave_height"] == round(first["wave_height"][0], 2)
    assert h["swell_period"] == first["swell_wave_period"][0]
    assert h["swell_dir"] == first["swell_wave_direction"][0]
    assert state.from_iso(cache["hourly"][-1]["t"]) >= RECORDED + timedelta(hours=48)
    assert surf.stale_reason(cache, RECORDED) is None


def test_surf_water_temperature_is_converted_to_fahrenheit():
    raw = fixture("openmeteo_marine.json")
    assert raw["hourly_units"]["sea_surface_temperature"] == "°C"  # what the API really sends
    cache = surf.parse(raw, CONFIG, RECORDED)
    for c, row in zip(raw["hourly"]["sea_surface_temperature"], cache["hourly"]):
        assert row["water_temp"] == round(c * 9 / 5 + 32, 1)
    assert cache["hourly"][0]["water_temp"] == 73.2  # 22.9 °C
    assert surf.water_temp_f(0) == 32.0
    assert surf.water_temp_f(22.9) == 73.2
    assert surf.water_temp_f(-1.5) == 29.3
    assert surf.water_temp_f(100) == 212.0
    assert surf.water_temp_f(None) is None
    assert surf.water_temp_f(73.2, "°F") == 73.2


def test_surf_converts_metres_if_the_api_sends_them():
    raw = fixture("openmeteo_marine.json")
    raw["hourly_units"].update(wave_height="m", swell_wave_height="m")
    raw["hourly"]["wave_height"][0] = 1.0
    assert surf.parse(raw, CONFIG, RECORDED)["hourly"][0]["wave_height"] == 3.28


@pytest.mark.parametrize("change, message", [
    (lambda r: r["hourly_units"].update(sea_surface_temperature="K"), "sea_surface_temperature"),
    (lambda r: r["hourly_units"].update(wave_height="m"), "wave_height/swell_wave_height"),
    (lambda r: r.pop("latitude"), "model cell"),
    (lambda r: r.pop("hourly"), "no marine hourly data"),
])
def test_surf_bad_responses_raise_fetch_error(change, message):
    raw = fixture("openmeteo_marine.json")
    change(raw)
    with pytest.raises(FetchError, match=message):
        surf.parse(raw, CONFIG, RECORDED)


def test_open_meteo_error_body():
    with pytest.raises(FetchError, match="Open-Meteo: Invalid value"):
        weather.parse(fixture("openmeteo_error.json"), CONFIG, RECORDED)


# --- ensure_fresh ----------------------------------------------------------------------


@pytest.mark.parametrize("name", MODULES)
def test_no_cache_fetches_and_writes_the_cache(api, name):
    r = run(name)
    assert (r.source, r.fetched, r.error) == (name, True, None)
    assert r.fetched_at == RECORDED
    assert r.where == f"{name}-fetch"
    written = MODULES[name].load()
    assert written == r.cache
    assert written["fetched_at"] == "2026-11-03T23:18:03-08:00"
    assert len(api.requests) == (2 if name in ("tides", "gauge") else 1)


def test_tides_fetch_writes_what_parse_gives(api):
    run("tides")
    assert tides.load() == tides.parse(fixture("noaa_hilo.json"), fixture("noaa_curve.json"),
                                       CONFIG, RECORDED)


def test_requests_identify_the_app_without_personal_details(api):
    ensure_all_fresh(CONFIG, RECORDED)
    assert sorted(set(api.hosts())) == sorted(ROUTES)
    for request, timeout in api.requests:
        assert timeout == 10
        agent = request.get_header("User-agent")
        assert agent.startswith("photopainter/") and "@" not in agent
        query = parse_qs(urlsplit(request.full_url).query)
        if "noaa.gov" in request.full_url:
            assert query["application"] == ["photopainter"]
        else:
            assert query["timezone"] == ["America/Los_Angeles"]


@pytest.mark.parametrize("name", MODULES)
def test_fresh_cache_is_not_refetched(api, name):
    run(name)
    api.requests.clear()
    max_age = MODULES[name].MAX_AGE
    for now in (RECORDED + timedelta(minutes=1), RECORDED + max_age - timedelta(minutes=1)):
        r = run(name, now)
        assert (r.fetched, r.error, r.note) == (False, None, "fresh")
        assert r.fetched_at == RECORDED and r.cache is not None
    assert api.requests == []


@pytest.mark.parametrize("name", ["weather", "surf"])
def test_refetched_before_the_day_screen_prints_its_age(name):
    """On a working connection the "as of" note never shows."""
    assert MODULES[name].MAX_AGE < STALE_AFTER
    assert MODULES[name].MAX_AGE < SHOW_AGE_AFTER


@pytest.mark.parametrize("name", MODULES)
def test_old_cache_is_refetched(api, name):
    run(name)
    api.requests.clear()
    later = RECORDED + MODULES[name].MAX_AGE + timedelta(minutes=1)
    r = run(name, later)
    assert r.fetched and r.error is None and r.fetched_at == later
    assert "older than" in r.note
    assert api.requests


def test_tides_short_cache_is_refetched(api):
    run("tides")
    path = common.cache_path(tides.FILE)
    cache = json.loads(path.read_text())
    cutoff = (RECORDED + timedelta(days=13)).isoformat()
    cache["curve"] = [p for p in cache["curve"] if p["t"] < cutoff]
    state.write_json(path, cache)
    api.requests.clear()
    r = run("tides", RECORDED + timedelta(hours=1))
    assert r.fetched and "needs 14 days ahead" in r.note
    assert len(tides.load()["curve"]) > len(cache["curve"])


def test_tides_station_or_datum_change_refetches(api):
    run("tides")
    api.requests.clear()
    other = replace(CONFIG, tides=Tides(station="9410660", datum="MLLW"))
    r = run("tides", RECORDED + timedelta(minutes=5), other)
    assert r.fetched and "for station 9410580" in r.note
    assert parse_qs(urlsplit(api.requests[0][0].full_url).query)["station"] == ["9410660"]
    assert tides.load()["station"] == "9410660"
    r = run("tides", RECORDED + timedelta(minutes=6), replace(other, tides=Tides(station="9410660", datum="MSL")))
    assert r.fetched and "datum" in r.note


def test_tides_reach_back_as_far_as_the_chart_starts(api):
    p = tides.request_params(CONFIG, RECORDED, "hilo")
    assert p["begin_date"] == "20261102 08:00"  # yesterday, by default
    half = replace(CONFIG, tides=replace(CONFIG.tides, chart_days_behind=0.5))
    assert tides.request_params(half, RECORDED, "hilo")["begin_date"] == "20261102 08:00"  # never less
    longer = replace(CONFIG, tides=replace(CONFIG.tides, chart_days_behind=2.5))
    assert tides.request_params(longer, RECORDED, "hilo")["begin_date"] == "20261031 07:00"  # 3 days, in PDT

    run("tides")
    api.requests.clear()
    assert not run("tides", RECORDED + timedelta(hours=1)).fetched
    r = run("tides", RECORDED + timedelta(hours=2), longer)
    assert r.fetched and "the chart goes back 2.5 days" in r.note
    assert parse_qs(urlsplit(api.requests[0][0].full_url).query)["begin_date"] == ["20261101 07:00"]


@pytest.mark.parametrize("fetched", [RECORDED, datetime(2026, 11, 4, 0, 5, tzinfo=LA)])
@pytest.mark.parametrize("behind", [0, 0.5, 1, 1.5, 2, 7])
def test_tides_cache_still_reaches_the_chart_start_a_day_later(fetched, behind):
    """A cache stays good for all of its 24 hours: no refetching just because now moved on."""
    config = replace(CONFIG, tides=replace(CONFIG.tides, chart_days_behind=behind))
    begin = datetime.strptime(tides.request_params(config, fetched, "30")["begin_date"], "%Y%m%d %H:%M")
    end = fetched + timedelta(days=tides.FETCH_DAYS_AHEAD)
    cache = {
        "station": config.tides.station, "datum": config.tides.datum, "units": tides.UNITS,
        "fetched_at": state.to_iso(fetched),
        "hilo": [{"t": state.to_iso(end), "v": 1.0, "type": "H"}],
        "curve": [{"t": state.to_iso(begin.replace(tzinfo=timezone.utc)), "v": 1.0},
                  {"t": state.to_iso(end), "v": 1.0}],
    }
    for hours in (0, 1, 12, 23.9):
        assert tides.stale_reason(cache, config, fetched + timedelta(hours=hours)) is None


def test_tides_reach_back_as_far_as_the_harbor_chart_starts():
    longer = replace(CONFIG, harbor=replace(CONFIG.harbor, chart_days_behind=2.5))
    assert tides.request_params(longer, RECORDED, "hilo")["begin_date"] == "20261031 07:00"  # 3 days, in PDT
    cache = tides.parse(fixture("noaa_hilo.json"), fixture("noaa_curve.json"), CONFIG, RECORDED)
    assert "the chart goes back 2.5 days" in tides.stale_reason(cache, longer, RECORDED)


def test_tide_chart_never_asks_for_more_than_the_cache_keeps():
    assert MAX_CHART_DAYS_AHEAD <= tides.MIN_AHEAD.days


def test_weather_location_change_refetches(api):
    run("weather")
    moved = replace(CONFIG, location=replace(CONFIG.location, lat=33.6))
    r = run("weather", RECORDED + timedelta(minutes=5), moved)
    assert r.fetched and r.cache["lat"] == 33.6


@pytest.mark.parametrize("name", MODULES)
def test_cache_from_the_future_is_refetched(api, name):
    """A cache stamped well after `now` means the clock moved back; don't trust it."""
    run(name)
    r = run(name, RECORDED - timedelta(hours=1))
    assert r.fetched and "the clock moved back" in r.note


@pytest.mark.parametrize("name", MODULES)
def test_corrupt_cache_is_refetched(api, name):
    path = common.cache_path(MODULES[name].FILE)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert run(name).fetched
    path.write_text('{"fetched_at": 5}')
    r = run(name, RECORDED + timedelta(minutes=1))
    assert r.fetched


@pytest.mark.parametrize("name", MODULES)
def test_force_fetches_a_fresh_cache(api, name):
    run(name)
    api.requests.clear()
    r = run(name, RECORDED + timedelta(minutes=1), force=True)
    assert r.fetched and r.note == "forced" and api.requests


@pytest.mark.parametrize("name", MODULES)
@pytest.mark.parametrize("failure, message", [
    (urllib.error.URLError(OSError(8, "nodename nor servname provided")), "can't reach"),
    (socket.timeout("timed out"), "didn't answer within 10 s"),
    (urllib.error.URLError(socket.timeout("timed out")), "didn't answer within 10 s"),
    (ConnectionResetError(54, "Connection reset by peer"), "lost the connection"),
])
def test_failed_fetch_keeps_the_old_cache(api, name, failure, message):
    run(name)
    before = cache_bytes(MODULES[name])
    api.fail = failure
    later = RECORDED + MODULES[name].MAX_AGE + timedelta(minutes=1)
    r = run(name, later)
    assert not r.fetched
    assert message in r.error
    assert r.fetched_at == RECORDED and r.cache == json.loads(before)  # still the old one
    assert cache_bytes(MODULES[name]) == before


@pytest.mark.parametrize("name", MODULES)
def test_failed_fetch_without_a_cache_writes_nothing(api, name):
    api.fail = urllib.error.URLError("no route")
    r = run(name)
    assert (r.fetched, r.fetched_at, r.cache) == (False, None, None)
    assert "can't reach" in r.error
    assert cache_bytes(MODULES[name]) is None


@pytest.mark.parametrize("name", MODULES)
def test_bad_response_keeps_the_old_cache(api, name):
    run(name)
    before = cache_bytes(MODULES[name])
    names = {"tides": ["noaa_hilo.json", "noaa_curve.json"], "weather": ["openmeteo_forecast.json"],
             "surf": ["openmeteo_marine.json"],
             "gauge": ["noaa_gauge_water_level.json", "noaa_gauge_predictions.json"]}[name]
    later = RECORDED + MODULES[name].MAX_AGE + timedelta(minutes=1)
    api.bodies = dict.fromkeys(names, b"<html>Service Unavailable</html>")
    r = run(name, later)
    assert not r.fetched and "isn't JSON" in r.error
    api.bodies = dict.fromkeys(names, b'{"unexpected": true}')
    r = run(name, later + common.RETRY_AFTER)
    assert not r.fetched and r.error
    assert cache_bytes(MODULES[name]) == before


def test_http_error_reports_the_reason(api):
    body = (FIXTURES / "openmeteo_error.json").read_bytes()
    api.fail = urllib.error.HTTPError("https://api.open-meteo.com/v1/forecast", 400, "Bad Request",
                                      None, io.BytesIO(body))
    r = run("weather")
    assert "api.open-meteo.com answered HTTP 400: Invalid value" in r.error


def test_after_a_failure_the_next_try_waits(api, sdir):
    api.fail = urllib.error.URLError("no route")
    first = run("weather")
    assert first.error and len(api.requests) == 1
    # every minute for the next five: no network, same error reported
    for minutes in range(1, 5):
        r = run("weather", RECORDED + timedelta(minutes=minutes))
        assert r.error == first.error and "next try after 23:23" in r.note
    assert len(api.requests) == 1
    api.fail = None
    r = run("weather", RECORDED + common.RETRY_AFTER)
    assert r.fetched and r.error is None and len(api.requests) == 2
    attempts = state.read_json(sdir / common.ATTEMPTS)
    assert attempts["weather"] == {"at": state.to_iso(RECORDED + common.RETRY_AFTER), "error": None}


def test_backoff_is_per_source(api):
    api.fail = urllib.error.URLError("no route")
    run("weather")
    api.fail = None
    assert run("surf", RECORDED + timedelta(minutes=1)).fetched


def test_data_still_stale_after_fetching_is_kept_but_backs_off(api):
    """NOAA sending too few days: keep it (it's all there is), report it, don't hammer."""
    raw = fixture("noaa_curve.json")
    raw["predictions"] = raw["predictions"][:48 * 5]
    api.bodies["noaa_curve.json"] = json.dumps(raw).encode()
    r = run("tides")
    assert r.fetched and "fetched, but still stale" in r.error
    assert tides.load() is not None
    api.requests.clear()
    r = run("tides", RECORDED + timedelta(minutes=1))
    assert not r.fetched and api.requests == []


@pytest.mark.parametrize("name, section", [("weather", Weather), ("surf", Surf)])
def test_disabled_sources_dont_fetch(api, name, section):
    run(name)
    before = cache_bytes(MODULES[name])
    api.requests.clear()
    off = replace(CONFIG, **{name: section(enabled=False)})
    r = run(name, RECORDED + timedelta(days=2), off, force=True)
    assert (r.fetched, r.error, r.note) == (False, None, "disabled in config")
    assert api.requests == []
    assert cache_bytes(MODULES[name]) == before  # left alone


def test_disabled_sources_are_skipped_by_ensure_all_fresh(api):
    off = replace(CONFIG, weather=Weather(enabled=False), surf=Surf(enabled=False), gauge=Gauge(enabled=False))
    results = ensure_all_fresh(off, RECORDED)
    assert list(results) == ["tides", "weather", "surf", "gauge"]
    assert results["tides"].fetched
    assert set(api.hosts()) == {"api.tidesandcurrents.noaa.gov"}


@pytest.mark.parametrize("name", MODULES)
def test_ensure_fresh_never_raises(api, monkeypatch, name):
    run(name)
    before = cache_bytes(MODULES[name])

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(state, "write_json", disk_full)
    r = run(name, RECORDED + timedelta(days=2))
    assert not r.fetched and "No space left" in r.error
    assert cache_bytes(MODULES[name]) == before

    naive = run(name, datetime(2026, 11, 3, 12, 0))
    assert "timezone-aware" in naive.error

    monkeypatch.setattr(state, "read_json", disk_full)
    assert run(name).error


def test_attempt_file_problems_dont_stop_a_fetch(api, sdir):
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / common.ATTEMPTS).write_text('["not", "an", "object"]')
    assert run("surf").fetched
    assert state.read_json(sdir / common.ATTEMPTS)["surf"]["error"] is None


def test_default_now_is_aware_local_time(api):
    r = surf.ensure_fresh(CONFIG)
    assert r.fetched and r.fetched_at.utcoffset() is not None


def test_cli_refreshes_and_reports(api, capsys, monkeypatch):
    from photopainter.sources.__main__ import main

    # the CLI reads the clock itself; the recorded forecasts only reach so far past RECORDED
    monkeypatch.setattr(common, "now_in", lambda tz: RECORDED.astimezone(tz))
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "tides    fetched" in out and "surf     fetched" in out
    assert main([]) == 0
    assert "fresh" in capsys.readouterr().out
    api.fail = urllib.error.URLError("no route")
    assert main(["--force"]) == 1
    assert "error: can't reach" in capsys.readouterr().out


# --- get_json --------------------------------------------------------------------------


def test_get_json_refuses_huge_responses(api, monkeypatch):
    monkeypatch.setattr(common, "MAX_BYTES", 100)
    api.bodies["openmeteo_forecast.json"] = b"[" + b"1," * 100 + b"1]"
    with pytest.raises(FetchError, match="sent more than"):
        common.get_json(weather.API, {"a": 1})


def test_get_json_encodes_parameters(api):
    common.get_json(weather.API, {"begin_date": "20261102 08:00", "hourly": "a,b"})
    url = api.requests[0][0].full_url
    assert url.startswith(weather.API + "?")
    assert parse_qs(urlsplit(url).query) == {"begin_date": ["20261102 08:00"], "hourly": ["a,b"]}


# --- config ----------------------------------------------------------------------------


def test_config_has_weather_and_surf_sections():
    c = default_config()
    assert c.weather.enabled and c.surf.enabled
    assert c.to_dict()["weather"] == {"enabled": True}
    assert c.to_dict()["surf"] == {"enabled": True}
    off = validate({"weather": {"enabled": False}, "surf": {"enabled": False}})
    assert not off.weather.enabled and not off.surf.enabled
    assert validate(off.to_dict()) == off
    assert off.tides == c.tides


@pytest.mark.parametrize("data, fragment", [
    ({"weather": {"enabled": "yes"}}, "weather.enabled: must be true or false"),
    ({"surf": {"enabled": 1}}, "surf.enabled: must be true or false"),
    ({"surf": {"units": "m"}}, "surf.units: unknown setting"),
    ({"weather": True}, "weather: must be an object"),
])
def test_bad_weather_and_surf_settings_are_rejected(data, fragment):
    with pytest.raises(ConfigError, match=fragment):
        validate(data)


def test_timestamps_have_offsets(api):
    ensure_all_fresh(CONFIG, RECORDED.astimezone(timezone.utc))
    for module in MODULES.values():
        cache = module.load()
        assert cache["fetched_at"] == "2026-11-03T23:18:03-08:00"  # stored in local time
        rows = cache.get("curve") or cache.get("gap") or cache["hourly"]
        assert all(state.from_iso(r["t"]).utcoffset() is not None for r in rows)
