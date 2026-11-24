/*
 * Drives the FlashForge C5 tab's JavaScript outside OrcaSlicer.
 *
 * The page is one inline script that talks to the plugin over
 * orca.postMessage / orca.onMessage. This runs it in a DOM stub against the
 * plugin's real normalised /detail payload, so the tab's message handling,
 * rendering and button wiring can be checked without a slicer or a printer.
 *
 *   node tests/page_harness.js <page.js> <states.json>
 */
"use strict";
const fs = require("fs");
const vm = require("vm");

const jsPath = process.argv[2];
const statesPath = process.argv[3];
const idsPath = process.argv[4];
const JS_SOURCE = fs.readFileSync(jsPath, "utf8");
const STATES = JSON.parse(fs.readFileSync(statesPath, "utf8"));

const errors = [];
let failures = 0;
function check(name, condition, detail) {
  const ok = !!condition;
  if (!ok) { failures++; }
  console.log("  [" + (ok ? "PASS" : "FAIL") + "] " + name +
              (ok || !detail ? "" : " — " + detail));
}
function must(name, fn) {
  try { fn(); check(name, true); }
  catch (e) { check(name, false, e.message); }
}
function assert(cond, msg) { if (!cond) { throw new Error(msg); } }

// --- DOM stub ------------------------------------------------------------- //
// Every element the page looks up must exist: a null property write throws and
// aborts the whole inline script, which is exactly the failure this catches.
function makeEl(id) {
  return {
    id: id, _listeners: {}, style: {}, dataset: {}, children: [], parentNode: null,
    className: "", textContent: "", innerHTML: "", value: "", checked: true,
    disabled: false, files: null,
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t) { (this._listeners[t] || []).forEach(fn => fn({ currentTarget: this })); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    setAttribute(k, v) { this.dataset[k] = v; },
    getAttribute(k) { return this.dataset[k]; },
    // Just enough of the element tree for the page's one reorder: the
    // connection panel moves to the top while the printer has no address.
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c, ref) {
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i >= 0) { this.children.splice(i, 0, c); } else { this.children.push(c); }
      c.parentNode = this; return c;
    },
    get lastElementChild() { return this.children[this.children.length - 1] || null; },
    get nextElementSibling() {
      if (!this.parentNode) { return null; }
      const i = this.parentNode.children.indexOf(this);
      return this.parentNode.children[i + 1] || null;
    },
    focus() {}, scrollIntoView() {},
    width: 600, height: 120,
    getContext: () => ({
      clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
      strokeStyle: "", lineWidth: 1,
    }),
    removeAttribute(k) { delete this[k]; delete this.dataset[k]; },
  };
}

const FALLBACK_IDS = ["banner", "pname", "pip", "pfw", "dot", "stt", "jfile", "barfill",
  "jpct", "jlay", "jel", "jrem", "nozzles", "tbed", "tch", "tdoor", "tdisk",
  "spools", "upwrap", "upbar", "uplabel", "files", "prevwrap", "prevname",
  "previmg", "host",
  "port", "serial", "code", "poll", "bsave", "borca", "cfginfo", "bpause", "bresume",
  "bcancel", "blight", "bclear", "setup"];

// The ids come from the real markup, so this stub cannot drift out of sync with
// the page — a page that looks up an element it never defines is precisely the
// failure this harness exists to catch, and a stale list here would hide it.
let IDS = FALLBACK_IDS;
if (idsPath && fs.existsSync(idsPath)) {
  const fromMarkup = JSON.parse(fs.readFileSync(idsPath, "utf8"));
  IDS = Array.from(new Set(FALLBACK_IDS.concat(fromMarkup)));
}
const byId = {};
IDS.forEach(id => { byId[id] = makeEl(id); });

// The page reorders the setup card, so the stub needs a parent for it. This
// mirrors the markup: header, banner, then the cards, with the connection
// panel last — which is the placement that caused the bug.
const wrap = makeEl("wrap");
wrap.appendChild(byId["pname"]);          // stands in for #hdr
wrap.appendChild(byId["banner"]);
wrap.appendChild(makeEl("cardJob"));
wrap.appendChild(makeEl("cardFiles"));
wrap.appendChild(byId["setup"]);

