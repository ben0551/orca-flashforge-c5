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
    removeAttribute(k) { delete this[k]; delete this.dataset[k]; },
  };
}

const IDS = ["banner", "pname", "pip", "pfw", "dot", "stt", "jfile", "barfill",
  "jpct", "jlay", "jel", "jrem", "nozzles", "tbed", "tch", "tdoor", "tdisk",
  "spools", "upwrap", "upbar", "uplabel", "files", "prevwrap", "prevname",
  "previmg", "host",
  "port", "serial", "code", "poll", "bsave", "cfginfo", "bpause", "bresume",
  "bcancel", "blight", "bclear", "setup"];
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
  ["bsave", "bpause", "bresume", "bcancel", "blight", "bclear"].forEach(id => {
    assert(byId[id]._listeners.click, id + " has no handler");
  });
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

check("no null-element lookups fired during the run", errors.length === 0,
      errors.join("; "));

console.log("");
if (failures) {
  console.log(failures + " check(s) failed");
  process.exit(1);
}
console.log("all page checks passed");
