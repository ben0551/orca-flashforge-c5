/*
 * Drives the MakerWorld tab's JavaScript outside OrcaSlicer.
 *
 * Same idea as page_harness.js: the page is one inline script that talks to the
 * plugin over orca.postMessage / orca.onMessage. This runs it in a DOM stub so
 * the message handling, the rendering and the button wiring can be checked
 * without a slicer.
 *
 *   node tests/converter_harness.js <page.js>
 */
"use strict";
const fs = require("fs");
const vm = require("vm");

const JS_SOURCE = fs.readFileSync(process.argv[2], "utf8");
const idsPath = process.argv[3];

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
// Every element the page looks up has to exist: a null property write throws and
// aborts the whole inline script, which is the failure this catches.
function makeEl(id) {
  const el = {
    id: id, _listeners: {}, style: {}, dataset: {}, children: [], parentNode: null,
    className: "", value: "", checked: true, disabled: false, hidden: false,
    src: "", _text: "", _html: "",
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); },
    dispatch(t, ev) {
      (this._listeners[t] || []).forEach(fn => fn(ev || { currentTarget: this, target: this }));
    },
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      contains(c) { return this._set.has(c); },
      toggle(c, on) { if (on) { this._set.add(c); } else { this._set.delete(c); } },
    },
    click() { this.dispatch("click"); },
    querySelectorAll() { return []; },
    querySelector() { return null; },
    setAttribute(k, v) { this.dataset[k] = v; },
    getAttribute(k) { return this.dataset[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    focus() {}, scrollIntoView() {},
    removeAttribute(k) { delete this[k]; delete this.dataset[k]; },
  };
  Object.defineProperty(el, "textContent", {
    configurable: true,
    get() { return el._text; },
    set(v) { el._text = v === null || v === undefined ? "" : String(v); },
  });
  Object.defineProperty(el, "innerHTML", {
    configurable: true,
    get() { return el._html; },
    set(v) { el._html = v === null || v === undefined ? "" : String(v); },
  });
  return el;
}

const FALLBACK_IDS = ["banner", "token", "saveToken", "url", "resolve", "pick",
             "pickTitle", "plates", "path", "convertPath", "report"];

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
// Mirrors the markup: these two start hidden and the page only ever reveals them.
byId["pick"].hidden = true;
byId["report"].hidden = true;

const sent = [];
const listeners = [];
const timers = [];
const errors = [];
const doc = {
  getElementById: id => byId[id] || null,
  // Enough of an element for esc(): textContent in, escaped innerHTML out.
  createElement: () => {
    const span = makeEl("span");
    Object.defineProperty(span, "innerHTML", {
      configurable: true,
      get() {
        return String(span._text).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                                  .replace(/>/g, "&gt;");
      },
    });
    return span;
  },
};

// The page reads a dropped file with readAsDataURL, so the stub has to supply
// one. The payload is whatever the fake file carries, so the test can assert on
// exactly the bytes the plugin would receive.
let lastRead = null;
function FileReader() {
  this.readAsDataURL = (blob) => {
    lastRead = blob;
    this.result = "data:application/octet-stream;base64," +
                  ((blob && blob.b64) || "");
    this.onload();
  };
  this.readAsArrayBuffer = () => {
    throw new Error("the page should read a picked file with readAsDataURL");
  };
}

