"""A stand-in for the FlashForge Creator 5's port 8898 API.

Implements exactly the endpoints the plugin uses, and records what it received so
the tests can assert on the *bytes and headers the printer would see* rather than
on the plugin's own idea of what it sent.
"""

import base64
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# A real 8x8 PNG, the shape /gcodeThumb actually returns (base64 in imageData).
PREVIEW_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAI0lEQVR4nGNgIAAY"
                   "GRgYGE6cOPEfm6SFhQUjEyETBoMCggAAeWwEDHRk95IAAAAASUVORK5CYII=")

SPOOLS = [
    {"slotId": 1, "materialName": "PLA", "materialColor": "#FFFFFF", "hasFilament": True},
    {"slotId": 2, "materialName": "PETG", "materialColor": "#000000", "hasFilament": True},
    {"slotId": 3, "materialName": "PLA", "materialColor": "#D01012", "hasFilament": True},
    {"slotId": 4, "materialName": "PLA", "materialColor": "#0057B8", "hasFilament": False},
]


class MockPrinter:
    def __init__(self, serial="SN123", check_code="CODE9"):
        # Per-printer credentials, so a second machine can answer on its own.
        self.serial = serial
        self.check_code = check_code
        self.lock = threading.Lock()
        self.state = {
            "status": "ready",
            "lightStatus": "close",
            "printProgress": 0.0,
            "printDuration": 0,
            "estimatedTime": 0,
            "printFileName": "",
            "platTemp": 24.0,
            "platTargetTemp": 0.0,
            "chamberTemp": 27.0,
            "chamberTargetTemp": 0.0,
            "nozzleTemps": [22.0, 23.0, 21.0, 24.0],
            "nozzleTargetTemps": [0.0, 0.0, 0.0, 0.0],
            "doorStatus": "closed",
            "autoShutdown": "close",
            "autoShutdownTime": 0,
            "remainingDiskSpace": 12.5,
            "ipAddr": "127.0.0.1",
            "name": "Creator 5 Pro",
            "firmwareVersion": "2.0.0",
            "matlStationInfo": {"slotCnt": 4, "currentSlot": 1, "slotInfos": SPOOLS},
        }
        # Records, for assertions.
        self.uploads = []          # {"headers": {...}, "filename":..., "bytes":...}
        self.prints = []           # /printGcode bodies
        self.controls = []         # /control payloads
        self.thumbs = []           # filenames sent to /gcodeThumb
        self.auth_probe = []       # credentials seen on /detail
        self.fail_next_upload = False
        # Set by a test to make /control accept but never change anything, which
        # is exactly what the real firmware does for a command it ignores.
        self.control_noop = True

    def detail(self):
        with self.lock:
            return dict(self.state)

    def set(self, **kw):
        with self.lock:
            self.state.update(kw)


def _parse_multipart(body, boundary):
    """Return {field_name: {"filename":..., "content": bytes}}."""
    parts = {}
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        head, _, content = chunk.partition(b"\r\n\r\n")
        content = content[:-2] if content.endswith(b"\r\n") else content
        text = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', text)
        fname = re.search(r'filename="([^"]*)"', text)
        if name:
            parts[name.group(1)] = {
                "filename": fname.group(1) if fname else None,
                "content": content,
            }
    return parts


