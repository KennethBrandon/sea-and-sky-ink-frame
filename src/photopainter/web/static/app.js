/* The frame's dashboard: one page over the JSON API in web/server.py. Plain JS, no build. */
"use strict";

const POLL_MS = 10000;      // PRD W2: poll /api/status every 10 s
const BUSY_POLL_MS = 2000;  // while a tick runs or the panel refreshes
const REFRESH_S = 35;       // a full refresh takes about 30 s on this panel
const SWAP_S = 15;          // the panel changes to the new picture about halfway through
const MAX_SIDE = 1600;      // uploads are resized to this before sending
const PREVIEW_S = 5;        // a screen takes about this long to draw on the Pi
const WIDE = matchMedia("(min-width: 900px)");  // two columns: previews go on the frame itself
const CALM = matchMedia("(prefers-reduced-motion: reduce)");
const LIFT_MS = 350;        // a finger holds a photo this long to pick it up, so a swipe still scrolls
const DRAG_PX = 6;          // a mouse moves this far to pick one up, so a click still opens the sheet
const EDGE_PX = 70;         // a photo dragged this close to the window's edge scrolls the page

const KIND = { tides: "Tides", sky: "Sky", sea: "Sea", photo: "Photo", message: "Message", card: "Color test card", combo: "Combo", harbor: "Harbor", auto: "Auto" };
const SCREENS = { tides: "Tides", sky: "Sky", sea: "Sea", combo: "Combo", photo: "Photo", harbor: "Harbor" };  // what a step of the schedule's rotation can show

const S = {
  status: null,
  config: null,     // the settings as last loaded or saved
  configText: {},   // …as each card's form first showed them, to tell when something changed
  photos: [],
  photoSig: null,
  photoLoads: 0,    // counts lists asked for, so an old answer can't put back an old order
  tiles: new Map(), // photo name -> its tile in the grid
  drag: null,       // the tile being dragged: {tile}
  dragEnded: 0,     // when the last one was put down: the click that ends a drag opens nothing
  orderTimer: null,
  uploads: [],      // files waiting to upload: {file, tile}
  uploading: false,
  localThumbs: new Map(),  // photo name -> the upload's own picture, until its preview is drawn
  sheet: null,      // the photo open in the sheet
  confirmDelete: false,
  until: "next_change",
  working: null,    // the mode button being applied
  timer: null,
  preview: null,    // a preview on the frame (wide screens): {req, label, pinnable, url}
  viewerReq: null,  // the preview the viewer is drawing, so a late answer isn't shown over a newer one
  viewerUrl: null,
  batteryHeld: false,   // a finger or button is on the battery chart, reading it
  batteryStale: false,  // new battery data came while it was: draw it on release
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/* el("span", {class: "x"}, "text", child…): text always goes in as text, never as HTML. */
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "checked" || k === "selected" || k === "hidden" || k === "open" || k === "disabled") node[k] = !!v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}

/* An icon from the <template id="icon-…"> set in index.html (our own markup, never data). */
function icon(name) {
  const span = el("span", { class: "icon" });
  const tpl = document.getElementById(`icon-${name}`);
  if (tpl) span.append(tpl.content.cloneNode(true));
  return span;
}

/* --- talking to the frame ----------------------------------------------------------- */

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("Can't reach the frame. Is it online?");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `The frame answered ${res.status}`);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

/* --- times, always in the frame's time zone ------------------------------------------ */

function tz() { return (S.status && S.status.timezone) || undefined; }

function time(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: tz() })
    .replace(" AM", " am").replace(" PM", " pm");
}

function dayTime(iso) {
  // "4:56 pm", or "Thu 6:13 am" when it isn't today at the frame
  if (!iso) return "";
  const d = new Date(iso);
  const day = (x) => x.toLocaleDateString("en-US", { timeZone: tz() });
  const now = S.status ? new Date(S.status.server_time) : new Date();
  if (day(d) === day(now)) return time(iso);
  return d.toLocaleDateString("en-US", { weekday: "short", timeZone: tz() }) + " " + time(iso);
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 36) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

/* --- toast -------------------------------------------------------------------------- */

let toastTimer = null;
/* toast(text, isError, {label, run}): the action is a button in the toast (Redraw now). */
function toast(text, isError = false, action = null) {
  const box = $("#toast");
  $("#toast-text").textContent = text;
  const act = $("#toast-act");
  act.hidden = !action;
  if (action) {
    act.textContent = action.label;
    act.onclick = () => { box.hidden = true; action.run(); };
  }
  box.classList.toggle("error", isError);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, isError ? 7000 : action ? 9000 : 3200);
}

/* --- status ------------------------------------------------------------------------- */

async function poll() {
  clearTimeout(S.timer);
  try {
    S.status = await api("GET", "/api/status");
    renderStatus();
  } catch (e) {
    $("#clock").textContent = e.message;
    $(".dot").dataset.level = "error";
  }
  S.timer = setTimeout(poll, busy() ? BUSY_POLL_MS : POLL_MS);
}

function busy() {
  const st = S.status;
  return !!(st && (st.refreshing || st.ticks.running || st.ticks.queued || S.working));
}

function renderStatus() {
  const st = S.status;
  document.title = `${st.location} · Frame`;
  $("#place").textContent = st.location;
  $("#clock").textContent = `${time(st.server_time)} at the frame` + (st.fake_panel ? " · fake panel" : "");
  renderImage(st);
  renderPlacard(st);
  renderActivity(st);
  renderAlerts(st);
  renderMode(st);
  renderHealth(st);
  renderBatteryNow(st);
  maybeLoadBattery(st);
  renderConfigError(st);
  const when = $('#schedule [data-when="day"]');
  if (when && st.day_night && st.day_night.sunrise) when.textContent = dayWhen(st.day_night);
  const worst = st.problems.some((p) => p.level === "error") ? "error"
    : st.problems.length ? "warn" : "ok";
  $(".dot").dataset.level = worst;
  // What's on the wall, the queue or a pin changed: the photo badges follow.
  const sig = JSON.stringify([st.showing && st.showing.key, st.overrides.up_next, st.mode.photo]);
  if (sig !== S.photoSig) {
    S.photoSig = sig;
    loadPhotos();
  }
}

// The picture on the wall right now: the old one for the first half of a refresh, then the new
// one, as on the panel. Timed by the frame's clock alone: it can be wrong, but not with itself.
function wallSrc(st) {
  const r = st.refreshing;
  if (r && r.panel_at && (new Date(st.server_time) - new Date(r.panel_at)) / 1000 >= SWAP_S) {
    return `/api/incoming.png?v=${encodeURIComponent(r.panel_at)}`;
  }
  return st.image ? `/api/current.png?v=${st.image.version}` : null;
}

function renderImage(st) {
  const img = $("#current");
  const src = wallSrc(st);
  if (!src) {
    img.hidden = true;
    $("#empty").hidden = !!S.preview;
    return;
  }
  if (!S.preview && st.image) $("#frame").classList.toggle("portrait", st.image.height > st.image.width);
  if (img.dataset.src !== src) {
    const next = new Image();
    next.onload = () => { img.src = src; img.dataset.src = src; img.hidden = !!S.preview; $("#empty").hidden = true; };
    next.src = src;
  }
}

function showingLabel(showing) {
  if (!showing) return "Nothing yet";
  if (showing.kind === "photo") return showing.photo ? `Photo · ${showing.photo}` : "Photo";
  if (showing.kind === "message") return "Message";
  return KIND[showing.kind] || showing.kind;
}

function renderPlacard(st) {
  const sh = st.showing;
  const previewing = !!S.preview;
  $("#now-kind").textContent = previewing ? `On the wall: ${showingLabel(sh)}` : showingLabel(sh);
  $("#now-kind").classList.toggle("muted", previewing);
  $("#now-drawn").textContent = sh && sh.refreshed_at ? `drawn ${dayTime(sh.refreshed_at)}` : "";
  $("#redraw").hidden = !sh;
  renderComing(st);
}

function whyText(u) {
  switch (u.why) {
    case "sunrise": return "sunrise";
    case "sunset": return "sunset";
    case "pin_ends": return "the pin ends";
    case "photo": return (u.photo || "a photo") + (u.minutes ? `, ${u.minutes} min` : "");
    case "photo_ends": return "after the photo";
    case "redraw": return u.kind === "sky" ? "redraws as the sky turns" : "redraws";
    default: return "";
  }
}

/* What the frame will do next, from /api/status `upcoming`. */
function renderComing(st) {
  const list = st.upcoming || [];
  const box = $("#coming");
  const note = $("#now-next");
  if (!list.length) {
    box.hidden = true;
    const stays = st.mode.kind !== "auto" && !st.mode.until && !st.refreshing;
    note.textContent = stays ? "Stays until you change it" : "";
    note.hidden = !stays;
    return;
  }
  note.hidden = true;
  $("#coming-list").replaceChildren(...list.map((u) => el("li", {},
    el("time", { datetime: u.at }, dayTime(u.at)),
    icon(u.kind),
    el("span", { class: "what" }, el("b", {}, KIND[u.kind] || u.kind), " ", el("span", {}, whyText(u))))));
  $("#coming-next").textContent = `${KIND[list[0].kind] || list[0].kind} at ${dayTime(list[0].at)}`;
  box.hidden = false;
}

function renderActivity(st) {
  const box = $("#activity");
  let title = "", detail = "";
  if (st.refreshing) {
    const s = Math.max(0, Math.round((Date.now() - new Date(st.refreshing.started_at)) / 1000));
    title = "Updating the frame…";
    detail = s < REFRESH_S ? `E-ink takes about 30 seconds (${s} s so far)` : "Almost done";
  } else if (st.ticks.running || st.ticks.queued || S.working) {
    title = "Checking what to show…";
  }
  $(".wall").classList.toggle("refreshing", !!(st.refreshing && st.refreshing.panel_at));  // not while it's drawing
  box.hidden = !title;
  $("#activity-title").textContent = title;
  $("#activity-detail").textContent = detail;
  $("#activity-detail").hidden = !detail;
}

function renderAlerts(st) {
  $("#alerts").replaceChildren(...st.problems.filter((p) => p.level === "error")
    .map((p) => el("div", { class: "alert" }, p.text)));
}

/* Draw the wall again now (after a settings change): the next tick redraws the same screen. */
async function redrawNow() {
  const b = $("#redraw");
  b.disabled = true;
  try {
    const res = await api("POST", "/api/redraw");
    toast(res.message);
  } catch (e) {
    toast(e.message, true);
  } finally {
    b.disabled = false;
    poll();
  }
}

/* --- mode --------------------------------------------------------------------------- */

function rotationText(steps) {
  if (!steps || !steps.length) return "nothing";
  const name = (s) => (SCREENS[s.screen] || s.screen).toLowerCase();
  if (steps.length === 1) return name(steps[0]);
  return steps.map((s) => `${name(s)} for ${s.minutes} min`).join(", then ");
}

function scheduleSummary() {
  const c = S.config;
  if (!c) return "Following the schedule.";
  return `Following the schedule: ${rotationText(c.schedule.day)} by day, ${rotationText(c.schedule.night)} at night.`;
}

function renderMode(st) {
  const mode = st.mode;
  const showNow = mode.kind === "photo" && mode.photo;
  for (const b of $$("#modes [data-kind]")) {
    const on = b.dataset.kind === mode.kind && !showNow;
    b.setAttribute("aria-pressed", on ? "true" : "false");
    b.classList.toggle("working", S.working === b.dataset.kind);
    b.closest(".seg").classList.toggle("on", on);
  }
  const dn = st.day_night;
  $("#until-next").textContent = dn ? `${dn.next} · ${time(dn.next_at)}` : "the next change";
  // With a pin on, the chips say how long it lasts (tapping one changes that); on Auto they
  // say how long the next tap will last.
  let checked = S.until;
  if (mode.kind !== "auto") {
    checked = mode.until === null ? "" : dn && mode.until === dn.next_at ? "next_change" : null;
  }
  for (const c of $$("#untils button")) {
    c.setAttribute("aria-checked", c.dataset.until === checked ? "true" : "false");
  }

  const note = $("#mode-note");
  note.replaceChildren();
  if (mode.kind === "auto") {
    note.append(scheduleSummary());
  } else {
    const what = showNow ? `Showing ${mode.photo}` : `${KIND[mode.kind]} pinned`;
    note.append(el("b", {}, mode.until ? `${what} until ${dayTime(mode.until)}.` : `${what} until you change it.`),
      " ", el("button", { type: "button", class: "link", onclick: () => setMode("auto") }, "Back to Auto"));
  }
  if (st.screens && st.screens.sky === false && (mode.kind === "sky" || S.working === "sky")) {
    note.append(el("br"), "The sky chart isn't built yet, so Sky shows tides for now.");
  }
  const cardShow = $("#card-show");
  cardShow.textContent = mode.kind === "card" ? "Back to the schedule" : "Show on the frame";
  cardShow.disabled = !!S.working;
}

// `until` is for callers with their own length; the mode buttons use the chips' choice.
async function setMode(kind, until) {
  if (S.working) return;
  S.working = kind;
  renderMode(S.status);
  try {
    const body = { kind };
    if (kind !== "auto") body.until = until !== undefined ? until : (S.until === "" ? null : S.until);
    const res = await api("POST", "/api/mode", body);
    toast(res.message);
  } catch (e) {
    toast(e.message, true);
  } finally {
    S.working = null;
    poll();
  }
}

