#!/usr/bin/env python3
"""Mac: try the sea screen's look live, with a slider for the day's numbers and for each
`SeaStyle` setting. Every image is drawn by the real `scenes/sea.py`, in the six inks.

    .venv/bin/python scripts/sea-playground.py            # http://127.0.0.1:8743

"Copy settings" gives the values that differ from the defaults, to put in `SeaStyle`. Local
only (it listens on 127.0.0.1); it isn't part of the frame and doesn't touch the Pi.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

from photopainter import places, state
from photopainter.config import default_config
from photopainter.astro.moon import MoonPhase
from photopainter.render.draw import InkCanvas
from photopainter.scenes import sea
from photopainter.scenes.tides import compass

# name, label, min, max, step, default
DAY_SLIDERS = (
    ("swell_ft", "Swell height (ft)", 0, 15, 0.5, 2),
    ("period_s", "Swell period (s)", 4, 22, 1, 13),
    ("swell_from", "Swell from (°)", 150, 290, 5, 225),
    ("wind_mph", "Wind (mph)", 0, 45, 1, 4),
    ("tide_ft", "Tide (ft)", -1.5, 7.5, 0.5, 3.5),
    ("light_az", "Sun or Moon bearing (°)", 120, 260, 1, 216),
    ("light_alt", "Sun or Moon height (°)", 0, 70, 1, 31),
    ("wind_from", "Wind from (°)", 0, 355, 5, 270),
    ("moon_lit", "Moon lit (night)", 0, 1, 0.05, 0.9),
    ("seed", "Seed (changes with each redraw)", 0, 500, 1, 3),
)
LOOK_RANGES = {  # SeaStyle field: min, max, step
    "rows_at_10s": (2, 20, 0.5), "rows_min": (1, 15, 1), "rows_max": (3, 24, 1),
    "perspective": (1, 3, 0.05), "nearness_power": (0.5, 2.5, 0.05),
    "amp_base_px": (0, 30, 1), "amp_per_ft": (0, 12, 0.5), "peak_base": (0.5, 4, 0.1), "peak_per_ft": (0, 1, 0.05),
    "crest_px_per_s": (10, 90, 1), "crest_min_px": (20, 300, 5), "tilt": (0, 1, 0.01), "trough_at": (0.1, 1.5, 0.02),
    "chop_per_mph": (0, 0.8, 0.01), "foam_from_px": (0, 60, 1), "foam_chance": (0, 1, 0.05),
    "whitecap_mph": (0, 30, 1), "whitecap_reach": (0, 1, 0.05), "whitecap_reach_per_mph": (0, 0.1, 0.005),
    "whitecap_base": (0, 15, 0.5), "whitecap_per_mph": (0, 6, 0.1), "whitecap_length": (0.3, 4, 0.1),
}
DEFAULT_STYLE = {f.name: f.default for f in dataclasses.fields(sea.SeaStyle)}
PRESETS = {
    "Calm afternoon": {"swell_ft": 1.5, "period_s": 13, "swell_from": 225, "wind_mph": 4, "tide_ft": 3.5,
                       "light_az": 216, "light_alt": 31, "sky": "clear", "time": "day"},
    "Big south swell": {"swell_ft": 8, "period_s": 17, "swell_from": 190, "wind_mph": 15, "tide_ft": 6.5,
                        "sky": "overcast", "time": "day"},
    "Windy, 19 mph": {"swell_ft": 3, "period_s": 9, "swell_from": 250, "wind_mph": 19, "tide_ft": 0.5,
                      "light_az": 240, "light_alt": 14, "sky": "clear", "time": "day"},
    "Gale, 35 mph": {"swell_ft": 6, "period_s": 8, "swell_from": 270, "wind_mph": 35, "tide_ft": 3,
                     "light_az": 200, "light_alt": 35, "sky": "partly", "time": "day"},
    "Moonlit night": {"swell_ft": 3, "period_s": 13, "swell_from": 210, "wind_mph": 6, "tide_ft": 3,
                      "light_az": 200, "light_alt": 40, "moon_lit": 0.9, "sky": "clear", "time": "night"},
}


def render_png(params: dict[str, str]) -> bytes:
    """One picture from the page's values; anything missing takes its default."""
    day = {name: float(params.get(name, default)) for name, _, _, _, _, default in DAY_SLIDERS}
    night = params.get("time") == "night"
    fields = {f.name: f for f in dataclasses.fields(sea.SeaStyle)}
    style = sea.SeaStyle(**{k: int(float(v)) if fields[k].type == "int" else float(v)
                            for k, v in params.items() if k in fields})
    light = None
    if day["light_alt"] > 0:
        phase = MoonPhase(day["moon_lit"], 180.0, True, "") if night else None
        light = sea.Light(day["light_az"], day["light_alt"], phase, 270.0 if night else 0.0)
    word = {"clear": "sunny", "partly": "partly cloudy", "overcast": "cloudy"}[params.get("sky", "clear")]
    wind = round(day["wind_mph"])
    model = sea.SeaModel(
        seed=int(day["seed"]), daylight=not night, sky=params.get("sky", "clear"), light=light,
        swell_ft=day["swell_ft"], period_s=day["period_s"], swell_from=day["swell_from"],
        wind_mph=day["wind_mph"], beach_px=sea.beach_px(day["tide_ft"]),
        words=(f"Thursday, November 5  ·  {word}, 71°",
               f"{day['swell_ft']:.0f} ft at {day['period_s']:.0f} s from the {compass(day['swell_from'])}{sea.ARROW}",
               f"Wind {wind} mph {compass(day['wind_from'])}{sea.ARROW}  ·  Water 62°  ·  High tide 3:42 pm"),
        wind_from=day["wind_from"], updated="Updated 2:10 pm")
    canvas = InkCanvas((800, 480), "white")
    loc = default_config().location  # the Wedge: its island and jetty
    place = places.sea_for(loc.lat, loc.lon, 190.0, state.data_dir())
    sea.draw(canvas, model, 190.0, places.skyline_for(loc.lat, loc.lon, state.data_dir()), style,
             jetty=None if place is None else place.jetty)
    out = io.BytesIO()
    canvas.preview().save(out, "PNG")
    return out.getvalue()


