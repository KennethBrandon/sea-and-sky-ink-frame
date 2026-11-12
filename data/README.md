# data/

Files the frame reads at run time. On the Pi this folder is `~/eink-photopainter/data/`
(copied by `scripts/deploy.sh`); code finds it with `state.data_dir()`, which tests and
the Mac can point elsewhere with `PHOTOPAINTER_DATA_DIR`.

Everything committed here must be licensed for redistribution in this repo. Record each
file's source, version and license below, and add it to the dashboard's About box
(`web/static/index.html`, `#about-card`), the last box on the page, which gives users
every credit and license. Its closed line reads "© Kenneth Brandon, credits" (Owner,
2026-11-07: the credits moved there from the fine print under the boxes).

| File | What | Source | License |
|---|---|---|---|
| `fonts/DejaVuSans.ttf`, `fonts/DejaVuSans-Bold.ttf` | The font for every drawn screen (`render/draw.py`) | DejaVu Fonts 2.37, `dejavu-fonts-ttf-2.37.tar.bz2` from <https://github.com/dejavu-fonts/dejavu-fonts/releases/tag/version_2_37>, unmodified | Bitstream Vera license + Arev license; DejaVu changes are public domain. Permissive (free use, modification and redistribution, including bundling with software). Full text: [`fonts/DejaVu-LICENSE.txt`](fonts/DejaVu-LICENSE.txt) |
| `de421.bsp` | JPL DE421 planetary and lunar ephemeris, 1900–2050, about 17 MB (Skyfield reads it for the Sun, Moon and planets) | JPL Solar System Dynamics, <https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de421.bsp> (SHA-256 `a20a7139…deedc`; NAIF's copy has a different layout and checksum, both accepted) | Published by NASA/JPL for public use. **Not committed** (gitignored): `scripts/fetch-ephemeris.sh` downloads it once per machine to `~/.cache/photopainter-dev/` and links it here |

### The night sky (`scenes/sky.py`)

Built on the Mac by `scripts/build-star-data.py` and `scripts/build-skyline.py`, from
pinned source files (URLs and SHA-256 in the scripts). Rebuild only to change sources.

| File | What | Source | License |
|---|---|---|---|
| `stars.json` | 1,663 stars: every Hipparcos star to magnitude 5, plus the 38 fainter ones the constellation lines use. Rows are `[hip, ra_deg, dec_deg, mag, name, bv]` (J2000; IAU star names; B−V color index). 80 KB | [HYG database](https://codeberg.org/astronexus/hyg) v4.1 by David Nash (astronexus), `hygdata_v41.csv` from GitHub `astronexus/HYG-Database` at commit `c7f7f88` (2025-02-14). Changed: filtered, RA in degrees, rounded; Betelgeuse's B−V set to 1.85 (SIMBAD; HYG has Hipparcos' 1.50) | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Credit: "HYG database, astronexus.com" |
| `constellations.json` | Lines for all 88 constellations, as pairs of Hipparcos numbers, with IAU names. 15 KB | [Stellarium](https://stellarium.org) v26.3, `skycultures/modern/index.json` (Stellarium's team). Changed: polylines split into de-duplicated pairs; names and boundaries left out | [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) (the sky culture's text and data). Credit: "Stellarium" |
| `milkyway.png` | The Milky Way's brightness, 600 × 300 grayscale on a galactic-coordinate grid (0.6° cells; longitude 0 at the center, increasing to the left; latitude up), stored as sqrt(brightness) × 255. 58 KB | ESO's all-sky Milky Way panorama by Serge Brunier, [eso0932a](https://www.eso.org/public/images/eso0932a/), the 6000 × 3000 "large" JPEG. Changed: converted to brightness, stars removed with a 10 × 10 pixel median, sky background subtracted, scaled, downsampled | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). **Credit required, visible to users:** "ESO/S. Brunier", in the dashboard's About box with the other credits (not on the panel: Owner, 2026-11-04) |
| `places/wedge/skyline.json` | Santa Catalina Island's skyline from the Wedge: height above the sea horizon (degrees) every 0.25° of azimuth, with Earth's curvature and refraction, for an eye 10 m above the sea; smoothed over about 1° (checked against a photo of the island from the coast). 4 KB | Copernicus DEM GLO-90 elevations via the [Open-Meteo elevation API](https://open-meteo.com/en/docs/elevation-api), sampled every 0.005° over the island and interpolated 5× finer | Open-Meteo data [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); Copernicus DEM © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA |

`stars.json` and `constellations.json` are adaptations of CC BY-SA 4.0 data, so they are
CC BY-SA 4.0 too.

### The harbor chart (`scenes/harbor.py`)

Built on the Mac by `scripts/build-harbor.py`, from an Overpass response and NOAA soundings
(it can fetch both, or read saved copies). Rebuild only to change sources or to freshen them.

| File | What | Source | License |
|---|---|---|---|
| `places/wedge/harbor.json` | Newport Bay and the coast from 33.54 to 33.70 N, 118.02 to 117.78 W: the coastline chained, the bay's multipolygon (islands and marsh as holes), other water, marsh, beaches, breakwaters, named piers, channel marks with color and number, named islands and thinned soundings, as lat/lon lists simplified to about 3 m. Fetched 2026-11-04. About 150 KB | Shoreline, marks, piers: [OpenStreetMap](https://www.openstreetmap.org) via the [Overpass API](https://overpass-api.de); the query is in the script. Soundings: NOAA [ENC Direct to GIS](https://encdirect.noaa.gov), harbor-scale `Sounding_point` first, approach-scale where those are missing; metres converted to whole feet below MLLW, rounded down | OpenStreetMap data: [ODbL](https://opendatacommons.org/licenses/odbl/). **Credit required, visible to users:** "© OpenStreetMap contributors", in the dashboard's About box with the other credits (not on the panel: Owner, 2026-11-06). NOAA ENC data: US government, public domain. Not for navigation |

`harbor.json` is a derived database of OpenStreetMap data, so it stays under ODbL.

### Places (`places/`, `src/photopainter/places.py`)

What's particular to one spot on the coast lives in a folder per place, so the screens name no
place of their own. The Wedge's is bundled (`places/wedge/`): its `skyline.json` and `harbor.json`
above, built by the scripts and never edited by hand, and `place.json`, the choices made by hand
for that spot. Places built for one frame go in `~/.config/photopainter/places/` on the Pi and
come first. Each part is matched by location: the skyline within 2 km of its observer, the
harbor chart inside its bounds, the sea's jetty within 2 km and 45° of where and how it was drawn.

| File | What | Source | License |
|---|---|---|---|
| `places/wedge/place.json` | The island's label (CATALINA); the harbor chart's title, default view, place names and where they sit, bold names, big piers and where the marsh fringe starts; the sea screen's jetty outline (drawn by the Owner over a preview), its beach's tide range (0-7 ft) and the observer and window bearing they were drawn for. Coordinates are public landmarks, never a home | Written for this project | The repo's license (`LICENSE`) |

## Data fetched at run time

The day screen's tides, water level, weather and surf come from web APIs
(`src/photopainter/sources/`). They're cached in the state dir on the Pi (`tides.json`,
`gauge.json`, `weather.json`, `surf.json`), not stored here. Recorded responses used by the tests are committed in `tests/fixtures/`
(`noaa_*.json`, `openmeteo_*.json`) under the same terms.

| Data | Source | License and credit |
|---|---|---|
| Tide predictions, station 9410580 (Newport Bay Entrance) | NOAA CO-OPS data API, <https://api.tidesandcurrents.noaa.gov/api/prod/> | US Government work, public domain. No key; requests carry `application=photopainter` as NOAA asks |
| Water levels and tide predictions, station 9410660 (Los Angeles) | NOAA CO-OPS data API, same as above | US Government work, public domain. The recent readings are preliminary. The day screen shows the gap from the table as an estimate for the Wedge, and the dashboard says it comes from this gauge |
| Weather forecast | Open-Meteo forecast API, <https://open-meteo.com/en/docs> | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), free for non-commercial use. **Credit required:** "Weather data by [Open-Meteo.com](https://open-meteo.com/)" wherever it's shown (here, and on the dashboard) |
| Swell, waves and sea surface temperature | Open-Meteo marine API, <https://open-meteo.com/en/docs/marine-weather-api> | Same as above (CC BY 4.0, non-commercial, credit Open-Meteo). It combines several wave models; see the page for their own sources |

## Why DejaVu Sans

Charts are drawn without anti-aliasing (the panel has six inks and no grays), so small
text depends on the font's hinting. Compared at 12–24 px with anti-aliasing off,
DejaVu Sans kept an even stroke weight; Atkinson Hyperlegible (OFL), the other candidate,
snapped some glyphs to bold mid-word at 14 px. Swap the font by changing `FONT_FILES` in
`render/draw.py` and recording the new one here.