function pickUntil(c) {
  S.until = c.dataset.until;
  const mode = S.status && S.status.mode;
  // A pin is on: the chip changes how long it lasts. (A Show now photo keeps its own length.)
  if (mode && mode.kind !== "auto" && !(mode.kind === "photo" && mode.photo)) setMode(mode.kind);
  else if (S.status) renderMode(S.status);
}

/* --- previews: a screen drawn without touching the wall ----------------------------- */

/* req: {kind, at?, photo?, message?, config?} -> a blob URL of the drawn screen. With
   `config` (the form's unsaved settings) it goes as JSON, so the frame draws with those. */
async function fetchPreview(req) {
  let res;
  try {
    if (req.config) {
      res = await fetch("/api/preview.png", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(req) });
    } else {
      const params = new URLSearchParams(Object.entries(req).filter(([, v]) => v !== undefined && v !== null && v !== ""));
      res = await fetch(`/api/preview.png?${params}`);
    }
  } catch (e) {
    throw new Error("Can't reach the frame. Is it online?");
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const err = new Error(data.error || `The frame answered ${res.status}`);
    err.data = data;
    throw err;
  }
  return URL.createObjectURL(await res.blob());
}

/* Draw a screen and show it: on a wide screen on the frame itself; on a phone in the viewer,
   or inline under the settings it was drawn with (`inline`: that group's preview block). */
async function showPreview(req, { label, pinnable = false, inline = null }) {
  const where = inline && !WIDE.matches ? "inline" : WIDE.matches ? "frame" : "viewer";
  const drawing = `Drawing ${label.split(" ")[0] === "the" ? label : "the " + label}…`;
  if (where === "frame") startFramePreview(req, label, pinnable, drawing);
  else if (where === "viewer") openViewer(null, { drawing, caption: `Preview: ${label}`, pinnable: pinnable ? req.kind : null, req });
  else startInlinePreview(inline, drawing);
  let url;
  try {
    url = await fetchPreview(req);
  } catch (e) {
    if (where === "frame") endFramePreview();
    else if (where === "viewer") closeViewer();
    else endInlinePreview(inline, null);
    if (e.data && Array.isArray(e.data.errors)) showFieldErrors(e.data.errors);
    toast(e.message, true);
    return;
  }
  if (where === "frame") {
    if (S.preview && S.preview.req === req) setFramePreview(url);
    else URL.revokeObjectURL(url);  // a newer preview or the wall took its place meanwhile
  } else if (where === "viewer") {
    if (!$("#viewer").hidden && S.viewerReq === req) setViewerImage(url);
    else URL.revokeObjectURL(url);
  } else {
    endInlinePreview(inline, url);
  }
}

/* on the frame (wide screens): the image gets a ribbon, a bar above says it isn't on the wall */
function startFramePreview(req, label, pinnable, drawing) {
  if (S.preview && S.preview.url) URL.revokeObjectURL(S.preview.url);
  S.preview = { req, label, pinnable, url: null };
  $("#preview-what").textContent = `${label}.`;
  const pin = $("#preview-pin");
  pin.hidden = !pinnable;
  pin.textContent = `Pin ${KIND[req.kind] || req.kind}`;
  $("#preview-bar").hidden = false;
  $("#panel-drawing-text").textContent = drawing;
  $("#panel-drawing").hidden = false;
  $("#panel").classList.add("previewing");
  $("#preview-ribbon").hidden = false;
  $("#current").hidden = true;
  $("#empty").hidden = true;
  if (S.status) renderPlacard(S.status);
}

function setFramePreview(url) {
  S.preview.url = url;
  const img = $("#preview-img");
  img.onload = () => { $("#frame").classList.toggle("portrait", img.naturalHeight > img.naturalWidth); };
  img.src = url;
  img.hidden = false;
  $("#panel-drawing").hidden = true;
}

function endFramePreview() {
  if (!S.preview) return;
  if (S.preview.url) URL.revokeObjectURL(S.preview.url);
  S.preview = null;
  const img = $("#preview-img");
  img.hidden = true;
  img.removeAttribute("src");
  $("#preview-bar").hidden = true;
  $("#panel-drawing").hidden = true;
  $("#preview-ribbon").hidden = true;
  $("#panel").classList.remove("previewing");
  if (S.status) {
    $("#current").hidden = !$("#current").dataset.src;
    renderImage(S.status);
    renderPlacard(S.status);
  }
}

/* inline, under the settings it was drawn with */
function startInlinePreview(box, drawing) {
  const holder = $(".pv-img", box);
  holder.replaceChildren(el("span", { class: "pv-drawing" }, el("span", { class: "spin", "aria-hidden": "true" }), drawing));
  holder.hidden = false;
  $(".pv-cap", box).hidden = true;
}

function endInlinePreview(box, url) {
  const holder = $(".pv-img", box);
  if (box.dataset.url) URL.revokeObjectURL(box.dataset.url);
  delete box.dataset.url;
  if (!url) {
    holder.hidden = true;
    holder.replaceChildren();
    return;
  }
  box.dataset.url = url;
  holder.replaceChildren(el("img", { src: url, alt: "The screen drawn with these settings" }));
  $(".pv-cap", box).hidden = false;
}

/* --- the viewer: the image larger, or a preview being drawn ---------------------------- */

function openViewer(src, opts = {}) {
  const img = $("#viewer-img");
  S.viewerReq = opts.req || null;
  if (S.viewerUrl) { URL.revokeObjectURL(S.viewerUrl); S.viewerUrl = null; }
  if (opts.drawing) {
    $("#viewer-title").textContent = opts.drawing;
    $("#viewer-detail").textContent = `About ${PREVIEW_S} seconds on the frame. This doesn't touch the wall.`;
    $("#viewer-drawing").hidden = false;
    img.hidden = true;
  } else {
    $("#viewer-drawing").hidden = true;
    img.src = src;
    img.hidden = false;
  }
  $("#viewer-caption").textContent = opts.caption || "";
  const pin = $("#viewer-pin");
  pin.hidden = !opts.pinnable;
  if (opts.pinnable) {
    pin.textContent = `Pin ${KIND[opts.pinnable] || opts.pinnable}`;
    pin.onclick = () => { closeViewer(); setMode(opts.pinnable); };
  }
  $("#viewer-bar").hidden = !opts.caption && !opts.pinnable;
  $("#viewer").hidden = false;
}

function setViewerImage(url) {
  S.viewerUrl = url;
  const img = $("#viewer-img");
  img.src = url;
  img.hidden = false;
  $("#viewer-drawing").hidden = true;
}

function closeViewer() {
  $("#viewer").hidden = true;
  S.viewerReq = null;
  if (S.viewerUrl) { URL.revokeObjectURL(S.viewerUrl); S.viewerUrl = null; }
}

function openCurrent() {
  if (S.preview) {
    if (S.preview.url) openViewer(S.preview.url, { caption: `Preview: ${S.preview.label}` });
    return;
  }
  const img = $("#current");
  if (img.hidden) return;
  openViewer(img.src);
}

/* --- photos ------------------------------------------------------------------------- */

async function loadPhotos() {
  const load = ++S.photoLoads;
  try {
    const list = await api("GET", "/api/photos");
    if (load !== S.photoLoads) return;
    S.photos = list;
    renderPhotos();
  } catch (e) {
    toast(e.message, true);
  }
}

function photo(name) { return S.photos.find((p) => p.name === name); }

function badgesFor(p) {
  const out = [];
  if (p.showing) out.push(el("span", { class: "badge b-wall" }, "On the wall"));
  if (p.up_next_position) {
    out.push(el("span", { class: "badge b-queue" }, p.up_next_position === 1 ? "Up next" : `Up next #${p.up_next_position}`));
  } else if (p.next && !p.showing) {
    out.push(el("span", { class: "badge" }, "Next"));
  }
  if (p.hidden) out.push(el("span", { class: "badge b-hidden" }, "Hidden"));
  return out;
}

function makeTile() {
  const img = el("img", { alt: "", decoding: "async", loading: "lazy" });
  const thumb = el("span", { class: "thumb loading" }, img, el("span", { class: "badges" }));
  img.addEventListener("load", () => {
    img.classList.add("loaded");
    thumb.classList.remove("loading");
    setTimeout(() => dropLocalThumb(thumb), 400);  // once the preview has faded in over it
  });
  img.addEventListener("error", () => {
    thumb.classList.remove("loading");
    dropLocalThumb(thumb);
    if (!$(".broken", thumb)) thumb.append(el("span", { class: "broken" }, "Can't draw this photo"));
  });
  const tile = el("button", { class: "tile", type: "button" }, thumb, el("span", { class: "tile-name" }));
  tile.addEventListener("click", () => {
    if (Date.now() - S.dragEnded > 100) openSheet(tile.dataset.name);
  });
  makeMovable(tile);
  return tile;
}

function dropLocalThumb(thumb) {
  if (!thumb.dataset.local) return;
  thumb.style.backgroundImage = "";
  URL.revokeObjectURL(thumb.dataset.local);
  delete thumb.dataset.local;
}

function renderPhotos() {
  if (S.drag) return;  // it would move the tile in hand; drawn when the photo is put down
  const grid = $("#grid");
  const seen = new Set(S.photos.map((p) => p.name));
  for (const [name, tile] of S.tiles) {
    if (!seen.has(name)) { tile.remove(); S.tiles.delete(name); }
  }
  const portrait = !!(S.config && S.config.display.portrait);
  let slot = grid.firstElementChild;  // tiles still uploading stay after this, at the end
  for (const p of S.photos) {
    let tile = S.tiles.get(p.name);
    if (!tile) {
      tile = makeTile();
      S.tiles.set(p.name, tile);
    }
    tile.dataset.name = p.name;
    const local = S.localThumbs.get(p.name);
    if (local) {  // just uploaded: its own picture shows until the dithered one is drawn
      S.localThumbs.delete(p.name);
      const thumb = $(".thumb", tile);
      thumb.dataset.local = local;
      thumb.style.backgroundImage = `url("${local}")`;
    }
    const img = $("img", tile);
    if (img.dataset.src !== p.preview_url) {  // new, or the photo or its settings changed
      img.dataset.src = p.preview_url;
      img.classList.remove("loaded");
      $(".thumb", tile).classList.add("loading");
      const broken = $(".broken", tile);
      if (broken) broken.remove();
      img.src = p.preview_url;
    }
    $(".thumb", tile).classList.toggle("portrait-frame", portrait);
    $(".badges", tile).replaceChildren(...badgesFor(p));
    $(".tile-name", tile).textContent = p.name;
    tile.classList.toggle("hidden-photo", p.hidden);
    tile.setAttribute("aria-label", `${p.name}${p.hidden ? ", hidden" : ""}. Drag, or press shift and an arrow key, to move it`);
    // Keeps the grid in list order. Only what's out of place moves: that would drop its focus.
    if (tile === slot) slot = slot.nextElementSibling;
    else grid.insertBefore(tile, slot);
  }
  const shown = S.photos.filter((p) => !p.hidden).length;
  $("#photo-count").textContent = !S.photos.length ? ""
    : shown === S.photos.length ? `· ${shown}` : `· ${shown} of ${S.photos.length}`;
  $("#photos-empty").hidden = S.photos.length > 0 || S.uploading;
  $("#photos-hint").hidden = S.photos.length < 2;

  const next = S.photos.find((p) => p.next);
  const line = $("#photo-next");
  line.replaceChildren();
  if (next) {
    const st = S.status;
    const step = st && (st.upcoming || []).find((u) => u.kind === "photo");
    const when = step ? ` ${dayTime(step.at)}` : "";
    line.append(`Next photo${when}: `, el("b", {}, next.name), next.up_next_position ? " (Up next)" : " (in turn)");
  }
  if (S.sheet) fillSheet();
}

/* Moving a photo in the grid, which is the order the frame shows them in. A mouse drags it
   straight away; a finger holds it first, since a swipe has to scroll the page; shift and the
   arrow keys move it too. The neighbours are what move in the DOM, never the tile itself, which
   holds the pointer capture and the keyboard focus (the same reason as rotationGrip's). */

function photoTiles() { return $$(".tile[data-name]", $("#grid")); }

/* Move `tile` to where `over` is now, sliding the tiles in between out of its way. */
function moveTile(tile, over) {
  const tiles = photoTiles();
  const from = tiles.indexOf(tile), to = tiles.indexOf(over);
  const moving = from < to ? tiles.slice(from + 1, to + 1) : tiles.slice(to, from);
  const was = moving.map((t) => [t.offsetLeft, t.offsetTop]);
  if (from < to) for (const t of moving) tile.before(t);
  else for (const t of [...moving].reverse()) tile.after(t);
  if (CALM.matches) return;
  moving.forEach((t, i) => {
    const dx = was[i][0] - t.offsetLeft, dy = was[i][1] - t.offsetTop;
    if (dx || dy) t.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }], { duration: 170, easing: "ease-out" });
  });
}

/* The grid's order is the order now: S.photos follows it at once and the frame is told. */
function adoptOrder() {
  const names = photoTiles().map((t) => t.dataset.name);
  const byName = new Map(S.photos.map((p) => [p.name, p]));
  S.photos = names.map((n) => byName.get(n)).filter(Boolean);
  S.photoLoads++;  // a list already on its way has the old order
  return names;
}

async function saveOrder() {
  const names = adoptOrder();
  try {
    await api("PUT", "/api/photos/order", { order: names });
  } catch (e) {
    toast(`The new order wasn't saved: ${e.message}`, true);
  }
  await loadPhotos();  // back to the frame's order if that failed; the Next badge follows
  poll();
}