def page() -> str:
    day = [dict(name=n, label=l, min=a, max=b, step=s, value=d) for n, l, a, b, s, d in DAY_SLIDERS]
    look = [dict(name=k, label=k.replace("_", " "), min=r[0], max=r[1], step=r[2], value=DEFAULT_STYLE[k])
            for k, r in LOOK_RANGES.items()]
    return PAGE.replace("/*DATA*/", f"const DAY={json.dumps(day)},LOOK={json.dumps(look)},PRESETS={json.dumps(PRESETS)};")


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Sea playground</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
body{font:14px system-ui,sans-serif;margin:0;background:#f4f4f2;color:#222}
#pic{position:sticky;top:0;background:#f4f4f2;padding:12px;z-index:1;border-bottom:1px solid #ccc}
#pic img{display:block;width:800px;max-width:100%;image-rendering:pixelated;border:1px solid #888}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;align-items:center}
button,select{font:inherit;padding:4px 10px}
#groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:4px 28px;padding:12px}
h2{font-size:14px;margin:8px 0}
label{display:grid;grid-template-columns:170px 1fr 48px;gap:8px;align-items:center;padding:2px 0}
label.changed span:first-child{font-weight:700}
output{text-align:right;font-variant-numeric:tabular-nums}
textarea{width:100%;height:90px;font:12px monospace;box-sizing:border-box;margin-top:6px}
</style></head><body><div id="pic"><img id="img" alt="the sea screen" width="800" height="480">
<div class="bar" id="presets"></div>
<div class="bar">Sky <select id="sky"><option>clear</option><option>partly</option><option>overcast</option></select>
Time <select id="time"><option>day</option><option>night</option></select>
<button id="reset">Reset the look</button><button id="copy">Copy settings</button></div></div>
<div id="groups"><div id="day"><h2>The day</h2></div><div id="look"><h2>The look (bold = changed)</h2>
<textarea id="out" readonly placeholder="Changed settings appear here"></textarea></div></div>
<script>/*DATA*/
const $=(i)=>document.getElementById(i),inputs={};
function row(d,into,isLook){const l=document.createElement("label");
 l.innerHTML=`<span>${d.label}</span><input type="range" min="${d.min}" max="${d.max}" step="${d.step}" value="${d.value}"><output>${d.value}</output>`;
 const i=l.querySelector("input"),o=l.querySelector("output");
 i.oninput=()=>{o.textContent=i.value;if(isLook)l.classList.toggle("changed",+i.value!==d.value);draw()};
 inputs[d.name]={i,o,l,d,isLook};into.insertBefore(l,isLook?$("out"):null)}
DAY.forEach(d=>row(d,$("day"),false));LOOK.forEach(d=>row(d,$("look"),true));
function set(n,v){const x=inputs[n];x.i.value=v;x.o.textContent=x.i.value;if(x.isLook)x.l.classList.toggle("changed",+x.i.value!==x.d.value)}
for(const [name,p] of Object.entries(PRESETS)){const b=document.createElement("button");b.textContent=name;
 b.onclick=()=>{for(const [k,v] of Object.entries(p)){if(k==="sky"||k==="time")$(k).value=v;else set(k,v)}draw()};$("presets").append(b)}
$("sky").onchange=$("time").onchange=()=>draw();
$("reset").onclick=()=>{LOOK.forEach(d=>set(d.name,d.value));draw()};
function changed(){const o={};LOOK.forEach(d=>{const v=+inputs[d.name].i.value;if(v!==d.value)o[d.name]=v});return o}
$("copy").onclick=()=>{navigator.clipboard&&navigator.clipboard.writeText($("out").value)};
let busy=false,again=false;
function draw(){$("out").value=JSON.stringify(changed(),null,1);if(busy){again=true;return}busy=true;
 const q=new URLSearchParams({sky:$("sky").value,time:$("time").value});
 for(const [n,x] of Object.entries(inputs))q.set(n,x.i.value);
 const next=new Image();next.onload=next.onerror=()=>{if(next.complete&&next.naturalWidth)$("img").src=next.src;busy=false;if(again){again=false;draw()}};
 next.src="/render.png?"+q}
draw();</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path == "/":
            body, kind = page().encode(), "text/html; charset=utf-8"
        elif url.path == "/render.png":
            try:
                body, kind = render_png(dict(parse_qsl(url.query))), "image/png"
            except (ValueError, KeyError) as exc:
                self.send_error(400, str(exc))
                return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8743)
    port = parser.parse_args().port
    print(f"Sea playground: http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
