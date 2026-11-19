# FlashForge C5 — an OrcaSlicer plugin

Send prints to a FlashForge Creator 5 / 5 Pro from inside OrcaSlicer and watch the
job there, instead of handing the file to FlashForge's own app or a web page.

FlashForge's Creator 5 firmware exposes a complete local HTTP API on port 8898 —
status, temperature, the material station, the stored file list, upload, start
print, pause/resume/cancel, light — but the printer presents itself to a slicer as
a cloud-bound Bambu-style device, so OrcaSlicer will not upload to it or show its
status. This plugin speaks the printer's own LAN protocol from inside OrcaSlicer.

It is one Python file in the standard OrcaSlicer plugin format
([plugin docs](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_development.html)),
with no third-party dependencies — nothing is downloaded at plugin load time.

## Requirements

- **OrcaSlicer 2.5.0 or newer.** The Python plugin system landed in 2.5.0; older
  builds will not list the plugin at all.
- The printer on the same LAN, in LAN/offline mode, with its **serial number** and
  **LAN check code** to hand (printer touchscreen → Device → About for the serial;
  Network for the check code).

## What you get

| Capability | Type | What it does |
|---|---|---|
| **FlashForge C5** tab | `pages` | Live status, temperatures, the 4-slot material station, job progress, pause/resume/cancel, light and clear-platform, and the files stored on the printer with a plate **preview** and one-click **reprint**. |
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

3. Open the **FlashForge C5** tab, fill in the connection panel (address, serial,
   LAN check code) and press **Save & test**. Settings are written to
   `settings.json` next to the plugin, mode 0600. They go nowhere except your
   printer.

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

Two things the plugin does that the raw API does not:

- **OrcaSlicer writes `.gcode.3mf` bundles, and the Creator 5 cannot start one.**
  It uploads `code: 0`, appears in the file list, and then fails on the printer
  with *"print file error, please check"* because the firmware tries to unzip it.
  The plugin extracts `Metadata/plate_1.gcode` and sends that instead, so the file
  the printer ends up holding has the name it will be known by.
- **The plate preview.** The firmware keeps a thumbnail beside every stored job
  and serves it from `/gcodeThumb`; the tab fetches it on demand and shows it
  inline.

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
3. Set the printer **serial number** on the FlashForge C5 tab — OrcaSlicer's
   preset stores the address and access code but not the serial.

OrcaSlicer's **Send to printer** then uploads with the same bundle-unwrapping the
tab does, and **Print** uploads and starts the job in one step.

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
`FFC5_SERIAL`, `FFC5_CHECK_CODE`.

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
publishing**, fill the portal metadata, and publish a GitHub Release with a
`vX.Y.Z` tag. `.github/workflows/publish-orcacloud.yml` does the upload over OIDC,
so there is no token to store.

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
  a DOM stub against the plugin's real normalised `/detail` payload, checking the
  rendering, the button wiring and the file-row preview and reprint actions.

## What is and is not verified

Verified by the test suites above:

- The upload wire contract: credentials as request headers, the `gcodeFile`
  multipart part, `fileSize`, `printNow=false`, levelling, and the extracted plain
  `.gcode` bytes rather than the bundle.
- `/gcodeThumb` returning a PNG, and the tab showing it inline against the file
  name.
- Reprint rebuilding the five-field mapping elements (0-based `toolId`, 1-based
  `slotId`) from the printer's own `gcodeListDetail`.
- `/detail` normalisation, including zeroing the job block when the printer is
  idle — the firmware pads those fields with uninitialised memory between states.
- Both `/control` payload shapes, with confirmation by re-reading `/detail`,
  because `/control` answers `Success` to any command including ones it ignores.
- The tab's rendering and message protocol, the file-row actions, and the printer
  agent's methods.
- Zero third-party dependencies; standard library only.

Not verified here, because it needs the real application and the real printer:

- That OrcaSlicer 2.5.0 loads the plugin and renders the tab. The plugin follows
  the documented API and passes a stub of it, but the stub is not OrcaSlicer.
- That the printer accepts an upload from this client. The request shape matches a
  client verified against Creator 5 Pro firmware 2.0.0, and the mock enforces that
  shape, but no bytes have been sent to a real machine from here.
- The printer-agent path end to end, which additionally depends on OrcaSlicer's
  experimental "use printer agents" mode.

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