function pickUp(tile, start, pointer) {
  const grid = $("#grid");
  const box = tile.getBoundingClientRect();
  const grab = { x: start.clientX - box.left, y: start.clientY - box.top };
  const before = photoTiles().map((t) => t.dataset.name).join("/");
  let at = pointer, moved = true, frame = 0;

  const place = () => {
    const g = grid.getBoundingClientRect();
    const x = at.clientX - g.left, y = at.clientY - g.top;
    // Offsets ignore transforms, so a tile sliding away can't be hit again and bounce back.
    const over = photoTiles().find((t) => t !== tile && x >= t.offsetLeft && x < t.offsetLeft + t.offsetWidth
      && y >= t.offsetTop && y < t.offsetTop + t.offsetHeight);
    if (over) moveTile(tile, over);
    tile.style.transform = `translate(${x - grab.x - tile.offsetLeft}px, ${y - grab.y - tile.offsetTop}px) scale(1.04)`;
  };
  const step = () => {
    const y = at.clientY, h = window.innerHeight;
    const speed = y < EDGE_PX ? y - EDGE_PX : y > h - EDGE_PX ? y - (h - EDGE_PX) : 0;
    if (speed) window.scrollBy(0, speed * 0.3);
    if (speed || moved) place();
    moved = false;
    frame = requestAnimationFrame(step);
  };
  const follow = (e) => { at = e; moved = true; };
  const finish = (keep) => {
    cancelAnimationFrame(frame);
    window.removeEventListener("pointermove", follow);
    window.removeEventListener("pointerup", put);
    window.removeEventListener("pointercancel", cancel);
    window.removeEventListener("keydown", escape, true);
    const last = tile.style.transform;
    tile.style.transform = "";
    tile.classList.remove("dragging");
    document.body.classList.remove("reordering");
    S.drag = null;
    S.dragEnded = Date.now();
    if (last && !CALM.matches) tile.animate([{ transform: last }, { transform: "none" }], { duration: 150, easing: "ease-out" });
    if (keep && photoTiles().map((t) => t.dataset.name).join("/") !== before) saveOrder();
    else renderPhotos();  // anything that came in meanwhile, and the old order after a cancel
  };
  const put = () => finish(true);
  const cancel = () => finish(false);
  const escape = (e) => { if (e.key === "Escape") { e.stopPropagation(); cancel(); } };

  S.drag = { tile };
  tile.classList.add("dragging");
  document.body.classList.add("reordering");
  try { tile.setPointerCapture(start.pointerId); } catch (e) { /* the pointer has gone; pointerup ends it */ }
  window.addEventListener("pointermove", follow);
  window.addEventListener("pointerup", put);
  window.addEventListener("pointercancel", cancel);
  window.addEventListener("keydown", escape, true);
  if (start.pointerType !== "mouse" && navigator.vibrate) navigator.vibrate(8);
  frame = requestAnimationFrame(step);
}

function makeMovable(tile) {
  tile.addEventListener("dragstart", (ev) => ev.preventDefault());
  tile.addEventListener("contextmenu", (ev) => { if (S.drag) ev.preventDefault(); });  // a long press
  tile.addEventListener("keydown", (ev) => {
    const key = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: "up", ArrowDown: "down" }[ev.key];
    if (!ev.shiftKey || !key) return;
    ev.preventDefault();
    const tiles = photoTiles();
    const columns = getComputedStyle($("#grid")).gridTemplateColumns.split(" ").length;
    const by = key === "up" ? -columns : key === "down" ? columns : key;
    const to = Math.min(Math.max(tiles.indexOf(tile) + by, 0), tiles.length - 1);
    if (tiles[to] === tile) return;
    moveTile(tile, tiles[to]);
    adoptOrder();  // so a redraw before the save below doesn't put it back
    clearTimeout(S.orderTimer);
    S.orderTimer = setTimeout(saveOrder, 600);  // once the arrow keys have stopped
  });
  tile.addEventListener("pointerdown", (start) => {
    if (start.button !== 0 || S.drag) return;
    const mouse = start.pointerType === "mouse";
    const forget = () => {
      clearTimeout(timer);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", forget);
      window.removeEventListener("pointercancel", forget);
    };
    let latest = start;
    const timer = mouse ? 0 : setTimeout(() => { forget(); pickUp(tile, start, latest); }, LIFT_MS);
    const move = (e) => {
      latest = e;
      if (Math.hypot(e.clientX - start.clientX, e.clientY - start.clientY) < DRAG_PX) return;
      forget();  // a finger moving before the hold is up is a swipe, and the page scrolls
      if (mouse) pickUp(tile, start, e);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", forget);
    window.addEventListener("pointercancel", forget);
  });
}

/* the sheet: one photo and what you can do with it */

function openSheet(name) {
  S.sheet = name;
  S.confirmDelete = false;
  fillSheet();
  $("#sheet").hidden = false;
}

function closeSheet() {
  $("#sheet").hidden = true;
  S.sheet = null;
}

function fillSheet() {
  const p = photo(S.sheet);
  if (!p) { closeSheet(); return; }
  const img = $("#sheet-img");
  if (img.dataset.src !== p.preview_url) { img.dataset.src = p.preview_url; img.src = p.preview_url; }
  img.alt = `${p.name} as the frame will show it`;
  $("#sheet-name").textContent = p.name;
  $("#sheet-meta").textContent = p.size >= 1e6 ? `${(p.size / 1e6).toFixed(1)} MB` : `${Math.round(p.size / 1e3)} KB`;
  const minutes = S.config ? S.config.photos.show_now_minutes : null;
  $('[data-act="show-now"]').textContent = minutes ? `Show now · ${minutes} min` : "Show now";
  const q = $('[data-act="up-next"]');
  q.disabled = p.hidden && !p.up_next_position;
  q.textContent = q.disabled ? "Unhide it to queue it"
    : p.up_next_position ? `Take off Up next (#${p.up_next_position})` : "Add to Up next";
  $('[data-act="hide"]').textContent = p.hidden ? "Unhide" : "Hide";
  const del = $('[data-act="delete"]');
  del.textContent = S.confirmDelete ? "Tap to confirm" : "Delete";
  del.classList.toggle("confirm", S.confirmDelete);
}

async function photoAction(act) {
  const p = photo(S.sheet);
  if (!p) return;
  const path = `/api/photos/${encodeURIComponent(p.name)}`;
  if (act === "delete" && !S.confirmDelete) {  // two taps: it goes to the trash on the Pi
    S.confirmDelete = true;
    fillSheet();
    clearTimeout(S.confirmTimer);
    S.confirmTimer = setTimeout(() => { S.confirmDelete = false; if (S.sheet) fillSheet(); }, 4000);
    return;
  }
  const buttons = $$(".sheet-actions button");
  buttons.forEach((b) => { b.disabled = true; });
  try {
    let res;
    if (act === "show-now") res = await api("POST", `${path}/show-now`);
    else if (act === "up-next") res = await api(p.up_next_position ? "DELETE" : "POST", `${path}/up-next`);
    else if (act === "hide") res = await api("POST", `${path}/hide`, { hidden: !p.hidden });
    else if (act === "delete") res = await api("DELETE", path);
    toast(res.message);
    if (act === "show-now" || act === "delete") {
      closeSheet();
      if (act === "show-now") window.scrollTo({ top: 0, behavior: "smooth" });
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
    S.confirmDelete = false;
    await loadPhotos();
    poll();
  }
}

/* uploads: the browser makes each photo a JPEG of at most 1600 px (the Pi can't read HEIC) */

async function toJpeg(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    try {
      await img.decode();
    } catch (e) {
      throw new Error("this browser can't open it (Safari can read iPhone HEIC photos)");
    }
    let w = img.naturalWidth, h = img.naturalHeight;  // already turned upright
    const scale = Math.min(1, MAX_SIDE / Math.max(w, h));
    const tw = Math.round(w * scale), th = Math.round(h * scale);
    // Halve in steps first: one big downscale aliases badly in some browsers.
    let src = img;
    while (w / 2 > tw * 1.5) {
      w = Math.round(w / 2); h = Math.round(h / 2);
      const c = el("canvas", { width: String(w), height: String(h) });
      c.getContext("2d").drawImage(src, 0, 0, w, h);
      src = c;
    }
    const canvas = el("canvas", { width: String(tw), height: String(th) });
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";  // transparent PNGs get white, like the mat
    ctx.fillRect(0, 0, tw, th);
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(src, 0, 0, tw, th);
    return await new Promise((ok, fail) => canvas.toBlob(
      (b) => (b ? ok(b) : fail(new Error("couldn't convert it"))), "image/jpeg", 0.9));
  } finally {
    URL.revokeObjectURL(url);
  }
}

function jpegName(name) {
  const dot = name.lastIndexOf(".");
  return (dot > 0 ? name.slice(0, dot) : name || "photo") + ".jpg";
}

/* A tile at the end of the grid for a file on its way; the real one takes over once it's saved. */
function uploadTile(file) {
  const state = el("span", { class: "upload-state" }, "Waiting");
  return el("div", { class: "tile uploading" }, el("span", { class: "thumb loading" }, state),
    el("span", { class: "tile-name" }, file.name));
}

function flashTile(name) {
  const tile = S.tiles.get(name);
  if (!tile || CALM.matches) return;
  tile.classList.add("landed");
  $(".thumb", tile).addEventListener("animationend", () => tile.classList.remove("landed"), { once: true });
}

/* Open the Photos box and add these to what's uploading: their tiles appear at once. */
function uploadFiles(files) {
  const list = [...files];
  if (!list.length) return;
  $("#photos-card").open = true;
  const items = list.map((file) => ({ file, tile: uploadTile(file) }));
  $("#grid").append(...items.map((i) => i.tile));
  $("#photos-empty").hidden = true;
  items[0].tile.scrollIntoView({ block: "center", behavior: CALM.matches ? "auto" : "smooth" });
  S.uploads.push(...items);
  if (!S.uploading) runUploads();
}

async function runUploads() {
  S.uploading = true;
  const box = $("#upload-progress");
  const bar = $("span", box), label = $("b", box);
  box.hidden = false;
  const saved = [], dupes = [], failed = [];
  let done = 0;
  while (S.uploads.length) {
    const { file: f, tile } = S.uploads.shift();
    const total = done + S.uploads.length + 1;
    const step = total > 1 ? `${done + 1} of ${total}: ` : "";
    const state = $(".upload-state", tile);
    bar.style.width = `${((done + 0.3) / total) * 100}%`;
    let picture = null;  // the JPEG as a URL: the real tile takes it over if the photo is new
    try {
      label.textContent = `Preparing ${step}${f.name}`;
      state.textContent = "Preparing";
      const jpeg = await toJpeg(f);
      picture = URL.createObjectURL(jpeg);
      $(".thumb", tile).prepend(el("img", { alt: "", class: "loaded", src: picture }));
      label.textContent = `Uploading ${step}${f.name}`;
      state.textContent = "Uploading";
      const form = new FormData();
      form.append("photo", jpeg, jpegName(f.name));
      const res = await api("POST", "/api/photos", form);
      for (const s of res.saved) {
        (s.duplicate ? dupes : saved).push(s.name);
        if (!s.duplicate && picture) { S.localThumbs.set(s.name, picture); picture = null; }
      }
      tile.remove();
      await loadPhotos();
      for (const s of res.saved) flashTile(s.name);
    } catch (e) {
      tile.remove();
      failed.push(`${f.name}: ${e.message}`);
    }
    if (picture) URL.revokeObjectURL(picture);
    done++;
  }
  box.hidden = true;
  S.uploading = false;
  if (failed.length) toast(`Not uploaded: ${failed.join("; ")}`, true);
  else if (saved.length) toast(saved.length === 1 ? `Added ${saved[0]}` : `Added ${saved.length} photos`);
  else if (dupes.length) toast(dupes.length === 1 ? `${dupes[0]} is already there` : "Those are already there");
  await loadPhotos();
}

/* Files dragged over the page: anywhere is a place to drop them. The overlay covers the window
   as soon as they're over it, so everything after that happens on the overlay, and a dragleave
   there means the pointer has left the window (which counting enters and leaves over the page's
   elements gets wrong). A browser only says in part what's coming while it's over the page:
   Safari may give no list and some files have no type, so only a known type that isn't a photo
   refuses. */

function isPhotoFile(file) {
  return file.type.startsWith("image/") || /\.(heic|heif)$/i.test(file.name);
}

function initDrop() {
  const box = $("#drop");
  let fromPage = false;  // a picture dragged from this page also says it has Files
  const hasFiles = (e) => !fromPage && e.dataTransfer && [...e.dataTransfer.types].includes("Files");
  const describe = (e) => {
    const files = [...(e.dataTransfer.items || [])].filter((i) => i.kind === "file");
    const maybe = (i) => !i.type || i.type === "application/x-moz-file" || i.type.startsWith("image/");  // Firefox's word for "a file"
    const refuse = files.length > 0 && !files.some(maybe);
    const photos = files.filter(maybe).length;
    box.classList.toggle("refuse", refuse);
    $("#drop-title").textContent = refuse ? (files.length > 1 ? "Those aren't photos" : "That isn't a photo")
      : photos > 1 ? `Drop to upload ${photos} photos` : photos === 1 ? "Drop to upload this photo" : "Drop to upload";
    $("#drop-detail").textContent = refuse ? "Upload a JPEG, PNG, WebP or iPhone photo"
      : photos === 1 ? "It goes at the end of your photos" : "They'll go at the end of your photos";
    e.dataTransfer.dropEffect = refuse ? "none" : "copy";
  };
  document.addEventListener("dragstart", (e) => { fromPage = !e.defaultPrevented; });
  document.addEventListener("dragend", () => { fromPage = false; });
  for (const type of ["dragenter", "dragover"]) {
    window.addEventListener(type, (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();  // without this a missed drop would open the file in place of the page
      describe(e);
      box.hidden = false;
    });
  }
  // Gone from the window: either it left the overlay, or it left so fast that it never got there
  // and left whatever was under it for nowhere.
  window.addEventListener("dragleave", (e) => { if (e.target === box || !e.relatedTarget) box.hidden = true; });
  box.addEventListener("pointermove", () => { box.hidden = true; });  // no pointer events during a drag: it's over
  window.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    box.hidden = true;
    const files = [...e.dataTransfer.files];
    if (!files.length) { toast("That didn't arrive as a file. Try dragging it from a folder.", true); return; }
    const others = files.filter((f) => !isPhotoFile(f)).map((f) => f.name);
    if (others.length) toast(`Not a photo: ${others.join(", ")}`, true);
    if (others.length === files.length) return;
    closeViewer();
    closeSheet();
    uploadFiles(files.filter(isPhotoFile));
  });
}

