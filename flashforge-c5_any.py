"""FlashForge Creator 5 Device Tab — an OrcaSlicer plugin for the Creator 5 / 5 Pro.

What it does
------------
FlashForge's Creator 5 firmware exposes a complete local HTTP API on port 8898
(status, file list, upload, print, job control) but the printer registers itself
with the slicer as a *cloud-bound* Bambu-style device, so OrcaSlicer refuses to
send prints to it or show its status. This plugin closes that gap by speaking the
printer's own LAN protocol from inside OrcaSlicer, and by registering a printer
agent so OrcaSlicer's own Send / Print buttons work too.

Two capabilities, registered by one plugin:

* ``FlashForge C5`` tab (Pages) - live status, temperatures, material station,
  camera, job control, and a file browser for what is already on the printer,
  with reprint. The *plugin* is named FlashForge Creator 5 Device Tab for the
  marketplace; the tab keeps the shorter name, which is what the user is looking
  at when they read it.
* ``FlashForge Creator 5`` (PrinterAgent) - makes OrcaSlicer's native Send to
  printer / Print buttons work against the Creator 5, so printing stays the
  ordinary OrcaSlicer flow and the tab is there to watch and drive the machine.

Only the Python standard library is used, so nothing has to be downloaded at
plugin load time.

Author: Ben Fischer.  License: MIT.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "FlashForge Creator 5 Device Tab"
# description = "Send prints to a FlashForge Creator 5 / 5 Pro over LAN, watch the job and the camera inside OrcaSlicer."
# author = "Ben Fischer"
# version = "1.5.0"
# ///

import base64
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile

import orca

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# Where versions before the durable home kept settings.json. Still read,
# so an update finds what the user already saved; never written again.
LEGACY_SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")

SETTINGS_SLUG = 'flashforge-c5'


def orca_data_dir():
    """Orca's data directory, or '' when this file is not installed as a plugin.

    Plugins live at <datadir>/orca_plugins/<name>/<file>.py and cloud-subscribed
    ones a level deeper, so walk up until orca_plugins is the child.
    """
    path = PLUGIN_DIR
    while True:
        parent, leaf = os.path.split(path)
        if leaf == "orca_plugins":
            return parent
        if not parent or parent == path:
            return ""
        path = parent


def settings_home():
    """The directory settings.json belongs in.

    Installing a new plugin version replaces <datadir>/orca_plugins/<plugin>/,
    which takes a settings file kept there with it — a saved token was lost on
    every update. A sibling of orca_plugins rather than something inside it, so
    OrcaSlicer's plugin scanner does not read the directory as a plugin. Falls
    back to the plugin directory outside OrcaSlicer, where there is no update to
    survive.
    """
    datadir = orca_data_dir()
    if not datadir:
        return PLUGIN_DIR
    return os.path.join(datadir, "orca_plugin_data", SETTINGS_SLUG)


HISTORY_LIMIT = 50          # records kept on disk
HISTORY_RESULTS = ("finished", "cancelled", "error")


def _write_json(path, payload):
    """Atomically replace `path` with `payload`, mode 0600."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def history_file():
    """Where finished jobs are recorded, so they survive a restart and an update."""
    return os.path.join(settings_home(), "history.json")


def load_history(printer_id=None):
    """Recorded jobs for one printer (or all of them), oldest first, with totals.

    A record carrying no printer tag predates multi-printer support and belonged
    to the only machine there was, so it counts for whichever printer asks.
    """
    stored = _read_stored(history_file()) or {}
    jobs = [j for j in stored.get("jobs") or [] if isinstance(j, dict)]
    tagged = [j for j in jobs
              if not j.get("printer") or j.get("printer") == printer_id]
    return {"jobs": tagged, "totals": history_totals(tagged)}


def history_totals(jobs):
    done = [j for j in jobs if j.get("result") in HISTORY_RESULTS]
    good = [j for j in done if j.get("result") == "finished"]
    seconds = sum(_num(j.get("duration_s"), 0.0) for j in done)
    return {
        "jobs": len(done),
        "print_seconds": int(seconds),
        "success_pct": round(100.0 * len(good) / len(done), 1) if done else 0.0,
    }


def record_job(job):
    """Append one finished job, newest last, pruned to HISTORY_LIMIT."""
    stored = _read_stored(history_file()) or {}
    jobs = [j for j in stored.get("jobs") or [] if isinstance(j, dict)]
    jobs.append({key: job.get(key) for key in
                 ("start", "end", "duration_s", "name", "result", "printer")})
    jobs = jobs[-HISTORY_LIMIT:]
    _write_json(history_file(), {"jobs": jobs})
    return {"jobs": jobs, "totals": history_totals(jobs)}


def notify(message, warning=False):
    """Surface a line in OrcaSlicer's own 3D view. Never raises.

    Must be called on the GUI thread: the notification manager lives there, and
    a call marshalled from a worker deadlocks the way any UI call does.
    """
    try:
        ui = orca.host.ui
        levels = ui.NotificationLevel
        level = getattr(levels, "WarningNotificationLevel" if warning
                        else "RegularNotificationLevel")
        ui.push_notification(level, f"{TAB_NAME}: {message}")
    except Exception:
        pass

STAGING_DIR = os.path.join(tempfile.gettempdir(), "ffc5-staging")

TAB_NAME = "FlashForge C5"
AGENT_ID = "flashforge_c5"
DEFAULT_API_PORT = 8898
DEFAULT_CAM_PORT = 8080
DEFAULT_CAM_PATH = "/?action=stream"
# The printer's MJPEG server is gated: it serves nothing until `streamCtrl open`,
# and it serves exactly *one* client per open — when that client disconnects the
# server stops again. It also needs a moment to spin the encoder up, so the first
# connect is retried rather than reported as a failure. These are the timings a
# working client of this printer settled on.
CAM_OPEN_TRIES = 4
CAM_WARMUP_S = 2.0
CAM_RETRY_S = 3.0

# Machine states in which the job block of /detail is meaningful. Outside these
# the firmware pads the block with uninitialised memory (see job_metric below),
# so an "unzipping" state must not count as printing.
ACTIVE_STATES = ("printing", "paused", "heating", "preheating", "resuming")
# A status the firmware reports while something is wrong. The tab has no error
# code field on this model, so the word is what there is to key on.
ERROR_STATES = ("error", "fault", "failed", "exception", "alarm")
# Levelling and input shaping move the gantry, so the maintenance card locks
# during them. Deliberately not part of ACTIVE_STATES: job tracking keys off
# that, and a calibration must not be recorded as a print.
BUSY_STATES = ACTIVE_STATES + ("leveling", "shaping", "calibrating")

# The temperature chart: one sample every five seconds, half an hour of them.
TEMP_SAMPLE_S = 5.0
TEMP_HISTORY_S = 1800.0

# /control answers {"code":0,"message":"Success"} to *any* command, including
# nonsense, so its reply proves nothing. Confirm by re-reading /detail.
CONTROL_CONFIRM_TRIES = 4
CONTROL_CONFIRM_DELAY = 0.7

CHUNK_BYTES = 512 * 1024

# --------------------------------------------------------------------------- #
# Settings (kept next to the plugin)
# --------------------------------------------------------------------------- #

_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE = None

_SETTINGS_KEYS = ("host", "serial", "check_code", "port", "poll_seconds",
                  "name", "printers", "active")

# The single-printer layout older versions wrote. Still read, so an upgrade
# finds the printer the user already set up instead of asking again.
LEGACY_PRINTER_ID = "legacy"
PRINTER_FIELDS = ("name", "host", "port", "serial", "check_code", "poll_seconds")


def printer_list(cfg=None):
    """Every configured printer, oldest first, each with an id and a name."""
    cfg = load_settings() if cfg is None else cfg
    out = []
    for index, raw in enumerate(cfg.get("printers") or []):
        if not isinstance(raw, dict) or not raw.get("host"):
            continue
        entry = {key: raw.get(key) for key in PRINTER_FIELDS}
        entry["id"] = str(raw.get("id") or f"p{index + 1}")
        entry["name"] = entry["name"] or entry["host"]
        entry["port"] = int(entry["port"] or DEFAULT_API_PORT)
        entry["poll_seconds"] = int(entry["poll_seconds"] or 3)
        out.append(entry)
    if out:
        return out
    if cfg.get("host"):
        entry = {key: cfg.get(key) for key in PRINTER_FIELDS}
        entry["id"] = LEGACY_PRINTER_ID
        entry["name"] = cfg.get("name") or cfg.get("host")
        entry["port"] = int(entry["port"] or DEFAULT_API_PORT)
        entry["poll_seconds"] = int(entry["poll_seconds"] or 3)
        return [entry]
    return []


def active_printer(cfg=None):
    """The selected printer, or the first one, or None when nothing is set up."""
    cfg = load_settings() if cfg is None else cfg
    printers = printer_list(cfg)
    if not printers:
        return None
    wanted = str(cfg.get("active") or "")
    for entry in printers:
        if entry["id"] == wanted:
            return entry
    return printers[0]


def new_printer_id():
    return "p" + uuid.uuid4().hex[:8]


def save_printer(data):
    """Insert or update one printer from the tab. Returns (ok, error).

    The selected printer is mirrored onto the old top-level keys as well, so an
    older settings file and anything written against the single-printer layout
    keep describing the machine that is selected.
    """
    cfg = load_settings()
    entries = [dict(p) for p in printer_list(cfg)]
    host = str(data.get("host") or "").strip()
    if not host:
        return False, "An address is required."
    pid = str(data.get("id") or "").strip()
    record = {
        "id": pid or new_printer_id(),
        "name": str(data.get("name") or "").strip() or host,
        "host": host,
        "port": int(data.get("port") or DEFAULT_API_PORT),
        "serial": str(data.get("serial") or "").strip(),
        "check_code": str(data.get("check_code") or "").strip(),
        "poll_seconds": int(data.get("poll_seconds") or 3),
    }
    for index, entry in enumerate(entries):
        if entry["id"] == record["id"]:
            entries[index] = record
            break
    else:
        entries.append(record)
    update = {key: cfg.get(key) for key in _SETTINGS_KEYS}
    update["printers"] = entries
    update["active"] = record["id"]
    update.update(_mirror(record))
    return save_settings(update)


def remove_printer(pid):
    """Drop one printer; the first remaining becomes the selection."""
    cfg = load_settings()
    entries = [dict(p) for p in printer_list(cfg) if p["id"] != str(pid)]
    update = {key: cfg.get(key) for key in _SETTINGS_KEYS}
    update["printers"] = entries
    if entries:
        update["active"] = entries[0]["id"]
        update.update(_mirror(entries[0]))
    else:
        update["active"] = ""
        update.update({"host": "", "port": DEFAULT_API_PORT, "serial": "",
                       "check_code": "", "poll_seconds": 3})
    return save_settings(update)


def _mirror(entry):
    """Copy the active printer onto the old top-level keys.

    Kept in step so anything written against the single-printer layout -- the
    agent, the tab's own checks, an older settings file -- still reads the
    printer that is selected.
    """
    return {key: entry.get(key) for key in
            ("host", "port", "serial", "check_code", "poll_seconds")}


def _default_settings():
    return {
        "host": os.environ.get("FFC5_HOST", ""),
        "port": int(os.environ.get("FFC5_PORT", DEFAULT_API_PORT)),
        "serial": os.environ.get("FFC5_SERIAL", ""),
        "check_code": os.environ.get("FFC5_CHECK_CODE", ""),
        "poll_seconds": 3,
    }

def _read_stored(path):
    """A parsed settings.json, or None when it is absent or unusable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except Exception:
        return None          # absent or unreadable: never stop the plugin loading
    return stored if isinstance(stored, dict) else None


def load_settings():
    """Read settings.json, falling back to env vars and then to empty values.

    The durable copy is read first; the copy older versions kept in the plugin
    directory is the fallback, so updating does not strand what was saved.
    """
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE is not None:
            return dict(_SETTINGS_CACHE)
        data = _default_settings()
        home = settings_home()
        candidates = [os.path.join(home, "settings.json")]
        if home != PLUGIN_DIR:
            candidates.append(LEGACY_SETTINGS_FILE)
        for path in candidates:
            stored = _read_stored(path)
            if not stored:
                continue
            found = False
            for key in _SETTINGS_KEYS:
                if stored.get(key) not in (None, ""):
                    data[key] = stored[key]
                    found = True
            if found:
                break
        _SETTINGS_CACHE = dict(data)
        return dict(data)


def save_settings(update):
    """Merge `update` into settings.json. Returns (ok, error_or_None)."""
    global _SETTINGS_CACHE
    current = load_settings()
    for key in _SETTINGS_KEYS:
        if key in update and update[key] is not None:
            current[key] = update[key]
    with _SETTINGS_LOCK:
        try:
            _write_json(os.path.join(settings_home(), "settings.json"), current)
        except Exception as exc:
            _SETTINGS_CACHE = dict(current)
            return False, f"{type(exc).__name__}: {exc}"
    _SETTINGS_CACHE = dict(current)
    return True, None

# --------------------------------------------------------------------------- #
# OrcaSlicer's own printer preset — the same three values already live there
# --------------------------------------------------------------------------- #
#
# OrcaSlicer 2.5.0 ships a FlashForge print host, so anyone who set this printer
# up in the slicer has already typed the address, the serial number and the LAN
# check code once. They are kept on the printer preset under these keys —
# verified against the 2.5.0 source, where `Flashforge::Flashforge` reads exactly
# them (`src/slic3r/Utils/Flashforge.cpp`) and `Preset::printer_options()` lists
# them (`src/libslic3r/Preset.cpp`):
#
#   print_host                "Hostname, IP or URL"
#   flashforge_serial_number  "Serial Number"
#   printhost_apikey          "API Key / Password"   (the LAN check code)
#
# `orca.host.preset_bundle()` gives a read-only view of the printer preset
# collection. Physical Printer profiles are a *separate* collection the plugin
# API does not expose, so the values are looked for on the selected preset, then
# in the bundle's resolved configuration, then on any printer preset at all.

ORCA_KEY_HOST = "print_host"
ORCA_KEY_SERIAL = "flashforge_serial_number"
ORCA_KEY_CODE = "printhost_apikey"
ORCA_KEY_AGENT = "printer_agent"

ORCA_FIELDS = ((ORCA_KEY_HOST, "host"),
               (ORCA_KEY_SERIAL, "serial"),
               (ORCA_KEY_CODE, "check_code"))


def _bare_host(value):
    """Reduce whatever OrcaSlicer stored to the bare host our client wants.

    The field accepts a URL and carries its own port setting, so a scheme, a
    path, embedded credentials and a trailing ``:port`` all have to come off.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("@")[-1]
    if text.count(":") == 1:
        head, tail = text.rsplit(":", 1)
        if tail.isdigit():
            text = head
    return text.strip()


