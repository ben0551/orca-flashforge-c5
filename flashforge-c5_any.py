"""FlashForge C5 — an OrcaSlicer plugin for the FlashForge Creator 5 / 5 Pro.

What it does
------------
FlashForge's Creator 5 firmware exposes a complete local HTTP API on port 8898
(status, file list, upload, print, job control) but the printer registers itself
with the slicer as a *cloud-bound* Bambu-style device, so OrcaSlicer refuses to
send prints to it or show its status. This plugin closes that gap by speaking the
printer's own LAN protocol from inside OrcaSlicer, and by registering a printer
agent so OrcaSlicer's own Send / Print buttons work too.

Three capabilities, registered by one plugin:

* ``FlashForge C5`` tab (Pages) - live status, temperatures, material station,
  job control, a file browser for what is already on the printer, and a
  send-to-print panel. This is the interface described in the brief.
* ``Capture last slice`` (SlicingPipeline) - remembers the G-code OrcaSlicer just
  exported, so the tab can send it with one click. Needs selecting in the process
  preset (see README).
* ``FlashForge Creator 5`` (PrinterAgent) - makes OrcaSlicer's native Send to
  printer / Print buttons work against the Creator 5, so no tab interaction is
  needed at all for the common "slice then print" flow.

Only the Python standard library is used, so nothing has to be downloaded at
plugin load time.

Author: Ben Fischer.  License: MIT.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "FlashForge C5"
# description = "Send prints to a FlashForge Creator 5 / 5 Pro over LAN and watch the job inside OrcaSlicer."
# author = "Ben Fischer"
# version = "1.0.0"
# ///

import base64
import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile

import orca

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
STAGING_DIR = os.path.join(tempfile.gettempdir(), "ffc5-staging")

AGENT_ID = "flashforge_c5"
DEFAULT_API_PORT = 8898
DEFAULT_CAM_PORT = 8080
DEFAULT_CAM_PATH = "/?action=stream"

# Machine states in which the job block of /detail is meaningful. Outside these
# the firmware pads the block with uninitialised memory (see job_metric below),
# so an "unzipping" state must not count as printing.
ACTIVE_STATES = ("printing", "paused", "heating", "preheating", "resuming")

# /control answers {"code":0,"message":"Success"} to *any* command, including
# nonsense, so its reply proves nothing. Confirm by re-reading /detail.
CONTROL_CONFIRM_TRIES = 4
CONTROL_CONFIRM_DELAY = 0.7

CHUNK_BYTES = 512 * 1024


# --------------------------------------------------------------------------- #
# Settings (shared by every capability; kept next to the plugin)
# --------------------------------------------------------------------------- #

_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE = None


def _default_settings():
    return {
        "host": os.environ.get("FFC5_HOST", ""),
        "port": int(os.environ.get("FFC5_PORT", DEFAULT_API_PORT)),
        "serial": os.environ.get("FFC5_SERIAL", ""),
        "check_code": os.environ.get("FFC5_CHECK_CODE", ""),
        "leveling": True,
        "poll_seconds": 3,
    }


def load_settings():
    """Read settings.json, falling back to env vars and then to empty values."""
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE is not None:
            return dict(_SETTINGS_CACHE)
        data = _default_settings()
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key in ("host", "serial", "check_code", "port",
                            "leveling", "poll_seconds"):
                    if stored.get(key) not in (None, ""):
                        data[key] = stored[key]
        except FileNotFoundError:
            pass
        except Exception:
            pass  # unreadable/corrupt settings must not stop the plugin loading
        data["port"] = int(data.get("port") or DEFAULT_API_PORT)
        _SETTINGS_CACHE = dict(data)
        return dict(data)


def save_settings(update):
    """Merge `update` into settings.json. Returns (ok, error_or_None)."""
    global _SETTINGS_CACHE
    current = load_settings()
    for key in ("host", "serial", "check_code", "port", "leveling", "poll_seconds"):
        if key in update and update[key] is not None:
            current[key] = update[key]
    current["port"] = int(current.get("port") or DEFAULT_API_PORT)
    with _SETTINGS_LOCK:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(current, fh, indent=2, sort_keys=True)
            os.replace(tmp, SETTINGS_FILE)
            try:
                os.chmod(SETTINGS_FILE, 0o600)
            except OSError:
                pass
        except Exception as exc:
            _SETTINGS_CACHE = dict(current)
            return False, f"{type(exc).__name__}: {exc}"
    _SETTINGS_CACHE = dict(current)
    return True, None


# --------------------------------------------------------------------------- #
# Printer client — stdlib only, mirroring the vendor app's request shapes
# --------------------------------------------------------------------------- #

class PrinterError(Exception):
    """Any failure talking to the printer, with a human-readable message."""


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

    def gcode_list(self, timeout=15):
        data = self._unwrap(self._post("/gcodeList", timeout=timeout), "/gcodeList")
        names = data.get("gcodeList") or data.get("fileList") or data.get("files") or []
        detail = data.get("gcodeListDetail") or []
        return list(names), list(detail)

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
    }


def nearest_slot(spools, color):
    """Index of the loaded slot whose colour is closest to `color` (or None)."""
    def rgb(value):
        text = str(value or "").lstrip("#")
        if len(text) == 6:
            try:
                return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
            except ValueError:
                pass
        return None

    want = rgb(color)
    best, best_d = None, None
    for slot in spools:
        if not slot.get("loaded"):
            continue
        have = rgb(slot.get("color"))
        if want is None or have is None:
            if best is None:
                best = slot["id"]
            continue
        dist = sum((a - b) ** 2 for a, b in zip(want, have))
        if best_d is None or dist < best_d:
            best, best_d = slot["id"], dist
    if best is None and spools:
        best = spools[0]["id"]
    return best


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
# Staged-file inspection
# --------------------------------------------------------------------------- #

def staged_tools(path):
    """Tools a staged file needs, as [{"tool":1,"material":"PLA","color":"#fff"}].

    A `.3mf` carries `Metadata/slice_info.config` with one `<filament>` element
    per tool. A plain `.gcode` carries no tool metadata at all — and mappings are
    ignored by the printer for plain G-code anyway — so the list comes back empty.
    """
    if not path or not os.path.exists(path):
        return []
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as bundle:
                names = [n for n in bundle.namelist()
                         if n.endswith("slice_info.config")]
                if not names:
                    return []
                root = ET.fromstring(bundle.read(names[0]))
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(65536)
            marker = head.find("<!-- slice_info")
            if marker < 0:
                return []
            end = head.find("-->", marker)
            root = ET.fromstring(head[marker + 4:end])
    except Exception:
        return []

    tools = []
    for node in root.iter("filament"):
        tool_id = int(_num(node.get("id"), 0))
        tools.append({
            "tool": tool_id,
            "material": (node.get("type") or "").upper(),
            "color": node.get("color") or "",
            "used_g": _num(node.get("used_g"), 0.0),
        })
    tools.sort(key=lambda t: t["tool"])
    return tools


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
            stem = preferred_name or os.path.basename(path)
            stem = os.path.basename(stem)
            if stem.lower().endswith(".gcode.3mf"):
                stem = stem[: -len(".3mf")]
            else:
                stem = os.path.splitext(stem)[0]
            if not stem.lower().endswith(".gcode"):
                stem += ".gcode"
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
    """The live connection + the last status poll, shared by all capabilities."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = None
        self.error = None
        self.stage = None              # {"path","name","size","tools","print"}
        self.job = {"running": False, "pct": 0.0, "label": "", "error": None,
                    "done": False, "result": None}

    def client(self):
        cfg = load_settings()
        return Creator5(cfg.get("host") or "", cfg.get("serial") or "",
                        cfg.get("check_code") or "", cfg.get("port") or DEFAULT_API_PORT)

    def refresh(self, force=False):
        """Poll /detail. Skips the HTTP round trip when the host is down."""
        cfg = load_settings()
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


