# Orca Cloud hub listing

Copy-paste material for the plugin's page on the Orca Cloud hub, kept here so the
listing text does not drift from the code.

## Checklist before publishing

| # | Requirement | Status |
|---|---|---|
| 1 | Plugin file carries a target-OS suffix — `any` for a pure Python plugin | Done: `flashforge-c5_any.py` |
| 2 | A `LICENSE` file at the repo root | Done: MIT |
| 3 | `.py` only, no binaries/archives/symlinks in the upload | Done: one `.py` |
| 4 | Release tagged with a semantic version greater than the current one | Not yet — `v1.0.0` on first publish |
| 5 | Repo connected in the portal: **Shared Plugins → Edit plugin → GitHub publishing** → `ben0551/orca-flashforge-c5` → Connect | Not yet — manual step, needs your login |
| 6 | Portal metadata filled: compatible OrcaSlicer version, supported OS, tags | Not yet — set in the portal |

Once 5 and 6 are done, publishing a new version is just: push, create a GitHub
Release with a `vX.Y.Z` tag, publish the release. The workflow in
`.github/workflows/publish-orcacloud.yml` uploads the file, the tag becomes the
version, and the release notes become the changelog.

## ⚠️ Read this first: the hub needs a Nightly build

Orca Cloud's own help page states that Plugins are a **Beta feature that currently
works only with current OrcaSlicer Nightly builds** — "stable releases without the
Plugins feature will not show or load Orca Cloud plugins."

That is a different gate from the plugin *system* itself, which shipped in
OrcaSlicer **2.5.0** and is what loads a locally installed `.py`. So:

- **Local install on 2.5.0** — works today. This is how you are testing now.
- **Hub listing** — the listing can be created now, but subscribers on 2.5.0
  stable may not be able to see or activate it until they run a Nightly.

Worth checking the hub's current state before investing in the listing, and worth
putting a line in the listing description saying which OrcaSlicer build it needs.

## Listing text

**Name:** FlashForge Creator 5

**Type:** Slicing-pipeline (the plugin also registers a tab and a printer agent —
see *Capability types and the hub* below)

**Compatible OrcaSlicer:** 2.5.0 or newer

**Supported OS:** any (pure Python, standard library only)

**Tags:** `flashforge`, `creator-5`, `creator-5-pro`, `printer-agent`, `lan`,
`local`, `no-cloud`

**Short description**

> Print to a FlashForge Creator 5 / 5 Pro straight from OrcaSlicer. FlashForge's
> firmware hides the printer behind its own cloud app, so OrcaSlicer will not send
> to it or show its status. This plugin talks to the printer's own local API on
> port 8898 instead: a FlashForge C5 tab with live status, temperatures, the
> 4-slot material station, job control, the files already on the printer, and a
> send-to-print panel with a per-tool spool picker — plus a printer agent so
> OrcaSlicer's own Send and Print buttons work too. Nothing leaves your LAN.

**Long description**

> **The problem.** A Creator 5 registers with a slicer as a cloud-bound
> Bambu-style device, so OrcaSlicer offers no way to upload to it or read its
> status — you have to hand the file to FlashForge's own app. The printer's
> firmware, however, exposes a complete local HTTP API, and this plugin uses it.
>
> **What you get**
>
> - **FlashForge C5 tab** — live print status and progress, nozzle and bed
>   temperatures, the 4-slot material station with the colour loaded in each slot,
>   pause / resume / cancel, chamber light and clear-platform, a browser for the
>   files already on the printer with one-click reprint, and a send-to-print panel.
> - **A real spool picker** — picking a file reads the tool list out of the 3MF and
>   offers a slot per tool, pre-selecting the nearest loaded colour and telling you
>   which substitution it made. Override any row before sending.
> - **OrcaSlicer's own buttons** — a printer agent so **Send to printer** and
>   **Print** work directly (needs OrcaSlicer's experimental "use printer agents"
>   preference).
>
> **Two things it handles that the raw API does not**
>
> - OrcaSlicer exports `.gcode.3mf` bundles, which this firmware accepts, lists,
>   and then fails to print — the plugin extracts the inner G-code and uploads
>   that instead.
> - The firmware pads idle-state job fields with uninitialised memory, so the
>   plugin zeroes the job block when the printer is idle rather than showing
>   garbage.
>
> **Privacy.** Your serial number and LAN check code are stored in `settings.json`
> beside the plugin at mode 0600 and are sent to nothing but your printer. The
> plugin has no dependencies and contacts no server.
>
> **Requirements.** OrcaSlicer 2.5.0 or newer. The printer on the same LAN, with
> its serial number and LAN check code to hand (printer touchscreen → Device →
> About, and → Network).

## Capability types and the hub

The hub's **Plugin Type** field offers Script, Slicing-pipeline and Resource
textures. This plugin registers three capabilities — `pages` (the tab),
`slicing-pipeline` (the slice capture) and `printer-connection` (the agent) — so
there is no single hub type that describes it. Slicing-pipeline is the closest of
the three offered, since the plugin does install a G-code pipeline capability, but
it undersells the tab.

Flag this to the Orca Cloud team when submitting rather than guessing: ask whether
a multi-capability plugin can be listed and which type it should carry. The hub
also has a **Compatible OrcaSlicer Version** field, which is the right place to
record the 2.5.0 floor.

## Differentiation

The only comparable published plugin targets the **Adventurer 5M / 5M Pro**. The
Creator 5 / 5 Pro is a different machine with a different API surface — a material
station with four slots and per-slot filament reporting, a different file and
job-control contract, and the uninitialised-memory quirk in its idle status
payload. This plugin is written against the Creator 5's protocol and is not a
reskin of the Adventurer one.