def _preset_value(preset, key):
    """One key off a preset, as a stripped string; "" when unset or unavailable."""
    try:
        value = preset.config_value(key)
    except Exception:
        return ""
    return str(value).strip() if value not in (None, "") else ""


def preset_connection():
    """Read the FlashForge connection out of OrcaSlicer's own printer preset.

    Returns ``(values, notes)`` — values holds whichever of host/serial/
    check_code were found, notes says where each came from. Never raises: outside
    OrcaSlicer, before the GUI is ready, or on a build without the preset API this
    comes back empty and the plugin falls back to its own settings file.
    """
    values, notes = {}, []
    try:
        import orca
        bundle = orca.host.preset_bundle()
    except Exception:
        return values, notes

    def take(preset, where):
        for key, field in ORCA_FIELDS:
            if values.get(field):
                continue
            raw = _preset_value(preset, key)
            if not raw:
                continue
            values[field] = _bare_host(raw) if field == "host" else raw
            notes.append(f"{field} from {where}")

    try:
        take(bundle.current_printer_preset(), "the selected printer preset")
    except Exception:
        pass

    # A value set on the preset's parent rather than on the preset itself only
    # shows up in the bundle's resolved configuration.
    for key, field in ORCA_FIELDS:
        if values.get(field):
            continue
        try:
            raw = bundle.full_config_value(key)
        except Exception:
            raw = None
        if raw in (None, ""):
            continue
        text = str(raw).strip()
        values[field] = _bare_host(text) if field == "host" else text
        notes.append(f"{field} from the resolved configuration")

    # The profile may be saved with a different printer selected. Prefer a preset
    # bound to this agent, then any other.
    if len(values) < len(ORCA_FIELDS):
        try:
            printers = bundle.printers
            bound, rest = [], []
            for index in range(printers.size()):
                try:
                    preset = printers.preset(index)
                except Exception:
                    continue
                if _preset_value(preset, ORCA_KEY_AGENT) == AGENT_ID:
                    bound.append(preset)
                else:
                    rest.append(preset)
            for preset, where in ([(p, "a preset bound to this plugin") for p in bound]
                                  + [(p, "a printer preset") for p in rest]):
                if len(values) >= len(ORCA_FIELDS):
                    break
                take(preset, where)
        except Exception:
            pass

    return values, notes


# --------------------------------------------------------------------------- #
# Printer client — stdlib only, mirroring the vendor app's request shapes
# --------------------------------------------------------------------------- #

class PrinterError(Exception):
    """Any failure talking to the printer, with a human-readable message."""


def permission_hint(exc):
    """Turn a plugin audit refusal into something the user can act on.

    The raw text names a Python audit event and a matching rule the user has no
    way to see, which tells them nothing about what to do next.
    """
    text = str(exc)
    if isinstance(exc, PermissionError) or "without permission" in text:
        return ("OrcaSlicer refused this on a permission check, so the plugin "
                "could not reach the printer. Reinstall the plugin and accept "
                "its network permission, or restart OrcaSlicer so the plugin "
                f"asks again. ({text})")
    return f"{type(exc).__name__}: {text}"