/* --- the Schedule, each screen's box and Frame: forms generated from the config --------- */

// Each box has its own form and Save bar, shown while that box has edits. Save and Cancel go for
// that box alone: the frame takes a whole config, so Save sends this box's edits with the rest as
// last saved, and another box's unsaved edits stay where they are, still unsaved.
const BOXES = ["tides", "sky", "harbor", "photos", "battery"];  // a config section each, drawn in a form of that name
const FORMS = {  // the form's id -> what draws it
  schedule: renderSchedule, tides: () => renderBox("tides"), sky: () => renderBox("sky"),
  harbor: () => renderBox("harbor"), photos: () => renderBox("photos"), battery: () => renderBox("battery"),
  settings: renderSettings,
};

// Settings that more than one screen reads, so they're in the Frame box whatever section they
// live in: the path -> its group there.
const SHARED_FIELDS = { "tides.station": "Data", "tides.datum": "Data", "sky.center_azimuth_deg": "Location" };

// Labels and help for the settings we know. Anything else in the config still gets a
// field, named after its key, so a new setting shows up here without changing this page.
const GROUPS = { location: "Location", display: "Display", weather: "Data", surf: "Data", gauge: "Data" };
const DATUMS = ["MLLW", "MLW", "MTL", "MSL", "MHW", "MHHW", "NAVD", "STND", "CRD", "IGLD", "LWD"];
const FIELDS = {
  "schedule.night_offset_minutes": { label: "Night starts after sunset", unit: "min", neg: true,
    help: "It ends as long before sunrise. Negative starts it before sunset." },
  "schedule.quiet_hours": { label: "Quiet hours", type: "quiet",
    help: "No scheduled changes in this window; what you do here still shows." },
  "photos.folder": { label: "Photo folder", wide: true },
  "photos.fit": { label: "Fit", choices: { auto: "Auto", fill: "Fill (crop)", fit: "Fit (white bars)" },
    help: "Auto fills the frame unless that would crop more than about a third" },
  "photos.hidden": { skip: true },
  "photos.show_now_minutes": { label: "Show now lasts", unit: "min" },
  "harbor.south": { label: "South edge", unit: "° lat", neg: true, help: "33.54 to 33.70. The buoy off the entrance is at 33.5844" },
  "harbor.north": { label: "North edge", unit: "° lat", help: "33.54 to 33.70. The map's width follows from its height" },
  "harbor.center_lon": { label: "Center longitude", unit: "°", neg: true, help: "-118.02 to -117.78. The default is about the middle of the lower bay" },
  "display.portrait": { label: "Hung in portrait" },
  "display.flip": { label: "Upside down", help: "Turns everything 180° (on for this frame)" },
  "weather.enabled": { label: "Weather", help: "Open-Meteo forecast for the location" },
  "surf.enabled": { label: "Surf", help: "Open-Meteo offshore swell model" },
  "tides.station": { label: "NOAA tide station", help: "9410580 is Newport Bay Entrance, by the Wedge. Tides, Sea and Harbor all use it" },
  "tides.datum": { label: "Tide datum", choices: Object.fromEntries(DATUMS.map((d) => [d, d])) },
  "tides.chart_days_behind": { label: "Chart starts", unit: "days back", help: "Up to 7. Fractions work: 0.5 is 12 hours" },
  "tides.chart_days_ahead": { label: "Chart ends", unit: "days ahead", help: "Up to 14" },
  "tides.label_tides": { label: "Which tides get a label",
    choices: { all: "All highs and lows", daily: "One high and one low a day" },
    help: "One a day labels each day's highest high and lowest low" },
  "tides.label_times": { label: "Times on highs and lows" },
  "tides.label_heights": { label: "Heights on highs and lows",
    choices: { extremes: "Highest and lowest", all: "All of them", none: "None" },
    help: "Labels that would overlap are left out, keeping the next tides first" },
  "tides.flip_arrows": { label: "Flip swell and wind arrows",
    help: "On: as seen facing south, so a west wind points left. Off: north is up, so it points right." },
  "gauge.enabled": { label: "Measured water level",
    help: "Draws how far the water is running from the tide table. Off: the table alone." },
  "gauge.station": { label: "Gauge station",
    help: "9410660 is Los Angeles. Newport's own gauge closed in 1994; LA tracked it to about an inch." },
  "sky.center_azimuth_deg": { label: "Window faces", unit: "°", help: "180 is south. Sky and Sea both look this way" },
  "sky.field_of_view_deg": { label: "Width of view", unit: "°" },
  "sky.max_altitude_deg": { label: "Up to", unit: "° high" },
  "sky.magnitude_limit": { label: "Faintest stars", unit: "mag", neg: true, help: "Higher shows more stars" },
  "sky.constellation_lines": { label: "Constellation lines" },
  "sky.labels": { label: "Labels" },
  "sky.day_magnitude_limit": { label: "Faintest stars by day", unit: "mag", neg: true,
    help: "Higher shows more stars, up to the limit above" },
  "sky.day_constellation_lines": { label: "Constellation lines by day",
    help: "While it's light out. Needs Constellation lines on." },
  "sky.sun_arc": { label: "Sun's path and hours", help: "A dashed arc across the sky with today's hours marked" },
  "battery.indicator_volts": { label: "Battery indicator below", unit: "V",
    help: "Running on the battery below this, every screen carries a small battery, with one redraw to put it up. 4.2 V means the whole time it's on the battery." },
  "battery.low_volts": { label: "Settle on a photo below", unit: "V",
    help: "Running on the battery below this, the frame moves to a photo (or keeps the one that's up) and stays there until the power is back, so a photo is what's left on the wall when the cell dies" },
  "battery.capacity_mah": { label: "Pack capacity", unit: "mAh", help: "What the pack is rated at; the time-left guess uses it" },
  "battery.low_power": { label: "Low power on battery",
    help: "On the battery, turn Wi-Fi and this dashboard off after the delay below. USB power brings them back. Wi-Fi comes on briefly every few hours for tides and weather." },
  "battery.low_power_after_minutes": { label: "Low power after", unit: "min on battery" },
  "battery.data_every_hours": { label: "Data while in low power every", unit: "h", help: "Wi-Fi comes on for about four minutes each time" },
  "location.name": { label: "Place name", wide: true },
  "location.lat": { label: "Latitude", unit: "°", neg: true },
  "location.lon": { label: "Longitude", unit: "°", neg: true },
  "location.timezone": { label: "Time zone", wide: true },
};
for (const key of ["chart_days_behind", "chart_days_ahead", "label_tides", "label_times", "label_heights"]) {
  FIELDS[`harbor.${key}`] = FIELDS[`tides.${key}`];  // the harbor's tide band has the same choices, set apart
}

function humanize(key) {
  const s = key.replace(/_(deg|minutes)$/, "").replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function switchInput(path, checked, kind = "bool") {
  return el("label", { class: "switch" },
    el("input", { type: "checkbox", "data-path": path, "data-kind": kind, checked, "aria-label": path }),
    el("span"));
}

function field(path, meta, label, control, extra = []) {
  return el("div", { class: `field${meta.wide ? " wide" : ""}`, "data-field": path },
    el("span", { class: "field-label" }, label), control,
    meta.help && el("span", { class: "field-help" }, meta.help), ...extra);
}

function fieldFor(path, key, value) {
  const meta = FIELDS[path] || {};
  if (meta.skip || Array.isArray(value)) return null;  // lists (hidden photos) are managed elsewhere
  const label = meta.label || humanize(key);
  if (meta.type === "quiet") {
    const on = value !== null && typeof value === "object";
    const times = el("span", { class: "quiet-times", hidden: !on },
      el("input", { type: "time", "data-path": `${path}.start`, "data-kind": "time", value: on ? value.start : "00:00" }),
      el("span", { class: "unit" }, "to"),
      el("input", { type: "time", "data-path": `${path}.end`, "data-kind": "time", value: on ? value.end : "05:00" }));
    const sw = switchInput(path, on, "quiet");
    $("input", sw).addEventListener("change", (e) => { times.hidden = !e.target.checked; });
    return field(path, meta, label, el("span", { class: "control" }, sw),
      [el("span", { class: "control", style: "grid-column: 1 / -1; justify-content: flex-start" }, times)]);
  }
  let control;
  if (typeof value === "boolean") {
    control = switchInput(path, value);
  } else if (meta.choices) {
    const choices = { ...meta.choices };
    if (!(value in choices)) choices[value] = String(value);
    control = el("select", { "data-path": path, "data-kind": "string" },
      Object.entries(choices).map(([v, text]) => el("option", { value: v, selected: v === value }, text)));
  } else if (typeof value === "number") {
    // Text, not type=number: a browser blanks what it can't parse, and the frame should see
    // what was typed. iPhone's decimal pad has no minus key, so not for settings that can be negative.
    control = el("input", { type: "text", inputmode: meta.neg ? null : "decimal", "data-path": path, "data-kind": "number",
      value: String(value), class: "num", autocomplete: "off" });
  } else {
    control = el("input", { type: "text", "data-path": path, "data-kind": "string",
      value: value === null ? "" : String(value), autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
  }
  return field(path, meta, label, el("span", { class: "control" }, control, meta.unit && el("span", { class: "unit" }, meta.unit)));
}

function fieldsFor(obj, prefix) {
  const out = [];
  for (const [key, value] of Object.entries(obj)) {
    const path = `${prefix}.${key}`;
    const meta = FIELDS[path] || {};
    if (value && typeof value === "object" && !Array.isArray(value) && !meta.type) out.push(...fieldsFor(value, path));
    else {
      const f = fieldFor(path, key, value);
      if (f) out.push(f);
    }
  }
  return out;
}

/* the schedule: a rotation of screens by day and one by night */

function dayWhen(dn) {
  return dn && dn.sunrise && dn.sunset ? `sunrise ${time(dn.sunrise)} to sunset ${time(dn.sunset)}` : "sunrise to sunset";
}

/* The handle that reorders a rotation's rows: drag it, or press the up and down arrow keys.
   The neighbours are what move in the DOM, never the row itself: taking the row out, even for
   an instant, would drop the pointer capture and the keyboard focus held by its handle. */
function rotationGrip(row) {
  const grip = el("button", { type: "button", class: "grip",
    "aria-label": "Move this screen: drag, or press the up and down arrow keys" }, icon("grip"));
  grip.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowUp" && ev.key !== "ArrowDown") return;
    ev.preventDefault();
    const up = ev.key === "ArrowUp";
    const other = up ? row.previousElementSibling : row.nextElementSibling;
    if (!other) return;
    if (up) row.after(other); else row.before(other);
    scheduleChanged();
  });
  grip.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    const rows = row.parentElement;
    const grab = ev.clientY - row.getBoundingClientRect().top;
    const middle = (r) => { const b = r.getBoundingClientRect(); return b.top + b.height / 2; };
    const move = (e) => {
      for (;;) {
        const prev = row.previousElementSibling, next = row.nextElementSibling;
        if (prev && e.clientY < middle(prev)) row.after(prev);
        else if (next && e.clientY > middle(next)) row.before(next);
        else break;
      }
      // The row follows the pointer, kept within its list.
      row.style.transform = "";
      const box = row.getBoundingClientRect(), list = rows.getBoundingClientRect();
      const top = Math.min(Math.max(e.clientY - grab, list.top), list.bottom - box.height);
      row.style.transform = `translateY(${top - box.top}px)`;
    };
    const end = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("lostpointercapture", end);
      row.classList.remove("dragging");
      row.style.transform = "";
      scheduleChanged();
    };
    grip.setPointerCapture(ev.pointerId);
    grip.addEventListener("pointermove", move);
    grip.addEventListener("lostpointercapture", end);  // after a release or a cancel
    row.classList.add("dragging");
  });
  return grip;
}

function rotationRow(period, step) {
  const row = el("div", { class: "rot", "data-rot": period });
  const select = el("select", { "aria-label": "Screen" },
    Object.entries(SCREENS).map(([v, text]) => el("option", { value: v, selected: v === step.screen }, text)));
  const minutes = el("input", { type: "text", inputmode: "numeric", value: String(step.minutes),
    "aria-label": "Minutes", autocomplete: "off" });
  const remove = el("button", { type: "button", class: "rm", "aria-label": "Remove this screen" }, icon("remove"));
  remove.addEventListener("click", () => {
    const rows = row.parentElement;
    if ($$(".rot", rows).length > 1) { row.remove(); scheduleChanged(); }
  });
  row.append(rotationGrip(row), select, minutes, el("span", { class: "unit" }, "min"), remove);
  return row;
}

