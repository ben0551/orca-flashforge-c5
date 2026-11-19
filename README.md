# Creator 5 plugins for OrcaSlicer

Two small plugins that close the two gaps between a FlashForge Creator 5 / 5 Pro
and OrcaSlicer. **They are separate plugins — installed separately, versioned
separately and released separately.** Neither imports or depends on the other, so
you can update one without touching the other, install one without the other, and
remove one without breaking the other.

FlashForge's Creator 5 firmware exposes a complete local HTTP API on port 8898 —
status, temperature, the material station, the stored file list, upload, start
print, pause/resume/cancel, light — but the printer presents itself to a slicer as
a cloud-bound Bambu-style device, so OrcaSlicer will not upload to it or show its
status. This plugin speaks the printer's own LAN protocol from inside OrcaSlicer.

Each is one Python file in the standard OrcaSlicer plugin format
([plugin docs](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_development.html)),
with no third-party dependencies — nothing is downloaded at plugin load time.

| Plugin file | Listing name | Adds | Tab |
|---|---|---|---|
| `flashforge-c5_any.py` | **FlashForge Creator 5 Device Tab** | a device tab and a printer agent | **FlashForge C5** |
| `3mf-converter_any.py` | **3mf Converter** | a converter tab | **3mf Converter** |

The listing name and the tab name differ on purpose: the hub list is where a
stranger has to work out what the plugin does, so it says so; the tab is what you
read while working, so it stays short.

## Requirements

- **OrcaSlicer 2.5.0 or newer.** The Python plugin system landed in 2.5.0; older
  builds will not list the plugin at all.
- The printer on the same LAN, in LAN/offline mode, with its **serial number** and
  **LAN check code** to hand (printer touchscreen → Device → About for the serial;
  Network for the check code). **If you have already set this printer up in
  OrcaSlicer, you do not need to type any of it** — see *Reading OrcaSlicer's own
  printer settings* below.
- For `3mf-converter` only, and only when you use a link rather than a file you
  already have: a **BambuCloud token**. See *The 3mf Converter tab* below.

## What you get

**`flashforge-c5`** — the printer itself.

| Capability | Type | What it does |
|---|---|---|
| **FlashForge C5** tab | `pages` | Live status, temperatures, the 4-slot material station, a **live camera view**, job progress, pause/resume/cancel, light and clear-platform, and the files stored on the printer with a plate **preview** and one-click **reprint**. |
| **FlashForge Creator 5** | `printer-connection` | Makes OrcaSlicer's own **Send to printer** / **Print** buttons work against the Creator 5. This is the sending path — the tab watches and drives the machine. |

**`3mf-converter`** — models from MakerWorld.

| Capability | Type | What it does |
|---|---|---|
| **3mf Converter** tab | `pages` | Paste a MakerWorld link (or drop a `.3mf`) and get the designer's project rebuilt for the printer you have selected, then opened in OrcaSlicer ready to slice. |

## Install

Install the plugin you want, or both. Each is independent, and each goes in its
own folder.

1. Open **Plugins** in OrcaSlicer, then **Browse plugins ▾ → Install local
   plugin**, and pick `flashforge-c5_any.py` and/or `3mf-converter_any.py`.

   Or copy each file into its own folder under the OrcaSlicer data directory, e.g.
   `%APPDATA%\OrcaSlicer\orca_plugins\flashforge-c5\flashforge-c5_any.py` and
   `%APPDATA%\OrcaSlicer\orca_plugins\3mf-converter\3mf-converter_any.py`. A
   plugin folder must contain exactly one entry file (one `.py` or one `.whl`), so
   **the two files must not share a folder** — one folder holding both will load
   neither.

   The `_any` suffix is not decoration — Orca Cloud's hub requires a target-OS
   suffix on uploaded plugin files, and `any` is its universal marker for a pure
   Python plugin. The same filename works for a local install, so there is only
   one file to keep track of.

2. OrcaSlicer prompts for the filesystem and network access the plugin declares.
   Accept both — without the network permission the plugin cannot reach the
   printer.

3. Open the **FlashForge C5** tab. If OrcaSlicer already knows this printer, the
   connection panel arrives filled in and says so. Otherwise fill in the address,
   serial number and LAN check code and press **Save & test**. Settings are
   written to `settings.json` next to the plugin, mode 0600, and go nowhere
   except your printer.