def make_handler(printer):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _json(self, payload, status=200):
            blob = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def do_GET(self):
            if urlparse(self.path).path == "/healthz":
                self._json({"ok": True})
            else:
                self._json({"code": 1, "message": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            raw = self._read_body()

            if path == "/uploadGcode":
                ctype = self.headers.get("Content-Type", "")
                match = re.search(r"boundary=([^;]+)", ctype)
                if "multipart/form-data" not in ctype or not match:
                    self._json({"code": -1, "message": "Content-type is error"})
                    return
                # Every piece of metadata must arrive as a HEADER. Body
                # credentials produce a misleading "SN is different".
                for needed in ("serialNumber", "checkCode", "fileSize", "printNow"):
                    if not self.headers.get(needed):
                        self._json({"code": 1, "message": "SN is different"})
                        return
                if str(self.headers.get("fileSize")) == "0":
                    self._json({"code": -1, "message": "File size is 0"})
                    return
                parts = _parse_multipart(raw, match.group(1).encode())
                if "gcodeFile" not in parts:
                    self._json({"code": -1, "message": "Content-type is error"})
                    return
                part = parts["gcodeFile"]
                if printer.fail_next_upload:
                    printer.fail_next_upload = False
                    self._json({"code": 2, "message": "Printer is busy"})
                    return
                if int(self.headers.get("fileSize")) != len(part["content"]):
                    self._json({"code": -1, "message": "File size is 0"})
                    return
                printer.uploads.append({
                    "headers": dict(self.headers),
                    "filename": part["filename"],
                    "bytes": part["content"],
                })
                printer.set(printFileName=part["filename"] or "")
                self._json({"code": 0, "message": "Success"})
                return

            try:
                body = json.loads(raw.decode() or "{}")
            except ValueError:
                body = {}

            if path == "/detail":
                printer.auth_probe.append(
                    (body.get("serialNumber"), body.get("checkCode")))
                if (body.get("serialNumber") != printer.serial
                        or body.get("checkCode") != printer.check_code):
                    self._json({"code": 1, "message": "SN is different"})
                    return
                self._json({"code": 0, "message": "Success",
                            "detail": printer.detail()})
                return

            if path == "/gcodeThumb":
                # The only read-back route the firmware has. Sending an
                # unrecognised path is how a wrong guess looks: a 404.
                name = body.get("fileName")
                stored = [u["filename"] for u in printer.uploads]
                if name not in stored:
                    self._json({"code": 1, "message": "file not found"})
                    return
                printer.thumbs.append(name)
                self._json({"code": 0, "message": "Success",
                            "imageData": PREVIEW_PNG_B64})
                return

            if path == "/gcodeList":
                names = [u["filename"] for u in printer.uploads]
                detail = [{
                    "gcodeFileName": u["filename"],
                    # The device's real units: seconds as an int, and an
                    # unrounded float weight. A pre-formatted string here would
                    # hide the page's formatting bug rather than catch it.
                    "printingTime": 910,
                    "totalFilamentWeight": 7.840000152587891,
                    "gcodeToolDatas": [
                        {"toolId": 0, "slotId": 1, "materialName": "PLA",
                         "materialColor": "#FFFFFF", "filamentWeight": 43},
                    ],
                } for u in printer.uploads]
                self._json({"code": 0, "message": "Success",
                            "gcodeList": names, "gcodeListDetail": detail})
                return

            if path == "/printGcode":
                printer.prints.append(body)
                name = body.get("fileName")
                stored = [u["filename"] for u in printer.uploads]
                if name not in stored:
                    self._json({"code": 3, "message": "file not found"})
                    return
                printer.set(status="printing", printFileName=name,
                            printProgress=0.02, printDuration=5,
                            estimatedTime=1800, platTargetTemp=60.0,
                            nozzleTargetTemps=[215.0, 0.0, 0.0, 0.0])
                self._json({"code": 0, "message": "Success"})
                return

            if path == "/control":
                printer.controls.append(body)
                if not printer.control_noop:
                    payload = body.get("payload") or {}
                    cmd = payload.get("cmd")
                    args = payload.get("args") or payload
                    if cmd == "lightControl_cmd":
                        printer.set(lightStatus=args.get("status") or "close")
                    elif cmd == "jobCtl_cmd":
                        action = args.get("action")
                        printer.set(status={"pause": "paused",
                                            "continue": "printing",
                                            "cancel": "ready"}.get(action, "ready"))
                    elif cmd == "stateCtrl_cmd":
                        printer.set(status="ready")
                    elif cmd == "calibration_cmd":
                        if args.get("levelingDetection") == "open":
                            printer.set(status="leveling")
                        if args.get("vibrationCompensation") == "open":
                            printer.set(status="shaping")
                    elif cmd == "delayClose_cmd":
                        on = args.get("automaticShutdown") == "open"
                        printer.set(autoShutdown="open" if on else "close",
                                    autoShutdownTime=args.get("shutdownAfterTime") or 0)
                self._json({"code": 0, "message": "Success"})
                return

            self._json({"code": 1, "message": "unknown endpoint"}, 404)

    return Handler


class MockServer:
    def __init__(self, serial="SN123", check_code="CODE9"):
        self.printer = MockPrinter(serial=serial, check_code=check_code)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.printer))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class MockCamera:
    """Stands in for the printer's MJPEG server, on its own port.

    The real thing is gated by `streamCtrl open` and holds exactly one client per
    open, so this records each connect and disconnect: a reader that walks off
    without closing leaves the printer's camera dead for the next one, and that
    is only visible if the double notices.
    """

    def __init__(self, frames=3, chunk=4096, fail_first=0):
        self.frames = frames
        self.chunk = chunk
        # Connects that are closed with no reply at all, which is what the
        # printer does when no stream was opened for this client.
        self.fail_first = fail_first
        self.connects = 0
        self.disconnects = 0
        cam = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                cam.connects += 1
                if cam.connects <= cam.fail_first:
                    cam.disconnects += 1
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    self.connection.close()
                    return
                try:
                    body = mjpeg_stream(cam.frames)
                    self.send_response(200)
                    self.send_header(
                        "Content-Type",
                        "multipart/x-mixed-replace;boundary=boundarydonotcross")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    for i in range(0, len(body), cam.chunk):
                        self.wfile.write(body[i:i + cam.chunk])
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    cam.disconnects += 1

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        # The real server is only up while the printer says it is.
        self.enabled = False

    @staticmethod
    def frame(i):
        """A stand-in JPEG: the real SOI/EOI markers, with a nameable payload."""
        return b"\xff\xd8" + b"FAKE-FRAME-" + str(i).encode() + b"\xff\xd9"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def mjpeg_stream(frames=3):
    """The multipart body the printer sends, as one blob, for parser tests."""
    out = b""
    for i in range(frames):
        out += (b"--boundarydonotcross\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + MockCamera.frame(i) + b"\r\n")
    return out + b"--boundarydonotcross--\r\n"