function rotationList(period, steps, title, when) {
  const box = el("div", { class: "rotation", "data-field": `schedule.${period}` },
    el("p", { class: "rot-head" }, el("span", {}, title),
      el("span", { class: "when", "data-when": period }, when)));
  const rows = el("div", { class: "rot-rows" }, ...steps.map((s) => rotationRow(period, s)));
  const add = el("button", { type: "button", class: "link" }, "+ Add a screen");
  add.addEventListener("click", () => {
    const used = new Set($$(".rot select", rows).map((s) => s.value));
    const screen = Object.keys(SCREENS).find((s) => !used.has(s)) || Object.keys(SCREENS)[0];
    rows.append(rotationRow(period, { screen, minutes: 30 }));
    scheduleChanged();
  });
  box.append(rows, el("p", { class: "add" }, add));
  return box;
}

function scheduleChanged() { updateDirty(); updateBudget(); }

function scheduleFields(sched) {
  return [
    rotationList("day", sched.day || [], "By day", dayWhen(S.status && S.status.day_night)),
    rotationList("night", sched.night || [], "At night", "sunset to sunrise"),
  ];
}

/* About how many refreshes an hour a rotation makes: one a step. */
function refreshesPerHour(steps) {
  const cycle = steps.reduce((a, s) => a + (Number(s.minutes) > 0 ? Number(s.minutes) : 0), 0);
  return cycle > 0 ? (steps.length * 60) / cycle : 0;
}

function updateBudget() {
  const box = $("#budget");
  if (!box || !S.config) return;
  const sched = collect().schedule;
  const fmt = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));
  const day = refreshesPerHour(sched.day || []);
  const night = refreshesPerHour(sched.night || []);
  box.replaceChildren(el("b", {}, `About ${fmt(day)} refreshes an hour by day, ${fmt(night)} at night.`),
    " Each one is a 30-second flash on the wall.");
}

/* --- a preview in a screen's box: any time of any day, and for the sea, any kind of day ---- */

// When: a clock time (HH:MM), or a moment that moves through the year (astro/moments.py).
const WHEN = { now: "Now", sunrise: "Sunrise", "12:00": "Noon", "15:00": "3 pm", sunset: "Sunset", dusk: "Dusk",
  "21:00": "9 pm", moon: "Moon", pick: "Pick a time" };
const NAMED_MOMENTS = ["sunrise", "sunset", "dusk", "moon"];
const WHEN_FOR = {
  tides: ["now", "sunrise", "12:00", "sunset", "21:00", "pick"],
  sky: ["now", "sunrise", "12:00", "sunset", "dusk", "21:00", "moon", "pick"],
  sea: ["now", "sunrise", "12:00", "15:00", "sunset", "dusk", "21:00", "moon", "pick"],
  harbor: ["now", "sunrise", "12:00", "sunset", "21:00", "pick"],
};
// The sea's looks: the names are scenes/base.py's WAVE_LOOKS and CLOUD_LOOKS; "" is the forecast.
const WAVE_CHOICES = { "": "Forecast", calm: "Calm", big: "Big swell", windy: "Windy", gale: "Gale" };
const CLOUD_CHOICES = { "": "Forecast", clear: "Clear", partly: "Some clouds", overcast: "Overcast" };

function chipRow(label, choices, picked, onPick) {
  const chips = el("div", { class: "chips", role: "radiogroup", "aria-label": label });
  const mark = (value) => { for (const b of $$("button", chips)) b.setAttribute("aria-checked", b.dataset.value === value ? "true" : "false"); };
  for (const [value, text] of Object.entries(choices)) {
    chips.append(el("button", { type: "button", role: "radio", "data-value": value,
      onclick: () => { mark(value); onPick(value); } }, text));
  }
  mark(picked);
  return el("div", { class: "pv-row" }, el("span", { class: "pv-label" }, label), chips);
}

/* The frame's date as words: today, tomorrow, or Nov 20. */
function dayWords(day) {
  if (day === frameDate(0)) return "today";
  if (day === frameDate(1)) return "tomorrow";
  return new Date(`${day}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/* What a preview has no data for, in words, or "". Sources' `until` is the end of their cache. */
function previewGap(kind, p, day) {
  const srcs = (S.status && S.status.sources) || {};
  const covers = (name) => {
    const until = srcs[name] && srcs[name].until;
    return !!until && day >= frameDate(0) && day <= until.slice(0, 10);
  };
  if (p.when === "now") return "";
  if (kind === "sea") {
    return p.waves || covers("surf") ? "" : "There's no swell forecast for that day, so the sea comes out flat. Pick a wave look.";
  }
  return covers("weather") ? "" : "There's no forecast for that day, so its weather is left out.";
}

/* The controls and the button; `box` also holds the drawn image on a phone. Drawn once, so a
   choice here outlasts a Save. */
function renderPreviewBlock(box) {
  const kind = box.dataset.pv;
  const p = { when: "now", waves: "", clouds: "" };
  const dayInput = el("input", { type: "date", "aria-label": "Day", class: "pv-input" });
  const timeInput = el("input", { type: "time", "aria-label": "Time", class: "pv-input", value: "18:00" });
  const dayRow = el("label", { class: "pv-row", hidden: true }, el("span", { class: "pv-label" }, "Day"), dayInput);
  const timeRow = el("label", { class: "pv-row", hidden: true }, el("span", { class: "pv-label" }, "Time"), timeInput);
  const note = el("p", { class: "pv-note", hidden: true });
  const day = () => dayInput.value || frameDate(0);
  const update = () => {
    if (p.when !== "now" && !dayInput.value) dayInput.value = frameDate(0);
    dayRow.hidden = p.when === "now";
    timeRow.hidden = p.when !== "pick";
    const gap = S.status ? previewGap(kind, p, day()) : "";
    note.textContent = gap;
    note.hidden = !gap;
  };
  dayInput.addEventListener("change", update);
  const whenChoices = Object.fromEntries(WHEN_FOR[kind].map((w) => [w, WHEN[w]]));
  box.append(chipRow("When", whenChoices, p.when, (v) => { p.when = v; update(); }), dayRow, timeRow);
  if (kind === "sea") {
    box.append(chipRow("Waves", WAVE_CHOICES, p.waves, (v) => { p.waves = v; update(); }),
      chipRow("Clouds", CLOUD_CHOICES, p.clouds, (v) => { p.clouds = v; update(); }));
  }
  const holder = el("button", { type: "button", class: "pv-img", hidden: true, "aria-label": "Show it larger" });
  const button = el("button", { type: "button", class: "button wide" }, icon("eye"), "Preview");
  let lastLabel = "";
  holder.addEventListener("click", () => {
    if (box.dataset.url) openViewer(box.dataset.url, { caption: `Preview: ${lastLabel}` });
  });
  button.addEventListener("click", () => {
    const req = { kind, config: collect() };
    const words = [KIND[kind].toLowerCase()];
    if (p.when === "now") {
      words.push("now");
    } else if (p.when === "pick") {
      const clock = timeInput.value || "12:00";
      req.at = `${day()}T${clock}`;
      words.push(dayWords(day()), "at", time(`${day()}T${clock}:00${zoneOffset()}`));
    } else if (NAMED_MOMENTS.includes(p.when)) {
      req.at = `${day()}@${p.when}`;
      words.push(dayWords(day()), p.when === "moon" ? "with the Moon out" : `at ${p.when}`);
    } else {
      req.at = `${day()}T${p.when}`;
      words.push(dayWords(day()), "at", WHEN[p.when].toLowerCase());
    }
    if (p.waves) { req.waves = p.waves; words.push(`· ${WAVE_CHOICES[p.waves].toLowerCase()}`); }
    if (p.clouds) { req.clouds = p.clouds; words.push(`· ${CLOUD_CHOICES[p.clouds].toLowerCase()}`); }
    if (Object.keys(FORMS).some((card) => !$(`#${card}-bar`).hidden)) words.push("with your unsaved settings");
    lastLabel = words.join(" ");
    showPreview(req, { label: lastLabel, inline: box });
  });
  box.append(note, button, holder,
    el("p", { class: "pv-cap", hidden: true }, "Drawn with the settings here, saved or not. The wall isn't touched. Tap to enlarge."));
}

/* The frame's UTC offset now, to read a clock time at the frame as a moment. */
function zoneOffset() {
  const m = S.status && S.status.server_time && S.status.server_time.match(/([+-]\d{2}:\d{2}|Z)$/);
  return m ? m[1] : "Z";
}

function renderSchedule() {
  const { day, night, ...rest } = S.config.schedule;
  $("#schedule").replaceChildren(...scheduleFields({ day, night }), ...fieldsFor(rest, "schedule"),
    el("p", { class: "budget", id: "budget" }));
}

const BATTERY_ORDER = ["capacity_mah", "indicator_volts", "low_volts", "low_power", "low_power_after_minutes", "data_every_hours"];

/* A screen's box: its config section, without the settings other screens read too. */
function renderBox(section) {
  const fields = fieldsFor(S.config[section] || {}, section).filter((f) => !(f.dataset.field in SHARED_FIELDS));
  const notes = [];
  if (section === "tides") {
    notes.push(el("p", { class: "group-note box-note" }, "The tide station and datum are under Frame, in Data: Sea and Harbor use them too."));
  } else if (section === "sky") {
    notes.push(el("p", { class: "group-note box-note" }, "Which way the window faces is under Frame, in Location: Sea looks that way too."));
    if (S.status && S.status.screens && S.status.screens.sky === false) {
      notes.push(el("p", { class: "group-note box-note" }, "The sky chart isn't built yet; these apply once it is."));
    }
  } else if (section === "photos") {
    notes.push(el("p", { class: "group-note" }, "Hide or unhide photos in the grid below."));
  }
  if (section === "battery") {  // the pack, its voltages from the higher down, then low power
    const place = (f) => {
      const i = BATTERY_ORDER.indexOf(f.dataset.field.split(".")[1]);
      return i < 0 ? BATTERY_ORDER.length : i;
    };
    fields.sort((a, b) => place(a) - place(b));
  }
  if (section === "harbor") {
    const chart = fields.findIndex((f) => f.dataset.field === "harbor.chart_days_behind");
    if (chart >= 0) fields.splice(chart, 0, el("h3", { class: "sub-head" }, "Tide chart"));
    fields.unshift(el("h3", { class: "sub-head" }, "Map"));
  }
  $(`#${section}`).replaceChildren(...notes, ...fields);
}

/* The Frame box: everything without a box of its own, in groups. */
function renderSettings() {
  const form = $("#settings");
  const open = new Set($$("details.group[open]", form).map((d) => d.dataset.group));
  const groups = new Map();
  const add = (title, fields) => {
    if (!groups.has(title)) groups.set(title, []);
    groups.get(title).push(...fields);
  };
  for (const [section, value] of Object.entries(S.config)) {
    if (section === "schedule" || BOXES.includes(section)) continue;  // these have boxes of their own
    add(GROUPS[section] || humanize(section), value && typeof value === "object" ? fieldsFor(value, section) : []);
  }
  for (const [path, title] of Object.entries(SHARED_FIELDS)) {
    const [section, key] = path.split(".");
    if (S.config[section] && key in S.config[section]) add(title, [fieldFor(path, key, S.config[section][key])]);
  }
  const order = Object.values(GROUPS);
  const rank = (title) => (order.includes(title) ? order.indexOf(title) : order.length);
  const sorted = [...groups].sort((a, b) => rank(a[0]) - rank(b[0]));
  form.replaceChildren(...sorted.map(([title, fields]) => el("details", { class: "group", "data-group": title, open: open.has(title) },
    el("summary", {}, title), ...fields)));
}

/* One line on each closed box, from the settings as saved. */
function renderSummaries() {
  const c = S.config;
  const span = (t) => `${t.chart_days_behind} day${t.chart_days_behind === 1 ? "" : "s"} back, ${t.chart_days_ahead} ahead`;
  const lines = {
    schedule: `${(c.schedule.day || []).length} screens by day, ${(c.schedule.night || []).length} at night`,
    tides: `Chart ${span(c.tides)}`,
    sky: `${c.sky.field_of_view_deg}° wide, stars to magnitude ${c.sky.magnitude_limit}`,
    sea: "Swell, wind and tide",
    harbor: `Tide chart ${span(c.harbor)}`,
  };
  for (const [box, line] of Object.entries(lines)) $(`[data-sum="${box}"]`).textContent = line;
}

/* Draw these boxes' forms from the saved config (all of them, or just the one saved or Canceled). */
function renderForms(cards = Object.keys(FORMS)) {
  for (const card of cards) FORMS[card]();
  const text = cardTexts(collect());
  for (const card of cards) S.configText[card] = text[card];
  renderSummaries();
  scheduleChanged();
}

/* The boxes are closed unless this browser left them open, and Coming up open unless it left that closed. */
const OPEN_BOXES = "frame-open-boxes";
const COMING_CLOSED = "frame-coming-closed";

function initBoxes() {
  const coming = $("#coming");
  try { if (localStorage.getItem(COMING_CLOSED) === "1") coming.open = false; } catch (e) { /* stays open */ }
  coming.addEventListener("toggle", () => {
    try { localStorage.setItem(COMING_CLOSED, coming.open ? "0" : "1"); } catch (e) { /* a private window: it starts open next time */ }
  });
  let open = [];
  try { open = JSON.parse(localStorage.getItem(OPEN_BOXES)) || []; } catch (e) { open = []; }
  const boxes = $$("details.box");
  for (const box of boxes) {
    box.open = open.includes(box.dataset.box);
    box.addEventListener("toggle", () => {
      try {
        localStorage.setItem(OPEN_BOXES, JSON.stringify(boxes.filter((b) => b.open).map((b) => b.dataset.box)));
      } catch (e) { /* a private window: they just start closed next time */ }
    });
  }
  for (const box of $$(".pv[data-pv]")) renderPreviewBlock(box);
}

