"""Named moments of a day, for previews: `moment_at(day, "sunset", lat, lon, tz)`.

The dashboard's previews offer these because they move through the year, so a clock time
can't stand in for them. Sunrise and sunset are `INSIDE_DAY` on the daylight side of the
event: at the event itself the Sun's center is already below the horizon, and a screen that
goes by that would draw it dark with no Sun.

`moon` is a moment between that evening's dusk and the next dawn, so it can fall on the next
calendar day, when the Moon stands west of the window's bearing. The sea screen's words cover
the top left of its sky, so this is the part where the Moon is sure to be seen there, however
high it is, and it's in the sky screen's view too. A Moon that never gets there is taken at its
highest. On a night with no Moon up that's still the best there is, and the picture has no Moon.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

MOMENTS: tuple[str, ...] = ("dawn", "sunrise", "sunset", "dusk", "moon")
INSIDE_DAY = timedelta(minutes=15)  # about 2° of Sun above the horizon at this latitude
MOON_STEP = timedelta(minutes=30)
MOON_MIN_ALTITUDE = 10.0  # degrees
MOON_WEST_OF_WINDOW = (15.0, 50.0)  # degrees past the window's bearing, right of the words…
MOON_BEST_WEST = 35.0  # …and best here


def moment_at(day: date, name: str, lat: float, lon: float, tz: ZoneInfo, window_az: float = 180.0) -> datetime:
    """The moment `name` (one of `MOMENTS`) of `day`, in `tz`. Raises EphemerisMissing without
    the ephemeris and ValueError if the Sun doesn't rise and set that day."""
    from photopainter.astro.sun import sun_events

    if name not in MOMENTS:
        raise ValueError(f"unknown moment {name!r}; moments are {', '.join(MOMENTS)}")
    events = sun_events(day, lat, lon, tz)
    if name == "dawn":
        return events.civil_dawn
    if name == "sunrise":
        return events.sunrise + INSIDE_DAY
    if name == "sunset":
        return events.sunset - INSIDE_DAY
    if name == "dusk":
        return events.civil_dusk
    dawn = sun_events(day + timedelta(days=1), lat, lon, tz).civil_dawn
    return _moon_moment(events.civil_dusk, dawn, lat, lon, window_az)


def _moon_moment(start: datetime, end: datetime, lat: float, lon: float, window_az: float) -> datetime:
    from photopainter.astro.sky import moon_altaz

    times = [start]
    while times[-1] + MOON_STEP < end:
        times.append(times[-1] + MOON_STEP)
    places = [moon_altaz(t, lat, lon) for t in times]
    best, best_off = None, None
    for t, (alt, az) in zip(times, places):
        west = (az - window_az + 180) % 360 - 180
        if alt >= MOON_MIN_ALTITUDE and MOON_WEST_OF_WINDOW[0] <= west <= MOON_WEST_OF_WINDOW[1]:
            off = abs(west - MOON_BEST_WEST)
            if best_off is None or off < best_off:
                best, best_off = t, off
    return best if best is not None else times[max(range(len(times)), key=lambda i: places[i][0])]