4. Slice a model, then use OrcaSlicer's own **Send to printer** / **Print** button
   on the preview screen. This plugin supplies the printer agent behind that
   button, so the Creator 5 behaves like a built-in target. Use the tab to watch
   the job and drive the machine.

## Using the tab

The tab watches and drives the printer. Sending is OrcaSlicer's own button.

- **Files on the printer** lists what is stored on the device with the printing
  time and filament weight the firmware reports. **Preview** fetches the plate
  thumbnail the printer keeps beside a file, so you can see what it is before
  choosing it. **Reprint** restarts a stored file with the slot mapping rebuilt
  from the printer's own metadata.
- **Job control** pauses, resumes, cancels, toggles the light and clears the
  platform. `/control` acknowledges every command whether or not it does
  anything, so each one is confirmed by re-reading the printer's state, and the
  tab says plainly when a command was accepted but demonstrably ignored.
- **Camera** switches on the printer's live view. The firmware gates its MJPEG
  server: it serves nothing until the stream is started, and it holds **one
  viewer at a time**, so the tab opens the stream when you ask for it and closes
  it when you stop or leave — which is also what frees the camera for
  OrcaSlicer's own device panel. Verified at 640×480, roughly 2 frames a second.
  It is the same feed the printer's own screen shows, so it is only private on
  your LAN to the extent your LAN is.

Two things the plugin does that the raw API does not:

- **OrcaSlicer writes `.gcode.3mf` bundles, and the Creator 5 cannot start one.**
  It uploads `code: 0`, appears in the file list, and then fails on the printer
  with *"print file error, please check"* because the firmware tries to unzip it.
  The plugin extracts `Metadata/plate_1.gcode` and sends that instead, so the file
  the printer ends up holding has the name it will be known by.
- **The plate preview.** The firmware keeps a thumbnail beside every stored job
  and serves it from `/gcodeThumb`; the tab fetches it on demand and shows it
  inline.

## The 3mf Converter tab

A MakerWorld `.3mf` is a **Bambu Studio project**. Opening one as it comes is not
an error — OrcaSlicer ships the Bambu profiles, so the file loads and slices
happily, but it slices for the *designer's* machine: their bed height, their
machine limits, their start and end G-code. Everything they tuned is buried under
a printer you do not own, and switching the printer afterwards throws the settings
away.

The **3mf Converter** tab rebuilds the project for whatever printer is selected in
OrcaSlicer. Paste a link and pick a plate, and it downloads the plate, converts it,
and opens the result in the running OrcaSlicer ready to slice.

**A `.3mf` you already have is easier still — drop it onto the tab**, or use the
file chooser in the drop zone; no token is involved. Both convert on their own, with no
button to press: a drop ends when the file lands and a picker when the dialog closes, so
there is nothing left to confirm.

Typing a path is not offered up front, because nobody chooses it when dragging works.
It stays behind a one-line link under the drop zone for the case where neither route
does — a webview that does not forward file drops or open a file dialog leaves a path
as the only way in. On a normal install you will never need it.

**Getting a token, if you pull a link.** It is the same BambuCloud token Bambu
Studio uses, and it is a one-off. The field is **collapsed under "Token settings"**
until you need it, and it opens by itself if you ask for a link without one — most of
what this plugin does needs no token, and a credentials box sitting open reads as a
requirement. The steps:

1. Sign in at `makerworld.com` in a browser.
2. Press **F12** for Developer Tools.
3. **Application** (Chrome, Edge) or **Storage** (Firefox) → **Cookies** →
   `makerworld.com`.
4. Click the row named `token` and copy its **Value**.
5. Paste it into the tab and press **Save token**.

No cookie called `token`? Reload the page, open the **Network** tab, click any
request to makerworld.com, and keep the `token=…` part of its `cookie:` request
header. Treat it like a password — it is your whole Bambu Cloud session — and it
lapses after roughly 90 days, at which point downloads start failing and you paste
a fresh one. It lives in `settings.json` beside the plugin (mode 0600) and goes to
MakerWorld and nowhere else.

**What the report tells you.** After a conversion the tab says what came *out*, not
just how many settings moved:

- **Every plate** in the file and the filament each one uses.
- **The tools** the project ends up with, and the filament and profile behind each.
- **The machine start and end G-code** that was swapped for your printer's, capped to
  a readable head with the line count, since these blocks run to dozens of lines of
  fan and temperature setup. A MakerWorld `.3mf` is unsliced and carries no `.gcode`
  at all, so the G-code shown is the machine block the conversion actually replaced.