def _reachable(host, port, timeout=0.6):
    """Cheap TCP probe so an offline printer costs 0.6 s, not the HTTP timeout."""
    if not host:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class Creator5:
    """The Creator 5 / 5 Pro LAN API on port 8898 (HTTP only, no TCP 8899).

    Two request shapes are load-bearing and they are *different*:

    * ``/detail``, ``/control``, ``/gcodeList``, ``/printGcode`` take
      ``serialNumber`` and ``checkCode`` in the **JSON body**.
    * ``/uploadGcode`` takes every piece of metadata as a **request header**
      (including ``fileSize``) with the bytes as a single multipart part named
      ``gcodeFile``. Sending body credentials there answers
      ``{"code":1,"message":"SN is different"}`` even though the serial is right,
      and omitting ``fileSize`` answers ``File size is 0``.
    """

    def __init__(self, host, serial, check_code, port=DEFAULT_API_PORT, timeout=8):
        self.host = host
        self.serial = serial
        self.check_code = check_code
        self.port = int(port or DEFAULT_API_PORT)
        self.timeout = timeout

    @property
    def base(self):
        return f"http://{self.host}:{self.port}"

    @property
    def camera_url(self):
        return f"http://{self.host}:{DEFAULT_CAM_PORT}{DEFAULT_CAM_PATH}"

    def configured(self):
        return bool(self.host and self.serial and self.check_code)

    # -- transport ---------------------------------------------------------- #

    def _body(self, extra=None):
        body = {"serialNumber": self.serial, "checkCode": self.check_code}
        if extra:
            body.update(extra)
        return body

    def _post(self, path, extra=None, timeout=None):
        if not self.host:
            raise PrinterError("Printer address is not set")
        payload = json.dumps(self._body(extra)).encode("utf-8")
        req = urllib.request.Request(
            self.base + path, data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
        )
        raw = self._open(req, timeout)
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise PrinterError(f"{path} returned a non-JSON reply")

    def _open(self, req, timeout=None):
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            try:
                return exc.read()
            except Exception:
                raise PrinterError(f"HTTP {exc.code} from the printer")
        except urllib.error.URLError as exc:
            raise PrinterError(f"Cannot reach {self.host}:{self.port} ({exc.reason})")
        except (socket.timeout, TimeoutError):
            raise PrinterError(f"Timed out talking to {self.host}:{self.port}")
        except OSError as exc:
            raise PrinterError(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _unwrap(data, path):
        if not isinstance(data, dict):
            raise PrinterError(f"{path} returned an unexpected reply")
        if data.get("code") not in (0, None):
            raise PrinterError(str(data.get("message") or f"{path} failed"))
        return data

    # -- status ------------------------------------------------------------- #

    def detail(self, timeout=6):
        """Raw /detail payload (the firmware nests it under a couple of keys)."""
        data = self._unwrap(self._post("/detail", timeout=timeout), "/detail")
        detail = data.get("detail") or data.get("data") or {}
        if isinstance(detail, dict) and isinstance(detail.get("detail"), dict):
            detail = detail["detail"]
        return detail if isinstance(detail, dict) else {}

    # -- files -------------------------------------------------------------- #

    def thumbnail(self, filename, timeout=20):
        """The plate preview the printer keeps beside a stored file, as PNG bytes.

        This is the only thing that can be read back out of the printer: its
        entire HTTP API is product / control / detail / uploadGcode / gcodeList /
        printGcode plus this one image route, so a stored file cannot be
        downloaded. A preview is what makes one identifiable before reprinting.
        """
        data = self._unwrap(self._post("/gcodeThumb", {"fileName": filename},
                                       timeout=timeout), "/gcodeThumb")
        image = data.get("imageData") or ""
        if not image:
            raise PrinterError(data.get("message")
                               or "The printer has no preview for that file")
        try:
            return base64.b64decode(image)
        except Exception as exc:
            raise PrinterError(f"Unreadable preview image: {exc}")

    def gcode_list(self, timeout=15):
        data = self._unwrap(self._post("/gcodeList", timeout=timeout), "/gcodeList")
        names = data.get("gcodeList") or data.get("fileList") or data.get("files") or []
        detail = data.get("gcodeListDetail") or []
        return list(names), list(detail)

    # -- camera ------------------------------------------------------------- #

    def calibrate(self, leveling=None, shaper=None):
        """Bed levelling and input shaper — the two motions the Creator 5 allows.

        The stock firmware exposes no axis motion at all: `moveCtrl_cmd`,
        `homingCtrl_cmd` and `extrudeCtrl_cmd` do not exist in it. Levelling and
        vibration compensation are the manual controls this model has, and each
        is sent as one field of `calibration_cmd`, so passing only one leaves the
        other untouched.
        """
        args = {}
        if leveling is not None:
            args["levelingDetection"] = "open" if leveling else "close"
        if shaper is not None:
            args["vibrationCompensation"] = "open" if shaper else "close"
        return self.control("calibration_cmd", args)

    def shutdown_timer(self, minutes):
        """Auto-shutdown after `minutes` (0 turns it off). Returns the reply."""
        minutes = int(minutes or 0)
        return self.control("delayClose_cmd", {
            "automaticShutdown": "open" if minutes else "close",
            "shutdownAfterTime": minutes,
        })

    def stream_ctrl(self, on):
        """Start or stop the printer's MJPEG server.

        The command name is ``streamCtrl`` with **no** ``_cmd`` suffix, unlike
        ``lightControl_cmd`` and ``jobCtl_cmd``. The firmware answers "Success"
        to anything it does not recognise, so these strings are load-bearing.
        """
        return self.control("streamCtrl", {"action": "open" if on else "close"})

    def camera_frames(self, on_stop=None, timeout=8):
        """Yield JPEG frames from the printer's camera.

        Opens the stream, yields frames until `on_stop()` says stop, then closes
        the stream — which is not optional. The printer serves one client per
        `streamCtrl open` and stops its server when that client goes away, so a
        reader that walks off without closing leaves the camera dead for the next
        one, and for OrcaSlicer's own device panel.
        """
        if not self.host:
            raise PrinterError("Printer address is not set")
        url = f"http://{self.host}:{DEFAULT_CAM_PORT}{DEFAULT_CAM_PATH}"
        self.stream_ctrl(True)
        response, last = None, None
        for attempt in range(CAM_OPEN_TRIES):
            time.sleep(CAM_WARMUP_S if attempt == 0 else CAM_RETRY_S)
            # The firmware answers "Success" to a streamCtrl it did not act on, so
            # the connect succeeding is the only proof the server is up. Re-issue
            # the open between attempts: the first one can be lost, and without
            # this every retry repeats a connect against a server nobody opened.
            if attempt:
                try:
                    self.stream_ctrl(True)
                except PrinterError:
                    pass
            try:
                response = urllib.request.urlopen(url, timeout=timeout)
                break
            except Exception as exc:                    # noqa: BLE001
                last = exc
        if response is None:
            self.stream_ctrl(False)
            # Name the URL, and say when the credentials were missing: a closed
            # connection means the stream was never opened, and an unset check
            # code opens nothing.
            hint = "" if self.check_code else (
                " - the LAN check code is not set, so the printer acknowledges "
                "streamCtrl but opens no stream")
            raise PrinterError(
                f"Camera did not start at {url} "
                f"({type(last).__name__}: {last}){hint}")
        try:
            for frame in mjpeg_frames(response.read):
                yield frame
                if on_stop and on_stop():
                    return
        finally:
            try:
                response.close()
            except Exception:
                pass
            self.stream_ctrl(False)

    # -- upload ------------------------------------------------------------- #

    def upload(self, file_path, print_now=False, leveling=True, timeout=600,
               on_progress=None):
        """Upload a file. Retries once past the "Printer is busy" completion dialog."""
        result = self._upload_once(file_path, print_now, leveling, timeout, on_progress)
        if isinstance(result, dict) and result.get("code") not in (0, None):
            message = str(result.get("message") or result.get("error") or "").lower()
            if "busy" in message:
                # A finished job leaves the completion dialog open, and
                # /uploadGcode then refuses even though nothing is printing.
                self.clear_platform()
                time.sleep(2.0)
                result = self._upload_once(file_path, print_now, leveling,
                                           timeout, on_progress)
        return self._unwrap(result, "/uploadGcode")

    def _upload_once(self, file_path, print_now, leveling, timeout, on_progress):
        """POST /uploadGcode with the exact header casing the firmware expects.

        This deliberately bypasses urllib: ``Request.add_header`` title-cases
        every key, so ``serialNumber`` goes out as ``Serialnumber`` and the plugin
        can no longer be sure the firmware will match it. ``http.client`` writes
        the header names verbatim, which is what the vendor app and the verified
        `requests` client do.
        """
        filename = os.path.basename(file_path)
        size = os.path.getsize(file_path)
        boundary = "----ffc5" + uuid.uuid4().hex
        preamble = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="gcodeFile"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
        with open(file_path, "rb") as fh:
            blob = fh.read()
        if on_progress:
            on_progress(0.30, "Uploading to the printer…")
        body = preamble + blob + epilogue

        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            conn.putrequest("POST", "/uploadGcode", skip_accept_encoding=True)
            conn.putheader("serialNumber", self.serial)
            conn.putheader("checkCode", self.check_code)
            conn.putheader("fileSize", str(size))
            conn.putheader("printNow", "true" if print_now else "false")
            conn.putheader("levelingBeforePrint", "true" if leveling else "false")
            conn.putheader("flowCalibration", "false")
            conn.putheader("timeLapseVideo", "false")
            conn.putheader("Content-Type",
                           f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(len(body)))
            conn.putheader("Accept", "application/json")
            conn.endheaders(body)
            response = conn.getresponse()
            raw = response.read()
        except (socket.timeout, TimeoutError):
            raise PrinterError(f"Timed out uploading to {self.host}:{self.port}")
        except OSError as exc:
            raise PrinterError(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise PrinterError("/uploadGcode returned a non-JSON reply")

    # -- print -------------------------------------------------------------- #

    def start_print(self, filename, mappings=None, leveling=True, timeout=30):
        return self._unwrap(self._post("/printGcode", {
            "fileName": filename,
            "levelingBeforePrint": leveling,
            "flowCalibration": False,
            "timeLapseVideo": False,
            "materialMappings": mappings or [],
        }, timeout=timeout), "/printGcode")

    def is_stored(self, filename, timeout=15):
        names, _ = self.gcode_list(timeout=timeout)
        return filename in names

    # -- control ------------------------------------------------------------ #

    def control(self, cmd, args=None):
        """Send a /control command.

        Two payload shapes are in circulation — ``{"cmd":c,"args":{...}}`` and
        ``{"cmd":c,"action":...}`` — and the firmware silently accepts whichever
        it does not understand, answering ``Success`` either way. So send the
        nested shape and, if the caller's confirmation loop finds nothing
        changed, retry flat. ``control_checked`` does that.
        """
        return self._post("/control", {"payload": {"cmd": cmd, "args": args or {}}})

    def control_flat(self, cmd, args=None):
        payload = {"cmd": cmd}
        payload.update(args or {})
        return self._post("/control", {"payload": payload})


def control_checked(client, cmd, args, verify, tries=CONTROL_CONFIRM_TRIES,
                    delay=CONTROL_CONFIRM_DELAY):
    """Run a /control command and prove it took effect.

    Returns (ok, status_now, how) where `how` is the shape that worked, or None
    when neither did. `verify(detail)` must return True once the machine shows
    the requested change.
    """
    for shape, send in (("nested", client.control), ("flat", client.control_flat)):
        try:
            send(cmd, args)
        except PrinterError as exc:
            return False, None, f"error: {exc}"
        for _ in range(tries):
            time.sleep(delay)
            try:
                detail = client.detail(timeout=5)
            except PrinterError:
                continue
            if verify(detail):
                return True, detail, shape
    return False, None, None


# An MJPEG frame is a JPEG (FFD8 … FFD9) inside a multipart part. The firmware's
# Content-Length is not dependable, and the stream can be cut mid-part when the
# printer stops its server, so frames are delimited by the JPEG markers
# themselves rather than by the multipart boundary.
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


def mjpeg_frames(read, chunk=16384):
    """Yield each JPEG frame from an MJPEG stream.

    `read(n)` returns the next chunk of bytes, or b"" once the stream ends.
    """
    buf = b""
    while True:
        block = read(chunk)
        if not block:
            return
        buf += block
        while True:
            start = buf.find(JPEG_SOI)
            if start < 0:
                buf = buf[-1:]          # a two-byte marker can straddle chunks
                break
            end = buf.find(JPEG_EOI, start + 2)
            if end < 0:
                buf = buf[start:]
                break
            yield buf[start:end + 2]
            buf = buf[end + 2:]


# --------------------------------------------------------------------------- #
# /detail normalisation
# --------------------------------------------------------------------------- #

def _num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick(d, *keys, default=None):
    for key in keys:
        if key in d and d[key] not in (None, ""):
            return d[key]
    return default


def normalize_detail(d):
    """Map raw firmware fields onto a stable shape for the tab."""
    if not isinstance(d, dict):
        return {}
    status = str(_pick(d, "status", "machineStatus", default="unknown")).lower()
    active = status in ACTIVE_STATES

    progress = _num(_pick(d, "printProgress", "progress"), 0.0)
    if 0.0 < progress <= 1.0:
        progress *= 100.0
    elapsed = _num(_pick(d, "printDuration"), 0.0)
    remaining = _num(_pick(d, "estimatedTime"), 0.0)

    # Outside a live job the firmware pads these with uninitialised memory: an
    # "unzipping" state genuinely reported printLayer 1310730, fan 917507% and a
    # denormal 1e-39 filament weight. Zero them unless a job is running.
    if not active:
        progress = elapsed = remaining = 0.0
    progress = max(0.0, min(progress, 100.0))

    def job_metric(value, high=None):
        if not active:
            return 0.0
        v = _num(value, 0.0)
        if v != v or v < 0:
            return 0.0
        if high is not None and v > high:
            return 0.0
        return v

    temps = d.get("nozzleTemps") or []
    targets = d.get("nozzleTargetTemps") or []
    if not temps and _pick(d, "nozzleTemp") is not None:
        temps = [_pick(d, "nozzleTemp")]
        targets = [_pick(d, "nozzleTargetTemp")]
    nozzles = [{"id": i + 1, "temp": _num(t, 0.0),
                "target": _num(targets[i] if i < len(targets) else 0, 0.0)}
               for i, t in enumerate(temps)]

    station = d.get("matlStationInfo") or {}
    spools = []
    for slot in station.get("slotInfos") or []:
        colour = slot.get("materialColor") or ""
        spools.append({
            "id": slot.get("slotId"),
            "material": slot.get("materialName") or "",
            "color": colour,
            # The tab is used by children too, and "#C0C0C0" means nothing to
            # them — every spool carries a human colour name as well.
            "color_name": color_name(colour),
            "loaded": bool(slot.get("hasFilament")),
            "active": slot.get("slotId") == station.get("currentSlot"),
            "grams": _num(slot.get("filamentWeight"), 0.0),
        })

    return {
        "status": status,
        "active": active,
        "ip": _pick(d, "ipAddr", default=""),
        "name": _pick(d, "name", "machineName", default="Creator 5"),
        "firmware": _pick(d, "firmwareVersion", default=""),
        "light_on": str(_pick(d, "lightStatus", "led", default="")).lower()
                    in ("open", "on", "1", "true"),
        "file": str(_pick(d, "printFileName", "fileName", default="") or ""),
        "progress": round(progress, 1),
        "layer": int(job_metric(_pick(d, "printLayer"), high=100000)),
        "layer_total": int(job_metric(_pick(d, "targetPrintLayer"), high=100000)),
        "elapsed_s": elapsed,
        "remaining_s": remaining,
        "speed_pct": job_metric(_pick(d, "printSpeedAdjust"), high=400),
        "nozzles": nozzles,
        "bed": _num(_pick(d, "platTemp", "platformTemp"), 0.0),
        "bed_target": _num(_pick(d, "platTargetTemp"), 0.0),
        "chamber": _num(_pick(d, "chamberTemp"), 0.0),
        "chamber_target": _num(_pick(d, "chamberTargetTemp"), 0.0),
        "spools": spools,
        "current_slot": station.get("currentSlot"),
        "door": _pick(d, "doorStatus", default=""),
        "disk_free_gb": _num(_pick(d, "remainingDiskSpace"), 0.0),
        # The maintenance card: whether the auto-shutdown timer is on and at
        # what delay, so the control can show the printer's own setting rather
        # than a value the tab invented.
        "auto_shutdown": str(_pick(d, "autoShutdown", default="")).lower(),
        "auto_shutdown_time": int(_num(_pick(d, "autoShutdownTime"), 0.0)),
        "error_state": status in ERROR_STATES,
    }


# Named colours for the spool picker, as (hue in degrees, name). Hue is circular
# and matches how people read a colour: #0057B8 is a blue, not a teal, even
# though plain RGB distance says otherwise.
HUE_NAMES = [
    (0, "Red"), (22, "Orange"), (45, "Gold"), (58, "Yellow"),
    (90, "Light green"), (130, "Green"), (180, "Teal"), (205, "Sky blue"),
    (240, "Blue"), (275, "Purple"), (305, "Magenta"), (330, "Pink"),
]


def color_name(value):
    """Human name for a hex colour.

    The tab is used by the children as well, and '#C0C0C0' means nothing to them.
    Matching is by hue rather than RGB distance, with near-greys resolved by
    brightness — squared-distance in RGB calls a medium blue "Teal".
    """
    text = str(value or "").lstrip("#")
    if len(text) != 6:
        return ""
    try:
        r, g, b = (int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return ""

    high, low = max(r, g, b), min(r, g, b)
    if high <= 0.0:
        return "Black"
    saturation = (high - low) / high
    if saturation < 0.12:                      # a grey of some sort
        if high < 0.20:
            return "Black"
        if high < 0.45:
            return "Grey"
        if high < 0.78:
            return "Light grey"
        return "White"

    if high == r:
        hue = (60.0 * ((g - b) / (high - low))) % 360.0
    elif high == g:
        hue = 60.0 * ((b - r) / (high - low)) + 120.0
    else:
        hue = 60.0 * ((r - g) / (high - low)) + 240.0

    if high < 0.62 and 10.0 <= hue <= 45.0:
        return "Brown"

    best, best_d = "Red", None
    for ref, label in HUE_NAMES:
        dist = abs(hue - ref)
        dist = min(dist, 360.0 - dist)         # hue wraps at 360
        if best_d is None or dist < best_d:
            best, best_d = label, dist
    return best


# --------------------------------------------------------------------------- #
# File naming: what the printer ends up calling the file
# --------------------------------------------------------------------------- #

def printer_name(name):
    """The filename the printer ends up with, given an export name.

    The printer is handed a file named after the path we upload, and a
    `.gcode.3mf` bundle is unwrapped to its plain `.gcode` on the way. Showing
    the user the bundle's name and sending the unwrapped one means the two
    disagree, and a later reprint-by-name then fails on a file the user was
    never told the name of. Both sides take the name from here.
    """
    stem = os.path.basename(name or "")
    if stem.lower().endswith(".gcode.3mf"):
        stem = stem[: -len(".3mf")]
    elif stem:
        stem = os.path.splitext(stem)[0]
    if not stem.lower().endswith(".gcode"):
        stem += ".gcode"
    return stem


def unwrap_for_printer(path, preferred_name=None):
    """Hand the printer a `.gcode`, never an OrcaSlicer `.gcode.3mf` bundle.

    A bundle uploads with `code: 0` and appears in the file list, then fails to
    start with "print file error, please check" because the firmware tries to
    unzip it on-device. Extract `Metadata/plate_1.gcode` and send that instead;
    the original stays on disk beside it for the tool metadata.

    `preferred_name` is the name the user picked, so the file that lands on the
    printer is `widget.gcode` rather than the staging file's uuid-prefixed name.
    """
    try:
        if not zipfile.is_zipfile(path):
            return path, None
        with zipfile.ZipFile(path) as bundle:
            candidates = [n for n in bundle.namelist()
                          if n.startswith("Metadata/") and n.endswith(".gcode")]
            if not candidates:
                return path, None
            candidates.sort(key=lambda n: (0 if "plate_1" in n else 1, n))
            stem = printer_name(preferred_name or os.path.basename(path))
            target = os.path.join(os.path.dirname(path), stem)
            with bundle.open(candidates[0]) as src, open(target, "wb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    dst.write(block)
            return target, candidates[0]
    except Exception:
        return path, None


# --------------------------------------------------------------------------- #
# Shared runtime state
# --------------------------------------------------------------------------- #

class Runtime:
    """The live connection + the last status poll for one printer."""

    def __init__(self, entry=None):
        self.entry = dict(entry or {})
        self.lock = threading.Lock()
        self.status = None
        self.error = None
        self.stage = None              # {"path","name","size","tools","print"}
        # Temperature chart: [{"t": epoch, "n": nozzle, "b": bed}], pruned to
        # TEMP_HISTORY_S. Sampled on the poll tick, not on a timer, so it only
        # ever moves when the tab is actually listening.
        self.temp_samples = []
        self._temp_clock = 0.0
        # The print in progress, for the history: opened when the printer starts
        # being active, closed when it stops.
        self.open_job = None
        self.job_cancel_seen = False
        self.last_history = load_history()
        self.job = {"running": False, "pct": 0.0, "label": "", "error": None,
                    "done": False, "result": None}

    def config(self):
        """This printer's settings.

        An entry carries everything the connection needs; a Runtime built with
        none (outside the tab, and in the tests) reads the file instead.
        """
        if self.entry.get("host"):
            return self.entry
        return load_settings()

    def client(self):
        cfg = self.config()
        return Creator5(cfg.get("host") or "", cfg.get("serial") or "",
                        cfg.get("check_code") or "", cfg.get("port") or DEFAULT_API_PORT)

    def refresh(self, force=False):
        """Poll /detail. Skips the HTTP round trip when the host is down."""
        cfg = self.config()
        host, port = cfg.get("host") or "", cfg.get("port") or DEFAULT_API_PORT
        if not host:
            with self.lock:
                self.status, self.error = None, (
                    "No printer address set — fill in Printer connection on this tab")
            return None, self.error
        if not _reachable(host, port):
            with self.lock:
                self.status = None
                self.error = f"Printer at {host}:{port} is not responding"
            return None, self.error
        try:
            detail = self.client().detail()
        except PrinterError as exc:
            with self.lock:
                self.error = str(exc)
            return None, self.error
        normalized = normalize_detail(detail)
        with self.lock:
            self.status, self.error = normalized, None
        return normalized, None

    def sample_temps(self, normalized, now=None):
        """Append one chart sample if TEMP_SAMPLE_S has passed; None otherwise.

        Returns the whole series when it sampled, so the caller can attach it to
        the state message, and None when nothing changed.
        """
        now = time.time() if now is None else now
        if now - self._temp_clock < TEMP_SAMPLE_S:
            return None
        self._temp_clock = now
        nozzles = normalized.get("nozzles") or [{}]
        self.temp_samples.append({
            "t": int(now),
            "n": round(_num(nozzles[0].get("temp"), 0.0), 1),
            "b": round(_num(normalized.get("bed"), 0.0), 1),
        })
        cutoff = now - TEMP_HISTORY_S
        self.temp_samples = [s for s in self.temp_samples if s["t"] >= cutoff]
        return list(self.temp_samples)

    def track_job(self, normalized, now=None):
        """Open a history record when a print starts, close it when one ends.

        Returns the finished record, or None. The result is decided here because
        this is the only place that sees both edges of a job.
        """
        now = time.time() if now is None else now
        active = bool(normalized.get("active"))
        status = str(normalized.get("status") or "")
        # Marked before the active test: an error state is not an active one, so
        # checking inside the active branch would let a failed print be recorded
        # as finished on the very tick that reported the fault.
        if self.open_job is not None and status in ERROR_STATES:
            self.open_job["errored"] = True
        if active:
            if self.open_job is None:
                self.open_job = {"start": now,
                                 "name": normalized.get("file") or "",
                                 "errored": False}
            return None
        if self.open_job is None:
            return None
        started = self.open_job
        self.open_job = None
        if self.job_cancel_seen:
            result = "cancelled"
        elif started.get("errored"):
            result = "error"
        else:
            result = "finished"
        self.job_cancel_seen = False
        record = {
            "start": int(started["start"]),
            "end": int(now),
            "duration_s": max(0, int(now - started["start"])),
            "name": started.get("name") or "",
            "result": result,
            "printer": self.entry.get("id") or "",
        }
        self.last_history = record_job(record)
        return record

    def start_job(self, fn, label):
        """Run a slow printer action off the UI thread; the tab polls for it."""
        with self.lock:
            if self.job.get("running"):
                return False
            self.job = {"running": True, "pct": 0.0, "label": label,
                        "error": None, "done": False, "result": None}

        def progress(pct, label=None):
            with self.lock:
                self.job["pct"] = pct
                if label:
                    self.job["label"] = label

        def runner():
            try:
                result = fn(progress)
                with self.lock:
                    self.job.update(running=False, done=True, pct=1.0,
                                    result=result, label="Done")
            except Exception as exc:
                with self.lock:
                    self.job.update(running=False, done=True,
                                    error=f"{type(exc).__name__}: {exc}")

        threading.Thread(target=runner, name="ffc5-job", daemon=True).start()
        return True


class Fleet:
    """One Runtime per configured printer.

    The runtimes are rebuilt from settings on each use so adding or removing a
    printer takes effect without restarting OrcaSlicer, and a runtime keeps its
    own chart, job record and history across the change.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.runtimes = {}

    def sync(self):
        cfg = load_settings()
        with self._lock:
            keep = {}
            for entry in printer_list(cfg):
                runtime = self.runtimes.get(entry["id"])
                if runtime is None:
                    runtime = Runtime(entry)
                else:
                    runtime.entry = dict(entry)
                keep[entry["id"]] = runtime
            self.runtimes = keep
            return cfg

    def active(self):
        cfg = self.sync()
        entry = active_printer(cfg) or {}
        with self._lock:
            return self.runtimes.get(entry.get("id")) or Runtime(entry)

    def all(self):
        cfg = self.sync()
        with self._lock:
            return [self.runtimes[e["id"]] for e in printer_list(cfg)]

    def get(self, pid):
        with self._lock:
            return self.runtimes.get(str(pid))


FLEET = Fleet()


class _ActiveRuntime:
    """Attribute access resolves to whichever printer is selected right now.

    The tab's handlers are written against a single runtime, and with several
    printers the selected one can change under them. Resolving at access time
    keeps every path -- status, camera, files, job control -- acting on the
    selected machine without threading an argument through all of them.
    """

    def __getattr__(self, name):
        return getattr(FLEET.active(), name)

    def __setattr__(self, name, value):
        setattr(FLEET.active(), name, value)


RUNTIME = _ActiveRuntime()


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #

PAGE = r"""
<style>
  :root{
    /* Surfaces and text come from OrcaSlicer, so the tab follows the app's theme
       instead of pinning itself to dark. OrcaSlicer injects these into every
       plugin page (WebViewHostDialog::host_theme_vars_css) and puts
       data-orca-theme="light"|"dark" on <html>. */
    --bg:var(--orca-bg);
    --panel:var(--orca-bg);      /* a panel is the page with a border, not a tint */
    --panel2:var(--orca-bg);
    --line:var(--orca-border);
    --fg:var(--orca-fg);
    --dim:var(--orca-muted);
    --accent:var(--orca-accent);
    --on-accent:var(--orca-accent-fg);
    /* The remaining three are fixed accents rather than surfaces: a second blue,
       amber and red, each picked to stay legible on both a light and a dark page. */
    --accent2:#2f7fd1;
    --warn:#a05a00;
    --bad:#c0392b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.45 var(--orca-font)}
  #wrap{padding:10px 12px 28px;max-width:1180px}
  .hidden{display:none !important}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:stretch}
  .grow{flex:1 1 320px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;
        padding:10px 12px;margin:0 0 10px}
  .card h3{margin:0 0 8px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;
           color:var(--dim);font-weight:600}
  #hdr{display:flex;align-items:center;gap:8px;margin-bottom:10px}
  #hdr b{font-size:15px}
  .spacer{flex:1}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--dim);flex:none}
  .dot.ok{background:var(--accent)} .dot.bad{background:var(--bad)}
  .dot.busy{background:var(--warn)}
  .pill{background:var(--panel2);border:1px solid var(--line);border-radius:99px;
        padding:1px 9px;font-size:11px;color:var(--dim)}
  #banner{border-radius:6px;padding:7px 10px;margin-bottom:10px;font-size:12px;
          border:1px solid var(--line);background:var(--panel2)}
  #banner.err{border-color:var(--bad);background:var(--bg);color:var(--bad)}
  #banner.ok{border-color:var(--accent);background:var(--bg);color:var(--accent)}
  #banner.info{border-color:var(--accent2);background:var(--bg);color:var(--accent2)}
  #banner button{margin-left:9px;padding:2px 9px;font-size:11px}
  .card.wanted{border-color:var(--accent2)}
  body button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer}
  body button:hover{border-color:var(--accent)}
  body button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
  body button.primary:hover{filter:brightness(1.1)}
  body button:disabled{opacity:.45;cursor:not-allowed}
  input[type=text],input[type=number],select{background:var(--bg);color:var(--fg);
        border:1px solid var(--line);border-radius:5px;padding:5px 7px;font-size:12px}
  input[type=text]{width:100%}
  /* A file input keeps its own intrinsic width, which is wider than the column
     it sits in once the panel is docked narrow — it then pushes the whole page
     sideways and clips the text beside it. Let it shrink instead. */
  input[type=file]{width:100%;max-width:100%;min-width:0;font-size:12px;color:var(--dim)}
  .grid2>*{min-width:0}
  .grid2 ol{margin:0;padding-left:18px}
  .grid2 li{margin-bottom:3px}
  label.chk{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim)}
  .kv{display:flex;justify-content:space-between;padding:2px 0;font-size:12px}
  .kv span:first-child{color:var(--dim)}
  #bar{height:9px;background:var(--orca-border);border:1px solid var(--line);border-radius:99px;
       overflow:hidden;margin:6px 0}
  #bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent2),var(--accent));
         transition:width .25s}
  .bar{height:7px;background:var(--orca-border);border:1px solid var(--line);border-radius:99px;overflow:hidden}
  .bar>i{display:block;height:100%;width:0;background:var(--accent2)}
  .spools{display:flex;gap:8px;flex-wrap:wrap}
  .slot{background:var(--panel2);border:1px solid var(--line);border-radius:7px;
        padding:7px 10px;min-width:118px}
  .slot.active{border-color:var(--accent)}
  .slot.empty{opacity:.5}
  .sw{width:100%;height:16px;border-radius:4px;border:1px solid #000a;margin-bottom:5px}
  .slot .nm{font-size:12px;font-weight:600}
  .slot .sub{font-size:11px;color:var(--dim)}
  .tools{display:flex;gap:8px;flex-wrap:wrap}
  .tool{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:8px 10px}
  .tool .t{font-size:11px;color:var(--dim);margin-bottom:5px}
  .files{max-height:230px;overflow:auto}
  .frow{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--line)}
  .frow:last-child{border-bottom:none}
  .fname{flex:1;font-size:12px;word-break:break-all}
  .fmeta{font-size:11px;color:var(--dim)}
  .fbtns{display:flex;gap:6px;flex-shrink:0}
  .nozzles{display:flex;gap:8px;flex-wrap:wrap}
  .noz{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:6px 9px;min-width:82px}
  .noz .t{font-size:11px;color:var(--dim)}
  .noz .v{font-size:14px;font-weight:600}
  .grid2{display:grid;grid-template-columns:130px 1fr;gap:7px 9px;align-items:center}
  /* Docked narrow, a fixed 130px label column leaves too little for the value:
     the file picker truncates to "No fi…sen" and the help text becomes a
     sliver. Stack the pairs instead of squeezing them. */
  @media (max-width:540px){
    .grid2{grid-template-columns:1fr;gap:3px}
    .grid2 label:not(.chk){margin-top:6px}
    .grid2>span{display:none}
  }
  .dim{color:var(--dim)}
  .mrow{display:flex;gap:8px;align-items:center;margin-top:9px;flex-wrap:wrap}
  .mrow label{font-size:12px;color:var(--dim)}
  .chart{display:block;width:100%;height:120px;border:1px solid var(--line);
         border-radius:6px;background:var(--bg)}
  .hrow{display:flex;justify-content:space-between;gap:8px;padding:4px 0;
        border-bottom:1px solid var(--line);font-size:12px}
  .hres{font-size:11px;flex-shrink:0}
  .chk{display:flex;align-items:center;gap:5px;font-size:12px;color:var(--dim)}
  .frow{display:flex;align-items:center;gap:10px;padding:7px 0;
        border-bottom:1px solid var(--line);font-size:12px;flex-wrap:wrap}
  .frow:last-child{border-bottom:0}
  .frow.current{border-left:2px solid var(--accent);padding-left:8px}
  .frow .fname{flex:1;min-width:120px;font-weight:600;overflow-wrap:anywhere}
  .frow .fmeta{color:var(--dim);font-size:11px;width:100%} .small{font-size:11px}
