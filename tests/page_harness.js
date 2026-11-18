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
    id: id, _listeners: {}, style: {}, dataset: {},
    className: "", textContent: "", innerHTML: "", value: "", checked: true,
    disabled: false, files: null,
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t) { (this._listeners[t] || []).forEach(fn => fn({ currentTarget: this })); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    setAttribute(k, v) { this.dataset[k] = v; },
    getAttribute(k) { return this.dataset[k]; },
  };
}

const IDS = ["banner", "pname", "pip", "pfw", "dot", "stt", "jfile", "barfill",
  "jpct", "jlay", "jel", "jrem", "nozzles", "tbed", "tch", "tdoor", "tdisk",
  "spools", "mapwrap", "tools", "bsend", "bsendprint", "blast", "lastinfo",
  "bfile", "finfo", "clevel", "upwrap", "upbar", "uplabel", "files", "host",
  "port", "serial", "code", "poll", "bsave", "cfginfo", "bpause", "bresume",
  "bcancel", "blight", "bclear"];
const byId = {};
IDS.forEach(id => { byId[id] = makeEl(id); });
// The markup ships the send buttons disabled; a stub has no markup to parse.
byId["bsend"].disabled = true;
byId["bsendprint"].disabled = true;

// The tool picker is generated as markup, so read the selects back out of the
// markup the page produced and honour which option carries `selected`.
byId["tools"].querySelectorAll = function (sel) {
  if (sel !== "select[data-tool]") { return []; }
  const out = [];
  const re = /<select data-tool="(\d+)">([\s\S]*?)<\/select>/g;
  let m;
  while ((m = re.exec(this.innerHTML)) !== null) {
    const chosen = /value="([^"]*)"\s+selected/.exec(m[2]) ||
                   /value="([^"]*)"/.exec(m[2]);
    const tool = m[1], value = chosen ? chosen[1] : "";
    out.push({ getAttribute: () => tool, value: value });
  }
  return out;
};
byId["files"].querySelectorAll = function (sel) {
  if (sel !== "button[data-reprint]") { return []; }
  const out = [];
  const re = /data-reprint="([^"]*)"/g;
  let m;
  while ((m = re.exec(this.innerHTML)) !== null) {
    const name = m[1];
    out.push({ getAttribute: () => name, addEventListener: (t, fn) => {
      (this._reprint = this._reprint || []).push({ name: name, fn: fn });
    } });
  }
  return out;
};