- **The full audit** — an expandable list of every key that was kept from the
  designer, every key replaced by your printer, and every key reset to OrcaSlicer's
  defaults. The counts in the summary are only worth trusting if the lists behind
  them can be read.
- **A warning when the tool counts differ**, because that is where filament ends up
  on the wrong extruder.

**Opening an earlier conversion.** Converted projects are kept in a folder inside
OrcaSlicer's own data directory, several levels down and awkward to reach. The tab
lists them — newest first, with size and age — and opens one again with a click.

**What is kept and what is replaced.** OrcaSlicer already knows which settings
belong to a machine: `Preset::printer_options()` is that list, and the plugin uses
it rather than a hand-written one. Every key the machine owns takes the selected
printer's value; **every other key keeps the designer's**, so layer height, walls,
infill pattern and density, shells, seam, supports, elephant-foot compensation,
temperatures and colours all survive. The tab reports the counts and names the
important ones, rather than asking you to take it on trust.

Two details that matter:

- **Per-extruder settings are resized to the project's own extruder count.** The
  Creator 5 Pro models its four-slot material station as four extruders; writing
  a four-entry array into a one-extruder project crashes OrcaSlicer on load, and
  leaving the designer's `extruder_offset` in place shifts the part. Both are
  handled.
- **A machine setting the printer leaves at its default is dropped, not kept.**
  Leaving the designer's value there would silently override your printer's own
  default.

**How the converted project reaches you.** The plugin API is read-only — there is
no "load this file" call — so the tab hands the path to the *running* OrcaSlicer
through the same message a second launch would send it (D-Bus on Linux,
LaunchServices on macOS, `WM_COPYDATA` on Windows). The converted file is written
to `converted/` beside the plugin. If that hand-off cannot be made, the tab says
so and prints the path, and you can open it with **File → Open Project…**.

## What the printer will not do

**A stored file cannot be downloaded.** The Creator 5's whole HTTP API is
`/product`, `/control`, `/detail`, `/uploadGcode`, `/gcodeList`, `/printGcode`
and `/gcodeThumb`. There is no route that reads a file back, no FTP, and no TCP
command channel on this model, so a job sitting on the printer cannot be pulled
off it to view or edit. The `/gcodeThumb` image above is the only view of a
stored file the device will give up. To edit a model, go back to the source you
sliced it from.

## Using OrcaSlicer's own buttons (the printer agent)

This path is the cleanest when it works, but it depends on an experimental
OrcaSlicer feature:

1. **Preferences → Developer → Experimental Features → Use printer agents instead
   of print hosts.**
2. In the printer preset, **Basic information → Advanced → Printer Agent**, choose
   **FlashForge Creator 5**, and set the preset's print host to the printer's
   address with the LAN check code as its API key.
3. The printer **serial number** is read from OrcaSlicer's own preset when it is
   there (see below). If it is not, set it once on the FlashForge C5 tab —
   OrcaSlicer passes the address and access code to an agent but never the serial.

OrcaSlicer's **Send to printer** then uploads with the same bundle-unwrapping the
tab does, and **Print** uploads and starts the job in one step.

## Reading OrcaSlicer's own printer settings

OrcaSlicer 2.5.0 has a built-in FlashForge print host, so a printer set up there
has already had all three values typed once. They live on the printer preset:

| OrcaSlicer field | Preset key | Used as |
|---|---|---|
| **Hostname, IP or URL** | `print_host` | `host` |
| **Serial Number** | `flashforge_serial_number` | `serial` |
| **API Key / Password** | `printhost_apikey` | `check_code` |

Open that dialog with the **Connection** button (the signal-strength icon) beside
**Printer**, set **Host Type** to **Flashforge**, and the three fields appear.
Changing the address field there also runs a network discovery that fills in the
address *and* the serial number for you.

The plugin reads those keys on start-up through `orca.host.preset_bundle()` and
fills in anything its own settings file does not have. **Load from OrcaSlicer** in
the connection panel forces the overwrite. When it takes something it says which
values came from where, because a panel that fills itself in silently is
indistinguishable from one that was already right.

Two honest limits:

- The plugin API exposes the printer preset collection but **not** Physical
  Printer profiles, which is where a modern OrcaSlicer keeps these keys. So the
  read is best-effort: it looks on the selected printer preset, then in the
  bundle's resolved configuration, then at every printer preset in the collection.
  If none of them carry the values, the panel simply stays empty and you type them
  once, exactly as before.
