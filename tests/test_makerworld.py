"""Tests for the MakerWorld C5 plugin: the project conversion and the hand-off.

The conversion is the part with real behaviour to get wrong, so the assertions
are made against the *converted project settings* - what OrcaSlicer would load -
and against the bytes the hand-off would send, not against what the plugin
believes it produced.

    python3 tests/test_makerworld.py
"""

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import fake_orca                                     # noqa: E402

RESULTS = []


def check(name, condition, detail=""):
    RESULTS.append((name, bool(condition), detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    return bool(condition)


# --- fixtures -------------------------------------------------------------- #

# A MakerWorld .3mf as it actually arrives: a Bambu Studio project, with the
# designer's tuned values on top of a machine the user does not own. The values
# that matter are deliberately different from the FlashForge profile's, so
# "kept" and "replaced" are distinguishable.
GEOMETRY = b"<model>the designer's mesh</model>"
DESIGNER = {
    "printer_settings_id": "Bambu Lab P1P 0.4 nozzle",
    "printer_model": "Bambu Lab P1P",
    "printer_vendor": "BBL",
    "print_settings_id": "0.20mm Standard @BBL P1P",
    "filament_settings_id": ["Generic PLA @BBL P1P"],
    "print_compatible_printers": ["Bambu Lab P1P 0.4 nozzle"],
    "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    "printable_height": "250",
    "machine_max_acceleration_x": ["20000", "20000"],
    "machine_start_gcode": ";===== machine: P1P ==========\nG28\n",
    "machine_end_gcode": ";===== machine: P1P end ====\n",
    "extruder_offset": ["0x2"],
    "extruder_colour": ["#018001"],
    "nozzle_diameter": ["0.4"],
    "nozzle_type": "stainless_steel",
    "retraction_length": ["0.8"],
    "bed_mesh_probe_distance": "50,50",
    # the designer's intent - none of these may change
    "layer_height": "0.24",
    "wall_loops": "3",
    "sparse_infill_density": "7%",
    "sparse_infill_pattern": "gyroid",
    "top_shell_layers": "6",
    "bottom_shell_layers": "4",
    "seam_position": "back",
    "support_type": "tree(auto)",
    "support_threshold_angle": "35",
    "elephant_foot_compensation": "0.2",
    "nozzle_temperature": ["215"],
    "filament_colour": ["#FF00AA"],
}

# The selected printer in OrcaSlicer: a Creator 5 Pro, which models its 4-slot
# material station as four extruders.
MACHINE = {
    "printer_settings_id": "Flashforge Creator 5 Pro 0.4 nozzle",
    "printer_model": "Flashforge Creator 5 Pro",
    "printer_vendor": "Flashforge",
    "default_print_profile": "0.20mm Standard @FF C5",
    "default_filament_profile": ["Flashforge PLA Basic @FF C5"],
    "print_compatible_printers": ["Flashforge Creator 5",
                                  "Flashforge Creator 5 Pro 0.4 nozzle"],
    "printable_area": ["0x0", "256x0", "256x256", "0x256"],
    "printable_height": "256",
    "machine_max_acceleration_x": ["30000", "20000"],
    "machine_start_gcode": ";start_gcode\nM140 S[bed_temperature_initial_layer_single]\n",
    "machine_end_gcode": ";end_gcode\nM104 S0\n",
    "extruder_offset": ["0x0", "0x0", "0x0", "0x0"],
    "extruder_colour": ["#FCE94F", "#FCE94F", "#FCE94F", "#FCE94F"],
    "nozzle_diameter": ["0.4", "0.4", "0.4", "0.4"],
    "nozzle_type": "hardened_steel",
    "retraction_length": ["0.8", "0.8", "0.8", "0.8"],
    "retract_before_wipe": ["100%", "100%", "100%", "100%"],
    "gcode_flavor": "marlin2",
    # a machine key the preset leaves at its default: the designer's value must
    # be dropped rather than kept
    "bed_mesh_probe_distance": None,
}

PRINTER_NAME = "Flashforge Creator 5 Pro 0.4 nozzle"


def build_project(path, config=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("Metadata/project_settings.config",
                        json.dumps(config if config is not None else DESIGNER))
        bundle.writestr("3D/3dmodel.model", GEOMETRY)
        bundle.writestr("Metadata/plate_1.png", b"\x89PNG not really")
    return path


def read_config(path):
    with zipfile.ZipFile(path) as bundle:
        return json.loads(bundle.read("Metadata/project_settings.config"))


def load_plugin(workdir):
    source = os.path.join(ROOT, "makerworld-c5_any.py")
    target = os.path.join(workdir, "makerworld-c5_any.py")
    shutil.copy2(source, target)
    sys.modules["orca"] = fake_orca.make_module()
    spec = importlib.util.spec_from_file_location("ffc5_mw_under_test", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ffc5_mw_under_test"] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload if isinstance(payload, bytes) else payload.encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main():
    workdir = tempfile.mkdtemp(prefix="ffc5-mw-")
    try:
        plugin = load_plugin(workdir)
        orca_stub = sys.modules["orca"]
        import urllib.request

        print("Registration")
        package = fake_orca.make_module()
        sys.modules["orca"] = package
        spec = importlib.util.spec_from_file_location(
            "mwc5_reg", os.path.join(ROOT, "makerworld-c5_any.py"))
        reg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reg)
        package.plugin_class().register_capabilities()
        names = {c.__name__ for c in package.registered_capabilities}
        check("the MakerWorld plugin registers only its own tab",
              names == {"MakerWorldPage"}, str(sorted(names)))
        check("it declares the hosts it downloads from",
              {"https://makerworld.com", "https://api.bambulab.com",
               "https://makerworld.bblmw.com"}
              <= set(package.requested_permissions.get("network_http", [])),
              str(package.requested_permissions.get("network_http")))
        check("it asks for no printer host (that is the other plugin's job)",
              not any(u.startswith("http://") for u in
                      package.requested_permissions.get("network_http", [])),
              str(package.requested_permissions.get("network_http")))
        sys.modules["orca"] = orca_stub

        print("Reading a MakerWorld link")
        check("a plain model URL resolves to its design id",
              plugin.parse_makerworld_url("https://makerworld.com/en/models/15106-benchy")
              == ("https://makerworld.com/en/models/15106", 15106))
        check("a locale path, a profile anchor and a bare id all work",
              plugin.parse_makerworld_url("makerworld.com/de/models/1115073-3dbenchy#profileId-9")[1]
              == 1115073
              and plugin.parse_makerworld_url("15106")[1] == 15106)
        try:
            plugin.parse_makerworld_url("https://example.com/thing")
            check("a non-MakerWorld link is rejected", False, "no error raised")
        except plugin.MakerWorldError as exc:
            check("a non-MakerWorld link is rejected with a usable message",
                  "/models/" in str(exc), str(exc))

        print()
        print("Downloading a plate")
        calls = []

        def fake_urlopen(request, timeout=None):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            calls.append((url, dict(getattr(request, "headers", {}) or {})))
            if "/instance/" in url:
                return FakeResponse(json.dumps(
                    {"name": "3DBenchy.3mf", "url": "https://makerworld.bblmw.com/x.3mf"}))
            if "bblmw" in url:
                return FakeResponse(b"PK\x03\x04 not really a zip")
            return FakeResponse(json.dumps({"title": "3D Benchy"}))

        urllib.request.urlopen = fake_urlopen
        blob, name = plugin.fetch_instance_3mf(14865, "tok123")
        check("the instance endpoint is the one used", "/instance/14865/f3mf" in calls[0][0],
              str(calls[0][0]))
        check("the token is sent as a bearer header",
              calls[0][1].get("Authorization") == "Bearer tok123", str(calls[0][1]))
        check("the presigned CDN URL is followed and its bytes returned",
              blob.startswith(b"PK") and name == "3DBenchy.3mf", name)
        try:
            plugin.fetch_instance_3mf(14865, "")
            check("a missing token is refused before any request", False, "no error")
        except plugin.MakerWorldError as exc:
            check("a missing token is refused before any request",
                  "token" in str(exc).lower(), str(exc))

        print()
        print("Resizing per-extruder values")
        check("a 4-entry machine array becomes the project's 1 entry",
              plugin._fit_extruder(["a", "b", "c", "d"], ["x"], 1) == ["a"], "")
        check("a scalar machine value keeps the project's scalar shape",
              plugin._fit_extruder("hard", "soft", 1) == "hard", "")
        check("a short machine array is broadcast to the project's count",
              plugin._fit_extruder(["a"], ["x", "y", "z"], 3) == ["a", "a", "a"], "")
        check("equal counts pass through untouched",
              plugin._fit_extruder(["a", "b"], ["x", "y"], 2) == ["a", "b"], "")

        print()
        print("Converting a MakerWorld project")
        source = build_project(os.path.join(workdir, "mw.3mf"))
        with open(source, "rb") as handle:
            original = handle.read()

        orca_stub.install_preset_bundle(fake_orca.FakePresetBundle(
            current=fake_orca.FakePreset(PRINTER_NAME,
                                         **{k: v for k, v in MACHINE.items() if v is not None}),
            full={k: v for k, v in MACHINE.items() if v is not None},
        ))
        converted, report, printer = plugin.convert_3mf(original)
        config = json.loads(zipfile.ZipFile(io.BytesIO(converted))
                            .read("Metadata/project_settings.config"))

        check("the project is relabelled for the selected printer",
              config["printer_settings_id"] == PRINTER_NAME
              and config["printer_model"] == "Flashforge Creator 5 Pro", "")
        check("the machine's own limits replace the designer's",
              config["machine_max_acceleration_x"] == ["30000", "20000"],
              str(config.get("machine_max_acceleration_x")))
        check("the machine's start G-code replaces the designer's",
              config["machine_start_gcode"].startswith(";start_gcode"),
              str(config.get("machine_start_gcode"))[:40])
        check("the bed height follows the printer, not the designer",
              config["printable_height"] == "256", str(config.get("printable_height")))
        check("the designer's extruder offset does not survive",
              config["extruder_offset"] == ["0x0"], str(config.get("extruder_offset")))
        check("per-extruder arrays are resized to the project's own count",
              config["nozzle_diameter"] == ["0.4"]
              and config["retraction_length"] == ["0.8"], "")
        check("a machine key the printer leaves default is dropped, not kept",
              "bed_mesh_probe_distance" not in config, "")
        check("the preset names resolve against the user's own profiles",
              config["print_settings_id"] == "0.20mm Standard @FF C5"
              and config["filament_settings_id"] == ["Flashforge PLA Basic @FF C5"], "")

        kept_ok = all(config[k] == DESIGNER[k] for k in (
            "layer_height", "wall_loops", "sparse_infill_density", "sparse_infill_pattern",
            "top_shell_layers", "bottom_shell_layers", "seam_position", "support_type",
            "support_threshold_angle", "elephant_foot_compensation", "nozzle_temperature",
            "filament_colour"))
        check("every setting the designer tuned is kept", kept_ok,
              str({k: (config.get(k), DESIGNER[k]) for k in
                   ("layer_height", "sparse_infill_pattern", "support_type")
                   if config.get(k) != DESIGNER[k]}))

        machine_owned = [k for k in DESIGNER
                         if k in plugin.PRESET_PRINTER_KEYS or k in MACHINE]
        check("the report counts what was kept, replaced and dropped",
              report["kept"] == len(DESIGNER) - len(machine_owned)
              and report["replaced"] > 0 and report["dropped"] == 1
              and report["printer"] == PRINTER_NAME
              and report["source_printer"] == "Bambu Lab P1P", str(report)[:200])
        check("the report names the intent in the user's words",
              "layer_height" in report["kept_examples"]
              and "sparse_infill_pattern" in report["kept_examples"], "")

        with zipfile.ZipFile(io.BytesIO(converted)) as bundle:
            names = bundle.namelist()
            geometry = bundle.read("3D/3dmodel.model")
            plate = bundle.read("Metadata/plate_1.png")
        check("the geometry is carried through byte for byte", geometry == GEOMETRY, "")
        check("everything else in the archive is preserved",
              "Metadata/plate_1.png" in names and plate.startswith(b"\x89PNG"), "")

        print()
        print("Refusing what it cannot convert")
        plain = os.path.join(workdir, "plain.3mf")
        with zipfile.ZipFile(plain, "w") as bundle:
            bundle.writestr("3D/3dmodel.model", GEOMETRY)
        try:
            plugin.convert_3mf(open(plain, "rb").read())
            check("a geometry-only file is refused, not silently passed through",
                  False, "no error raised")
        except plugin.MakerWorldError as exc:
            check("a geometry-only file is refused, not silently passed through",
                  "geometry only" in str(exc), str(exc))
        try:
            plugin.convert_3mf(b"not a zip at all")
            check("a non-zip is refused", False, "no error raised")
        except plugin.MakerWorldError as exc:
            check("a non-zip is refused with a readable message",
                  "readable" in str(exc), str(exc))

        orca_stub.install_preset_bundle(None)
        try:
            plugin.convert_3mf(original)
            check("with no printer selected it explains itself", False, "no error raised")
        except plugin.MakerWorldError as exc:
            check("with no printer selected it explains itself",
                  "select" in str(exc).lower(), str(exc))

        print()
        print("Handing the finished project to OrcaSlicer")
        payload = plugin.instance_payload(["/tmp/a b.3mf"])
        check("the payload is the argv list instance_check sends",
              payload == '"orca-slicer";"/tmp/a b.3mf"', payload)
        check("quotes and backslashes are escaped the way instance_check reads them",
              plugin.instance_payload(['/tmp/x"y.3mf']) == '"orca-slicer";"/tmp/x\\"y.3mf"',
              plugin.instance_payload(['/tmp/x"y.3mf']))

        import subprocess
        real_run = subprocess.run
        real_popen = subprocess.Popen
        seen = []

        class Done:
            returncode = 0
            stdout = "com.orcaslicer.OrcaSlicer.InstanceCheck.Object12345"
            stderr = ""

        def fake_run(argv, **kw):
            seen.append(argv)
            return Done()

        if not hasattr(plugin, "_handoff_linux"):
            check("a linux hand-off exists", False, "")
        else:
            subprocess.run = fake_run
            try:
                ok, detail = plugin._handoff_linux(["/tmp/out.3mf"])
            finally:
                subprocess.run = real_run
            check("the hand-off reports success", ok and not detail, detail)
            check("it asks the session bus who is running",
                  any("ListNames" in " ".join(a) for a in seen), str(seen[:1]))
            check("and sends the file over the instance's own interface",
                  any("AnotherInstance" in " ".join(a) and "/tmp/out.3mf" in " ".join(a)
                      for a in seen), str(seen[-1:]))

        print()
        print("The tab's message flow")
        # the "no printer selected" case above uninstalled the bundle; put it back
        orca_stub.install_preset_bundle(fake_orca.FakePresetBundle(
            current=fake_orca.FakePreset(PRINTER_NAME,
                                         **{k: v for k, v in MACHINE.items() if v is not None}),
            full={k: v for k, v in MACHINE.items() if v is not None},
        ))
        page = plugin.MakerWorldPage()
        inbox = []
        page.post_message = inbox.append
        page.on_load()

        page.on_message({"type": "hello"})
        state = inbox[-1]
        check("the tab answers hello with the printer it will convert for",
              state["type"] == "state" and state["printer"] == PRINTER_NAME, str(state))

        page.on_message({"type": "save_token", "token": "tok-abc"})
        stored = json.load(open(os.path.join(workdir, "settings.json")))
        check("the token is written to settings.json", stored.get("bambu_token") == "tok-abc",
              str(stored))
        check("and is announced as saved", inbox[-1]["message"] == "Token saved.", "")

        page.on_message({"type": "resolve", "url": "https://makerworld.com/en/models/15106"})
        resolved = [m for m in inbox if m.get("type") == "resolved"][-1]
        check("resolving a link posts the plates it found",
              resolved["title"] == "3D Benchy", str(resolved))

        page.on_message({"type": "resolve", "url": "nonsense"})
        error = [m for m in inbox if m.get("type") == "error"][-1]
        check("a bad link is reported as an error, not a crash",
              "/models/" in error["message"], str(error))

        out_dir = os.path.join(workdir, "converted")
        check("the conversion directory is made on load, not by the worker",
              os.path.isdir(out_dir), out_dir)

        page._convert_and_open(original, "3D Benchy")
        report_message = [m for m in inbox if m.get("type") == "report"][-1]
        check("converting posts a single report carrying the outcome",
              report_message["opened"] is False
              and "dbus" in report_message["detail"].lower()
              or report_message["opened"] is True, str(report_message["detail"])[:60])
        check("the converted file lands in the plugin's own directory",
              os.path.dirname(report_message["path"]) == out_dir, report_message["path"])
        check("its name says what it is and is filesystem-safe",
              os.path.basename(report_message["path"]).startswith("3D Benchy")
              and "Flashforge Creator 5 Pro" in os.path.basename(report_message["path"])
              and "/" not in os.path.basename(report_message["path"]),
              os.path.basename(report_message["path"]))
        saved = read_config(report_message["path"])
        check("the saved file is the converted project",
              saved["printer_model"] == "Flashforge Creator 5 Pro"
              and saved["layer_height"] == "0.24", "")

        page.on_message({"type": "convert_path", "path": "/definitely/not/here.3mf"})
        error = [m for m in inbox if m.get("type") == "error"][-1]
        check("a missing local file is reported clearly",
              "not/here.3mf" in error["message"], str(error))

        local = build_project(os.path.join(workdir, "local.3mf"))
        page.on_message({"type": "convert_path", "path": local})
        import time
        for _ in range(100):
            if len([m for m in inbox if m.get("type") == "report"]) >= 2:
                break
            time.sleep(0.05)
        reports = [m for m in inbox if m.get("type") == "report"]
        check("converting a local .3mf works the same way",
              len(reports) >= 2
              and reports[-1]["report"]["printer"] == PRINTER_NAME, str(len(reports)))

        check("the hand-off failure names a menu item that exists",
              "Open Project" in plugin.MAKERWORLD_PAGE, "")

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print()
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    if failed:
        print("FAILURES:")
        for name in failed:
            print("  -", name)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