const sent = [];
const listeners = [];
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
  setTimeout: () => 1, clearTimeout: () => {},
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
  ["bsave", "bfile", "bsend", "bsendprint", "blast", "bpause", "bresume",
   "bcancel", "blight", "bclear"].forEach(id => {
    assert(byId[id]._listeners.click || byId[id]._listeners.change,
           id + " has no handler");
  });
});
must("both send buttons start disabled", () => {
  assert(byId["bsend"].disabled && byId["bsendprint"].disabled,
         "a send button was live before a file was staged");
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

console.log("\nStaging a file");
// Bigger than one 512 KB chunk, so the ack-driven pacing has something to do.
// `slice` is what the chunk reader calls; the send buttons start disabled in
// the markup, so mirror that here rather than assuming the stub's default.
const CHUNK = 524288;
const bundle = { name: "widget.gcode.3mf", size: CHUNK + 400000,
                 slice: (start, end) => new ArrayBuffer(end - start) };
must("picking a file starts staging immediately", () => {
  deliver({ type: "state", last_slice: null, status: STATES.idle });
  byId["bfile"].files = [bundle];
  sent.length = 0;
  byId["bfile"].dispatch("change");
  assert(sent.some(m => m.type === "stage_begin" && m.name === bundle.name),
         "no stage_begin posted: " + JSON.stringify(sent));
  assert(byId["bsend"].disabled, "send went live before staging finished");
});
must("nothing is read until the plugin acknowledges", () => {
  assert(!sent.some(m => m.type === "stage_chunk"),
         "a chunk was read before the plugin asked for it");
});
must("the first chunk is sent on the plugin's first ack", () => {
  // This is the bug that made every upload stall at 0%: the initial ack has
  // seq 0, and a naive "only continue if progress > 0" guard swallowed it.
  sent.length = 0;
  deliver({ type: "stage_ack", seq: 0 });
  assert(sent.some(m => m.type === "stage_chunk" && m.seq === 0),
         "no first chunk after ack 0: " + JSON.stringify(sent.map(m => [m.type, m.seq])));
});
must("an ack for something we did not send is ignored", () => {
  // Idempotency: a duplicated or out-of-order ack must not re-read the file.
  sent.length = 0;
  deliver({ type: "stage_ack", seq: 7 });
  assert(sent.length === 0, "a mismatched ack triggered a re-read: " + JSON.stringify(sent));
});
must("subsequent chunks follow each ack", () => {
  sent.length = 0;
  deliver({ type: "stage_ack", seq: CHUNK });
  assert(sent.some(m => m.type === "stage_chunk" && m.seq === CHUNK),
         "no follow-on chunk: " + JSON.stringify(sent.map(m => [m.type, m.seq])));
  sent.length = 0;
  deliver({ type: "stage_ack", seq: bundle.size });
  assert(sent.length === 0, "reading continued past the end of the file");
});
must("staged tools render one picker per tool, with human colour names", () => {
  deliver({ type: "staged", name: bundle.name, tools: [
    { tool: 1, material: "PLA", color: "#FF0000", suggested: 2, color_name: "Red" },
    { tool: 2, material: "PLA", color: "#0000FF", suggested: 3, color_name: "Blue" }] });
  assert(byId["mapwrap"].className !== "hidden", "mapping panel stayed hidden");
  const html = byId["tools"].innerHTML;
  assert((html.match(/data-tool=/g) || []).length === 2, "want 2 pickers: " + html);
  assert(/\(Black\)|\(Grey\)/.test(html) || /Slot 2/.test(html),
         "slot labels are not human-readable: " + html);
  assert(!html.includes("#000000"), "a raw hex code leaked into the picker");
});
must("staging re-enables the send buttons", () => {
  assert(!byId["bsend"].disabled && !byId["bsendprint"].disabled,
         "send still disabled after staging");
});

console.log("\nSending");
must("Send & print posts the pre-selected nearest slot", () => {
  sent.length = 0;
  byId["bsendprint"].dispatch("click");
  const commit = sent.find(m => m.type === "commit");
  assert(commit, "no commit posted: " + JSON.stringify(sent));
  assert(commit.print === true, "commit.print was not true");
  assert(commit.mappings.length === 2, "want 2 mappings: " + JSON.stringify(commit.mappings));
  assert(commit.mappings[0].tool === 1 && commit.mappings[0].slot === 2,
         "tool 1 did not default to the suggested slot 2");
});
must("Send only posts print=false", () => {
  sent.length = 0;
  deliver({ type: "staged", name: bundle.name, tools: [
    { tool: 1, material: "PLA", color: "#FF0000", suggested: 2 }] });
  byId["bsend"].dispatch("click");
  const commit = sent.find(m => m.type === "commit");
  assert(commit && commit.print === false, "print was not false");
});
must("committing starts polling for progress", () => {
  assert(byId["upwrap"].className !== "hidden", "progress bar stayed hidden");
});
must("a job error reaches the banner visibly", () => {
  deliver({ type: "job", running: false, done: true, pct: 1,
            error: "PrinterError: Printer is busy" });
  assert(byId["banner"].style.display === "block", "banner stayed hidden");
  assert(byId["banner"].className === "err", "banner class: " + byId["banner"].className);
  assert(byId["banner"].textContent.includes("Printer is busy"), "error text missing");
  assert(!byId["bsend"].disabled, "send buttons stayed disabled after the error");
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
must("stored files render with a Reprint button", () => {
  byId["files"]._reprint = [];
  deliver({ type: "files", files: ["widget.gcode", "bracket.gcode"],
            detail: [{ gcodeFileName: "widget.gcode", printingTime: "1h 2m",
                       totalFilamentWeight: 43 }] });
  const html = byId["files"].innerHTML;
  assert(html.includes("widget.gcode"), "file missing from the list");
  assert(html.includes("1h 2m"), "printing time missing");
  const buttons = byId["files"].querySelectorAll("button[data-reprint]");
  assert(buttons.length === 2, "want 2 reprint buttons, got " + buttons.length);
  sent.length = 0;
  buttons[0].addEventListener("x", buttons[0].fn);
  byId["files"]._reprint[0].fn({ currentTarget: buttons[0] });
  assert(sent.some(m => m.type === "reprint" && m.filename === "widget.gcode"),
         "reprint posted nothing: " + JSON.stringify(sent));
});
must("an empty printer file list says so rather than looking broken", () => {
  deliver({ type: "files", files: [], detail: [] });
  assert(byId["files"].innerHTML.includes("nothing stored"), byId["files"].innerHTML);
});
must("the last-slice hint reflects what the plugin captured", () => {
  deliver({ type: "state", last_slice: null, status: STATES.idle });
  assert(byId["lastinfo"].textContent.includes("no slice captured"),
         byId["lastinfo"].textContent);
  deliver({ type: "state", last_slice: "widget.gcode.3mf", status: STATES.idle });
  assert(byId["lastinfo"].textContent.includes("widget.gcode.3mf"),
         byId["lastinfo"].textContent);
});

console.log("\nSettings");
must("saved settings are painted back into the form", () => {
  deliver({ type: "cfg", cfg: { host: "10.0.0.9", port: 8898, serial: "SN1",
                                check_code: "CC", poll_seconds: 5, leveling: false } });
  assert(byId["host"].value === "10.0.0.9", "host: " + byId["host"].value);
  assert(byId["serial"].value === "SN1", "serial");
  assert(byId["code"].value === "CC", "check code");
  assert(byId["clevel"].checked === false, "leveling checkbox");
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

check("no null-element lookups fired during the run", errors.length === 0,
      errors.join("; "));

console.log("");
if (failures) {
  console.log(failures + " check(s) failed");
  process.exit(1);
}
console.log("all page checks passed");