- A value is only ever written into a blank. Loading from OrcaSlicer can replace
  a working configuration, so that is the one action that overwrites.

## Settings

| Key | Meaning |
|---|---|
| `host` | Printer LAN address (no `http://`) |
| `port` | Printer API port, default 8898 |
| `serial` | Serial number from the touchscreen |
| `check_code` | LAN check code from the touchscreen. A credential — never commit it |
| `leveling` | Default for "bed levelling before print" |
| `poll_seconds` | Status poll interval, default 3 |
| `bambu_token` | BambuCloud token, for downloading from MakerWorld. A credential — never commit it |

Environment overrides, handy for testing: `FFC5_HOST`, `FFC5_PORT`,
`FFC5_SERIAL`, `FFC5_CHECK_CODE`, `FFC5_BAMBU_TOKEN`.

`host`, `serial` and `check_code` can also be filled from OrcaSlicer's own printer
preset — see *Reading OrcaSlicer's own printer settings* above. The plugin's file
wins once a value is in it; the read only ever fills blanks.

## Publishing to the Orca Cloud hub

`HUB_LISTING.md` holds the listing text and the submission checklist. Two things
worth knowing before you start:

- **The hub is Nightly-only.** Orca Cloud's help page says plugins are a Beta
  feature that "currently work only with current OrcaSlicer Nightly builds" —
  stable releases will not show or load Orca Cloud plugins. That gate is separate
  from the plugin system itself, which is in 2.5.0 stable and is what loads this
  file locally. So the listing can go up now, but subscribers on 2.5.0 stable may
  not see it until they run a Nightly.
- **The filename suffix is a hard requirement.** Orca Cloud expects a target-OS
  suffix on uploaded plugin files; `any` is its universal marker. Hence
  `flashforge-c5_any.py`.

Publishing is then: connect the repo in **Shared Plugins → Edit plugin → GitHub
publishing**, fill the portal metadata, and publish a GitHub Release. The upload
runs over OIDC, so there is no token to store.

**There is one workflow per plugin**, because the two versions move
independently, and the release tag says which one is being published:

| Plugin | Workflow | Release tag | Version published |
|---|---|---|---|
| `flashforge-c5` | `.github/workflows/publish-flashforge-c5.yml` | `flashforge-v1.2.0` | `1.2.0` |
| `3mf-converter` | `.github/workflows/publish-3mf-converter.yml` | `3mf-v1.3.0` | `1.3.0` |

Each workflow ignores a tag that is not its own, so tagging one plugin never
republishes the other. Each plugin needs its **own** portal listing and its own
connection; the plugin `name` in its metadata — `FlashForge Creator 5 Device Tab`,
`3mf Converter` — is what tells them apart.

## Tests

```
./run_tests.sh
```

Three suites, none needing OrcaSlicer or a printer. Two cover `flashforge-c5`,
one covers `3mf-converter`:

- `tests/test_plugin.py` runs the real `flashforge-c5` plugin against a mock Creator 5
  (`tests/mock_printer.py`) and a stub `orca` module (`tests/fake_orca.py`), then
  drives the tab's message protocol exactly as the page's JavaScript does. It
  asserts on what the *printer* received — header casing, the `gcodeFile` part,
  the `fileSize` header, the extracted G-code bytes, the five-field mapping
  elements — not on what the plugin thinks it sent.
- `tests/test_converter.py` runs the real `3mf Converter` plugin end to end: the URL
  parsing, the download (against a stubbed transport, asserting the instance
  endpoint and the bearer header), the conversion, the saved file and the hand-off
  payload. The conversion is asserted key by key — that the designer's layer
  height, infill pattern, supports, temperatures and colours are unchanged, that
  the machine's limits, G-code and bed height replace theirs, that per-extruder
  arrays are resized rather than copied, and that the archive's geometry comes
  through byte for byte.
- `tests/test_page_js.py` extracts **each** plugin's tab JavaScript and runs it under Node in
  a DOM stub — the first against the plugin's real normalised `/detail` payload,
  the second against real conversion reports — checking the rendering, the button
  wiring, the file-row actions, and that a failure never replaces the report it
  belongs to.

## What is and is not verified

Verified by the test suites above:

- The upload wire contract: credentials as request headers, the `gcodeFile`
  multipart part, `fileSize`, `printNow=false`, levelling, and the extracted plain
  `.gcode` bytes rather than the bundle.