function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  for (const k of keys.slice(0, -1)) {
    if (!o[k] || typeof o[k] !== "object") o[k] = {};
    o = o[k];
  }
  o[keys[keys.length - 1]] = value;
}

const allForms = (inside) => Object.keys(FORMS).map((card) => `#${card} ${inside}`).join(", ");

/* The config as the forms have it now: the loaded config with every field's value set. With
   `only`, just that form's fields: the rest stay as last saved. */
function collect(only = null) {
  const data = JSON.parse(JSON.stringify(S.config));
  const inputs = $$(only ? `#${only} [data-path]` : allForms("[data-path]"));
  const quietOff = new Set();
  for (const input of inputs.filter((i) => i.dataset.kind === "quiet")) {
    if (input.checked) setPath(data, input.dataset.path, {});
    else { setPath(data, input.dataset.path, null); quietOff.add(input.dataset.path); }
  }
  for (const input of inputs) {
    const { path, kind } = input.dataset;
    if (kind === "quiet") continue;
    if (kind === "time") {
      if (!quietOff.has(path.slice(0, path.lastIndexOf(".")))) setPath(data, path, input.value);
    } else if (kind === "bool") {
      setPath(data, path, input.checked);
    } else if (kind === "number") {
      const raw = input.value.trim();
      const n = Number(raw);
      // Anything that isn't a number goes as typed, so the frame can say what's wrong with it.
      setPath(data, path, raw === "" || Number.isNaN(n) ? raw : n);
    } else {
      setPath(data, path, input.value);
    }
  }
  for (const period of ["day", "night"]) {  // the rotations
    const rows = $$(`#schedule .rot[data-rot="${period}"]`);
    if (!rows.length || (only && only !== "schedule")) continue;
    if (!data.schedule || typeof data.schedule !== "object") data.schedule = {};
    data.schedule[period] = rows.map((r) => {
      const raw = $("input", r).value.trim();
      const n = Number(raw);
      return { screen: $("select", r).value, minutes: raw === "" || Number.isNaN(n) ? raw : n };
    });
  }
  return data;
}

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o === null || o === undefined ? o : o[k]), obj);
}

/* A config as text, one piece per form: the schedule whole, and each other form's fields. */
function cardTexts(data) {
  const out = {};
  for (const card of Object.keys(FORMS)) {
    out[card] = card === "schedule" ? JSON.stringify(data.schedule)
      : JSON.stringify($$(`#${card} [data-path]`).map((i) => getPath(data, i.dataset.path)));
  }
  return out;
}

function updateDirty() {
  const text = S.config ? cardTexts(collect()) : S.configText;
  const dirty = Object.keys(FORMS).filter((card) => text[card] !== S.configText[card]);
  for (const card of Object.keys(FORMS)) {
    $(`#${card}-bar`).hidden = !dirty.includes(card);
    const mark = $(`[data-dirty="${card}"]`);  // on the box's closed line
    if (mark) mark.hidden = !dirty.includes(card);
  }
  document.body.classList.toggle("unsaved", dirty.length > 0);  // lifts toasts above the Save bar
}

function clearFieldErrors() {
  for (const f of $$(".field.bad, .rot.bad")) f.classList.remove("bad");
  for (const e of $$(".field-error")) e.remove();
}