// The file rows are generated as markup, so read the buttons back out of what
// the page produced and hand each one its own click handler.
byId["files"].querySelectorAll = function (sel) {
  const attr = sel === "button[data-reprint]" ? "data-reprint" :
               sel === "button[data-preview]" ? "data-preview" : null;
  if (!attr) { return []; }
  const out = [];
  const re = new RegExp(attr + '="([^"]*)"', "g");
  let m;
  while ((m = re.exec(this.innerHTML)) !== null) {
    const name = m[1];
    out.push({ getAttribute: () => name, addEventListener: (t, fn) => {
      this._clicks = this._clicks || [];
      this._clicks.push({ attr: attr, name: name, fn: fn });
    } });
  }
  return out;
};

const sent = [];
const listeners = [];
const timers = [];
const doc = { getElementById: id => byId[id] || null, createElement: () => makeEl("x") };

let readerResult = new ArrayBuffer(8);
function FileReader() {
  this.readAsArrayBuffer = (blob) => {
    this.result = new ArrayBuffer(blob.byteLength || 0);
    this.onload();
  };
}

const ctx = {
  document: doc, window: {}, Number, Math, JSON, String, Uint8Array, Boolean, Date,
  FileReader: FileReader,
  console: { log: () => {}, error: (...a) => errors.push(a.join(" ")) },
  orca: { postMessage: o => sent.push(o), onMessage: fn => listeners.push(fn) },
  // The chart reads its colours off the element rather than hardcoding them.
  getComputedStyle: () => ({ getPropertyValue: () => "#7cc36a" }),
  setInterval: () => 1, clearInterval: () => {},
  // Recorded rather than run. The auto-hide timer is only testable if it can be
  // fired on demand, and a sticky banner is only provably sticky if no timer
  // was armed at all.
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  btoa: s => Buffer.from(s, "binary").toString("base64"),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(JS_SOURCE, ctx);
const deliver = m => listeners.forEach(fn => fn(m));

// --- tests ---------------------------------------------------------------- //

console.log("\nPage start-up");
must("the page posts hello on load", () => {
  assert(sent.some(m => m.type === "hello"), "no hello posted");
});
must("every element the script touches exists", () => {
  assert(errors.length === 0, "console.error: " + errors.join("; "));
});
must("each control button is wired", () => {
  ["bsave", "bpause", "bresume", "bcancel", "blight", "bclear",
   "blevel", "bshaper", "btimer"].forEach(id => {
    assert(byId[id]._listeners.click, id + " has no handler");
  });
});
must("the page asks for the print history on load", () => {
  assert(sent.some(m => m.type === "history"), "no history request posted");
});
must("a maintenance button asks for exactly its own command", () => {
  sent.length = 0;
  byId["blevel"].dispatch("click");
  byId["bshaper"].dispatch("click");
  byId["timer"].value = "60";
  byId["btimer"].dispatch("click");
  const kinds = sent.filter(m => m.type === "maintenance").map(m => m.kind);
  assert(kinds.join(",") === "level,shaper,timer", "kinds: " + kinds);
  const timer = sent.filter(m => m.type === "maintenance"
                          && m.kind === "timer").pop();
  assert(timer.minutes === 60, "timer minutes: " + JSON.stringify(timer));
});

console.log("\nPainting the plugin's real /detail payload");
must("a live job paints progress, layer and timings", () => {
  deliver({ type: "state", last_slice: null, status: STATES.live });
  assert(byId["jfile"].textContent === "widget.gcode",
         "file name not painted: " + byId["jfile"].textContent);
  assert(byId["barfill"].style.width === "41.5%",
         "progress bar: " + byId["barfill"].style.width);
  assert(byId["jel"].textContent === "10m 0s", "elapsed: " + byId["jel"].textContent);
  assert(byId["jrem"].textContent === "15m 0s", "remaining: " + byId["jrem"].textContent);
  assert(byId["dot"].className === "dot busy", "status dot: " + byId["dot"].className);
  assert(byId["pname"].textContent === "Creator 5 Pro", "printer name");
});
must("temperatures and the material station render", () => {
  assert(byId["nozzles"].innerHTML.indexOf("Tool 4") >= 0, "nozzles missing");
  assert(byId["tbed"].textContent.indexOf("60") >= 0, "bed temp: " + byId["tbed"].textContent);
  assert(byId["spools"].innerHTML.indexOf("Slot 1") >= 0, "spools missing");
  assert(byId["spools"].innerHTML.indexOf("Grey") >= 0 ||
         byId["spools"].innerHTML.indexOf("White") >= 0, "no colour swatch name");
});
must("an idle payload paints dashes, not stale numbers", () => {
  deliver({ type: "state", last_slice: null, status: STATES.idle });
  assert(byId["jel"].textContent === "—", "elapsed: " + byId["jel"].textContent);
  assert(byId["jrem"].textContent === "—", "remaining: " + byId["jrem"].textContent);
  assert(byId["barfill"].style.width === "0%", "progress: " + byId["barfill"].style.width);
});
must("an offline payload does not throw", () => {
  byId["nozzles"].innerHTML = "";
  deliver({ type: "state", last_slice: null,
            status: { name: "FlashForge", status: "offline", progress: 0,
                      nozzles: [], spools: [] } });
  assert(byId["nozzles"].innerHTML.indexOf("no nozzle data") >= 0,
         "empty nozzles not handled");
});

console.log("\nStarting a job from the file list");
// Sending a fresh slice is OrcaSlicer's own button now, so the only job this
// page starts is a reprint — and that is what has to report progress.
must("a reprint starts polling for progress", () => {
  byId["files"]._clicks = [];
  deliver({ type: "files", files: ["widget.gcode"], detail: [] });
  const reprint = byId["files"]._clicks.find(c => c.attr === "data-reprint");
  assert(reprint, "no reprint handler was wired");
  sent.length = 0;
  timers.length = 0;
  reprint.fn({ currentTarget: { getAttribute: () => "widget.gcode" } });
  assert(sent.some(m => m.type === "reprint" && m.filename === "widget.gcode"),
         "reprint posted nothing: " + JSON.stringify(sent));
  assert(byId["upwrap"].className !== "hidden", "progress bar stayed hidden");
  assert(timers.length > 0, "no job-progress timer was started");
});

must("a job error reaches the banner visibly", () => {
  deliver({ type: "job", running: false, done: true, pct: 1,
            error: "PrinterError: Printer is busy" });
  assert(byId["banner"].style.display === "block", "banner stayed hidden");
  assert(byId["banner"].className === "err", "banner class: " + byId["banner"].className);
  assert(byId["banner"].textContent.includes("Printer is busy"), "error text missing");
});
must("a successful job reports what was sent", () => {
  deliver({ type: "job", running: false, done: true, pct: 1,
            result: "widget.gcode sent and printing." });
  assert(byId["banner"].className === "ok", "banner class: " + byId["banner"].className);
  assert(byId["banner"].textContent.includes("sent and printing"), "result text missing");
});

console.log("\nJob control and file list");
must("the control buttons post the right actions", () => {
  sent.length = 0;
  byId["bcancel"].dispatch("click");
  byId["bpause"].dispatch("click");
  byId["bresume"].dispatch("click");
  byId["bclear"].dispatch("click");
  ["cancel", "pause", "resume", "clear"].forEach(a => {
    assert(sent.some(m => m.type === "control" && m.action === a), "no " + a);
  });
});
must("the light toggle sends the opposite of the current state", () => {
  deliver({ type: "state", last_slice: null, status: STATES.idle }); // light off
  sent.length = 0;
  byId["blight"].dispatch("click");
  const m = sent.find(x => x.type === "control" && x.action === "light");
  assert(m && m.on === true, "expected on:true, got " + JSON.stringify(m));
  deliver({ type: "state", last_slice: null, status: STATES.live });  // light on
  sent.length = 0;
  byId["blight"].dispatch("click");
  const m2 = sent.find(x => x.type === "control" && x.action === "light");
  assert(m2 && m2.on === false, "expected on:false, got " + JSON.stringify(m2));
});
must("stored files render with Preview and Reprint buttons", () => {
  byId["files"]._clicks = [];
  // The device's own units: printingTime in seconds, and an unrounded float
  // weight. Both used to be printed raw, which is what the user saw.
  deliver({ type: "files", files: ["widget.gcode.3mf", "bracket.gcode.3mf"],
            detail: [{ gcodeFileName: "widget.gcode.3mf", printingTime: 910,
                       totalFilamentWeight: 7.840000152587891 }] });
  const html = byId["files"].innerHTML;
  assert(html.includes("widget.gcode.3mf"), "file missing from the list");
  assert(html.includes("15m 10s"), "printing time not formatted: " + html);
  assert(html.includes("7.8 g"), "weight not rounded to one decimal: " + html);
  assert(!html.includes("7.840000152587891"), "a raw device float leaked into the UI");
  const prev = byId["files"]._clicks.filter(c => c.attr === "data-preview");
  const reprint = byId["files"]._clicks.filter(c => c.attr === "data-reprint");
  assert(prev.length === 2, "want 2 preview buttons, got " + prev.length);
  assert(reprint.length === 2, "want 2 reprint buttons, got " + reprint.length);
  sent.length = 0;
  prev[0].fn({ currentTarget: { getAttribute: () => "widget.gcode.3mf" } });
  assert(sent.some(m => m.type === "preview" && m.filename === "widget.gcode.3mf"),
         "preview posted nothing: " + JSON.stringify(sent));
  assert(byId["prevwrap"].className !== "hidden", "the preview panel stayed hidden");
});
must("a returned preview is shown against its filename", () => {
  deliver({ type: "preview", filename: "widget.gcode.3mf",
            image: "data:image/png;base64,AAAA" });
  assert(byId["prevname"].textContent === "widget.gcode.3mf",
         "filename: " + byId["prevname"].textContent);
  assert(byId["previmg"].src === "data:image/png;base64,AAAA",
         "image not set: " + byId["previmg"].src);
});
must("an empty printer file list says so rather than looking broken", () => {
  deliver({ type: "files", files: [], detail: [] });
  assert(byId["files"].innerHTML.includes("nothing stored"), byId["files"].innerHTML);
});

console.log("\nSettings");
must("saved settings are painted back into the form", () => {
  deliver({ type: "cfg", cfg: { host: "10.0.0.9", port: 8898, serial: "SN1",
                                check_code: "CC", poll_seconds: 5 } });
  assert(byId["host"].value === "10.0.0.9", "host: " + byId["host"].value);
  assert(byId["serial"].value === "SN1", "serial");
  assert(byId["code"].value === "CC", "check code");
});
must("Save & test posts the form values", () => {
  byId["host"].value = "192.168.1.50";
  byId["port"].value = "8898";
  byId["serial"].value = "SN999";
  byId["code"].value = "ABCD";
  byId["poll"].value = "4";
  sent.length = 0;
  byId["bsave"].dispatch("click");
  const m = sent.find(x => x.type === "save_cfg");
  assert(m && m.host === "192.168.1.50" && m.serial === "SN999" &&
         m.check_code === "ABCD" && m.port === 8898 && m.poll_seconds === 4,
         JSON.stringify(m));
});

console.log("\nLoading the printer from OrcaSlicer");
must("the Load from OrcaSlicer button asks the plugin to read the preset", () => {
  sent.length = 0;
  byId["borca"].dispatch("click");
  assert(sent.some(x => x.type === "load_orca"), JSON.stringify(sent));
});
must("values taken from the preset are announced, and named in plain words", () => {
  deliver({ type: "cfg",
            cfg: { host: "192.168.8.128", port: 8898, serial: "SNORCA1",
                   check_code: "ORCACODE", poll_seconds: 3 },
            orca: { found: true,
                    applied: { host: "192.168.8.128", serial: "SNORCA1",
                               check_code: "ORCACODE" } } });
  const text = byId["banner"].textContent;
  assert(/OrcaSlicer/.test(text), text);
  assert(/Filled address, serial number, access code/.test(text), text);
  assert(byId["host"].value === "192.168.8.128" && byId["serial"].value === "SNORCA1",
         "the form was not filled from the preset");
});
must("a config that came from the file rather than the preset announces nothing", () => {
  byId["banner"].textContent = "";
  deliver({ type: "cfg", cfg: { host: "10.0.0.9", port: 8898, serial: "SN1",
                                check_code: "CC", poll_seconds: 5 },
            orca: { found: false, applied: {} } });
  assert(byId["banner"].textContent === "", byId["banner"].textContent);
});

console.log("\nFinding the printer settings");
// Let any transient message left by the tests above expire, as nine seconds of
// real use would — otherwise the guard below (correctly) refuses to replace it.
timers.splice(0).forEach(fn => fn());

must("with no address, the connection panel moves to the top", () => {
  // The bug this covers: the panel sat sixth on the page, so the banner told
  // the user to open a Settings that does not exist while nothing that would
  // fix it was on screen.
  deliver({ type: "cfg", cfg: { host: "", port: 8898, serial: "", check_code: "" } });
  assert(wrap.children.indexOf(byId["banner"]) + 1 ===
         wrap.children.indexOf(byId["setup"]),
         "setup is at index " + wrap.children.indexOf(byId["setup"]) +
         " of " + wrap.children.length);
  assert(String(byId["setup"].className).includes("wanted"),
         "setup was not highlighted: " + byId["setup"].className);
});
must("once an address is set the panel goes back to the end", () => {
  deliver({ type: "cfg", cfg: { host: "10.0.0.9", port: 8898, serial: "SN1",
                                check_code: "CC" } });
  assert(wrap.children[wrap.children.length - 1] === byId["setup"],
         "setup is at index " + wrap.children.indexOf(byId["setup"]));
  assert(!String(byId["setup"].className).includes("wanted"),
         "setup is still highlighted: " + byId["setup"].className);
});
must("an unconfigured tab shows a banner the user can act on", () => {
  deliver({ type: "state", status: STATES.idle, last_slice: null, needs_setup: true });
  assert(byId["banner"].style.display === "block", "the banner is not shown");
  assert(byId["banner"].textContent.includes("Printer connection"),
         "the banner does not say where to look: " + byId["banner"].textContent);
  assert(!byId["banner"].textContent.includes("Settings"),
         "the banner still points at a Settings that does not exist");
  assert(byId["banner"].children.length === 1, "no action button on the banner");
});
must("that banner's button focuses the address field", () => {
  let focused = false;
  byId["host"].focus = () => { focused = true; };
  byId["banner"].children[0].dispatch("click");
  assert(focused, "the button did not focus the address field");
});
must("a setup banner is sticky, not on the nine-second auto-hide", () => {
  const before = timers.length;
  deliver({ type: "state", status: STATES.idle, last_slice: null, needs_setup: true });
  assert(timers.length === before, "a timer was armed for the sticky banner");
});
must("a setup banner does not stomp a live status message", () => {
  deliver({ type: "log", message: "Sending…" });
  deliver({ type: "state", status: STATES.idle, last_slice: null, needs_setup: true });
  assert(byId["banner"].textContent === "Sending…",
         "the setup banner overwrote a live message: " + byId["banner"].textContent);
});
must("an ordinary message still auto-hides", () => {
  deliver({ type: "log", message: "hello" });
  assert(timers.length > 0, "no timer armed for a transient message");
  timers[timers.length - 1]();
  assert(byId["banner"].style.display === "none", "the transient banner did not hide");
});
must("a configured tab clears the setup banner", () => {
  deliver({ type: "state", status: STATES.idle, last_slice: null, needs_setup: true });
  assert(byId["banner"].style.display === "block", "setup banner did not appear");
  deliver({ type: "state", status: STATES.idle, last_slice: null, needs_setup: false });
  assert(byId["banner"].style.display === "none",
         "the setup banner outlived the setup: " + byId["banner"].style.display);
});

must("the preview panel survives a failed preview", () => {
  deliver({ type: "preview", filename: "widget.gcode.3mf", image: "data:image/png;base64,AAAA" });
  deliver({ type: "error", message: "The printer has no preview for that file" });
  assert(byId["banner"].className === "err", "the failure was not shown");
  assert(byId["banner"].textContent.includes("no preview"), "error text missing");
});

console.log("\nThe camera");
must("the camera button starts the stream", () => {
  sent.length = 0;
  byId["bcam"].dispatch("click");
  assert(sent.some(m => m.type === "camera_on"),
         "clicking the camera button posted: " + JSON.stringify(sent));
});
must("a camera message reveals the panel and renames the button", () => {
  deliver({ type: "camera", on: true });
  assert(byId["camwrap"].className === "", "the panel stayed hidden");
  assert(byId["bcam"].textContent === "Stop camera",
         "button label: " + byId["bcam"].textContent);
});
must("a frame paints the image and drops the placeholder", () => {
  deliver({ type: "frame", image: "data:image/jpeg;base64,AAAA" });
  assert(byId["camimg"].src === "data:image/jpeg;base64,AAAA",
         "frame not painted: " + byId["camimg"].src);
  assert(byId["camnote"].textContent === "",
         "the placeholder outlived the first frame: " + byId["camnote"].textContent);
});
must("a second click stops the stream rather than opening another", () => {
  sent.length = 0;
  byId["bcam"].dispatch("click");
  assert(sent.some(m => m.type === "camera_off"),
         "the toggle posted: " + JSON.stringify(sent));
});
must("a frame arriving after the stop cannot repaint the panel", () => {
  deliver({ type: "camera", on: false });
  assert(byId["camwrap"].className === "hidden", "the panel stayed open");
  assert(byId["bcam"].textContent === "View camera",
         "button label: " + byId["bcam"].textContent);
  deliver({ type: "frame", image: "data:image/jpeg;base64,BBBB" });
  assert(!byId["camimg"].src,
         "a late frame repainted the panel: " + byId["camimg"].src);
});
must("stopping the camera clears the last frame", () => {
  deliver({ type: "camera", on: true });
  deliver({ type: "frame", image: "data:image/jpeg;base64,AAAA" });
  deliver({ type: "camera", on: false });
  assert(!byId["camimg"].src, "the stale frame was left behind");
});

check("no null-element lookups fired during the run", errors.length === 0,
      errors.join("; "));

console.log("");
if (failures) {
  console.log(failures + " check(s) failed");
  process.exit(1);
}
console.log("all page checks passed");

console.log("\nTemperatures, maintenance and history");
must("the chart draws from the samples on the state message", () => {
  const now = Math.floor(Date.now() / 1000);
  deliver({ type: "state", last_slice: null, status: STATES.idle,
            temps: [{ t: now - 10, n: 200, b: 60 },
                    { t: now - 5,  n: 205, b: 61 },
                    { t: now,      n: 210, b: 62 }] });
  assert(byId["charnote"].textContent.indexOf("210") >= 0,
         "chart note: " + byId["charnote"].textContent);
  assert(byId["charnote"].textContent.indexOf("sampled") >= 0,
         "sample span not shown: " + byId["charnote"].textContent);
});
must("fewer than two samples leaves the chart waiting", () => {
  deliver({ type: "state", last_slice: null, status: STATES.idle,
            temps: [{ t: 1, n: 200, b: 60 }] });
  assert(byId["charnote"].textContent === "collecting…",
         "note: " + byId["charnote"].textContent);
});
must("the maintenance card mirrors the printer's own setting", () => {
  deliver({ type: "state", last_slice: null, status: Object.assign(
    {}, STATES.idle, { auto_shutdown: "open", auto_shutdown_time: 45 }) });
  assert(byId["timer"].value === "45", "select: " + byId["timer"].value);
  assert(byId["mntnote"].textContent.indexOf("45 min") >= 0,
         "note: " + byId["mntnote"].textContent);
});
must("and locks while a print runs", () => {
  deliver({ type: "state", last_slice: null, status: STATES.live });
  assert(byId["blevel"].disabled && byId["bshaper"].disabled,
         "maintenance stayed enabled during a print");
});
must("the history card shows the totals and the newest jobs", () => {
  const now = Math.floor(Date.now() / 1000);
  deliver({ type: "history", totals: { jobs: 3, print_seconds: 190,
                                       success_pct: 33.3 },
            jobs: [{ name: "benchy.3mf", result: "finished", duration_s: 120,
                     start: now - 300, end: now - 180 },
                   { name: "bad.3mf", result: "error", duration_s: 10,
                     start: now - 100, end: now - 90 }] });
  assert(byId["hjobs"].textContent === "3", "jobs: " + byId["hjobs"].textContent);
  assert(byId["hrate"].textContent === "33.3%", "rate: " + byId["hrate"].textContent);
  const rows = byId["hist"].innerHTML;
  assert(rows.indexOf("benchy.3mf") >= 0 && rows.indexOf("bad.3mf") >= 0,
         "job rows missing");
  assert(rows.indexOf("error") >= 0, "result label missing");
});