const ctx = {
  document: doc, window: {}, Number, Math, JSON, String, Boolean, Date, Array, Object,
  console: { log: () => {}, error: (...a) => errors.push(a.join(" ")) },
  orca: { postMessage: o => sent.push(o), onMessage: fn => listeners.push(fn) },
  setInterval: () => 1, clearInterval: () => {},
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: () => {},
  FileReader: FileReader,
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(JS_SOURCE, ctx);
const deliver = m => listeners.forEach(fn => fn(m));

// The plates list is generated as markup and clicked through delegation, so read
// the buttons back out of what the page produced.
function plateButtons() {
  const out = [];
  const re = /data-instance="([^"]*)"[^>]*data-title="([^"]*)"/g;
  let m;
  while ((m = re.exec(byId["plates"].innerHTML)) !== null) {
    out.push({ instance: m[1], title: m[2] });
  }
  return out;
}
function clickPlate(index) {
  const buttons = plateButtons();
  const wanted = buttons[index];
  byId["plates"].dispatch("click", {
    target: {
      closest: sel => (sel === "button[data-instance]" && wanted ? {
        getAttribute: k => (k === "data-instance" ? wanted.instance : wanted.title),
      } : null),
    },
  });
}

// --- tests ---------------------------------------------------------------- //

console.log("\n3mf Converter page start-up");
must("the page posts hello on load", () => {
  assert(sent.some(m => m.type === "hello"), "no hello posted");
});
must("every element the script touches exists", () => {
  assert(errors.length === 0, "console.error: " + errors.join("; "));
});
must("the banner starts hidden rather than mid-message", () => {
  assert(byId["banner"].style.display === "none",
         "banner display: " + byId["banner"].style.display);
});
must("the plate list and the report start hidden", () => {
  assert(byId["pick"].hidden === true && byId["report"].hidden === true, "");
});

console.log("\nSaving the token");
must("Save token posts what was typed", () => {
  byId["token"].value = "  tok-123  ";
  sent.length = 0;
  byId["saveToken"].dispatch("click");
  const m = sent.find(x => x.type === "save_token");
  assert(m && m.token === "tok-123", JSON.stringify(m));
});
must("a saved token is confirmed and never echoed back into the field", () => {
  deliver({ type: "state", token_set: true, printer: "Flashforge Creator 5 Pro" });
  assert(byId["token"].placeholder === "(saved)", byId["token"].placeholder);
  assert(byId["token"].value === "  tok-123  ",
         "the page rewrote the token field: " + JSON.stringify(byId["token"].value));
  assert(byId["banner"].textContent.indexOf("Flashforge Creator 5 Pro") >= 0,
         "the printer being converted for is not named: " + byId["banner"].textContent);
});

console.log("\nResolving a link");
must("Resolve with an empty field explains itself and posts nothing", () => {
  byId["url"].value = "";
  sent.length = 0;
  byId["resolve"].dispatch("click");
  assert(sent.length === 0, "posted anyway: " + JSON.stringify(sent));
  assert(byId["banner"].className === "err", "no error shown");
  assert(/link/i.test(byId["banner"].textContent), byId["banner"].textContent);
});
must("Resolve posts the link and disables the buttons while it works", () => {
  byId["url"].value = "https://makerworld.com/en/models/15106-benchy";
  sent.length = 0;
  byId["resolve"].dispatch("click");
  const m = sent.find(x => x.type === "resolve");
  assert(m && m.url.indexOf("makerworld.com") >= 0, JSON.stringify(m));
  assert(byId["resolve"].disabled === true, "the button stayed live");
});
must("a resolution renders one row per plate with a Convert button", () => {
  deliver({ type: "resolved", title: "3D Benchy", plates: [
    { instance_id: 14865, title: "Standard Benchmark Settings",
      filament_weight: 12.5, size: 4871651 },
    { instance_id: 122577, title: "16.5 minute benchy", filament_weight: 0, size: 0 },
  ] });
  const html = byId["plates"].innerHTML;
  assert(byId["pick"].hidden === false, "the plate list stayed hidden");
  assert(byId["pickTitle"].innerHTML.indexOf("3D Benchy") >= 0, "no design title");
  assert(html.indexOf("Standard Benchmark Settings") >= 0, "plate 1 missing");
  assert(html.indexOf("13 g") >= 0, "weight not rounded: " + html);
  assert(html.indexOf("4.6 MB") >= 0, "size not shown in MB: " + html);
  assert(plateButtons().length === 2, "want 2 buttons, got " + plateButtons().length);
  assert(byId["resolve"].disabled === false, "the button was never re-enabled");
});
must("a design with no plates says so instead of showing an empty list", () => {
  deliver({ type: "resolved", title: "Empty", plates: [] });
  assert(byId["pick"].hidden === true, "the empty list was shown");
  assert(byId["banner"].className === "err", "no error shown");
});

console.log("\nConverting a plate");
must("clicking a plate's button posts that plate's id", () => {
  deliver({ type: "resolved", title: "3D Benchy", plates: [
    { instance_id: 14865, title: "Standard Benchmark Settings" },
    { instance_id: 122577, title: "16.5 minute benchy" },
  ] });
  sent.length = 0;
  clickPlate(1);
  const m = sent.find(x => x.type === "fetch");
  assert(m && m.instance_id === "122577", JSON.stringify(m));
  assert(m.title === "16.5 minute benchy", "the title was not carried: " + m.title);
});
must("a second click while busy is ignored", () => {
  sent.length = 0;
  clickPlate(0);
  assert(sent.length === 0, "a second conversion was started: " + JSON.stringify(sent));
});
must("a completed conversion reports what was kept and what was replaced", () => {
  deliver({ type: "report", opened: true, detail: "",
            path: "/plugins/converted/3D Benchy - Flashforge Creator 5 Pro.3mf",
            report: { printer: "Flashforge Creator 5 Pro", source_printer: "Bambu Lab P1P",
                      kept: 284, replaced: 105, dropped: 3,
                      kept_examples: ["layer_height", "sparse_infill_pattern"] } });
  const html = byId["report"].innerHTML;
  assert(byId["report"].hidden === false, "the report stayed hidden");
  assert(html.indexOf("284") >= 0, "the kept count is missing: " + html);
  assert(html.indexOf("105") >= 0, "the replaced count is missing");
  assert(html.indexOf("layer height") >= 0, "the kept keys are not named in words: " + html);
  assert(html.indexOf("Bambu Lab P1P") >= 0, "the designer's printer is not named");
  assert(byId["banner"].className === "ok", "the success was not shown as success");
  assert(byId["resolve"].disabled === false, "the buttons stayed disabled");
});
must("a conversion that could not be opened still says where the file is", () => {
  // The bug this covers: posting the report and the failure as two messages, so
  // the failure replaced the report before it could be read.
  deliver({ type: "report", opened: false,
            detail: "no running OrcaSlicer instance on the session bus",
            path: "/plugins/converted/thing.3mf",
            report: { printer: "Flashforge Creator 5 Pro", source_printer: "Bambu Lab P1P",
                      kept: 10, replaced: 4, dropped: 0, kept_examples: [] } });
  const text = byId["banner"].textContent;
  assert(byId["banner"].className === "err", "the failure was not shown as an error");
  assert(text.indexOf("no running OrcaSlicer instance") >= 0,
         "the reason is missing: " + text);
  assert(text.indexOf("Open Project") >= 0,
         "the fallback does not name a menu item that exists: " + text);
  assert(text.indexOf("/plugins/converted/thing.3mf") >= 0,
         "the path to open by hand is missing: " + text);
  assert(byId["report"].hidden === false, "the report was dropped");
});
must("an error from the plugin reaches the banner visibly", () => {
  deliver({ type: "error", message: "A BambuCloud token is needed to download." });
  assert(byId["banner"].style.display === "block", "the banner stayed hidden");
  assert(byId["banner"].className === "err", "not shown as an error");
  assert(byId["banner"].textContent.indexOf("token") >= 0, "the message was lost");
  assert(byId["resolve"].disabled === false, "the buttons stayed disabled after an error");
});

console.log("\nConverting a local file");
must("Convert & open with an empty field explains itself", () => {
  byId["path"].value = "";
  sent.length = 0;
  byId["convertPath"].dispatch("click");
  assert(sent.length === 0, "posted anyway");
  assert(/path/i.test(byId["banner"].textContent), byId["banner"].textContent);
});
must("Convert & open posts the path", () => {
  byId["path"].value = "/home/me/Downloads/benchy.3mf";
  sent.length = 0;
  byId["convertPath"].dispatch("click");
  const m = sent.find(x => x.type === "convert_path");
  assert(m && m.path === "/home/me/Downloads/benchy.3mf", JSON.stringify(m));
});

console.log("\nUntrusted text");
must("a title with quotes and markup cannot break the generated rows", () => {
  deliver({ type: "resolved", title: 'x" onmouseover="evil()', plates: [
    { instance_id: 1, title: '<img src=x onerror="evil()"> "quoted"', size: 0 },
  ] });
  const html = byId["plates"].innerHTML;
  assert(html.indexOf("<img") < 0, "raw markup reached the page: " + html);
  assert(html.indexOf('onmouseover="evil()"') < 0, "a quote escaped its attribute: " + html);
  assert(html.indexOf("&lt;img") >= 0, "the markup was not escaped: " + html);
  assert(plateButtons().length === 1, "the row was not rendered");
});

console.log("\nDropping a .3mf");
must("a dropped .3mf is read and handed to the plugin", () => {
  sent.length = 0;
  var file = { name: "Model.3mf", b64: "UEsDBAoAAAAA" };
  byId["drop"].dispatch("drop", {
    preventDefault() {},
    dataTransfer: { files: [file] },
  });
  assert(lastRead === file, "the dropped file was not read");
  var msg = sent.filter(m => m.type === "convert_data")[0];
  assert(msg, "nothing was posted: " + JSON.stringify(sent));
  assert(msg.name === "Model.3mf", "name: " + msg.name);
  assert(msg.data === "UEsDBAoAAAAA",
         "the payload is not the file's own bytes: " + msg.data);
});
must("anything that is not a .3mf is refused before it is sent", () => {
  sent.length = 0;
  byId["drop"].dispatch("drop", {
    preventDefault() {},
    dataTransfer: { files: [{ name: "notes.txt", b64: "abc" }] },
  });
  assert(sent.length === 0, "it was sent anyway: " + JSON.stringify(sent));
  assert(byId["banner"].className === "err", "no error shown");
  assert(byId["banner"].textContent.indexOf("notes.txt") >= 0,
         "the banner does not name the file: " + byId["banner"].textContent);
});
must("the drop zone highlights while a file is over it", () => {
  byId["drop"].dispatch("dragover", { preventDefault() {} });
  assert(byId["drop"].classList.contains("over"), "no highlight on dragover");
  byId["drop"].dispatch("drop", {
    preventDefault() {},
    dataTransfer: { files: [{ name: "x.3mf", b64: "AA" }] },
  });
  assert(!byId["drop"].classList.contains("over"),
         "the highlight survived the drop");
});
must("choosing a file opens the picker", () => {
  var opened = false;
  byId["file"].click = function () { opened = true; };
  byId["browse"].dispatch("click", { preventDefault() {} });
  assert(opened, "the picker was not opened");
});
must("a file chosen through the picker takes the same route", () => {
  sent.length = 0;
  byId["file"].dispatch("change", { target: { files: [{ name: "picked.3mf", b64: "QUJD" }] } });
  var msg = sent.filter(m => m.type === "convert_data")[0];
  assert(msg && msg.name === "picked.3mf" && msg.data === "QUJD",
         "posted: " + JSON.stringify(sent));
});

console.log("\nWhat the conversion reports");
must("the report names the plates, the tools and the machine G-code", () => {
  deliver({ type: "report", path: "/tmp/x.3mf", opened: true,
    report: { printer: "Flashforge Creator 5 Pro", source_printer: "Bambu Lab P1P",
              kept: 12, replaced: 9, dropped: 3,
              kept_examples: ["layer_height"],
              plates: [{ index: "1", filaments: [
                { type: "PLA", colour: "#FF0000", used_g: "12.5" }] }],
              filaments: [{ tool: 1, type: "PLA", colour: "#FF0000",
                            profile: "Flashforge PLA" }],
              gcode: [{ which: "start", lines: 42,
                        head: ["G28", "G1 Z5 F600"] }] } });
  var html = byId["report"].innerHTML;
  assert(html.indexOf("plate 1") >= 0, "no plate row: " + html);
  assert(html.indexOf("12.5 g") >= 0, "no filament weight: " + html);
  assert(html.indexOf("tool 1") >= 0, "no tool row: " + html);
  assert(html.indexOf("Flashforge PLA") >= 0, "no filament profile: " + html);
  assert(html.indexOf("42 line") >= 0, "no G-code line count: " + html);
  assert(html.indexOf("G28") >= 0, "no G-code head: " + html);
});
must("a file with no plates or G-code still reports cleanly", () => {
  deliver({ type: "report", path: "/tmp/x.3mf", opened: true,
    report: { printer: "P", source_printer: "S", kept: 1, replaced: 1, dropped: 0,
              kept_examples: [], plates: [], filaments: [], gcode: [] } });
  var html = byId["report"].innerHTML;
  assert(html.indexOf("Saved as") >= 0, "the saved path went missing: " + html);
});

console.log("\nRe-opening an earlier conversion");
must("the list renders one row per file, with its size and age", () => {
  deliver({ type: "files", files: [
    { name: "Benchy - FF.3mf", path: "/plugins/converted/Benchy - FF.3mf",
      size: 3145728, modified: Date.now() / 1000 - 120 },
    { name: "Tower.3mf", path: "/plugins/converted/Tower.3mf",
      size: 2048, modified: Date.now() / 1000 - 90000 }] });
  const html = byId["convFiles"].innerHTML;
  assert(html.indexOf("Benchy - FF.3mf") >= 0, "no file row: " + html);
  assert(html.indexOf("3.0 MB") >= 0, "size not humanised: " + html);
  assert(html.indexOf("2 kB") >= 0, "small size not humanised: " + html);
  assert(html.indexOf("min ago") >= 0, "age not shown: " + html);
  assert(!byId["convertList"].hidden, "the panel stayed hidden");
});
must("clicking Open posts that file's path", () => {
  sent.length = 0;
  byId["convFiles"].dispatch("click", {
    target: { getAttribute: k => k === "data-open" ? "/plugins/converted/Tower.3mf" : null },
  });
  const msg = sent.filter(m => m.type === "open_file")[0];
  assert(msg && msg.path === "/plugins/converted/Tower.3mf",
         "posted: " + JSON.stringify(sent));
});
must("a click that is not on a file's button posts nothing", () => {
  sent.length = 0;
  byId["convFiles"].dispatch("click", { target: { getAttribute: () => null } });
  assert(sent.length === 0, "posted anyway: " + JSON.stringify(sent));
});
must("a closed file is reported with a way to open it by hand", () => {
  deliver({ type: "opened", path: "/x.3mf", opened: false, detail: "dbus-send unavailable" });
  assert(byId["banner"].className === "err", "not shown as an error");
  assert(byId["banner"].textContent.indexOf("Open Project") >= 0,
         "no fallback offered: " + byId["banner"].textContent);
});
must("an empty list hides the panel rather than showing an empty box", () => {
  deliver({ type: "files", files: [] });
  assert(byId["convertList"].hidden, "the panel stayed visible");
});

console.log("\nThe audit behind the counts");
must("the full key lists are rendered, not only the counts", () => {
  deliver({ type: "report", path: "/tmp/x.3mf", opened: true,
    report: { printer: "P", source_printer: "S", kept: 2, replaced: 1, dropped: 1,
              kept_examples: ["layer_height"],
              kept_keys: ["layer_height", "wall_loops"],
              replaced_keys: ["machine_start_gcode"],
              dropped_keys: ["printer_model"],
              plates: [], filaments: [], gcode: [],
              warnings: [{ level: "check", message: "1 extruder and this printer has 4." }] } });
  const html = byId["report"].innerHTML;
  assert(html.indexOf("Kept from the designer") >= 0, "no kept section");
  assert(html.indexOf("Replaced by your printer") >= 0, "no replaced section");
  assert(html.indexOf("Reset to OrcaSlicer&#39;s defaults") >= 0 ||
         html.indexOf("Reset to OrcaSlicer's defaults") >= 0, "no reset section");
  assert(html.indexOf("wall_loops") >= 0, "kept keys missing: " + html);
  assert(html.indexOf("machine_start_gcode") >= 0, "replaced keys missing");
  assert(html.indexOf("printer_model") >= 0, "dropped keys missing");
  assert(html.indexOf("1 extruder and this printer has 4.") >= 0, "no warning shown");
});
must("a report with no lists still renders its summary", () => {
  deliver({ type: "report", path: "/tmp/x.3mf", opened: true,
    report: { printer: "P", source_printer: "S", kept: 0, replaced: 0, dropped: 0,
              kept_examples: [], plates: [], filaments: [], gcode: [], warnings: [] } });
  const html = byId["report"].innerHTML;
  assert(html.indexOf("Saved as") >= 0, "the saved path went missing: " + html);
  assert(html.indexOf("class='keys'") < 0 && html.indexOf('class="keys"') < 0,
         "an empty list rendered a box");
});
must("the G-code says how much of it is not being shown", () => {
  deliver({ type: "report", path: "/tmp/x.3mf", opened: true,
    report: { printer: "P", source_printer: "S", kept: 0, replaced: 0, dropped: 0,
              kept_examples: [], plates: [], filaments: [], warnings: [],
              gcode: [{ which: "start", lines: 31, head: [";start_gcode", "G28"],
                        more: 29 }] } });
  const html = byId["report"].innerHTML;
  assert(html.indexOf("31 line") >= 0, "no line count: " + html);
  assert(html.indexOf("first 2 shown") >= 0, "no truncation note: " + html);
  assert(html.indexOf("&quot;&quot;") < 0, "escaped quotes leaked: " + html);
});
must("a finished conversion refreshes the list", () => {
  sent.length = 0;
  deliver({ type: "report", path: "/tmp/x.3mf", opened: true,
    report: { printer: "P", source_printer: "S", kept: 0, replaced: 0, dropped: 0,
              kept_examples: [], plates: [], filaments: [], gcode: [], warnings: [] } });
  assert(sent.some(m => m.type === "list_files"),
         "the list was not refreshed: " + JSON.stringify(sent));
});

console.log("\nThe token settings stay out of the way");
must("there is a token panel to open", () => {
  assert(byId["tokenPanel"], "there is no token panel");
});
must("being told a token is needed opens it, and says why", () => {
  byId["tokenPanel"].open = false;
  deliver({ type: "need_token", message: "A BambuCloud token is needed to download." });
  assert(byId["tokenPanel"].open, "the panel stayed closed");
  assert(byId["banner"].className === "err", "the reason was not shown");
  assert(byId["banner"].textContent.indexOf("token") >= 0,
         "the banner does not mention a token: " + byId["banner"].textContent);
});
must("a saved token opens it too, so the confirmation is visible", () => {
  byId["tokenPanel"].open = false;
  byId["token"].placeholder = "";
  deliver({ type: "state", token_set: true, printer: "P" });
  assert(byId["tokenPanel"].open, "the panel stayed closed");
  assert(byId["token"].placeholder === "(saved)", "not shown as saved");
});
must("no token means it is left closed", () => {
  byId["tokenPanel"].open = false;
  deliver({ type: "state", token_set: false, printer: "P" });
  assert(!byId["tokenPanel"].open, "it opened with nothing saved");
});

console.log("\nThe typed-path fallback");
must("the path box is out of the way until that link is followed", () => {
  byId["pathRow"].hidden = true;
  byId["pathFallback"].hidden = false;
  byId["showPath"].dispatch("click", { preventDefault: () => {} });
  assert(!byId["pathRow"].hidden, "the box did not appear");
  assert(byId["pathFallback"].hidden, "the link stayed there after being used");
});

console.log("\nA typed path");
must("Enter converts the path, so the button is an alternative", () => {
  sent.length = 0;
  byId["path"].value = "/tmp/project.3mf";
  let prevented = false;
  byId["path"].dispatch("keydown", { key: "Enter", preventDefault: () => { prevented = true; } });
  const msg = sent.filter(m => m.type === "convert_path")[0];
  assert(msg && msg.path === "/tmp/project.3mf", "posted: " + JSON.stringify(sent));
  assert(prevented, "the keypress was allowed to do whatever it would have done");
});
must("any other key in the path box does nothing", () => {
  sent.length = 0;
  byId["path"].dispatch("keydown", { key: "a", preventDefault: () => {} });
  assert(sent.length === 0, "posted: " + JSON.stringify(sent));
});
must("the button still converts the path", () => {
  sent.length = 0;
  byId["convertPath"].dispatch("click");
  assert(sent.some(m => m.type === "convert_path"), "the button stopped working");
});

check("no null-element lookups fired during the run", errors.length === 0,
      errors.join("; "));

console.log("");
if (failures) {
  console.log(failures + " check(s) failed");
  process.exit(1);
}
console.log("all 3mf Converter page checks passed");
