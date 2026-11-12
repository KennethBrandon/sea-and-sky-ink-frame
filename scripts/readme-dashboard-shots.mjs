#!/usr/bin/env node
// Take the README's dashboard pictures (docs/images/dashboard-*.png) from a dashboard running on
// the Mac, with headless Chrome over the DevTools protocol. Node's built-in WebSocket, no install.
//
//   Start one with sample photos and no personal anything (CLAUDE.md "the dashboard on the Mac"), then:
//   node scripts/readme-dashboard-shots.mjs http://127.0.0.1:8742 [only-this-picture ...]
//
// The "fake panel" note in the header is hidden: it marks a run on the Mac, which the wall's own
// dashboard never shows. Nothing else on the page is changed.

import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "docs", "images");
const URL_BASE = process.argv[2] ?? "http://127.0.0.1:8742";
const ONLY = process.argv.slice(3);
const PORT = 9333;

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

// Every picture: the window, light or dark, what to do first, then the page or one element.
const PREPARE = `
  (async () => {
    const sub = document.getElementById("clock");
    if (sub) new MutationObserver(() => {
      const t = sub.textContent.replace(/ · fake panel/, "");
      if (t !== sub.textContent) sub.textContent = t;
    }).observe(sub, { childList: true, characterData: true, subtree: true });
    if (sub) sub.textContent = sub.textContent.replace(/ · fake panel/, "");
  })()`;
const open = (id) => `document.getElementById(${JSON.stringify(id)}).open = true`;
// Sea at sunset in a gale, through the Sea box's own buttons.
const PREVIEW = `
  (async () => {
    const card = document.getElementById("sea-card");
    card.open = true;
    await new Promise((done) => setTimeout(done, 500));
    const tap = (text) => [...card.querySelectorAll("button, label")].find((b) => b.textContent.trim() === text).click();
    tap("Sunset"); tap("Gale"); tap("Overcast"); tap("Preview");
  })()`;
const SHOTS = [
  { out: "dashboard-desktop", width: 1240, height: 1000, scheme: "light", dpr: 2 },
  { out: "dashboard-phone", width: 390, height: 844, scheme: "dark", dpr: 3 },
  { out: "dashboard-photos", width: 420, height: 900, scheme: "light", dpr: 2, run: open("photos-card"), element: "#photos-card", wait: 3500 },
  { out: "dashboard-schedule", width: 420, height: 900, scheme: "light", dpr: 2, run: open("schedule-card"), element: "#schedule-card" },
  { out: "dashboard-battery", width: 420, height: 900, scheme: "light", dpr: 2, run: open("battery-card"), element: "#battery-card", wait: 2000 },
  { out: "dashboard-preview", width: 1240, height: 790, scheme: "light", dpr: 2, run: PREVIEW, wait: 6000 },
  // The drawing of the frame's back and what each press does, down to "Give it time".
  { out: "dashboard-help", width: 460, height: 900, scheme: "light", dpr: 2, run: open("help-card"), element: "#help-card", until: "Give it time" },
  // Needs the setup hotspot up: `{"connected": false}` in fake_wifi.json, then `photopainter press
  // wifi-setup --fake-panel` (CLAUDE.md "No Wi-Fi"), so take it on its own after the others.
  { out: "dashboard-setup", path: "/setup", width: 390, height: 530, scheme: "light", dpr: 3, wait: 1500,
    run: `(async () => { await new Promise((done) => setTimeout(done, 800)); document.querySelector('input[type=radio]').click();
      document.getElementById("password").value = "example-password"; document.getElementById("password").dispatchEvent(new Event("input", { bubbles: true })); })()` },
];

const profile = mkdtempSync(join(tmpdir(), "readme-shots-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--hide-scrollbars", "--no-first-run", "--no-default-browser-check", "about:blank"], { stdio: "ignore" });

let target;
for (let i = 0; i < 60 && !target; i++) {
  await sleep(250);
  try {
    target = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
  } catch { /* not up yet */ }
}
if (!target) throw new Error("Chrome didn't start");

const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((done) => ws.addEventListener("open", done));
let nextId = 1;
const pending = new Map();
const waiters = [];
ws.addEventListener("message", ({ data }) => {
  const message = JSON.parse(data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  } else if (message.method) {
    for (const [name, done] of waiters) if (name === message.method) done();
  }
});
const send = (method, params = {}) => new Promise((done, fail) => {
  const id = nextId++;
  pending.set(id, (m) => (m.error ? fail(new Error(`${method}: ${m.error.message}`)) : done(m.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const run = async (expression) => {
  const result = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(`${expression}: ${result.exceptionDetails.text}`);
  return result.result.value;
};

await send("Page.enable");
for (const shot of SHOTS) {
  if (ONLY.length && !ONLY.includes(shot.out)) continue;
  await send("Emulation.setDeviceMetricsOverride", { width: shot.width, height: shot.height, deviceScaleFactor: shot.dpr, mobile: false });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: shot.scheme }] });
  const loaded = new Promise((done) => waiters.push(["Page.loadEventFired", done]));
  await send("Page.navigate", { url: URL_BASE + (shot.path ?? "") });
  await loaded;
  waiters.length = 0;
  await sleep(1800);
  await run(PREPARE);
  if (shot.run) await run(shot.run);
  await sleep(shot.wait ?? 900);
  let clip = { x: 0, y: 0, width: shot.width, height: shot.height, scale: 1 };
  if (shot.element) {
    const box = await run(`(() => { const r = document.querySelector(${JSON.stringify(shot.element)}).getBoundingClientRect();
      const stop = ${JSON.stringify(shot.until ?? null)};
      const at = stop && [...document.querySelectorAll(${JSON.stringify(shot.element)} + " h3")].find((h) => h.textContent.trim() === stop);
      const height = at ? at.getBoundingClientRect().top - r.top - 4 : r.height;
      return { x: r.x + scrollX, y: r.y + scrollY, width: r.width, height }; })()`);
    clip = { ...box, scale: 1 };
  }
  const { data } = await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, clip });
  writeFileSync(join(OUT, `${shot.out}.png`), Buffer.from(data, "base64"));
  console.log(shot.out, `${Math.round(clip.width)}x${Math.round(clip.height)}`);
}

ws.close();
chrome.kill();
await sleep(300);
rmSync(profile, { recursive: true, force: true });