RUNTIME = Runtime()
# Set by the slicing-pipeline capability so the tab can one-click send a slice.
LAST_SLICE = {"path": None, "when": 0.0, "name": ""}


# --------------------------------------------------------------------------- #
# The tab
# --------------------------------------------------------------------------- #

PAGE = r"""
<style>
  :root{
    --bg:#1e1f22; --panel:#26282c; --panel2:#2d2f34; --line:#3a3d43;
    --fg:#e6e6e6; --dim:#9aa0a6; --accent:#7cc36a; --accent2:#4a90d9;
    --warn:#e0a030; --bad:#d9534f;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font:13px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
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
  #banner.err{border-color:var(--bad);background:#3a2323;color:#ffcdc9}
  #banner.ok{border-color:var(--accent);background:#243322;color:#d5f2cc}
  #banner.info{border-color:var(--accent2);background:#20303f;color:#cfe6ff}
  #banner button{margin-left:9px;padding:2px 9px;font-size:11px}
  .card.wanted{border-color:var(--accent2)}
  button{background:var(--panel2);color:var(--fg);border:1px solid var(--line);
         border-radius:6px;padding:6px 12px;font-size:12px;cursor:pointer}
  button:hover{border-color:#5a5f66}
  button.primary{background:#2f5d2a;border-color:#3f7a38}
  button.primary:hover{background:#376c31}
  button:disabled{opacity:.45;cursor:not-allowed}
  input[type=text],input[type=number],select{background:#1b1c1f;color:var(--fg);
        border:1px solid var(--line);border-radius:5px;padding:5px 7px;font-size:12px}
  input[type=text]{width:100%}
  label.chk{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim)}
  .kv{display:flex;justify-content:space-between;padding:2px 0;font-size:12px}
  .kv span:first-child{color:var(--dim)}
  #bar{height:9px;background:#1b1c1f;border:1px solid var(--line);border-radius:99px;
       overflow:hidden;margin:6px 0}
  #bar>i{display:block;height:100%;width:0;background:linear-gradient(90deg,#4a90d9,#7cc36a);
         transition:width .25s}
  .bar{height:7px;background:#1b1c1f;border:1px solid var(--line);border-radius:99px;overflow:hidden}
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
  .frow{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid #2f3238}
  .frow:last-child{border-bottom:none}
  .fname{flex:1;font-size:12px;word-break:break-all}
  .fmeta{font-size:11px;color:var(--dim)}
  .nozzles{display:flex;gap:8px;flex-wrap:wrap}
  .noz{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:6px 9px;min-width:82px}
  .noz .t{font-size:11px;color:var(--dim)}
  .noz .v{font-size:14px;font-weight:600}
  .grid2{display:grid;grid-template-columns:130px 1fr;gap:7px 9px;align-items:center}
  .dim{color:var(--dim)} .small{font-size:11px}
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
    <h3>Material station</h3>
    <div class="spools" id="spools"><span class="dim small">waiting for the printer…</span></div>
  </div>

  <div class="card">
    <h3>Send a print</h3>
    <div class="row" style="margin-bottom:8px">
      <button id="blast">Send last OrcaSlicer slice</button>
      <span class="small dim" id="lastinfo">no slice captured yet</span>
    </div>
    <div class="grid2">
      <label class="chk" for="bfile">Model file</label>
      <div>
        <input type="file" id="bfile" accept=".gcode,.3mf,.gx,.gcode.3mf">
        <div class="small dim" id="finfo">OrcaSlicer writes .gcode.3mf bundles; the plugin
          strips the bundle down to the G-code the printer can actually start.</div>
      </div>
      <span></span>
      <label class="chk"><input type="checkbox" id="clevel" checked> Bed levelling before print</label>
    </div>
    <div id="mapwrap" class="hidden" style="margin-top:9px">
      <div class="small dim" style="margin-bottom:5px">Which spool feeds each tool — check this,
        the default is only a nearest-colour guess.</div>
      <div class="tools" id="tools"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <button id="bsend" disabled>Send only</button>
      <button id="bsendprint" class="primary" disabled>Send &amp; print</button>
    </div>
    <div id="upwrap" class="hidden" style="margin-top:8px">
      <div class="bar"><i id="upbar"></i></div>
      <div class="small dim" id="uplabel" style="margin-top:4px"></div>
    </div>
  </div>

  <div class="card">
    <h3>Files on the printer</h3>
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
      <span class="small dim" id="cfginfo">Saved to settings.json beside the plugin, mode 0600.
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
  function fmtSize(n){
    n = Number(n) || 0;
    if (n > 1048576) { return (n / 1048576).toFixed(1) + " MB"; }
    if (n > 1024) { return (n / 1024).toFixed(0) + " KB"; }
    return n + " B";
  }
  function esc(s){
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function post(obj){ orca.postMessage(obj); }

  var state = { spools: [], status: null, staged: null, cfg: null, files: [] };
  var pollTimer = null, uploadTimer = null, stagedFile = null, stageSent = 0;

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
    paintPicker();
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

  function slotOptions(selected){
    var out = "";
    var spools = state.spools || [];
    for (var i = 0; i < spools.length; i++) {
      var s = spools[i];
      if (!s.loaded) { continue; }
      var label = "Slot " + s.id + " — " + (s.material || "?") +
                  (s.color_name ? (" (" + s.color_name + ")") : "");
      out += '<option value="' + esc(s.id) + '"' +
             (String(s.id) === String(selected) ? " selected" : "") + '>' +
             esc(label) + '</option>';
    }
    if (!out) { out = '<option value="">no loaded spools</option>'; }
    return out;
  }

  function paintPicker(){
    var tools = (state.staged && state.staged.tools) || [];
    var wrap = $("mapwrap");
    if (!tools.length) { wrap.className = "hidden"; $("tools").innerHTML = ""; return; }
    var out = "";
    for (var i = 0; i < tools.length; i++) {
      var t = tools[i];
      var sw = t.color ? t.color : "#555";
      out += '<div class="tool"><div class="t">Tool ' + t.tool + ' · ' +
             esc(t.material || "?") + '</div>' +
             '<div class="sw" style="background:' + esc(sw) + '"></div>' +
             '<select data-tool="' + t.tool + '">' + slotOptions(t.suggested) + '</select></div>';
    }
    $("tools").innerHTML = out;
    wrap.className = "";
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
      var sub = d.printingTime ? (" · " + d.printingTime) : "";
      if (d.totalFilamentWeight) { sub += " · " + d.totalFilamentWeight + " g"; }
      out += '<div class="frow"><div class="fname">' + esc(name) +
             '<div class="fmeta">stored on printer' + esc(sub) + '</div></div>' +
             '<button data-reprint="' + esc(name) + '">Reprint</button></div>';
    }
    $("files").innerHTML = out;
    var btns = $("files").querySelectorAll("button[data-reprint]");
    for (var k = 0; k < btns.length; k++) {
      btns[k].addEventListener("click", onReprint);
    }
  }

  function paintCfg(cfg){
    state.cfg = cfg || {};
    $("host").value   = cfg.host || "";
    $("port").value   = cfg.port || 8898;
    $("serial").value = cfg.serial || "";
    $("code").value   = cfg.check_code || "";
    $("poll").value   = cfg.poll_seconds || 3;
    $("clevel").checked = cfg.leveling !== false;
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
    var ids = ["bsend", "bsendprint", "blast"];
    for (var i = 0; i < ids.length; i++) { $(ids[i]).disabled = !!on; }
  }

  // ---- upload: read the chosen file into the plugin, in chunks ---------- //
  //
  // The bytes never go to the printer directly: the plugin stages them in a temp
  // file so it can read the tool list out of the 3MF and show a real spool
  // picker. Pacing is ack-driven — the plugin acknowledges each chunk and only
  // then is the next one read, so a slow disk or a big file cannot flood the
  // message bridge.

  function readChunk(blob, start, end, cb){
    var reader = new FileReader();
    reader.onload = function(){
      var bytes = new Uint8Array(reader.result);
      var bin = "";
      var STRIDE = 8192;
      for (var i = 0; i < bytes.length; i += STRIDE) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + STRIDE));
      }
      cb(btoa(bin));
    };
    reader.onerror = function(){ showBanner("Could not read the file", "err"); };
    reader.readAsArrayBuffer(blob.slice(start, end));
  }

  function stageFile(file){
    stagedFile = file;
    stageSent = 0;
    $("upwrap").className = "";
    $("upbar").style.width = "0%";
    $("uplabel").textContent = "Reading " + file.name + "…";
    $("bsend").disabled = true;
    $("bsendprint").disabled = true;
    post({ type: "stage_begin", name: file.name, size: file.size });
  }

  function sendChunkFrom(offset){
    if (!stagedFile || offset >= stagedFile.size) { return; }
    var end = Math.min(offset + 524288, stagedFile.size);
    readChunk(stagedFile, offset, end, function(b64){
      stageSent = end;
      post({ type: "stage_chunk", seq: offset, data: b64 });
    });
  }

  function fileChunksReset(){ stagedFile = null; stageSent = 0; }

  // ---- events ----------------------------------------------------------- //

  function onReprint(ev){
    var name = ev.currentTarget.getAttribute("data-reprint");
    if (!name) { return; }
    showBanner("Starting " + name + "…", "info");
    post({ type: "reprint", filename: name });
  }

  function wire(){
    $("bsave").addEventListener("click", function(){
      post({ type: "save_cfg", host: $("host").value.trim(),
             port: Number($("port").value) || 8898,
             serial: $("serial").value.trim(),
             check_code: $("code").value.trim(),
             poll_seconds: Number($("poll").value) || 3 });
    });
    $("bfile").addEventListener("change", function(){
      var f = $("bfile").files && $("bfile").files[0];
      state.staged = null;
      $("mapwrap").className = "hidden";
      $("bsend").disabled = true;
      $("bsendprint").disabled = true;
      if (!f) { $("finfo").textContent = "No file chosen."; return; }
      $("finfo").textContent = f.name + " — " + fmtSize(f.size);
      stageFile(f);
    });
    $("bsend").addEventListener("click", function(){ clickSend(false); });
    $("bsendprint").addEventListener("click", function(){ clickSend(true); });
    $("blast").addEventListener("click", function(){ post({ type: "send_last" }); });

    $("bpause").addEventListener("click", function(){ post({ type: "control", action: "pause" }); });
    $("bresume").addEventListener("click", function(){ post({ type: "control", action: "resume" }); });
    $("bcancel").addEventListener("click", function(){ post({ type: "control", action: "cancel" }); });
    $("blight").addEventListener("click", function(){
      var on = !(state.status && state.status.light_on);
      post({ type: "control", action: "light", on: on });
    });
    $("bclear").addEventListener("click", function(){ post({ type: "control", action: "clear" }); });

    $("poll").addEventListener("change", schedule);
  }

  function clickSend(alsoPrint){
    if (!state.staged) {
      showBanner("Pick a model file above, or capture a slice with 'Send last "
                 + "OrcaSlicer slice'.", "err");
      return;
    }
    var f = $("bfile").files && $("bfile").files[0];
    if (f && state.staged.name !== f.name) {
      showBanner("Still reading " + f.name + " — one moment.", "info");
      return;
    }
    commit(alsoPrint);
  }

  function currentMappings(){
    var sel = $("tools").querySelectorAll("select[data-tool]");
    var out = [];
    for (var i = 0; i < sel.length; i++) {
      out.push({ tool: Number(sel[i].getAttribute("data-tool")),
                 slot: Number(sel[i].value) });
    }
    return out;
  }

  function commit(alsoPrint){
    setJobRunning(true);
    $("upwrap").className = "";
    $("upbar").style.width = "0%";
    $("uplabel").textContent = "Sending to the printer…";
    post({ type: "commit", print: !!alsoPrint, leveling: $("clevel").checked,
           mappings: currentMappings() });
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
        $("lastinfo").textContent = m.last_slice
          ? ("last slice: " + m.last_slice + " — ready to send")
          : "no slice captured yet";
        $("stt").textContent = (m.status && m.status.status) || "unknown";
        showSetupBanner(!!m.needs_setup);
        break;
      case "cfg":   paintCfg(m.cfg); break;
      case "files": paintFiles(m.files, m.detail); break;
      case "staged":
        state.staged = { name: m.name, tools: m.tools || [] };
        paintPicker();
        $("upwrap").className = "hidden";
        setJobRunning(false);
        showBanner("Ready — " + m.name + " inspected. Check the spool mapping, then send.",
                   "ok");
        break;
      case "stage_ack":
        if (stagedFile && Number(m.seq) === stageSent) {
          var pct = Math.round(100 * stageSent / Math.max(1, stagedFile.size));
          $("upbar").style.width = pct + "%";
          $("uplabel").textContent = "Reading " + stagedFile.name + " — " + pct + "%";
          sendChunkFrom(stageSent);
        }
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
          fileChunksReset();
          $("upwrap").className = "hidden";
          post({ type: "files" });
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
                "stage_begin": self._stage_begin,
                "stage_chunk": self._stage_chunk,
                "commit": self._commit,
                "send_last": self._send_last,
                "reprint": self._reprint,
                "control": self._control,
                "job_status": self._job_status,
            }.get(kind)
            if handler:
                handler(msg)
        except PrinterError as exc:
            self.post_message({"type": "error", "message": str(exc)})
        except Exception as exc:            # never let the tab die on a bug
            self.post_message({"type": "log", "level": "error",
                               "message": f"{type(exc).__name__}: {exc}"})

    # -- handlers ----------------------------------------------------------- #

    def _hello(self, _msg):
        # Runs on the UI thread, so this is where the network permission prompt
        # is raised for the first time.
        self.post_message({"type": "cfg", "cfg": load_settings()})
        self._status({})
        self._files({})

    def _status(self, _msg):
        status, error = RUNTIME.refresh()
        needs_setup = not (load_settings().get("host") or "")
        self.post_message({
            "type": "state",
            "status": status or {"name": "FlashForge", "status": "offline",
                                 "progress": 0, "nozzles": [], "spools": []},
            "last_slice": LAST_SLICE.get("name") or None,
            "needs_setup": needs_setup,
        })
        if error and not needs_setup:
            # With nothing configured the page shows a persistent setup banner
            # carrying a button; a plain log line would only overwrite it.
            self.post_message({"type": "log", "message": error})

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

    def _save_cfg(self, msg):
        ok, error = save_settings({
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
        self.post_message({"type": "cfg", "cfg": load_settings()})
        self._status({})

    def _stage_begin(self, msg):
        name = os.path.basename(str(msg.get("name") or "model.gcode"))
        size = int(msg.get("size") or 0)
        os.makedirs(STAGING_DIR, exist_ok=True)
        path = os.path.join(STAGING_DIR, f"{uuid.uuid4().hex[:8]}-{name}")
        handle = open(path, "wb")
        ensure_spools()
        RUNTIME.stage = {"path": path, "name": name, "size": size,
                         "handle": handle, "received": 0, "tools": []}
        self.post_message({"type": "stage_ack", "seq": 0})

    def _stage_chunk(self, msg):
        stage = RUNTIME.stage
        if not stage or stage.get("handle") is None:
            self.post_message({"type": "error",
                               "message": "No file is being staged — pick a file again"})
            return
        blob = base64.b64decode(msg.get("data") or "")
        stage["handle"].write(blob)
        stage["received"] += len(blob)
        if stage["received"] < stage["size"]:
            self.post_message({"type": "stage_ack", "seq": stage["received"]})
            return
        stage["handle"].close()
        stage["handle"] = None
        tools = staged_tools(stage["path"])
        spools = (RUNTIME.status or {}).get("spools") or []
        for tool in tools:
            tool["suggested"] = nearest_slot(spools, tool.get("color"))
            tool["color_name"] = color_name(tool.get("color"))
        stage["tools"] = tools
        self.post_message({"type": "staged", "name": stage["name"], "tools": tools})

    def _commit(self, msg):
        stage = RUNTIME.stage
        if not stage:
            self.post_message({"type": "error",
                               "message": "No file is staged — pick a file and click Send"})
            return
        print_now = bool(msg.get("print"))
        leveling = bool(msg.get("leveling", True))
        chosen = {int(m.get("tool") or 0): int(m.get("slot") or 0)
                  for m in (msg.get("mappings") or [])}
        path, name = stage["path"], stage["name"]
        tools = stage["tools"] or []

        def work(progress):
            client = RUNTIME.client()
            if not client.configured():
                raise PrinterError("Printer address, serial or check code is missing "
                                   "— fill in the connection panel")
            progress(0.10, "Preparing the file…")
            send_path, extracted = unwrap_for_printer(path, name)
            note = ""
            if extracted:
                note = (f"OrcaSlicer bundles G-code inside {os.path.basename(path)}; "
                        f"sent {os.path.basename(send_path)} instead, which the "
                        f"Creator 5 can start.")
            progress(0.30, "Uploading to the printer…")
            client.upload(send_path, print_now=False, leveling=leveling)
            filename = os.path.basename(send_path)
            if print_now:
                progress(0.75, "Starting the print…")
                client.start_print(filename, mappings=mappings_for(client, tools, chosen),
                                   leveling=leveling)
            detail, _ = RUNTIME.refresh(force=True)
            verb = "sent and printing" if print_now else "sent to the printer"
            return f"{filename} {verb}. {note}".strip()

        RUNTIME.start_job(work, "Sending…")

    def _send_last(self, msg):
        path = LAST_SLICE.get("path")
        if not path or not os.path.exists(path):
            self.post_message({"type": "error",
                               "message": "No OrcaSlicer slice captured yet. Enable "
                                          "'Capture last slice' in the process preset, "
                                          "or pick a file above."})
            return
        ensure_spools()
        tools = staged_tools(path)
        spools = (RUNTIME.status or {}).get("spools") or []
        for tool in tools:
            tool["suggested"] = nearest_slot(spools, tool.get("color"))
        RUNTIME.stage = {"path": path, "name": os.path.basename(path), "size": 0,
                         "handle": None, "received": 0, "tools": tools}
        self.post_message({"type": "staged", "name": os.path.basename(path),
                           "tools": tools})
        self.post_message({"type": "log",
                           "message": f"Last slice {os.path.basename(path)} staged — "
                                      f"check the spool mapping, then Send & print."})

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


def ensure_spools():
    """Populate the spool cache if the tab has not polled yet."""
    if not (RUNTIME.status or {}).get("spools"):
        RUNTIME.refresh()


def mappings_for(client, tools, chosen):
    """Build the printer's 5-field mapping per tool from the user's slot picks.

    ``toolId`` is 0-based and ``slotId`` is 1-based; the older four-field shape is
    silently ignored by the firmware, so keep these five keys exactly.
    """
    spools = {s.get("id"): s for s in ((RUNTIME.status or {}).get("spools") or [])}
    if not spools:
        try:
            spools = {s["id"]: s for s in normalize_detail(client.detail())["spools"]}
        except PrinterError:
            spools = {}

    rows = tools or []
    if not rows:
        # A plain .gcode carries no tool metadata. Map tool N to slot N so a
        # single-colour print still names the spool it is actually drawn from.
        rows = [{"tool": i + 1, "material": "", "color": ""}
                for i in range(max(1, len(spools) or 1))]
        if not spools:
            rows = [{"tool": 1, "material": "", "color": ""}]

    out = []
    for row in rows:
        tool_no = int(row.get("tool") or 1)
        slot_id = chosen.get(tool_no) or row.get("suggested") or 1
        spool = spools.get(slot_id) or {}
        out.append({
            "toolId": tool_no - 1,
            "slotId": int(slot_id),
            "materialName": spool.get("material") or row.get("material") or "PLA",
            "toolMaterialColor": row.get("color") or spool.get("color") or "#FFFFFF",
            "slotMaterialColor": spool.get("color") or "#FFFFFF",
        })
    return out


# --------------------------------------------------------------------------- #
# Capture the last slice
# --------------------------------------------------------------------------- #

class LastSliceCapture(orca.slicing.SlicingPipelineCapabilityBase):
    """Remember the G-code OrcaSlicer just exported.

    Needs enabling per process preset: add it under ``post_process_plugin``
    (Plugin Development -> 'plugin references in presets'). Reading the file is
    allowed at this step because the export folder is a pre-approved root for the
    duration of the call, so no permission prompt appears.
    """

    def get_name(self):
        return "Capture last slice"

    def execute(self, ctx):
        try:
            if getattr(ctx, "step", None) != orca.slicing.Step.psGCodePostProcess:
                return orca.ExecutionResult.success()
            path = getattr(ctx, "gcode_path", None)
            if path and os.path.exists(path):
                LAST_SLICE["path"] = path
                LAST_SLICE["when"] = time.time()
                LAST_SLICE["name"] = getattr(ctx, "output_name", None) or os.path.basename(path)
            return orca.ExecutionResult.success("Captured for the FlashForge C5 tab")
        except Exception as exc:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                f"Could not capture the slice: {type(exc).__name__}: {exc}")


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
            serial = cfg.get("serial") or ""
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
    """One plugin, three capabilities: a tab, a slice capturer, a printer agent."""

    def register_capabilities(self):
        cfg = load_settings()
        declared = []
        if cfg.get("host"):
            declared.append(f"http://{cfg['host']}:{cfg.get('port') or DEFAULT_API_PORT}")
        if cfg.get("host"):
            declared.append(f"http://{cfg['host']}:{DEFAULT_CAM_PORT}")
        try:
            orca.request_permissions(
                network_http=declared,
                fs_readwrite=[PLUGIN_DIR, STAGING_DIR],
                threading=True,
            )
        except Exception:
            pass  # older builds may not expose the declaration

        orca.register_capability(FlashForgePage)
        orca.register_capability(LastSliceCapture)
        orca.register_capability(FlashForgeAgent)