function showFieldErrors(errors) {
  clearFieldErrors();
  const fields = $$(allForms("[data-field]"));
  const readable = [];
  let first = null;
  for (const err of errors) {
    const i = err.indexOf(": ");
    const path = i > 0 ? err.slice(0, i) : "";
    const msg = i > 0 ? err.slice(i + 2) : err;
    // the field with this path, or the nearest one above it (quiet_hours.start -> quiet_hours,
    // schedule.day[1].minutes -> the day rotation, and its row)
    const match = fields.filter((f) => path === f.dataset.field || path.startsWith(`${f.dataset.field}.`)
        || path.startsWith(`${f.dataset.field}[`))
      .sort((a, b) => b.dataset.field.length - a.dataset.field.length)[0];
    if (!match) {  // no field for it: at the top of the card it belongs to
      const loose = el("p", { class: "field-error" }, err);
      $(path.startsWith("schedule") ? "#schedule" : "#settings").prepend(loose);
      for (let d = loose.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
      readable.push(err);
      first = first || loose;
      continue;
    }
    match.classList.add("bad");
    const row = path.match(/^schedule\.(day|night)\[(\d+)\]/);
    if (row) {
      const r = $$(".rot", match)[Number(row[2])];
      if (r) r.classList.add("bad");
    }
    match.append(el("span", { class: "field-error" }, msg.charAt(0).toUpperCase() + msg.slice(1)));
    const label = $(".field-label", match) || $(".rot-head span", match);
    readable.push(`${label ? label.textContent : path}: ${msg}`);
    // Open whatever it's closed inside: a box, a group in it. The schedule isn't in either.
    for (let d = match.closest("details"); d; d = d.parentElement.closest("details")) d.open = true;
    first = first || match;
  }
  if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  return readable;
}

async function loadConfig() {
  try {
    S.config = await api("GET", "/api/config");
    renderForms();
    if (S.status) { renderMode(S.status); renderBatteryNow(S.status); }
    if (S.photos.length) renderPhotos();
  } catch (e) {
    toast(e.message, true);
  }
}

async function saveCard(card, event) {
  event.preventDefault();
  const saves = $$(".save-bar .primary");
  saves.forEach((b) => { b.disabled = true; });
  try {
    const data = collect(card);
    // Photos are hidden from the grid; keep the list as it is now, not as the form loaded it.
    const latest = await api("GET", "/api/config");
    data.photos.hidden = latest.photos.hidden;
    const res = await api("PUT", "/api/config", data);
    clearFieldErrors();
    S.config = res.config;
    renderForms([card]);  // another box's unsaved edits stay as they are
    S.batteryKey = null;  // the time-left guess and the chart's lines follow the settings
    loadPhotos();
    await poll();
    // The wall keeps its drawing until the schedule's next redraw: say when, offer it now.
    const nc = S.status && S.status.next_change;
    const when = nc && nc.at ? ` The wall redraws at ${dayTime(nc.at)}.` : "";
    toast(`Saved.${when}`, false, { label: "Redraw now", run: redrawNow });
  } catch (e) {
    if (e.data && Array.isArray(e.data.errors)) {
      const problems = showFieldErrors(e.data.errors);
      toast(problems.length === 1 ? `Not saved. ${problems[0]}` : `Not saved: ${problems.length} problems, marked in red`, true);
    } else {
      toast(e.message, true);
    }
  } finally {
    saves.forEach((b) => { b.disabled = false; });
  }
}

function renderConfigError(st) {
  const box = $("#config-error");
  box.hidden = !st.config_error;
  box.textContent = st.config_error
    ? `The settings file has a problem, so the frame is using the defaults shown here. Saving replaces it. (${st.config_error})`
    : "";
}

/* --- health ------------------------------------------------------------------------- */

function duration(s) {
  if (s === null || s === undefined) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} d ${h} h` : h ? `${h} h ${m} min` : `${m} min`;
}

function row(dt, dd, detail, bad) {
  return [el("dt", {}, dt),
    el("dd", {}, dd, detail ? el("span", { class: `detail${bad ? " bad" : ""}` }, detail) : null)];
}

/* --- the Battery box: the reading, low power, the last 5 days charted, the settings ----- */

function hoursText(h) { return duration(Math.round(h * 3600)); }

/* The word for the battery's state, for the box's closed line and its first row. */
function batteryWord(b) {
  return b.state === "charging" ? "Charging" : b.state === "discharging" ? (b.low ? "Low, on battery" : "On battery")
    : b.percent >= 90 ? "Charged" : "Not charging";
}

function batteryRow(b) {
  if (!b) return row("Now", "—", "No reading");
  if (b.state === "none") return row("Now", "None fitted", `${b.volts.toFixed(2)} V on the connector`);
  const bits = [`${b.volts.toFixed(2)} V`, `${b.milliamps > 0 ? "+" : ""}${Math.round(b.milliamps)} mA`];
  if (b.percent !== null) bits.push(`about ${b.percent} %`);
  return row("Now", batteryWord(b), bits.join(" · "), b.low);
}

function powerRow(p) {
  const enabled = S.config && S.config.battery && S.config.battery.low_power;
  if (!p || p.mode !== "low") return row("Low power", enabled ? "Armed" : "Off",
    enabled ? `Wi-Fi and the dashboard go off after ${S.config.battery.low_power_after_minutes} min on battery` : null);
  return row("Low power", `On since ${dayTime(p.since)}`, p.wifi === "on" ? "Wi-Fi on for a data window" : "Wi-Fi off");
}

/* Time left on the closed line: short, since it's a guess and the line is narrow. */
function hoursShort(h) {
  return h >= 10 ? `${Math.round(h)} h` : h >= 1 ? `${h.toFixed(1)} h` : `${Math.max(1, Math.round(h * 60))} min`;
}

/* The box's closed line says the state at a glance. */
function batteryLine(st) {
  const b = st.battery, p = st.power;
  if (p && p.mode === "low") return p.wifi === "on" ? "Low power: Wi-Fi on for data" : "Low power: Wi-Fi off";
  if (!b) return "No battery reading";
  if (b.state === "none") return "No battery fitted";
  if (b.state !== "discharging") return `${batteryWord(b)}, ${b.volts.toFixed(2)} V`;
  const est = S.battery && S.battery.cycle && S.battery.cycle.ongoing ? S.battery.estimate : null;
  const bits = [b.low ? "Low" : "On battery"];  // "~", not "about": this has to fit a phone's closed line
  bits.push(b.percent === null ? `${b.volts.toFixed(2)} V` : `${b.percent} %`);
  if (est) bits.push(`~${hoursShort(est.hours_left)} left`);
  return bits.join(", ");
}

function renderBatteryNow(st) {
  const line = $('[data-sum="battery"]');
  line.textContent = batteryLine(st);
  line.classList.toggle("bad", !!(st.battery && st.battery.low));
  $("#battery-now").replaceChildren(...batteryRow(st.battery), ...powerRow(st.power));
}

/* The chart and the guess change with each reading (the tick's, once a minute). */
function maybeLoadBattery(st) {
  const b = st.battery;
  const key = b ? `${b.read_at}|${b.state}` : "none";
  if (key === S.batteryKey) return;
  S.batteryKey = key;
  loadBattery();
}

async function loadBattery() {
  try {
    S.battery = await api("GET", "/api/battery");
  } catch (e) {
    $("#battery-summary").textContent = e.message;
    return;
  }
  renderBattery(S.battery, S.status);
  renderBatteryNow(S.status);  // the closed line carries the time-left guess
}

const READOUT_HINT = "Drag along the chart to read a time";

function renderBattery(d, st) {
  if (S.batteryHeld) { S.batteryStale = true; return; }  // don't pull the chart from under a finger
  const c = d.cycle, est = d.estimate, b = st.battery, h = d.history;
  const summary = $("#battery-summary"), note = $("#battery-note"), readout = $("#battery-readout");
  const readings = h && h.readings.length > 0;
  readout.classList.remove("live");
  readout.textContent = readings ? READOUT_HINT : "No battery readings in the last 5 days.";
  $("#battery-chart").replaceChildren(...(readings ? [batteryChart(h, est, d.indicator_volts, d.low_volts, st.server_time)] : []));
  if (!c) {
    summary.textContent = readings ? "It hasn't run on the battery in the last 5 days." : "";
    note.textContent = "";
    return;
  }
  const first = c.readings[0], last = c.readings[c.readings.length - 1];
  const ended = c.ended_at || last.t;
  const runSeconds = (new Date(ended) - new Date(c.started_at)) / 1000;
  if (c.ongoing) {
    summary.textContent = `On the battery since ${dayTime(c.started_at)} (${duration(runSeconds)})` + (est ? `, drawing about ${est.avg_ma} mA` : "");
    note.textContent = est
      ? `About ${hoursText(est.hours_to_low)} until it settles on a last photo and ${hoursText(est.hours_left)} until it runs out, `
        + `at this draw and a ${d.capacity_mah.toLocaleString()} mAh pack. ${c.used_mah.toLocaleString()} mAh used so far. A guess from the voltage; it runs short rather than long.`
      : `${c.used_mah.toLocaleString()} mAh used so far. Not enough readings yet for a time-left guess.`;
  } else {
    summary.textContent = `Last run on the battery: ${dayTime(c.started_at)} to ${dayTime(ended)} (${duration(runSeconds)})`
      + ` · ${first.volts.toFixed(2)} → ${last.volts.toFixed(2)} V`;
    note.textContent = `${c.used_mah.toLocaleString()} mAh used of a ${d.capacity_mah.toLocaleString()} mAh pack. `
      + (b && b.state === "charging" ? "Charging now." : "");
  }
}

/* What a reading says above the chart: "Sat 6:13 am · 4.02 V · −160 mA, on battery". */
function readingText(r) {
  const ma = r.milliamps, sign = ma > 0 ? "+" : ma < 0 ? "−" : "";
  const amps = Math.abs(ma) >= 1000 ? `${sign}${(Math.abs(ma) / 1000).toFixed(2)} A` : `${sign}${Math.round(Math.abs(ma))} mA`;
  const word = { charging: "charging", discharging: "on battery", idle: "on USB, not charging" }[r.state] || r.state;
  return `${dayTime(r.t)} · ${r.volts.toFixed(2)} V · ${amps}, ${word}`;
}

/* The voltage over the last days (`history` from /api/battery), tinted where the cell charged
   or ran the frame, with a line at each midnight, the indicator's and the last photo's lines,
   and during a run on the battery a dashed guess out to where it runs out. Under it, a strip
   of the current on the same days: filled green above zero where it charged, amber below where
   the frame ran on the cell (the Owner chose it over a second line on the volts' plot, which
   crossed the voltage and its labels, 2026-11-09). Dragging along it (or hovering with a mouse)
   reads the nearest point into the line above the chart, where a finger doesn't hide it.
   Drawn at the card's own width (initBatteryChart redraws it when that changes), so its
   labels stay a readable size on a phone; no library. */
function batteryChart(h, est, indicatorVolts, lowVolts, nowIso) {
  const width = $("#battery-chart").clientWidth;
  const W = width >= 240 ? Math.round(width) : 640, L = 30, R = 10, T = 10, B = 24;
  const vB = 156;                    // the bottom of the volts' plot
  const aT = vB + 14, aB = aT + 64;  // the current's strip, under it
  const H = aB + B;
  const ms = (iso) => new Date(iso).getTime();
  const rs = h.readings.map((r) => ({ ...r, ms: ms(r.t) }));
  const now = ms(nowIso), lastMs = rs[rs.length - 1].ms;
  let t0 = Math.min(h.spans.length ? ms(h.spans[0].start) : rs[0].ms, rs[0].ms);
  let t1 = Math.max(now, lastMs);
  if (t1 - t0 < 60 * 60 * 1000) t0 = t1 - 60 * 60 * 1000;  // at least an hour across
  // The guess gets at most a quarter of the chart, so a big pack's days ahead don't squeeze
  // the days behind; past the edge it's cut off, and the note says when it runs out.
  if (est) t1 = Math.min(Math.max(t1, ms(est.runs_out_at)), t1 + (t1 - t0) / 3);
  const vLo = 3.0, vHi = 4.3;
  const x = (t) => L + (t - t0) / (t1 - t0) * (W - L - R);
  const y = (v) => T + (vHi - v) / (vHi - vLo) * (vB - T);
  // Amps from -0.5 to 1 at least, widened to the readings in half amps (charging peaks near 2.1).
  const mas = rs.map((r) => r.milliamps);
  const aLo = Math.min(-0.5, Math.floor(Math.min(...mas) / 500) / 2);
  const aHi = Math.max(1, Math.ceil(Math.max(...mas) / 500) / 2);
  const ya = (ma) => aT + (aHi - ma / 1000) / (aHi - aLo) * (aB - aT);
  const svgEl = (tag, attrs, ...kids) => {
    const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    for (const k of kids) n.append(k);
    return n;
  };
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });

  // Where it charged and where it ran on the cell, at the log's full detail.
  for (const s of h.spans) {
    const cls = { charging: "span-charging", discharging: "span-battery" }[s.state];
    if (!cls) continue;
    const xa = x(ms(s.start)), xb = x(ms(s.end));
    svg.append(svgEl("rect", { x: xa.toFixed(1), y: T, width: Math.max(1, xb - xa).toFixed(1), height: vB - T, class: cls }));
  }
  for (const v of [4.2, 4.0, 3.8, 3.6, 3.4, 3.2]) {
    svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid" }));
    svg.append(svgEl("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end" }, v.toFixed(1)));
  }

  // Midnights at the frame, found in quarter hours (every time zone's offset is a multiple),
  // and each day's name under its part of the chart when there's room for it.
  const dayOf = new Intl.DateTimeFormat("en-US", { timeZone: tz(), year: "numeric", month: "numeric", day: "numeric" });
  const weekday = new Intl.DateTimeFormat("en-US", { timeZone: tz(), weekday: "short" });
  const QUARTER = 15 * 60 * 1000;
  const edges = [t0];
  let day = dayOf.format(t0);
  for (let t = Math.ceil(t0 / QUARTER) * QUARTER; t <= t1; t += QUARTER) {
    const d = dayOf.format(t);
    if (d !== day) { edges.push(t); day = d; }
  }
  edges.push(t1);
  for (let i = 0; i < edges.length - 1; i++) {
    if (i > 0) svg.append(svgEl("line", { x1: x(edges[i]), x2: x(edges[i]), y1: T, y2: aB, class: "day" }));
    const a = x(edges[i]), b = x(edges[i + 1]);
    if (b - a >= 24) svg.append(svgEl("text", { x: ((a + b) / 2).toFixed(1), y: aB + 16, "text-anchor": "middle" }, weekday.format(edges[i])));
  }

  svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(indicatorVolts), y2: y(indicatorVolts), class: "indicator" }));
  svg.append(svgEl("text", { x: W - R, y: y(indicatorVolts) - 4, "text-anchor": "end", class: "indicator-label" }, "indicator"));
  svg.append(svgEl("line", { x1: L, x2: W - R, y1: y(lowVolts), y2: y(lowVolts), class: "low" }));
  const lowBelow = Math.abs(indicatorVolts - lowVolts) < 0.15 || lowVolts >= 4.1;  // keep the two labels apart
  svg.append(svgEl("text", { x: W - R, y: y(lowVolts) + (lowBelow ? 13 : -4), "text-anchor": "end", class: "low-label" }, "last photo"));

  // The voltage, broken where the log has a gap (the Pi off, or no reading).
  const runs = [];
  for (const r of rs) {
    if (!runs.length || r.after_gap) runs.push([]);
    runs[runs.length - 1].push(`${x(r.ms).toFixed(1)},${y(r.volts).toFixed(1)}`);
  }
  for (const pts of runs) {
    if (pts.length === 1) pts.push(pts[0]);  // a lone point still shows, as a dot (round caps)
    svg.append(svgEl("polyline", { points: pts.join(" "), class: "volts" }));
  }
  if (est) {
    const last = rs[rs.length - 1];
    const lowX = x(ms(est.low_at)), outX = x(ms(est.runs_out_at));
    svg.append(svgEl("clipPath", { id: "battery-plot" }, svgEl("rect", { x: L, y: T, width: W - L - R, height: vB - T })));
    svg.append(svgEl("polyline", { points: `${x(last.ms)},${y(last.volts)} ${lowX},${y(lowVolts)} ${outX},${y(vLo)}`,
      class: "guess", "clip-path": "url(#battery-plot)" }));
    svg.append(svgEl("text", { x: Math.min(outX, W - R) - 2, y: vB - 6, "text-anchor": "end" }, `out ${dayTime(est.runs_out_at)}`));
    svg.append(svgEl("line", { x1: x(now), x2: x(now), y1: T, y2: aB, class: "now" }));
  }

  // The current: one area per stretch of readings, drawn twice, clipped above zero (charging,
  // green) and below it (on the battery, amber), with a thin edge so a small draw still shows.
  for (let a = 0; a <= aHi; a++) {
    svg.append(svgEl("line", { x1: L, x2: W - R, y1: ya(a * 1000), y2: ya(a * 1000), class: a === 0 ? "zero" : "grid" }));
    svg.append(svgEl("text", { x: L - 6, y: ya(a * 1000) + 4, "text-anchor": "end" }, a === 0 ? "0" : `${a} A`));
  }
  const z = ya(0);
  svg.append(svgEl("clipPath", { id: "battery-amps-in" }, svgEl("rect", { x: L, y: aT, width: W - L - R, height: z - aT })));
  svg.append(svgEl("clipPath", { id: "battery-amps-out" }, svgEl("rect", { x: L, y: z, width: W - L - R, height: aB - z })));
  const aRuns = [];
  for (const r of rs) {
    if (!aRuns.length || r.after_gap) aRuns.push([]);
    aRuns[aRuns.length - 1].push([x(r.ms).toFixed(1), ya(r.milliamps).toFixed(1)]);
  }
  for (const run of aRuns) {
    if (run.length === 1) run.push(run[0]);
    const pts = run.map((p) => p.join(",")).join(" ");
    const area = `${pts} ${run[run.length - 1][0]},${z} ${run[0][0]},${z}`;
    svg.append(svgEl("polygon", { points: area, class: "amps-in", "clip-path": "url(#battery-amps-in)" }));
    svg.append(svgEl("polygon", { points: area, class: "amps-out", "clip-path": "url(#battery-amps-out)" }));
    svg.append(svgEl("polyline", { points: pts, class: "amps-edge" }));
  }

  // Reading it: the nearest point to the finger or mouse, or "no readings" over a gap.
  const cursor = svgEl("line", { y1: T, y2: aB, class: "cursor", visibility: "hidden" });
  const dot = svgEl("circle", { r: 4, class: "cursor-dot", visibility: "hidden" });
  const dotA = svgEl("circle", { r: 4, class: "cursor-dot amps-dot", visibility: "hidden" });
  svg.append(cursor, dot, dotA);
  const readout = () => $("#battery-readout");
  const show = (ev) => {
    const box = svg.getBoundingClientRect();
    const sx = Math.min(Math.max((ev.clientX - box.left) / box.width * W, L), W - R);
    const t = Math.min(t0 + (sx - L) / (W - L - R) * (t1 - t0), lastMs);  // the guess's part reads the latest
    let lo = 0, hi = rs.length - 1;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (rs[mid].ms < t) lo = mid + 1; else hi = mid; }
    const r = lo > 0 && t - rs[lo - 1].ms < rs[lo].ms - t ? rs[lo - 1] : rs[lo];
    const gap = Math.abs(r.ms - t) > h.step_s * 1000;
    const cx = gap ? x(t) : x(r.ms);
    cursor.setAttribute("x1", cx); cursor.setAttribute("x2", cx);
    cursor.setAttribute("visibility", "visible");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", y(r.volts));
    dot.setAttribute("visibility", gap ? "hidden" : "visible");
    dotA.setAttribute("cx", cx); dotA.setAttribute("cy", ya(r.milliamps));
    dotA.setAttribute("visibility", gap ? "hidden" : "visible");
    dotA.classList.toggle("out", r.milliamps < 0);
    const out = readout();
    out.classList.add("live");
    out.textContent = gap ? `${dayTime(new Date(t).toISOString())} · no readings` : readingText(r);
  };
  const hide = () => {
    cursor.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    dotA.setAttribute("visibility", "hidden");
    const out = readout();
    out.classList.remove("live");
    out.textContent = READOUT_HINT;
  };
  svg.addEventListener("pointerdown", (ev) => {
    S.batteryHeld = true;
    try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* the pointer is already gone */ }
    show(ev);
  });
  svg.addEventListener("pointermove", (ev) => { if (S.batteryHeld || ev.pointerType === "mouse") show(ev); });
  const release = (ev) => {
    S.batteryHeld = false;
    if (ev.pointerType !== "mouse" || ev.type === "pointercancel") hide();  // a mouse still hovers
    if (S.batteryStale) { S.batteryStale = false; renderBattery(S.battery, S.status); }
  };
  svg.addEventListener("pointerup", release);
  svg.addEventListener("pointercancel", release);
  svg.addEventListener("pointerleave", () => { if (!S.batteryHeld) hide(); });
  return svg;
}

/* The chart is drawn for the card's width: draw it again when the box opens or the width changes. */
function initBatteryChart() {
  let drawnFor = 0;
  new ResizeObserver(() => {
    const w = Math.round($("#battery-chart").clientWidth);
    if (!w || Math.abs(w - drawnFor) < 8 || !S.battery || !S.status) return;
    drawnFor = w;
    renderBattery(S.battery, S.status);
  }).observe($("#battery-chart"));
}

function signalWord(dbm) {
  if (dbm >= -55) return "strong";
  if (dbm >= -67) return "good";
  if (dbm >= -75) return "weak";
  return "very weak";
}

/* One row per interface, without the tailnet's: that has its own row. A long IPv6 address
   only stands in when an interface has no IPv4 one. */
function addressRows(network) {
  if (!network) return [row("Address", "—")];
  const lan = network.filter((n) => !n.interface.startsWith("tailscale"));
  if (!lan.length) return [row("Address", "None")];
  return lan.map((n) => row("Address", n.ipv4.length ? n.ipv4.join(", ") : n.ipv6[0], n.interface));
}

function tailscaleRow(t) {
  if (!t) return row("Tailscale", "—");
  if (t.state !== "connected") {
    return row("Tailscale", { stopped: "Not running", logged_out: "Logged out", not_installed: "Not installed" }[t.state]);
  }
  const offline = t.online === false;
  return row("Tailscale", t.ips.length ? `Connected · ${t.ips[0]}` : "Connected",
    [t.dns_name, offline ? "offline" : null].filter(Boolean).join(" · "), offline);
}

/* Health's closed line: what the dot at the top of the page means, in words. */
function healthLine(problems) {
  const errors = problems.filter((p) => p.level === "error").length;
  if (errors) return errors === 1 ? "1 problem" : `${errors} problems`;
  return problems.length ? `${problems.length} to look at` : "All good";
}

function renderHealth(st) {
  const line = $('[data-sum="health"]');
  line.textContent = healthLine(st.problems);
  line.classList.toggle("bad", st.problems.some((p) => p.level === "error"));
  $("#problems").replaceChildren(...st.problems.map((p) => el("div", { class: `problem ${p.level}` }, p.text)));
  const h = st.health;
  const w = h.wifi, pw = h.power, m = h.memory, clock = h.clock.synced;
  const rows = [
    row("Wi-Fi", w ? (w.ssid || "Not connected") : "—", w && w.connected ? `${w.signal_dbm} dBm · ${signalWord(w.signal_dbm)}` : null),
    ...addressRows(h.network),
    tailscaleRow(h.tailscale),
    row("Clock", clock === true ? "Set from the internet" : clock === false ? "Not set yet" : "—", null, clock === false),
    row("Power", !pw ? "—" : pw.undervoltage_now ? "Under-voltage now" : "OK",
      pw && pw.undervoltage_since_boot ? "Under-voltage since boot: try a stronger USB supply" : null,
      pw && pw.undervoltage_since_boot),
    row("Last tick", st.updated_at ? ago(st.updated_at) : "never", st.updated_at ? dayTime(st.updated_at) : null),
  ];
  const le = st.last_error;
  const source = le && le.where.endsWith("-fetch") ? st.sources[le.where.slice(0, -6)] : null;
  if (le && !(source && !source.error)) {  // a fetch that has worked again since isn't news
    rows.push(row("Last error", `${st.last_error.where} · ${ago(st.last_error.at)}`, st.last_error.message, true));
  }
  rows.push(
    row("Up", duration(h.uptime_s)),
    row("CPU", h.cpu_temp_c === null ? "—" : `${h.cpu_temp_c} °C`, h.load_1m === null ? null : `load ${h.load_1m}`),
    row("Memory", `Dashboard ${Math.round(m.server_mb)} MB`,
      m.available_mb ? `${Math.round(m.available_mb)} MB free of ${Math.round(m.total_mb)}` : null),
  );
  if (h.disk) rows.push(row("Storage", `${(h.disk.free_mb / 1024).toFixed(1)} GB free`));
  rows.push(row("Host", h.hostname));
  $("#health").replaceChildren(...rows.flat());
  renderName();

  const names = { tides: "Tides", weather: "Weather", surf: "Surf", gauge: "Water level" };
  $("#sources").replaceChildren(...Object.entries(st.sources).flatMap(([key, src]) => {
    const main = !src.enabled ? "off" : src.fetched_at ? `fetched ${ago(src.fetched_at)}` : "no data";
    const bits = [];
    if (key === "tides" && src.until) {
      bits.push(`through ${new Date(src.until).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: tz() })}`);
    }
    if (src.level === "warn" || src.level === "error") bits.push(src.note);
    return [el("dt", {}, el("span", { class: `lvl ${src.level}` }), names[key] || key),
      el("dd", {}, main, bits.length ? el("span", { class: `detail${src.level === "error" ? " bad" : ""}` }, bits.join(" · ")) : null)];
  }));
}

function frameDate(offsetDays = 0) {
  const [y, mo, d] = S.status.server_time.slice(0, 10).split("-").map(Number);  // the frame's date
  return new Date(Date.UTC(y, mo - 1, d + offsetDays)).toISOString().slice(0, 10);
}

/* --- the frame's name ---------------------------------------------------------------- */

const NAME_RE = /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$/;  // frame_name.check has the last word

/* This page's address with the name swapped, or null when the page isn't reached by name
   (by IP address, or on the Mac): then there's nowhere to move to. */
function addressWithName(oldName, newName) {
  const labels = location.hostname.split(".");
  if (labels[0].toLowerCase() !== oldName.toLowerCase()) return null;
  labels[0] = newName;
  return `${location.protocol}//${labels.join(".")}${location.port ? `:${location.port}` : ""}/`;
}

