# FlashForge Creator 5 Device Tab — an OrcaSlicer plugin

Send prints to a FlashForge Creator 5 / 5 Pro from inside OrcaSlicer and watch the
job there, instead of handing the file to FlashForge's own app or a web page.

FlashForge's Creator 5 firmware exposes a complete local HTTP API on port 8898 —
status, temperature, the material station, the stored file list, upload, start
print, pause/resume/cancel, light — but the printer presents itself to a slicer as
a cloud-bound Bambu-style device, so OrcaSlicer will not upload to it or show its
status. This plugin speaks the printer's own LAN protocol from inside OrcaSlicer.

It is one Python file in the standard OrcaSlicer plugin format
([plugin docs](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_development.html)),
with no third-party dependencies — nothing is downloaded at plugin load time. It
registers two capabilities: the device tab and the printer agent.

The listing name is **FlashForge Creator 5 Device Tab** and the tab is
**FlashForge C5** — deliberately different, since the hub list is where a stranger
works out what the plugin does and the tab is what you read while working.

## Requirements

- **OrcaSlicer 2.5.0 or newer.** The Python plugin system landed in 2.5.0; older
  builds will not list the plugin at all.
- The printer on the same LAN, in LAN/offline mode, with its **serial number** and
  **LAN check code** to hand (printer touchscreen → Device → About for the serial;
  Network for the check code). **If you have already set this printer up in
  OrcaSlicer, you do not need to type any of it** — see *Reading OrcaSlicer's own
  printer settings* below.

## Where the settings are kept

The printer's address, serial number and LAN check code are stored in
`settings.json` at `<OrcaSlicer data>/orca_plugin_data/flashforge-c5/`, **not** in
the plugin's own directory. Installing a new plugin version replaces
`orca_plugins/<plugin>/`, which took those settings with it. The old location is
still read, so an update carries them over the first time you save again.

## Theme

The tab follows OrcaSlicer: the page takes its background, text,
border, muted text, accent and font from the variables OrcaSlicer injects into
every plugin page, and it re-themes live when the app theme changes. It does not
pin itself to dark.

## What you get

| Capability | Type | What it does |
|---|---|---|
| **FlashForge C5** tab | `pages` | Live status, temperatures, a **30-minute temperature chart**, the 4-slot material station, a **live camera view**, job progress, pause/resume/cancel, light and clear-platform, **maintenance** (bed levelling, input shaper, auto-shutdown timer), a **print history** with totals that survives restarts, and the files stored on the printer with a plate **preview** and one-click **reprint**. When a print finishes, is cancelled or the printer reports an error, OrcaSlicer raises a notification in the 3D view. |
| **FlashForge Creator 5** | `printer-connection` | Makes OrcaSlicer's own **Send to printer** / **Print** buttons work against the Creator 5. This is the sending path — the tab watches and drives the machine. |

## Install

1. Open **Plugins** in OrcaSlicer, then **Browse plugins ▾ → Install local
   plugin**, and pick `flashforge-c5_any.py`.

   Or copy the file into its own folder under the OrcaSlicer data directory, e.g.
   `%APPDATA%\OrcaSlicer\orca_plugins\flashforge-c5\flashforge-c5_any.py`. A
   plugin folder must contain exactly one entry file (one `.py` or one `.whl`).

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

## Maintenance

The Creator 5 firmware allows exactly two manual motions, and both are on the tab:
**Level bed** (`calibration_cmd` / `levelingDetection`) and **Input shaper**
(`calibration_cmd` / `vibrationCompensation`). **Auto shutdown** sets the
firmware's own delay-off timer through `delayClose_cmd`, and the picker shows the
printer's current setting rather than a value the tab invented.

Both motion commands require an idle machine — the buttons lock while anything is
running — and neither is confirmed by the reply: `/control` answers `Success` to
anything it did not act on, so levelling and shaping are reported as started
rather than confirmed, and the timer is re-read from `/detail` before the tab says
it took.

## Print history

Every print the tab sees is recorded to
`<OrcaSlicer data>/orca_plugin_data/flashforge-c5/history.json`: start, end,
duration, file name and outcome. The totals count completed jobs, total print time
and success rate. A job starts when the printer first reports an active state and
ends when it stops; a cancel sent from the tab is recorded as *cancelled*, and an
error state as *error*. The file lives beside `orca_plugins`, not inside it, so
installing a new plugin version does not throw the history away.

## Notifications

Print finished, print cancelled and printer error raise a notification through
`orca.host.ui.push_notification`, which appears in OrcaSlicer's 3D view — so the
tab does not have to be open to notice. Errors use the warning level; everything
else the regular one.

## Several printers

Configure as many machines as you like. The picker at the top of the tab chooses which one
the status, camera, file and job cards describe; **Add printer** clears the connection form
for a new one and **Remove this printer** drops the selected one (it is disabled while only
one remains).

**All printers** opens the fleet: one compact row per machine showing its state, the file it
is printing and its progress, with the selected row marked. **Needs attention** narrows it to
printers that are unreachable, in an error state, or paused and waiting on someone. The fleet
refreshes every configured printer when it is open, and stays quiet while it is closed.

Settings live in `settings.json` under `<OrcaSlicer data>/orca_plugin_data/flashforge-c5/`:

```json
{"printers": [{"id": "p3f9c1", "name": "Studio", "host": "192.168.8.128",
               "port": 8898, "serial": "…", "check_code": "…", "poll_seconds": 3}],
 "active": "p3f9c1"}
```

A settings file written by an older version — one printer at the top level — is read as that
single printer, so upgrading does not ask for the address again. The selected printer is also
mirrored onto those old top-level keys, which keeps the printer agent and anything else
written against the single-printer layout working unchanged.

Print history is recorded per printer; a job record carries the id of the machine that
produced it, and records predating multi-printer support count for whichever printer asks.

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

Publishing is then: connect the repo in **Plugins → Shared → your plugin → Edit
plugin → GitHub publishing** → `ben0551/orca-flashforge-c5` → **Connect**, fill the
portal metadata, and publish a GitHub Release tagged `vX.Y.Z`.
`.github/workflows/publish-orcacloud.yml` does the upload over OIDC, so there is no
token to store: the tag becomes the version and the release notes become the
changelog.

**A repository connects to exactly one plugin.** Orca Cloud fails a run with a 401 if
the repository is connected to none, or to more than one.

## Tests

```
./run_tests.sh
```

Two suites, neither needing OrcaSlicer or a printer:

- `tests/test_plugin.py` runs the real plugin against a mock Creator 5
  (`tests/mock_printer.py`) and a stub `orca` module (`tests/fake_orca.py`), then
  drives the tab's message protocol exactly as the page's JavaScript does. It
  asserts on what the *printer* received — header casing, the `gcodeFile` part,
  the `fileSize` header, the extracted G-code bytes, the five-field mapping
  elements — not on what the plugin thinks it sent.
- `tests/test_page_js.py` extracts the tab's JavaScript and runs it under Node in
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
- The camera frame parser, against the real printer's live MJPEG stream: every
  frame a complete 640x480 baseline JPEG with a full scan, consecutive frames all
  different, at roughly two frames a second.
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