</style>
<div id="wrap">
  <div id="hdr">
    <span class="dot" id="dot"></span>
    <b id="pname">FlashForge</b>
    <span class="pill" id="pip">—</span>
    <span class="pill" id="pfw">—</span>
    <span class="spacer"></span>
    <span class="small dim" id="stt">connecting…</span>
  </div>
  <div id="banner" class="hidden"></div>

  <div class="card">
    <div class="mrow" style="margin-top:0">
      <label for="printer">Printer</label>
      <select id="printer"></select>
      <button id="badd">Add printer</button>
      <button id="bremove">Remove this printer</button>
      <span class="spacer"></span>
      <label class="chk"><input type="checkbox" id="attention"> Needs attention</label>
      <button id="bfleet">All printers</button>
    </div>
    <div id="fleet" class="hidden" style="margin-top:9px"></div>
  </div>

  <div class="row">
    <div class="card grow">
      <h3>Job</h3>
      <div class="kv"><span>File</span><span id="jfile">—</span></div>
      <div id="bar"><i id="barfill"></i></div>
      <div class="kv"><span id="jpct">0.0%</span><span id="jlay">—</span></div>
      <div class="kv"><span>Elapsed</span><span id="jel">—</span></div>
      <div class="kv"><span>Remaining</span><span id="jrem">—</span></div>
      <div class="row" style="margin-top:9px">
        <button id="bpause">Pause</button>
        <button id="bresume">Resume</button>
        <button id="bcancel">Cancel print</button>
        <button id="blight">Toggle light</button>
        <button id="bclear">Clear platform</button>
      </div>
    </div>

    <div class="card grow">
      <h3>Temperatures</h3>
      <div class="nozzles" id="nozzles"></div>
      <div class="kv" style="margin-top:8px"><span>Bed</span><span id="tbed">—</span></div>
      <div class="kv"><span>Chamber</span><span id="tch">—</span></div>
      <div class="kv"><span>Door</span><span id="tdoor">—</span></div>
      <div class="kv"><span>Disk free</span><span id="tdisk">—</span></div>
    </div>
  </div>

  <div class="card">
    <h3>Camera</h3>
    <div class="row" style="margin-top:2px">
      <button id="bcam">View camera</button>
      <span class="small dim" id="camhint" style="align-self:center">
        One viewer at a time — the printer stops the stream when you switch it off.
      </span>
    </div>
    <div id="camwrap" class="hidden" style="margin-top:9px">
      <img id="camimg" alt="printer camera"
           style="width:100%;max-width:520px;border:1px solid var(--line);border-radius:6px;background:var(--bg)">
      <div class="small dim" id="camnote" style="margin-top:4px">Starting the stream…</div>
    </div>
  </div>

  <div class="card">
    <h3>Material station</h3>
    <div class="spools" id="spools"><span class="dim small">waiting for the printer…</span></div>
  </div>

  <div class="card">
    <h3>Maintenance</h3>
    <div class="small dim" style="margin-bottom:7px">
      Levelling and input shaper are the two motions the Creator 5 firmware offers. It has no
      axis, home or extrusion command at all, so there is nothing to jog.
    </div>
    <div class="row">
      <button id="blevel">Level bed</button>
      <button id="bshaper">Input shaper</button>
    </div>
    <div class="mrow">
      <label for="timer">Auto shutdown</label>
      <select id="timer">
        <option value="">Choose&hellip;</option>
        <option value="0">Off</option>
        <option value="30">30 min</option>
        <option value="60">60 min</option>
        <option value="120">120 min</option>
      </select>
      <button id="btimer">Apply</button>
    </div>
    <div class="small dim" id="mntnote" style="margin-top:6px"></div>
  </div>

  <div class="card">
    <h3>Temperatures &mdash; last 30 min</h3>
    <canvas id="chart" class="chart" width="600" height="120"></canvas>
    <div class="small dim" id="charnote" style="margin-top:4px">collecting&hellip;</div>
  </div>

  <div class="card">
    <h3>Print history</h3>
    <div class="kv"><span>Completed jobs</span><b id="hjobs">&mdash;</b></div>
    <div class="kv"><span>Total print time</span><b id="htime">&mdash;</b></div>
    <div class="kv"><span>Success rate</span><b id="hrate">&mdash;</b></div>
    <div id="hist" style="margin-top:7px"></div>
  </div>

  <div class="card">
    <h3>Sending a print</h3>
    <div class="small dim">
      Slice a model, then use OrcaSlicer's own <b>Send to printer</b> / <b>Print</b> button on
      the preview screen. This plugin supplies the printer agent behind that button, so the
      Creator 5 behaves like any built-in target — there is nothing to set up on this tab.
    </div>
  </div>

  <div class="card">
    <h3>Files on the printer</h3>
    <div id="upwrap" class="hidden" style="margin-bottom:8px">
      <div class="bar"><i id="upbar"></i></div>
      <div class="small dim" id="uplabel" style="margin-top:4px"></div>
    </div>
    <div id="prevwrap" class="hidden" style="margin-bottom:8px">
      <div class="small dim" id="prevname" style="margin-bottom:4px"></div>
      <img id="previmg" alt="plate preview"
           style="width:110px;height:110px;image-rendering:auto;border:1px solid var(--line);border-radius:6px;background:var(--bg)">
    </div>
    <div class="files" id="files"><span class="dim small">loading…</span></div>
  </div>

  <div class="card" id="setup">
    <h3>Printer connection</h3>
    <div class="grid2">
      <label for="host">Address</label>
      <input type="text" id="host" placeholder="192.168.1.50">
      <label for="port">API port</label>
      <input type="number" id="port" value="8898">
      <label for="serial">Serial number</label>
      <input type="text" id="serial" placeholder="from the printer touchscreen">
      <label for="code">LAN check code</label>
      <input type="text" id="code" placeholder="printer screen &rarr; Network">
      <label for="poll">Poll every (s)</label>
      <input type="number" id="poll" value="3" min="1" max="60">
    </div>
    <div class="row" style="margin-top:9px">
      <button id="bsave" class="primary">Save &amp; test</button>
      <button id="borca">Load from OrcaSlicer</button>
      <span class="small dim" id="cfginfo">Kept in settings.json beside the plugin, mode 0600.
        Never sent anywhere except your printer.</span>
    </div>
  </div>