- `/gcodeThumb` returning a PNG, and the tab showing it inline against the file
  name.
- Reading OrcaSlicer's own printer preset: all three keys off the selected preset,
  a value that only appears in the bundle's resolved configuration, a printer
  preset that is not the selected one, the blank-only adoption rule, and the
  agent taking the serial from the preset. A build with no preset API at all is
  exercised too, since that has to degrade to the manual panel rather than fail.
- Reprint rebuilding the five-field mapping elements (0-based `toolId`, 1-based
  `slotId`) from the printer's own `gcodeListDetail`.
- `/detail` normalisation, including zeroing the job block when the printer is
  idle — the firmware pads those fields with uninitialised memory between states.
- Both `/control` payload shapes, with confirmation by re-reading `/detail`,
  because `/control` answers `Success` to any command including ones it ignores.
- The tab's rendering and message protocol, the file-row actions, and the printer
  agent's methods.
- **The MakerWorld conversion, against a real MakerWorld download and real
  OrcaSlicer.** A converted project was sliced by OrcaSlicer 2.4.2 with *no*
  `--load-settings` override at all and came out as a FlashForge Creator 5 Pro
  job — which is the point: the converted file has to carry the machine by itself,
  the way opening it as a project does. A control run of the same file unconverted
  sliced as a Bambu Lab P1P, with the designer's machine limits and G-code, and a
  controlled edit proved the designer's values survive conversion while the
  machine's replace theirs.
- **The camera frame parser, against the real printer.** Run over the live MJPEG
  stream from a Creator 5 Pro at `192.168.8.128:8080`: every frame a complete
  640×480 baseline JPEG with a full scan, consecutive frames all different — a
  live view rather than a frozen or truncated one, at roughly two frames a second.
- Zero third-party dependencies; standard library only.

Not verified here, because it needs the real application and the real printer:

- That OrcaSlicer 2.5.0 loads the plugin and renders the tabs. The plugin follows
  the documented API and passes a stub of it, but the stub is not OrcaSlicer.
- That the printer accepts an upload from this client. The request shape matches a
  client verified against Creator 5 Pro firmware 2.0.0, and the mock enforces that
  shape, but no bytes have been sent to a real machine from here.
- The printer-agent path end to end, which additionally depends on OrcaSlicer's
  experimental "use printer agents" mode.
- **The file hand-off against a live OrcaSlicer.** The payload format and the
  per-platform transport are taken from OrcaSlicer's own `InstanceCheck` and from a
  published plugin that uses the same route, and the Linux path is asserted against
  a stubbed `dbus-send` — but no converted file has been pushed into a running
  OrcaSlicer from here. If the hand-off is refused (the audit hook matches
  `process` targets verbatim, so a differently-resolved `dbus-send` would not
  match), the tab says so and prints the path to open by hand instead.
- **Whether the printer really stops its MJPEG server when this plugin lets go.**
  The `streamCtrl` open and close commands are asserted against the mock and come
  from a client verified against this firmware, but they have not been sent to a
  real machine from here. If the printer does not release the stream on
  disconnect, the camera stays held by whichever viewer last opened it — which the
  firmware's one-client-per-open rule makes the failure to watch for.
- **Whether `preset_bundle()` actually surfaces those keys on a real install.**
  The plugin API exposes the printer preset collection but *not* Physical Printer
  profiles, and a modern OrcaSlicer keeps `print_host`,
  `flashforge_serial_number` and `printhost_apikey` on the latter. The read tries
  the selected preset, the bundle's resolved configuration and every printer
  preset in turn, and falls back to the manual panel when none of them carry the
  values — but which of those paths answers on your machine has not been observed
  from here. The tab says which one it used.

## Known limitations

- The printer does not report the current job's tools for files already in
  storage, so **reprint** rebuilds the mapping from the file's own
  `gcodeListDetail` rather than letting you re-pick spools.
- The camera is exposed as a URL (`http://<host>:8080/?action=stream`) via the
  agent's camera accessors, not embedded in the tab.
- A modal dialog on the printer's touchscreen freezes its state machine, and
  `/control` will not clear it. **Clear platform** dismisses the completion
  dialog; anything else needs a press on the touchscreen.
- Status polling runs on the UI thread (the page asks for it), so it uses a short
  TCP probe first and a 6-second HTTP timeout to avoid stalling OrcaSlicer when
  the printer is off.