function renderName() {
  const input = $("#name-input");
  const current = S.status ? S.status.health.hostname : "";
  if (!input.dataset.touched && document.activeElement !== input) input.value = current;
  const typed = input.value.trim();
  const ok = NAME_RE.test(typed) && !/^[0-9]+$/.test(typed) && typed.toLowerCase() !== "localhost";
  const line = $("#name-address");
  line.classList.toggle("bad", !!typed && !ok);
  line.textContent = !typed ? "Type a name"
    : ok ? `http://${typed}.local`
    : "Use only letters, numbers and dashes, with no dash at the start or end";
  $("#name-save").disabled = !ok || typed === current;
}

/* mDNS and MagicDNS take a moment to answer for a new name: go once the new address does. */
async function moveTo(url) {
  for (let i = 0; i < 20; i++) {
    try {
      await fetch(`${url}api/status`, { mode: "no-cors", cache: "no-store" });
      location.replace(url);
      return;
    } catch (e) {
      await new Promise((done) => setTimeout(done, 1500));
    }
  }
  toast(`Renamed. If this page stops working, open ${url}`, false);
}

async function renameFrame(ev) {
  ev.preventDefault();
  const input = $("#name-input");
  $("#name-save").disabled = true;
  try {
    const res = await api("POST", "/api/name", { name: input.value.trim() });
    delete input.dataset.touched;
    S.status.health.hostname = res.name;
    input.blur();
    renderName();
    toast(res.warnings.length ? `${res.message}, but: ${res.warnings.join("; ")}` : res.message, res.warnings.length > 0);
    const url = addressWithName(res.previous, res.name);
    if (url) moveTo(url);
  } catch (e) {
    toast(e.message, true);
    renderName();
  }
}

/* --- Wi-Fi networks ------------------------------------------------------------------ */

/* The saved networks the frame joins on its own (web/wifi.py). Loaded each time the group
   opens; the add and remove answers carry the new list. Remove takes two taps, like the
   photo sheet's Delete. The password goes to the frame once and is cleared from the form. */
const WIFI = { data: null, confirm: null, timer: null };

async function loadWifi() {
  try {
    WIFI.data = await api("GET", "/api/wifi");
    renderWifi();
  } catch (e) {
    $("#wifi-list").replaceChildren(el("li", { class: "wifi-row" }, el("span", { class: "small muted" }, e.message)));
  }
}

function renderWifi() {
  const d = WIFI.data;
  if (!d) return;
  $("#wifi-list").replaceChildren(...d.networks.map((n) => {
    const arm = WIFI.confirm === n.name;
    const bits = [n.hidden ? "hidden network" : null, n.connected ? null : "joins when in range"].filter(Boolean);
    return el("li", { class: "wifi-row" },
      el("span", { class: "wifi-text" },
        el("span", { class: "wifi-name" }, n.ssid),
        bits.length ? el("span", { class: "small muted" }, bits.join(" · ")) : null),
      n.connected ? el("span", { class: "wifi-on" }, "Connected")
        : el("button", { type: "button", class: `button small danger${arm ? " confirm" : ""}`,
          "aria-label": `Remove ${n.ssid}`, onclick: () => removeWifi(n.name) }, arm ? "Tap to confirm" : "Remove"));
  }));
  $("#wifi-empty").hidden = d.networks.length > 0;
  checkWifiForm();
}

/* Mirrors wifi.py's checks, so the button only lights for something the frame will take;
   the frame has the last word. */
function checkWifiForm() {
  const ssid = $("#wifi-ssid").value.trim();
  const pw = $("#wifi-password").value;
  let why = "";
  if (ssid && new TextEncoder().encode(ssid).length > 32) why = "A network name is at most 32 characters (fewer with accents or emoji)";
  else if (pw && (pw.length < 8 || pw.length > 63)) why = "A Wi-Fi password is 8 to 63 characters; leave it empty for an open network";
  else if (pw && !/^[ -~]*$/.test(pw)) why = "Use only letters, numbers, spaces and punctuation in the password";
  else if (ssid && WIFI.data && WIFI.data.networks.some((n) => n.ssid === ssid || n.name === ssid)) why = `The frame already knows ${ssid}`;
  const line = $("#wifi-check");
  line.textContent = why || (ssid && !pw ? "No password: an open network" : "");
  line.classList.toggle("bad", !!why);
  $("#wifi-save").disabled = !ssid || !!why;
}

async function addWifi(ev) {
  ev.preventDefault();
  const body = { ssid: $("#wifi-ssid").value.trim(), password: $("#wifi-password").value, hidden: $("#wifi-hidden").checked };
  $("#wifi-save").disabled = true;
  try {
    const res = await api("POST", "/api/wifi", body);
    $("#wifi-form").reset();  // the password leaves the page
    if ($("#wifi-password").type !== "password") toggleWifiPassword();
    WIFI.data = res;
    renderWifi();
    toast(res.message);
  } catch (e) {
    toast(e.message, true);
    checkWifiForm();
  }
}

async function removeWifi(name) {
  if (WIFI.confirm !== name) {  // two taps, as the photo sheet's Delete
    WIFI.confirm = name;
    renderWifi();
    clearTimeout(WIFI.timer);
    WIFI.timer = setTimeout(() => { WIFI.confirm = null; renderWifi(); }, 4000);
    return;
  }
  clearTimeout(WIFI.timer);
  WIFI.confirm = null;
  try {
    const res = await api("DELETE", `/api/wifi/${encodeURIComponent(name)}`);
    WIFI.data = res;
    renderWifi();
    toast(res.message);
  } catch (e) {
    toast(e.message, true);
    loadWifi();
  }
}

function toggleWifiPassword() {
  const input = $("#wifi-password"), button = $("#wifi-show");
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  button.textContent = show ? "Hide" : "Show";
  button.setAttribute("aria-pressed", String(show));
}

/* Wired here rather than in init(), so this section stands on its own. */
async function saveWifiHelp(ev) {
  ev.preventDefault();
  $("#wifi-help-save").disabled = true;
  try {
    const latest = await api("GET", "/api/config");
    latest.wifi = { help: $("#wifi-help").value.trim() };
    const res = await api("PUT", "/api/config", latest);
    S.config = res.config;
    toast("Saved.");
  } catch (e) {
    toast(e.message, true);
  } finally {
    $("#wifi-help-save").disabled = false;
  }
}

function initWifi() {
  const tool = $("#wifi-tool");
  tool.addEventListener("toggle", () => {
    if (!tool.open) return;
    loadWifi();
    if (S.config && S.config.wifi) $("#wifi-help").value = S.config.wifi.help;
  });
  $("#wifi-help-form").addEventListener("submit", saveWifiHelp);
  $("#wifi-form").addEventListener("submit", addWifi);
  $("#wifi-form").addEventListener("input", checkWifiForm);
  $("#wifi-show").addEventListener("click", toggleWifiPassword);
}

initWifi();

/* --- wiring ------------------------------------------------------------------------- */

function init() {
  for (const s of $$(".icon[data-icon]")) s.replaceWith(icon(s.dataset.icon));
  $("#redraw").prepend(icon("redraw"));
  for (const b of $$("#modes [data-kind]")) b.addEventListener("click", () => setMode(b.dataset.kind));
  for (const b of $$("#modes .eye")) {
    b.addEventListener("click", () => showPreview({ kind: b.dataset.preview },
      { label: `${KIND[b.dataset.preview].toLowerCase()} as it would look now`, pinnable: true }));
  }
  for (const c of $$("#untils button")) c.addEventListener("click", () => pickUntil(c));
  $("#redraw").addEventListener("click", redrawNow);
  $("#frame").addEventListener("click", openCurrent);
  $("#preview-back").addEventListener("click", endFramePreview);
  $("#preview-pin").addEventListener("click", () => {
    const kind = S.preview && S.preview.req.kind;
    endFramePreview();
    if (kind) setMode(kind);
  });
  $("#viewer").addEventListener("click", (e) => { if (!e.target.closest("#viewer-bar")) closeViewer(); });
  $("#upload").addEventListener("change", (e) => {
    uploadFiles(e.target.files);
    e.target.value = "";  // so choosing the same file again still counts as a change
  });
  $("#grid").addEventListener("touchmove", (e) => { if (S.drag) e.preventDefault(); }, { passive: false });  // hold still while one is dragged
  initDrop();
  for (const b of $$(".sheet-actions [data-act]")) b.addEventListener("click", () => photoAction(b.dataset.act));
  for (const c of $$("#sheet [data-close]")) c.addEventListener("click", closeSheet);
  for (const card of Object.keys(FORMS)) {
    const form = $(`#${card}`);
    form.addEventListener("submit", (e) => saveCard(card, e));
    form.addEventListener("input", scheduleChanged);
    form.addEventListener("change", scheduleChanged);
    $(`#${card}-cancel`).addEventListener("click", () => renderForms([card]));
  }
  $("#name-form").addEventListener("submit", renameFrame);
  $("#name-input").addEventListener("input", (ev) => { ev.target.dataset.touched = "1"; renderName(); });
  $("#card-show").addEventListener("click", () => {
    if (S.status.mode.kind === "card") setMode("auto");
    else setMode("card", "1h");
  });
  $("#card-preview").addEventListener("click", () => showPreview({ kind: "card" }, { label: "the color test card" }));
  for (const [button, box] of [["#health-dot", "#health-card"], ["#help-open", "#help-card"]]) {
    $(button).addEventListener("click", () => {
      $(box).open = true;
      $(box).scrollIntoView({ behavior: "smooth" });
    });
  }
  initBoxes();
  initBatteryChart();
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeViewer(); closeSheet(); }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(S.timer);
    else poll();
  });
  // Count up the refreshing line between polls.
  setInterval(() => { if (S.status && !document.hidden) renderActivity(S.status); }, 1000);
  loadConfig();
  poll();
}

// Wide screens: a left column taller than the window sticks by its bottom instead of its
// top, so the page scrolls down to the Show card and it stays in reach from then on.
function stickLeftColumn() {
  const left = $(".cols > .left");
  const gap = 18;  // the top: style.css gives .left, and what a column that fits keeps
  const stick = () => {
    left.style.top = WIDE.matches ? `${Math.min(gap, window.innerHeight - left.offsetHeight - gap)}px` : "";
  };
  new ResizeObserver(stick).observe(left);  // the Coming up list, activity box and alerts change it
  window.addEventListener("resize", stick);
}

init();
stickLeftColumn();