</div>
<script>
(function(){
  "use strict";
  // `$` accepts both "id" and "#id". A helper that only understands one spelling
  // returns null on the other, and a null property write throws and kills the
  // whole script — which shows up as a permanently frozen tab.
  function $(id){
    var key = String(id).replace(/^#/, "");
    var el = document.getElementById(key);
    if (!el) { console.error("flashforge-c5: missing element #" + key); }
    return el;
  }
  var bannerKind = "info", bannerTimer = null;
  function hideBanner(){
    var b = $("banner");
    if (!b) { return; }
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    b.style.display = "none";
    bannerKind = "info";
  }
  // `actionLabel` + `action` give the message a button to point at and make it
  // stay put. A banner that names something the user cannot find, then expires
  // after nine seconds, is worse than saying nothing.
  function showBanner(msg, kind, actionLabel, action){
    var b = $("banner");
    if (!b) { return; }
    b.textContent = msg;
    if (actionLabel && action) {
      var btn = document.createElement("button");
      btn.textContent = actionLabel;
      btn.addEventListener("click", action);
      b.appendChild(btn);
    }
    b.className = kind || "info";
    b.style.display = "block";
    bannerKind = actionLabel ? "sticky" : "msg";
    if (bannerTimer) { clearTimeout(bannerTimer); }
    if (bannerKind === "msg") {
      bannerTimer = setTimeout(function(){
        if (bannerKind === "msg") { hideBanner(); }
      }, 9000);
    }
  }
  function focusSetup(){
    var s = $("setup");
    if (s && s.scrollIntoView) { s.scrollIntoView({ block: "nearest" }); }
    var h = $("host");
    if (h && h.focus) { h.focus(); }
  }
  function showSetupBanner(on){
    if (on) {
      // Never stomp a live status message such as "Sending…".
      if (bannerKind !== "msg") {
        showBanner("No printer address set — fill in the Printer connection panel.",
                   "info", "Set up printer", focusSetup);
      }
    } else if (bannerKind === "sticky") {
      hideBanner();
    }
  }
  function fmtDur(sec){
    sec = Math.max(0, Math.round(Number(sec) || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    if (h > 0) { return h + "h " + m + "m"; }
    if (m > 0) { return m + "m " + s + "s"; }
    return s + "s";
  }
  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function post(obj){ orca.postMessage(obj); }
  var CFG_LABEL = { host: "address", port: "port", serial: "serial number",
                    check_code: "access code" };
  var CFG_ORDER = ["host", "port", "serial", "check_code"];
  function cfgWords(keys){
    return keys.slice().sort(function(a, b){
      return CFG_ORDER.indexOf(a) - CFG_ORDER.indexOf(b);
    }).map(function(k){ return CFG_LABEL[k] || k; }).join(", ");
  }

  var state = { spools: [], status: null, cfg: null, files: [], camera: false };
  var pollTimer = null, uploadTimer = null;

  // ---- rendering -------------------------------------------------------- //

  function paint(s){
    if (!s) { return; }
    state.status = s;
    $("pname").textContent = s.name || "FlashForge";
    $("pip").textContent   = s.ip || "—";
    $("pfw").textContent   = s.firmware ? ("fw " + s.firmware) : "—";
    var st = s.status || "unknown";
    var dot = $("dot");
    dot.className = "dot " + (s.active ? "busy" : "ok");
    $("stt").textContent = st;

    $("jfile").textContent = s.file || "—";
    $("barfill").style.width = (s.progress || 0) + "%";
    $("jpct").textContent = (s.progress || 0).toFixed(1) + "%";
    $("jlay").textContent = (s.layer ? ("layer " + s.layer +
                            (s.layer_total ? " / " + s.layer_total : "")) : "—");
    $("jel").textContent = s.active ? fmtDur(s.elapsed_s) : "—";
    $("jrem").textContent = s.active ? fmtDur(s.remaining_s) : "—";

    var noz = "";
    var list = s.nozzles || [];
    for (var i = 0; i < list.length; i++) {
      var n = list[i];
      noz += '<div class="noz"><div class="t">Tool ' + n.id + '</div><div class="v">' +
             Math.round(n.temp) + '&deg;</div><div class="t">&rarr; ' +
             Math.round(n.target) + '&deg;</div></div>';
    }
    $("nozzles").innerHTML = noz || '<span class="dim small">no nozzle data</span>';
    $("tbed").textContent  = Math.round(s.bed) + "° → " + Math.round(s.bed_target) + "°";
    $("tch").textContent   = Math.round(s.chamber) + "° → " + Math.round(s.chamber_target) + "°";
    $("tdoor").textContent = s.door || "—";
    $("tdisk").textContent = s.disk_free_gb ? (s.disk_free_gb.toFixed(1) + " GB") : "—";

    state.spools = s.spools || [];
    paintSpools();
    paintMaintenance(s);
  }

  // The maintenance card mirrors the printer's own auto-shutdown setting rather
  // than a value the tab invented, and is locked while a job runs: levelling and
  // shaping move the gantry, which the firmware only allows when idle.
  function paintMaintenance(s){
    var busy = !!s.active;
    $("blevel").disabled = busy;
    $("bshaper").disabled = busy;
    var key = (s.auto_shutdown || "") + ":" + (s.auto_shutdown_time || 0);
    if (key === state.maint) { return; }
    state.maint = key;
    if (s.auto_shutdown === "open") {
      var mins = s.auto_shutdown_time || 0;
      $("timer").value = String(mins);
      $("mntnote").textContent = "On — shuts down " + mins + " min after a print.";
    } else if (s.auto_shutdown === "close") {
      $("timer").value = "0";
      $("mntnote").textContent = "Off.";
    } else {
      $("mntnote").textContent = "";
    }
  }

  function paintChart(samples){
    state.temps = samples || [];
    var cv = $("chart");
    if (!cv || !cv.getContext) { return; }
    var ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height, PAD = 5;
    ctx.clearRect(0, 0, W, H);
    var list = state.temps;
    if (list.length < 2) {
      $("charnote").textContent = "collecting…";
      return;
    }
    var lo = 1e9, hi = -1e9;
    for (var i = 0; i < list.length; i++) {
      if (list[i].n > hi) { hi = list[i].n; }
      if (list[i].n < lo) { lo = list[i].n; }
      if (list[i].b > hi) { hi = list[i].b; }
      if (list[i].b < lo) { lo = list[i].b; }
    }
    if (hi - lo < 10) { hi += 5; lo -= 5; }
    var cs = getComputedStyle(cv);
    var cN = cs.getPropertyValue("--accent").trim() || "#7cc36a";
    var cB = cs.getPropertyValue("--accent2").trim() || "#2f7fd1";
    function y(v){ return H - PAD - (v - lo) * (H - 2 * PAD) / (hi - lo); }
    function line(key, colour){
      ctx.beginPath();
      ctx.strokeStyle = colour;
      ctx.lineWidth = 1.5;
      for (var i = 0; i < list.length; i++) {
        var x = i * W / (list.length - 1);
        if (i) { ctx.lineTo(x, y(list[i][key])); } else { ctx.moveTo(x, y(list[i][key])); }
      }
      ctx.stroke();
    }
    line("n", cN);
    line("b", cB);
    var span = list[list.length - 1].t - list[0].t;
    $("charnote").textContent =
      Math.round(list[list.length - 1].n) + "° nozzle · " +
      Math.round(list[list.length - 1].b) + "° bed · " + fmtDur(span) + " sampled";
  }

  function paintHistory(h){
    h = h || {jobs: [], totals: {}};
    var t = h.totals || {};
    $("hjobs").textContent = String(t.jobs || 0);
    $("htime").textContent = fmtDur(t.print_seconds || 0);
    $("hrate").textContent = t.jobs ? (t.success_pct + "%") : "—";
    var jobs = (h.jobs || []).slice(-12).reverse();
    if (!jobs.length) {
      $("hist").innerHTML = '<span class="dim small">nothing recorded yet</span>';
      return;
    }
    var out = "";
    for (var i = 0; i < jobs.length; i++) {
      var j = jobs[i];
      var colour = j.result === "finished" ? "var(--accent)"
                 : (j.result === "error" ? "var(--bad)" : "var(--warn)");
      out += '<div class="hrow"><span>' + esc(j.name || "(unnamed)") +
             ' <span class="hres" style="color:' + colour + '">' +
             esc(j.result || "unknown") + '</span></span>' +
             '<span class="dim">' + fmtDur(j.duration_s) + '</span></div>';
    }
    $("hist").innerHTML = out;
  }

  function paintSpools(){
    var out = "";
    var spools = state.spools || [];
    if (!spools.length) {
      $("spools").innerHTML = '<span class="dim small">no material station reported</span>';
      return;
    }
    for (var i = 0; i < spools.length; i++) {
      var s = spools[i];
      var cls = "slot";
      if (s.active) { cls += " active"; }
      if (!s.loaded) { cls += " empty"; }
      var colour = s.color ? s.color : "#555";
      var name = s.color_name || (s.loaded ? (s.color || "") : "no filament");
      out += '<div class="' + cls + '">' +
             '<div class="sw" style="background:' + esc(colour) + '"></div>' +
             '<div class="nm">Slot ' + esc(s.id) + ' · ' + esc(s.material || "empty") + '</div>' +
             // A human colour name, not a hex code: this tab is used by the
             // children too, and "#C0C0C0" means nothing to them.
             '<div class="sub" title="' + esc(colour) + '">' + esc(name) +
             (s.active ? " · in use" : "") + '</div></div>';
    }
    $("spools").innerHTML = out;
  }

  function paintFiles(files, detail){
    state.files = files || [];
    if (!state.files.length) {
      $("files").innerHTML = '<span class="dim small">nothing stored on the printer</span>';
      return;
    }
    var meta = {};
    for (var i = 0; i < (detail || []).length; i++) {
      var d = detail[i];
      meta[d.gcodeFileName] = d;
    }
    var out = "";
    for (var j = 0; j < state.files.length; j++) {
      var name = state.files[j];
      var d = meta[name] || {};
      var bits = [];
      if (d.printingTime) { bits.push(fmtDur(d.printingTime)); }
      if (d.totalFilamentWeight) {
        bits.push(Number(d.totalFilamentWeight).toFixed(1) + " g");
      }
      out += '<div class="frow"><div class="fname">' + esc(name) +
             '<div class="fmeta">' + (bits.length ? esc(bits.join(" \u00b7 "))
                                                  : "stored on printer") +
             '</div></div><div class="fbtns">' +
             '<button data-preview="' + esc(name) + '">Preview</button>' +
             '<button data-reprint="' + esc(name) + '">Reprint</button>' +
             '</div></div>';
    }
    $("files").innerHTML = out;
    var btns = $("files").querySelectorAll("button[data-reprint]");
    for (var k = 0; k < btns.length; k++) {
      btns[k].addEventListener("click", onReprint);
    }
    var prevs = $("files").querySelectorAll("button[data-preview]");
    for (var q = 0; q < prevs.length; q++) {
      prevs[q].addEventListener("click", onPreview);
    }
  }

  // The picker and the fleet both come from the same list. The picker chooses
  // which printer the detailed cards describe; the fleet looks at all of them.
  function paintPrinters(){
    var list = state.printers || [];
    var want = state.activePrinter || "";
    var out = "";
    for (var i = 0; i < list.length; i++) {
      out += '<option value="' + esc(list[i].id) + '"' +
             (list[i].id === want ? " selected" : "") + '>' +
             esc(list[i].name || list[i].host) + '</option>';
    }
    $("printer").innerHTML = out;
    $("bremove").disabled = list.length <= 1;
  }

  function paintFleet(rows){
    state.fleet = rows || [];
    var only = $("attention").checked;
    var out = "", shown = 0;
    for (var i = 0; i < state.fleet.length; i++) {
      var r = state.fleet[i];
      if (only && !r.attention) { continue; }
      shown++;
      var bits = [esc(r.status)];
      if (r.file) { bits.push(esc(r.file)); }
      var pct = (r.percent == null) ? "" : (r.percent + "%");
      out += '<div class="frow' + (r.current ? " current" : "") + '">' +
             '<span class="fname">' + esc(r.name || r.id) + '</span>' +
             '<span class="dot ' + (r.error || r.attention ? "bad" : "ok") + '"></span>' +
             '<span>' + pct + '</span>' +
             '<button data-open="' + esc(r.id) + '">Open</button>' +
             '<span class="fmeta">' + bits.join(" &middot; ") + '</span></div>';
    }
    $("fleet").innerHTML = out ||
      '<span class="dim small">' +
      (state.fleet.length ? "no printer needs attention" : "no printers configured") +
      '</span>';
    var opens = $("fleet").querySelectorAll("button[data-open]");
    for (var j = 0; j < opens.length; j++) {
      // The id is captured rather than read off `this`: the handler may be
      // invoked with no receiver, and the row it belongs to is already known.
      (function(pid){
        opens[j].addEventListener("click", function(){
          post({ type: "switch", id: pid });
        });
      })(opens[j].getAttribute("data-open"));
    }
  }

  function paintCfg(cfg, orca){
    state.cfg = cfg || {};
    $("host").value   = cfg.host || "";
    $("port").value   = cfg.port || 8898;
    $("serial").value = cfg.serial || "";
    $("code").value   = cfg.check_code || "";
    $("poll").value   = cfg.poll_seconds || 3;
    // Say where the values came from. A panel that fills itself in silently is
    // indistinguishable from one the user already filled, and the serial number
    // is exactly the field people lose track of.
    if (orca && orca.found && orca.applied && Object.keys(orca.applied).length) {
      showBanner("Filled " + cfgWords(Object.keys(orca.applied))
                 + " from your OrcaSlicer printer preset.", "ok");
    }
    // Until an address is set this panel is the only thing that matters, so it
    // must not be the sixth card below the fold. Once configured it drops back
    // to the end, out of the way.
    var setup = $("setup"), banner = $("banner");
    if (!setup || !banner || !banner.parentNode) { return; }
    if (!cfg.host) {
      if (banner.nextElementSibling !== setup) {
        banner.parentNode.insertBefore(setup, banner.nextElementSibling);
      }
      setup.className = "card wanted";
    } else {
      if (setup.parentNode && setup.parentNode.lastElementChild !== setup) {
        setup.parentNode.appendChild(setup);
      }
      setup.className = "card";
    }
  }

  function setJobRunning(on){
    // Reprint is the only action the tab still starts itself, so it is the one
    // that gets locked while a job runs.
    var btns = $("files").querySelectorAll("button[data-reprint]");
    for (var i = 0; i < btns.length; i++) { btns[i].disabled = !!on; }
  }

  // ---- events ----------------------------------------------------------- //

  function onReprint(ev){
    var name = ev.currentTarget.getAttribute("data-reprint");
    if (!name) { return; }
    showBanner("Starting " + name + "…", "info");
    post({ type: "reprint", filename: name });
    startJobPoll();
  }

  // A stored file cannot be fetched back off the printer — its API has no
  // download route — so the plate thumbnail the device keeps beside the file is
  // the only way to see what it is before reprinting it.
  function onPreview(ev){
    var name = ev.currentTarget.getAttribute("data-preview");
    if (!name) { return; }
    var wrap = $("prevwrap");
    $("prevname").textContent = "Loading the preview…";
    $("previmg").removeAttribute("src");
    wrap.className = "";
    post({ type: "preview", filename: name });
  }

  function wire(){
    $("bsave").addEventListener("click", function(){
      post({ type: "save_cfg", host: $("host").value.trim(),
             port: Number($("port").value) || 8898,
             serial: $("serial").value.trim(),
             check_code: $("code").value.trim(),
             poll_seconds: Number($("poll").value) || 3 });
    });

    // The same three values usually already exist in OrcaSlicer's own printer
    // preset, so offer the explicit overwrite rather than only ever filling blanks.
    $("borca").addEventListener("click", function(){
      post({ type: "load_orca" });
    });

    $("bpause").addEventListener("click", function(){ post({ type: "control", action: "pause" }); });
    $("bresume").addEventListener("click", function(){ post({ type: "control", action: "resume" }); });
    $("bcancel").addEventListener("click", function(){ post({ type: "control", action: "cancel" }); });
    $("blight").addEventListener("click", function(){
      var on = !(state.status && state.status.light_on);
      post({ type: "control", action: "light", on: on });
    });
    $("bclear").addEventListener("click", function(){ post({ type: "control", action: "clear" }); });
    $("printer").addEventListener("change", function(){
      post({ type: "switch", id: $("printer").value }); });
    $("badd").addEventListener("click", function(){ post({ type: "new_printer" }); });
    $("bremove").addEventListener("click", function(){
      post({ type: "remove_printer", id: state.activePrinter || "" }); });
    $("bfleet").addEventListener("click", function(){
      var f = $("fleet");
      f.className = (f.className === "hidden") ? "" : "hidden";
      if (f.className !== "hidden") { post({ type: "fleet" }); }
    });
    // The rows are kept, so toggling the filter re-renders what is already
    // there rather than wiping the list.
    $("attention").addEventListener("change", function(){ paintFleet(state.fleet); });
    $("blevel").addEventListener("click", function(){
      post({ type: "maintenance", kind: "level" }); });
    $("bshaper").addEventListener("click", function(){
      post({ type: "maintenance", kind: "shaper" }); });
    $("btimer").addEventListener("click", function(){
      post({ type: "maintenance", kind: "timer",
             minutes: Number($("timer").value) || 0 }); });

    // The camera is a toggle: the printer's MJPEG server holds a single client,
    // so there is nothing to switch *to* while it is already running.
    $("bcam").addEventListener("click", function(){
      post({ type: state.camera ? "camera_off" : "camera_on" });
    });

    $("poll").addEventListener("change", schedule);
  }

  // Starting a reprint is the only job this tab kicks off itself, so that is
  // what polls for progress. Sending a fresh slice is OrcaSlicer's own button,
  // and its progress belongs to OrcaSlicer.
  function startJobPoll(){
    setJobRunning(true);
    $("upwrap").className = "";
    $("upbar").style.width = "0%";
    $("uplabel").textContent = "Starting the print…";
    if (uploadTimer) { clearInterval(uploadTimer); }
    uploadTimer = setInterval(function(){ post({ type: "job_status" }); }, 500);
  }

  function schedule(){
    if (pollTimer) { clearInterval(pollTimer); }
    var secs = Number($("poll").value) || 3;
    secs = Math.max(1, Math.min(60, secs));
    pollTimer = setInterval(function(){ post({ type: "status" }); }, secs * 1000);
  }

  // ---- inbound ---------------------------------------------------------- //

  orca.onMessage(function(m){
    if (!m || !m.type) { return; }
    switch (m.type) {
      case "state":
        paint(m.status);
        $("stt").textContent = (m.status && m.status.status) || "unknown";
        showSetupBanner(!!m.needs_setup);
        if (Array.isArray(m.temps)) { paintChart(m.temps); }
        if (Array.isArray(m.printers)) {
          state.printers = m.printers;
          state.activePrinter = m.activePrinter;
          paintPrinters();
        }
        // The fleet is only worth polling while it is on screen.
        if ($("fleet") && $("fleet").className !== "hidden") { post({ type: "fleet" }); }
        break;
      case "cfg":
        paintCfg(m.cfg, m.orca);
        if (Array.isArray(m.printers)) {
          state.printers = m.printers;
          state.activePrinter = m.activePrinter;
          paintPrinters();
        }
        break;
      case "temps": paintChart(m.temps); break;
      case "history": paintHistory(m); break;
      case "fleet": paintFleet(m.rows); break;
      case "files": paintFiles(m.files, m.detail); break;
      case "preview":
        $("prevname").textContent = m.filename;
        $("previmg").src = m.image;
        $("prevwrap").className = "";
        break;
      case "job":
        $("upbar").style.width = Math.round((m.pct || 0) * 100) + "%";
        $("uplabel").textContent = m.label || "";
        if (!m.running && m.done) {
          if (uploadTimer) { clearInterval(uploadTimer); uploadTimer = null; }
          setJobRunning(false);
          if (m.error) {
            showBanner("Failed: " + m.error, "err");
          } else if (m.result) {
            showBanner(m.result, "ok");
          }
          $("upwrap").className = "hidden";
          post({ type: "files" });
        }
        break;
      case "camera":
        state.camera = !!m.on;
        $("bcam").textContent = state.camera ? "Stop camera" : "View camera";
        $("camwrap").className = state.camera ? "" : "hidden";
        $("camhint").textContent = state.camera
          ? "Live. The printer serves one viewer at a time."
          : "One viewer at a time — the printer stops the stream when you switch it off.";
        $("camnote").textContent = state.camera ? "Starting the stream…" : "";
        if (!state.camera) { $("camimg").removeAttribute("src"); }
        break;
      case "frame":
        // Frames can still be in flight when the view is switched off, and a
        // late one would repaint a panel the user just closed.
        if (state.camera) {
          $("camimg").src = m.image;
          $("camnote").textContent = "";
        }
        break;
      case "log":
        showBanner(m.message, m.level === "error" ? "err" : "info");
        break;
      case "error":
        showBanner(m.message, "err");
        setJobRunning(false);
        break;
      default: break;
    }
  });

  // A panel left on "connecting…" because its loader only ran on setInterval
  // reads as broken, so ask for everything once, right now.
  wire();
  schedule();
  paintCfg({});
  post({ type: "hello" });
  post({ type: "history" });
  post({ type: "fleet" });
})();
</script>
"""


class FlashForgePage(orca.pages.PagesPluginCapabilityBase):
    """The 'FlashForge C5' tab."""

    def get_name(self):
        return "FlashForge C5"

    def get_ui(self):
        return PAGE

    # -- inbound messages --------------------------------------------------- #

    def on_message(self, msg):
        if not isinstance(msg, dict):
            return
        kind = msg.get("type")
        try:
            handler = {
                "hello": self._hello,
                "status": self._status,
                "files": self._files,
                "save_cfg": self._save_cfg,
                "load_orca": self._load_orca,
                "preview": self._preview,
                "reprint": self._reprint,
                "control": self._control,
                "job_status": self._job_status,
                "maintenance": self._maintenance,
                "history": self._history,
                "new_printer": self._new_printer,
                "fleet": self._fleet,
                "switch": self._switch,
                "remove_printer": self._remove,
                "camera_on": self._camera_on,
                "camera_off": self._camera_off,
            }.get(kind)
            if handler:
                handler(msg)
        except PrinterError as exc:
            self.post_message({"type": "error", "message": str(exc)})
        except Exception as exc:            # never let the tab die on a bug
            self.post_message({"type": "log", "level": "error",
                               "message": permission_hint(exc)})

    # -- handlers ----------------------------------------------------------- #

    def _hello(self, _msg):
        # Runs on the UI thread, so this is where the network permission prompt
        # is raised for the first time.
        info = self._sync_from_orca(overwrite=False)
        self.post_message({"type": "cfg", "cfg": load_settings(), "orca": info})
        self._status({})
        self._files({})

    def _sync_from_orca(self, overwrite):
        """Copy what OrcaSlicer already knows into the plugin's own settings.

        ``overwrite=False`` only fills blanks, so a working configuration is never
        silently replaced by a preset the user may have left stale; the explicit
        "Load from OrcaSlicer" action passes True.
        """
        values, notes = preset_connection()
        if not values:
            return {"found": False, "applied": {}, "notes": []}
        cfg = load_settings()
        applied = {key: val for key, val in values.items()
                   if overwrite or not cfg.get(key)}
        if applied:
            save_settings(applied)
        return {"found": True, "applied": applied, "notes": notes, "values": values}

    def _load_orca(self, _msg):
        info = self._sync_from_orca(overwrite=True)
        self.post_message({"type": "cfg", "cfg": load_settings(), "orca": info})
        if not info["found"]:
            self.post_message({
                "type": "log",
                "message": "Nothing to load — OrcaSlicer has no printer connection "
                           "saved. Open it with the Connection button beside "
                           "Printer, set Host Type to Flashforge and fill in the "
                           "address, serial number and access code."})
            return
        summary = ("Loaded " + ", ".join(sorted(info["applied"])) + " from OrcaSlicer."
                   if info["applied"] else "Already up to date with OrcaSlicer.")
        # Poll first and report second: both land in the same tick, so a probe
        # that fails would otherwise bury the confirmation before it is read —
        # and when it fails, the error is the message that matters.
        error = self._status({})
        self._files({})
        if not error:
            self.post_message({"type": "log", "message": summary})

    def _status(self, _msg):
        status, error = RUNTIME.refresh()
        # Nothing to plot while the printer is unreachable: sampling zeros
        # would put a flat line on the chart that means "no data".
        temps = RUNTIME.sample_temps(status) if status else None
        finished = RUNTIME.track_job(status or {})
        if finished:
            self._announce(finished, (RUNTIME.entry or {}).get("name") or "")
        needs_setup = not (load_settings().get("host") or "")
        payload = {
            "type": "state",
            "status": status or {"name": "FlashForge", "status": "offline",
                                 "progress": 0, "nozzles": [], "spools": []},
            "needs_setup": needs_setup,
            "printers": printer_list(),
            "activePrinter": (active_printer() or {}).get("id"),
        }
        # The chart rides along only on the ticks that actually sampled, so the
        # poll stays light between samples.
        if temps is not None:
            payload["temps"] = temps
        self.post_message(payload)
        if error and not needs_setup:
            # With nothing configured the page shows a persistent setup banner
            # carrying a button; a plain log line would only overwrite it.
            self.post_message({"type": "log", "message": error})
        return error

    def _announce(self, record, printer_name=""):
        """Tell OrcaSlicer a print ended, in the 3D view rather than only here."""
        name = record.get("name") or "a print"
        result = record.get("result")
        message = {"finished": f"Print finished — {name}",
                   "cancelled": f"Print cancelled — {name}",
                   "error": f"Printer error — {name}"}.get(result, f"Print ended — {name}")
        who = f"{TAB_NAME} · {printer_name}: " if printer_name else f"{TAB_NAME}: "
        notify(who + message, warning=result == "error")

    def _maintenance(self, msg):
        """Bed levelling, input shaper, auto-shutdown. Requires an idle printer."""
        client = RUNTIME.client()
        if not client.configured():
            self.post_message({"type": "log", "message":
                               "Set the printer address, serial and check code first."})
            return
        with RUNTIME.lock:
            status = dict(RUNTIME.status or {})
        if str(status.get("status") or "") in BUSY_STATES:
            self.post_message({"type": "log", "message":
                               "The printer must be idle for maintenance commands."})
            return
        kind = msg.get("kind")
        try:
            if kind == "level":
                reply = client.calibrate(leveling=True)
            elif kind == "shaper":
                reply = client.calibrate(shaper=True)
            elif kind == "timer":
                minutes = int(msg.get("minutes") or 0)
                reply = client.shutdown_timer(minutes)
            else:
                raise PrinterError(f"Unknown maintenance command: {kind}")
        except PrinterError as exc:
            self.post_message({"type": "log", "message": str(exc)})
            return
        # /control answers Success to anything, so confirm by re-reading /detail
        # before telling the user it worked.
        confirmed, change = False, ""
        for _ in range(CONTROL_CONFIRM_TRIES):
            time.sleep(CONTROL_CONFIRM_DELAY)
            try:
                after = normalize_detail(client.detail())
            except PrinterError:
                continue
            if kind == "timer":
                want = "open" if int(msg.get("minutes") or 0) else "close"
                if after.get("auto_shutdown") == want:
                    confirmed, change = True, f"{after.get('auto_shutdown_time') or 0} min"
                    break
            elif kind == "level":
                confirmed, change = True, ""
                break
            elif kind == "shaper":
                confirmed, change = True, ""
                break
        if kind == "level":
            self.post_message({"type": "log", "message":
                               "Levelling started. This takes a few minutes — "
                               "the printer is busy until it finishes."})
        elif kind == "shaper":
            self.post_message({"type": "log", "message":
                               "Input shaper running. The printer will move the "
                               "gantry to measure resonance."})
        elif confirmed:
            label = "off" if not change else change
            self.post_message({"type": "log", "message":
                               f"Auto shutdown set to {label}."})
        else:
            self.post_message({"type": "log", "level": "warn", "message":
                               "The printer did not confirm the setting. Check the "
                               "printer screen."})
        self._status({})

    def _history(self, _msg):
        entry = RUNTIME.entry or {}
        history = load_history(entry.get("id"))
        RUNTIME.last_history = history
        self.post_message({"type": "history", **history})

    def _files(self, _msg):
        client = RUNTIME.client()
        if not client.configured():
            self.post_message({"type": "files", "files": []})
            return
        try:
            names, detail = client.gcode_list()
        except PrinterError:
            self.post_message({"type": "files", "files": []})
            return
        # Announce a tool-colour label per stored multi-colour file so the tab
        # can show something friendlier than a hex code.
        for item in detail:
            for tool in item.get("gcodeToolDatas") or []:
                tool["color_name"] = color_name(tool.get("materialColor"))
        self.post_message({"type": "files", "files": names, "detail": detail})

    def _preview(self, msg):
        filename = str(msg.get("filename") or "").strip()
        if not filename:
            return
        png = RUNTIME.client().thumbnail(filename)
        self.post_message({
            "type": "preview",
            "filename": filename,
            "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        })

    def _cfg_payload(self, orca=None):
        """The settings plus the printer list, so one message repaints both."""
        cfg = load_settings()
        payload = {"type": "cfg", "cfg": cfg,
                   "printers": printer_list(cfg),
                   "activePrinter": (active_printer(cfg) or {}).get("id")}
        if orca is not None:
            payload["orca"] = orca
        return payload

    def _new_printer(self, _msg):
        """Clear the connection form for a printer that is not in the list yet.

        No blank entry is stored: a printer with no address cannot be connected
        to, and storing one would put an unreachable row in the picker.
        """
        self.post_message(self._cfg_payload())

    def _save_cfg(self, msg):
        ok, error = save_printer({
            "id": (msg.get("id") or "").strip(),
            "name": msg.get("name") or "",
            "host": (msg.get("host") or "").strip(),
            "port": int(msg.get("port") or DEFAULT_API_PORT),
            "serial": (msg.get("serial") or "").strip(),
            "check_code": (msg.get("check_code") or "").strip(),
            "poll_seconds": int(msg.get("poll_seconds") or 3),
        })
        if not ok:
            self.post_message({"type": "error",
                               "message": f"Could not save settings: {error}"})
            return
        FLEET.sync()
        self.post_message(self._cfg_payload())
        self._status({})

    def _switch(self, msg):
        """Select a different printer. Everything on the tab follows this."""
        pid = str(msg.get("id") or "")
        if not any(p["id"] == pid for p in printer_list()):
            self.post_message({"type": "log", "message":
                               "That printer is not in the list."})
            return
        ok, error = save_settings({"active": pid})
        if not ok:
            self.post_message({"type": "error",
                               "message": f"Could not switch printer: {error}"})
            return
        FLEET.sync()
        self.post_message(self._cfg_payload({"found": False}))
        self._stop_camera()
        self._status({})
        self._files({})
        self._history({})

    def _remove(self, msg):
        pid = str(msg.get("id") or "")
        ok, error = remove_printer(pid)
        if not ok:
            self.post_message({"type": "error",
                               "message": f"Could not remove that printer: {error}"})
            return
        FLEET.sync()
        self.post_message(self._cfg_payload())
        self._stop_camera()
        self._status({})
        self._files({})
        self._history({})

    def _fleet(self, _msg):
        """Refresh every configured printer and report one compact row each."""
        rows = []
        for runtime in FLEET.all():
            _, error = runtime.refresh()
            status = runtime.status or {}
            state_name = str(status.get("status") or "")
            rows.append({
                "id": runtime.entry.get("id"),
                "name": runtime.entry.get("name") or "",
                "current": runtime.entry.get("id")
                           == (active_printer() or {}).get("id"),
                "status": state_name or "offline",
                "file": status.get("file") or "",
                "percent": round(status.get("progress") or 0.0, 1),
                "error": bool(error),
                # What the fleet filter keys on: something is wrong, or a job
                # is paused and waiting on a human.
                "attention": bool(error) or status.get("error_state")
                              or state_name == "paused",
            })
        self.post_message({"type": "fleet", "rows": rows})

    def _reprint(self, msg):
        filename = str(msg.get("filename") or "").strip()
        if not filename:
            return

        def work(progress):
            client = RUNTIME.client()
            names, detail = client.gcode_list()
            if filename not in names:
                raise PrinterError(f"'{filename}' is no longer on the printer "
                                   f"— slice and send it again")
            mappings = []
            for item in detail:
                if item.get("gcodeFileName") != filename:
                    continue
                for tool in item.get("gcodeToolDatas") or []:
                    mappings.append({
                        "toolId": int(tool.get("toolId") or 0),
                        "slotId": int(tool.get("slotId") or 1),
                        "materialName": tool.get("materialName") or "PLA",
                        "toolMaterialColor": tool.get("materialColor") or "#FFFFFF",
                        "slotMaterialColor": tool.get("materialColor") or "#FFFFFF",
                    })
                break
            progress(0.5, "Starting the print…")
            client.start_print(filename, mappings=mappings, leveling=True)
            RUNTIME.refresh(force=True)
            return f"{filename} is printing again."

        RUNTIME.start_job(work, "Reprinting…")

    def _control(self, msg):
        action = msg.get("action")

        def verify_pause(d):
            return str(d.get("status", "")).lower() == "paused"

        def verify_resume(d):
            return str(d.get("status", "")).lower() in ("printing", "resuming")

        def verify_cancel(d):
            return str(d.get("status", "")).lower() not in ACTIVE_STATES

        def verify_light(d):
            want = bool(msg.get("on"))
            now = str(d.get("lightStatus", "")).lower() in ("open", "on", "1", "true")
            return now == want

        def verify_clear(d):
            return str(d.get("status", "")).lower() != "completed"

        table = {
            "pause":   ("jobCtl_cmd",    {"action": "pause"},    verify_pause,
                        "Print paused"),
            "resume":  ("jobCtl_cmd",    {"action": "continue"}, verify_resume,
                        "Print resumed"),
            "cancel":  ("jobCtl_cmd",    {"action": "cancel"},   verify_cancel,
                        "Print cancelled"),
            "light":   ("lightControl_cmd", {"status": "open" if msg.get("on") else "close"},
                        verify_light, "Light toggled"),
            "clear":   ("stateCtrl_cmd", {"action": "setClearPlatform"}, verify_clear,
                        "Completion dialog cleared"),
        }
        entry = table.get(action)
        if not entry:
            self.post_message({"type": "error", "message": f"Unknown action {action}"})
            return
        cmd, args, verify, done = entry
        client = RUNTIME.client()
        ok, detail, how = control_checked(client, cmd, args, verify)
        if ok:
            if detail is not None:
                with RUNTIME.lock:
                    RUNTIME.status = normalize_detail(detail)
                    RUNTIME.error = None
            self.post_message({"type": "log", "message": f"{done} ({how} payload)"})
        else:
            now = str((detail or {}).get("status", "unknown")).lower()
            self.post_message({
                "type": "log", "level": "error",
                "message": f"{cmd} was accepted but the printer still reports "
                           f"'{now}'. /control acknowledges every command, so clear "
                           f"any dialog on the touchscreen — it stays wedged until you do.",
            })
        self._status({})

    def _job_status(self, _msg):
        with RUNTIME.lock:
            job = dict(RUNTIME.job)
        self.post_message({"type": "job", **job})

    # -- camera ------------------------------------------------------------- #

    def _camera_on(self, _msg):
        """Start the live view.

        One stream at a time: the printer's MJPEG server holds a single client,
        so switching the view on while it is already on would leave the first
        reader holding the socket nobody can use.
        """
        self._stop_camera()
        stop = threading.Event()
        FlashForgePage._camera_stop = stop
        self.post_message({"type": "camera", "on": True})
        threading.Thread(target=self._camera_worker, args=(stop,),
                         name="ffc5-camera", daemon=True).start()

    def _camera_off(self, _msg):
        self._stop_camera()
        self.post_message({"type": "camera", "on": False})

    def _stop_camera(self):
        """Signal the current reader to stop. It closes the stream itself."""
        stop = getattr(FlashForgePage, "_camera_stop", None)
        if stop is not None:
            stop.set()
        FlashForgePage._camera_stop = None

    def _camera_worker(self, stop):
        client = RUNTIME.client()
        try:
            for frame in client.camera_frames(on_stop=stop.is_set):
                if stop.is_set():
                    return
                self.post_message({
                    "type": "frame",
                    "image": "data:image/jpeg;base64,"
                             + base64.b64encode(frame).decode("ascii"),
                })
        except PrinterError as exc:
            self.post_message({"type": "error", "message": str(exc)})
        except Exception as exc:
            self.post_message({"type": "error", "message": permission_hint(exc)})
        finally:
            # The page only un-dims the panel on a camera message, and a reader
            # that stops is the only thing that knows the stream is gone.
            if stop.is_set():
                self.post_message({"type": "camera", "on": False})


# --------------------------------------------------------------------------- #
# Native printer agent
# --------------------------------------------------------------------------- #

class FlashForgeAgent(orca.printer_agent.PrinterAgentBase):
    """Registers the Creator 5 as an OrcaSlicer printer agent.

    With Preferences -> Developer -> Experimental Features -> *Use printer agents
    instead of print hosts* switched on, and this agent selected in the printer
    preset's Advanced settings, OrcaSlicer's own Send to printer and Print buttons
    talk to the Creator 5 directly.
    """

    def __init__(self):
        super().__init__()
        self.callbacks = {}
        self.params = None
        self.selected = ""

    def get_name(self):
        return "FlashForge Creator 5"

    def get_agent_info(self):
        return orca.printer_agent.AgentInfo(
            AGENT_ID, "FlashForge Creator 5", "1.0.0",
            "FlashForge Creator 5 / 5 Pro over the LAN HTTP API on port 8898.")

    # -- host callbacks ----------------------------------------------------- #

    def _store(self, key, fn):
        self.callbacks[key] = fn
        return 0

    def _emit(self, key, *args):
        fn = self.callbacks.get(key)
        if fn is not None:
            try:
                fn(*args)
            except Exception:
                pass

    def set_on_ssdp_msg_fn(self, fn): return self._store("ssdp", fn)
    def set_on_local_connect_fn(self, fn): return self._store("local_connect", fn)
    def set_on_local_message_fn(self, fn): return self._store("local_message", fn)
    def set_on_message_fn(self, fn): return self._store("message", fn)
    def set_on_user_message_fn(self, fn): return self._store("user_message", fn)
    def set_on_printer_connected_fn(self, fn): return self._store("printer_connected", fn)
    def set_on_subscribe_failure_fn(self, fn): return self._store("subscribe_failure", fn)
    def set_server_callback(self, fn): return self._store("server_error", fn)
    def set_queue_on_main_fn(self, fn): return self._store("queue_on_main", fn)

    # -- discovery / selection --------------------------------------------- #

    def start_discovery(self, start, sending):
        cfg = load_settings()
        host = cfg.get("host") or ""
        if start and host:
            self._emit("ssdp", json.dumps({
                "dev_id": host, "dev_name": "FlashForge Creator 5", "dev_ip": host,
                "dev_type": "Creator 5", "dev_signal": "",
                "connect_type": "lan", "bind_state": "free",
            }))
        return True

    def get_user_selected_machine(self):
        return self.selected

    def set_user_selected_machine(self, dev_id):
        self.selected = dev_id or ""
        return 0

    # -- connection --------------------------------------------------------- #

    def connect_printer(self, params):
        """Called with the preset's print host and access code.

        OrcaSlicer supplies the address and the access code; the serial number is
        not part of the preset, so it comes from the plugin's own settings.
        """
        try:
            self.params = params
            cfg = load_settings()
            host = getattr(params, "host", "") or cfg.get("host") or ""
            port = int(getattr(params, "port", "") or 0) or cfg.get("port") or DEFAULT_API_PORT
            code = getattr(params, "password", "") or cfg.get("check_code") or ""
            # The preset hands over the address and the access code but never the
            # serial, so take that from OrcaSlicer's own preset when it is there —
            # otherwise the agent path dead-ends asking the user to go and type it.
            serial = (cfg.get("serial")
                      or preset_connection()[0].get("serial") or "")
            # Persist what OrcaSlicer told us so both paths agree.
            update = {"host": host, "port": port}
            if code:
                update["check_code"] = code
            if params and getattr(params, "dev_id", ""):
                update.setdefault("dev_id", params.dev_id)
            save_settings(update)

            client = Creator5(host, serial, code, port)
            if not serial:
                self._emit("local_connect", 1, getattr(params, "dev_id", host),
                           "Set the printer serial in the FlashForge C5 tab")
                return -1
            # A quick probe doubles as the credential check.
            try:
                client.detail(timeout=6)
            except PrinterError as exc:
                self._emit("local_connect", 1, getattr(params, "dev_id", host), str(exc))
                return -1
            self._emit("local_connect", 0, getattr(params, "dev_id", host), "")
            return 0
        except Exception as exc:
            self._emit("local_connect", 1, "", f"{type(exc).__name__}: {exc}")
            return -1

    def disconnect_printer(self):
        self.params = None
        return 0

    # -- messaging ---------------------------------------------------------- #

    def send_message(self, dev_id, json_str, qos, flag):
        return -7010                       # no cloud path for this printer

    def send_message_to_printer(self, dev_id, json_str, qos, flag):
        """Translate a Bambu-style JSON command into the Creator 5 /control API."""
        try:
            payload = json.loads(json_str) if isinstance(json_str, str) else json_str
            command = ((payload or {}).get("print") or {}).get("command") or ""
            param = ((payload or {}).get("print") or {}).get("param") or ""
            client = self._client(dev_id)
            if command == "gcode_line" and param:
                for line in str(param).splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.upper().startswith("M140"):
                        client.control("temperatureCtl_cmd",
                                       {"platform": _temp_of(line)})
                    elif line.upper().startswith("M104"):
                        client.control("temperatureCtl_cmd",
                                       {"nozzles": [_temp_of(line)]})
                    elif line.upper().startswith("G28"):
                        client.control("printerCtl_cmd", {"zAxisCompensation": -200})
                return 0
            if command == "set_bed_temp":
                client.control("temperatureCtl_cmd", {"platform": _temp_of(param)})
                return 0
            if command == "pause":
                client.control("jobCtl_cmd", {"action": "pause"})
                return 0
            if command == "resume":
                client.control("jobCtl_cmd", {"action": "continue"})
                return 0
            if command == "stop":
                RUNTIME.job_cancel_seen = True
                client.control("jobCtl_cmd", {"action": "cancel"})
                return 0
            return -7010
        except Exception:
            return -1

    def _client(self, dev_id=None):
        cfg = load_settings()
        host = (self.params and getattr(self.params, "host", "")) or cfg.get("host") or ""
        port = int((self.params and getattr(self.params, "port", "")) or 0) \
            or cfg.get("port") or DEFAULT_API_PORT
        code = (self.params and getattr(self.params, "password", "")) \
            or cfg.get("check_code") or ""
        return Creator5(host, cfg.get("serial") or "", code, port)

    # -- jobs --------------------------------------------------------------- #

    def start_send_gcode_to_sdcard(self, params, update_fn, cancel_fn, wait_fn):
        """Send (upload without printing). Also used for the "verify_job" probe."""
        try:
            client = self._client()
            filename = getattr(params, "filename", "")
            if not filename or not os.path.exists(filename):
                return -1
            if getattr(params, "project_name", "") == "verify_job":
                client.detail(timeout=6)
                return 0
            if update_fn:
                update_fn(1, 0, "Uploading to the Creator 5")
            send_path, _ = unwrap_for_printer(filename)
            client.upload(send_path, print_now=False)
            if update_fn:
                update_fn(5, 0, "Uploaded")
            return 0
        except Exception as exc:
            if update_fn:
                try:
                    update_fn(-1, 0, f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
            return -1

    def start_local_print(self, params, update_fn, cancel_fn):
        """Print a sliced file over LAN: upload it, then start it."""
        try:
            client = self._client()
            filename = getattr(params, "filename", "")
            if not filename or not os.path.exists(filename):
                return -1
            if update_fn:
                update_fn(1, 0, "Uploading to the Creator 5")
            send_path, _ = unwrap_for_printer(filename)
            client.upload(send_path, print_now=False)
            mappings = _agent_mappings(params)
            if update_fn:
                update_fn(3, 0, "Starting the print")
            client.start_print(os.path.basename(send_path), mappings=mappings,
                               leveling=True)
            return 0
        except Exception as exc:
            if update_fn:
                try:
                    update_fn(-1, 0, f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass
            return -1

    def start_local_print_with_record(self, params, update_fn, cancel_fn, wait_fn):
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_print(self, params, update_fn, cancel_fn, wait_fn):
        return -7010                       # no cloud path

    def start_sdcard_print(self, params, update_fn, cancel_fn):
        """Print a file that is already on the printer."""
        try:
            client = self._client()
            filename = getattr(params, "filename", "")
            if not filename:
                return -1
            if not os.path.basename(filename) and getattr(params, "dst_file", ""):
                filename = params.dst_file
            name = os.path.basename(filename) or filename
            names, detail = client.gcode_list()
            mappings = []
            for item in detail:
                if item.get("gcodeFileName") != name:
                    continue
                for tool in item.get("gcodeToolDatas") or []:
                    mappings.append({
                        "toolId": int(tool.get("toolId") or 0),
                        "slotId": int(tool.get("slotId") or 1),
                        "materialName": tool.get("materialName") or "PLA",
                        "toolMaterialColor": tool.get("materialColor") or "#FFFFFF",
                        "slotMaterialColor": tool.get("materialColor") or "#FFFFFF",
                    })
                break
            if name not in names:
                return -1
            client.start_print(name, mappings=mappings, leveling=True)
            return 0
        except Exception:
            return -1

    # -- filament ----------------------------------------------------------- #

    def get_filament_sync_mode(self):
        return orca.printer_agent.FilamentSyncMode.Pull

    def fetch_filament_info(self, dev_id, sync_mode):
        """Refresh and cache the material station so the tab and the sidebar agree."""
        try:
            status, _ = RUNTIME.refresh(force=True)
            return bool(status)
        except Exception:
            return False

    def get_camera_stream_mode(self):
        return orca.printer_agent.CameraStreamMode.HTTP

    def get_camera_url(self):
        cfg = load_settings()
        if not cfg.get("host"):
            return ""
        return f"http://{cfg['host']}:{DEFAULT_CAM_PORT}{DEFAULT_CAM_PATH}"

    # -- binding: this printer has none ------------------------------------- #

    def bind_detect(self, dev_ip, sec_link, detect):
        detect.dev_id = dev_ip
        detect.dev_name = "FlashForge Creator 5"
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return 0

    def check_cert(self): return 0
    def ping_bind(self, ping_code): return 0
    def bind(self, dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn): return 0
    def unbind(self, dev_id): return 0
    def request_bind_ticket(self): return (0, "")


def _temp_of(text):
    """Pull the temperature out of M104/M140/G-code, e.g. 'M104 S220' -> 220.0."""
    for token in str(text).split():
        if token[:1].upper() == "S":
            try:
                return float(token[1:])
            except ValueError:
                return 0.0
    return 0.0


def _agent_mappings(params):
    """Best-effort mapping from OrcaSlicer's ams_mapping_info JSON."""
    raw = getattr(params, "ams_mapping_info", "") or ""
    if not raw:
        return []
    try:
        info = json.loads(raw)
    except Exception:
        return []
    out = []
    rows = info if isinstance(info, list) else info.get("mapping") or []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        out.append({
            "toolId": int(row.get("toolId", i)),
            "slotId": int(row.get("slotId", i + 1)),
            "materialName": row.get("materialName") or "PLA",
            "toolMaterialColor": row.get("toolMaterialColor") or row.get("color") or "#FFFFFF",
            "slotMaterialColor": row.get("slotMaterialColor") or row.get("color") or "#FFFFFF",
        })
    return out


# --------------------------------------------------------------------------- #
# Package
# --------------------------------------------------------------------------- #

@orca.plugin
class FlashForgeC5Plugin(orca.base):
    """The Creator 5 device tab, and the printer agent behind OrcaSlicer's own
    Send / Print buttons."""

    def register_capabilities(self):
        cfg = load_settings()
        declared = []
        if cfg.get("host"):
            # The bare host, with no port — and that is not cosmetic.
            #
            # Matching normalizes an entry to ``scheme://host`` (dropping userinfo
            # and port) and then only accepts it as host-wide when it *already*
            # equals its normalized form. So ``http://host:8898`` is
            # non-canonical, normalizes to ``http://host``, fails that comparison,
            # and is treated as a legacy grant scoped to that exact string — which
            # no request ever has. It therefore authorized **nothing**, leaving
            # every call to fall through to an interactive prompt: the API
            # endpoints worked on approvals given one URL at a time, and the
            # camera's URL had never been approved.
            #
            # One canonical entry covers every port, path and query on the host,
            # which is why the camera and the port-8898 API need no separate
            # entries.
            declared.append(f"http://{cfg['host']}")
        try:
            orca.request_permissions(
                network_http=declared,
                # The settings home sits beside orca_plugins, so an update does
                # not take the printer's saved settings with it.
                fs_readwrite=[PLUGIN_DIR, STAGING_DIR]
                + ([] if settings_home() == PLUGIN_DIR else [settings_home()]),
                threading=True,
            )
        except Exception:
            pass  # older builds may not expose the declaration

        orca.register_capability(FlashForgePage)
        orca.register_capability(FlashForgeAgent)
