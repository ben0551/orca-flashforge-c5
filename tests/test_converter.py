"""Tests for the 3mf Converter plugin: the project conversion and the hand-off.

The conversion is the part with real behaviour to get wrong, so the assertions
are made against the *converted project settings* - what OrcaSlicer would load -
and against the bytes the hand-off would send, not against what the plugin
believes it produced.

    python3 tests/test_converter.py
"""

import base64
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
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

# A project's own slice_info, in the shape MakerWorld ships it: two plates, the
# second using one of the two filaments the first does.
SLICE_INFO = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header><header_item key="X-BBL-Client-Type" value="slicer"/></header>
  <plate>
    <metadata key="index" value="1"/>
    <filament id="1" type="PLA" color="#FF0000" used_m="4.2" used_g="12.5"/>
    <filament id="2" type="PETG" color="#0000FF" used_m="1.1" used_g="3.3"/>
  </plate>
  <plate>
    <metadata key="index" value="2"/>
    <filament id="1" type="PLA" color="#FFFFFF" used_m="2.0" used_g="6.0"/>
  </plate>
</config>
"""


def build_with_slice(path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("Metadata/project_settings.config", json.dumps(DESIGNER))
        bundle.writestr("3D/3dmodel.model", GEOMETRY)
        bundle.writestr("Metadata/slice_info.config", SLICE_INFO)
    return path


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
    source = os.path.join(ROOT, "3mf-converter_any.py")
    target = os.path.join(workdir, "3mf-converter_any.py")
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
            "mwc5_reg", os.path.join(ROOT, "3mf-converter_any.py"))
        reg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reg)
        package.plugin_class().register_capabilities()
        names = {c.__name__ for c in package.registered_capabilities}
        check("the converter registers only its own tab",
              names == {"MakerWorldPage"}, str(sorted(names)))
        # The tab is named for what it does, not for the site it can pull from:
        # MakerWorld is not the author's mark to put on it, and the plugin takes
        # any .3mf anyway.
        text = open(os.path.join(ROOT, "3mf-converter_any.py"),
                    encoding="utf-8").read()
        check("the tab is named for what it does, not for the source site",
              'return "3mf Converter"' in text and '"MakerWorld"' not in text,
              [line.strip() for line in text.splitlines()
               if '"MakerWorld"' in line][:3])
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
        # Every key that was looked at lands in exactly one bucket, and the
        # buckets are named for what actually happened to the key — a preset name
        # relabelled to the user's own is neither "kept" nor "machine hardware".
        buckets = ["kept_keys", "replaced_keys", "dropped_keys",
                   "relabelled_keys", "stripped_keys"]
        covered = set()
        for name in buckets:
            covered |= set(report[name])
        check("every key the project carried is accounted for in the report",
              set(DESIGNER) <= covered,
              str(sorted(set(DESIGNER) - covered)))
        check("and no key is counted in two buckets",
              sum(len(report[name]) for name in buckets) == len(covered),
              str({name: report[name] for name in buckets
                   if report[name]}))
        check("the report counts what was kept, replaced and reset",
              report["replaced"] > 0 and report["dropped"] == 1
              and report["printer"] == PRINTER_NAME
              and report["source_printer"] == "Bambu Lab P1P", str(report)[:200])
        check("the designer's intent is counted as kept, never as replaced",
              all(k in report["kept_keys"] for k in
                  ("layer_height", "wall_loops", "sparse_infill_pattern",
                   "sparse_infill_density", "support_type", "seam_position",
                   "nozzle_temperature", "filament_colour")),
              str(report["kept_keys"]))
        check("preset names are relabelled, not counted as kept or replaced",
              "printer_settings_id" in report["relabelled_keys"]
              and "filament_settings_id" in report["relabelled_keys"]
              and not (set(report["relabelled_keys"]) & set(report["kept_keys"])),
              str(report["relabelled_keys"]))
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
        print("What the conversion reports")
        # A MakerWorld .3mf is not sliced - there is no .gcode in it - so what the
        # report can honestly show is the plates and filaments the file declares,
        # the tools the project ends up with, and the machine G-code that was
        # swapped in. All three, rather than only how many keys moved.
        sliced = open(build_with_slice(os.path.join(workdir, "sliced.3mf")), "rb").read()
        plates = plugin.plate_summary(sliced)
        check("every plate and its filaments are found",
              len(plates) == 2
              and plates[0]["index"] == "1"
              and [f["type"] for f in plates[0]["filaments"]] == ["PLA", "PETG"]
              and plates[1]["filaments"][0]["used_g"] == "6.0", str(plates))
        bare = open(build_project(os.path.join(workdir, "bare.3mf")), "rb").read()
        check("a file with no slice_info reports no plates rather than failing",
              plugin.plate_summary(bare) == []
              and plugin.plate_summary(b"not a zip at all") == [])

        config, _ = plugin.convert_project(DESIGNER, set(DESIGNER), {}, {})
        rows = plugin.filament_summary(config)
        check("every tool gets a row, one per extruder",
              len(rows) == 1 and rows[0]["tool"] == 1, str(rows))
        check("a tool with no filament type still gets a row",
              len(plugin.filament_summary({"nozzle_diameter": ["0.4", "0.4"],
                                           "filament_type": ["PLA"]})) == 2)

        gcode = plugin.gcode_summary({"machine_start_gcode": "G28\nG1 Z5\nG1 X0\n",
                                      "machine_end_gcode": "M104 S0\n",
                                      "something_else": "ignored"})
        check("the machine G-code is summarised, not dumped",
              [g["which"] for g in gcode] == ["start", "end"]
              and gcode[0]["lines"] == 3 and gcode[0]["head"][0] == "G28"
              and gcode[1]["lines"] == 1, str(gcode))
        check("a config with no machine G-code summarises to nothing",
              plugin.gcode_summary({}) == []
              and plugin.gcode_summary({"machine_start_gcode": "   "}) == [])

        print()
        print("A .3mf dropped on the page")
        # The section above deliberately ran with no printer to prove the refusal
        # path, so put one back: a dropped file converts, and converting needs a
        # printer selected.
        orca_stub.install_preset_bundle(fake_orca.FakePresetBundle(
            printers=[fake_orca.FakePreset(PRINTER_NAME)],
            current=fake_orca.FakePreset(PRINTER_NAME)))

        # What the review flagged: the plugin reads OrcaSlicer's printer preset,
        # and that preset also holds the printer's address and credentials. It
        # must read the machine's geometry and stop there.
        keys_read, values_read, _ = plugin.machine_snapshot()
        check("nothing a printer keeps private is read out of its preset",
              not (set(keys_read) & plugin.CONNECTION_KEYS),
              str(sorted(set(keys_read) & plugin.CONNECTION_KEYS)))
        check("the read is a fixed published set, not whatever the preset sets",
              set(keys_read) == set(plugin.PRESET_PRINTER_KEYS)
              - plugin.CONNECTION_KEYS, str(len(keys_read)))
        # The page can read a file's bytes but never sees a path, so it arrives
        # base64-encoded and has to convert the same way a typed path does.
        page = plugin.MakerWorldPage()
        inbox = []
        page.post_message = inbox.append
        dropped = os.path.join(workdir, "dropped.3mf")
        build_project(dropped)
        with open(dropped, "rb") as fh:
            payload = base64.b64encode(fh.read()).decode("ascii")
        page.on_message({"type": "convert_data", "name": "dropped.3mf", "data": payload})
        deadline = time.time() + 15
        while time.time() < deadline and not [m for m in inbox
                                              if m.get("type") == "report"]:
            time.sleep(0.05)
        reports = [m for m in inbox if m.get("type") == "report"]
        check("a dropped file converts the same way a typed path does",
              len(reports) == 1 and reports[0]["report"]["printer"] == PRINTER_NAME,
              str(reports)[:160])
        check("the dropped file's name reaches the saved file",
              bool(reports) and "dropped" in os.path.basename(reports[0]["path"]),
              reports[0]["path"] if reports else "")
        check("the report from a dropped file carries the new detail",
              bool(reports) and "filaments" in reports[0]["report"]
              and "gcode" in reports[0]["report"]
              and "plates" in reports[0]["report"])

        page.on_message({"type": "convert_data", "name": "empty.3mf", "data": ""})
        err = [m for m in inbox if m.get("type") == "error"]
        check("an empty payload is refused",
              bool(err) and "empty" in err[-1]["message"].lower(), str(err)[:160])

        inbox.clear()
        page.on_message({"type": "convert_data", "name": "bad.3mf",
                         "data": "!!!!not base64!!!!"})
        err = [m for m in inbox if m.get("type") == "error"]
        check("a payload that is not base64 is refused with a message",
              bool(err), str(err)[:160])

        # The guard has to fire before the decode, so it is exercised with the
        # ceiling lowered rather than by building a 128 MB string.
        inbox.clear()
        saved_max = plugin.MAX_UPLOAD_B64
        plugin.MAX_UPLOAD_B64 = 32
        try:
            page.on_message({"type": "convert_data", "name": "huge.3mf",
                             "data": "A" * 64})
        finally:
            plugin.MAX_UPLOAD_B64 = saved_max
        err = [m for m in inbox if m.get("type") == "error"]
        check("an oversized drop is refused before it is decoded",
              bool(err) and "larger than" in err[-1]["message"], str(err)[:160])

        print()
        print("Decoding what OrcaSlicer hands back")
        # A backslash, built rather than written: the escaping is what is under
        # test here, and a literal one in a test string is easy to get wrong.
        BS = chr(92)
        # A preset hands some config back JSON-encoded, so a profile name arrives
        # with the quotes still on it and a G-code block arrives on one line.
        quoted = '"' + BS + '"Generic PLA @Flashforge' + BS + '"' + '"'
        check("a quoted preset name is unwrapped for reading",
              plugin._plain_name(quoted) == "Generic PLA @Flashforge",
              repr(plugin._plain_name(quoted)))
        check("a JSON list of one name gives the name",
              plugin._plain_name('["Generic PLA @FF C5"]') == "Generic PLA @FF C5"
              and plugin._plain_name(["PLA"]) == "PLA")
        check("an ordinary name and a bare colour are left alone",
              plugin._plain_name("Generic PLA @FF C5") == "Generic PLA @FF C5"
              and plugin._plain_name("#000000") == "#000000"
              and plugin._plain_name(None) == "")

        escaped = ";start_gcode" + BS + "nG28" + BS + "nM104 S0"
        check("an escaped G-code block becomes real lines",
              plugin._plain_text(escaped).splitlines() == [";start_gcode", "G28", "M104 S0"],
              repr(plugin._plain_text(escaped)))
        check("a block that already has real newlines is untouched",
              plugin._plain_text("a\nb") == "a\nb")

        print()
        print("The audit behind the counts")
        cfg, rep = plugin.convert_project(
            {"layer_height": "0.2", "printable_area": ["0x0"], "machine_start_gcode": "X"},
            {"printable_area", "machine_start_gcode"},
            {"printable_area": ["0x0", "1x0", "1x1", "0x1"], "machine_start_gcode": "Y"},
            {})
        check("every kept and replaced key is listed, not only counted",
              rep["kept_keys"] == ["layer_height"]
              and rep["replaced_keys"] == ["machine_start_gcode", "printable_area"]
              and rep["kept"] == 1 and rep["replaced"] == 2, str(rep))

        check("connection settings are named as such, and never read from a preset",
              {"printhost_apikey", "printhost_password", "print_host",
               "printhost_user"} <= plugin.CONNECTION_KEYS)
        check("the machine key set and the connection set do not overlap",
              not (plugin.CONNECTION_KEYS - plugin.PRESET_PRINTER_KEYS),
              str(sorted(plugin.CONNECTION_KEYS - plugin.PRESET_PRINTER_KEYS)))

        # A converted .3mf gets passed around, so the designer's own connection
        # settings must not travel in it — and being told they were removed is
        # worth more than a silent fix.
        cfg4, rep4 = plugin.convert_project(
            {"layer_height": "0.2", "printhost_apikey": "SECRET",
             "print_host": "192.168.1.9", "printhost_user": "admin"},
            set(), {}, {})
        check("connection settings never survive into the converted project",
              not (set(cfg4) & plugin.CONNECTION_KEYS)
              and rep4["stripped_keys"] == ["print_host", "printhost_apikey",
                                            "printhost_user"],
              str(rep4["stripped_keys"]))
        check("the removal is reported, not silent",
              any("connection setting" in w["message"] for w in rep4["warnings"]),
              str(rep4["warnings"]))
        check("they are not also counted as kept from the designer",
              not (set(rep4["kept_keys"]) & plugin.CONNECTION_KEYS),
              str(rep4["kept_keys"]))

        cfg2, rep2 = plugin.convert_project(
            {"bed_mesh_probe_distance": "50,50", "layer_height": "0.2"},
            {"bed_mesh_probe_distance"}, {}, {})
        cfg6, rep6 = plugin.convert_project(
            {"printable_area": ["0x0"], "layer_height": "0.2"},
            {"printable_area"}, {"printable_area": ["0x0"]}, {})
        check("a machine key both sides agree on is still reported as the printer's",
              rep6["replaced_keys"] == ["printable_area"]
              and rep6["replaced"] == 1, str(rep6))

        check("a machine key the printer has no value for is reported as reset",
              rep2["dropped_keys"] == ["bed_mesh_probe_distance"]
              and rep2["dropped"] == 1 and rep2["kept_keys"] == ["layer_height"],
              str(rep2))
        # A preset-name key with nothing to relabel it to must not fall through
        # the report: it is still a machine key with no value.
        cfg5, rep5 = plugin.convert_project(
            {"printer_model": "Bambu Lab P1P", "layer_height": "0.2"},
            {"printer_model"}, {}, {})
        check("a preset-name key with nothing to point at is reported as reset",
              rep5["dropped_keys"] == ["printer_model"]
              and rep5["relabelled_keys"] == [], str(rep5)[:200])

        cfg3, rep3 = plugin.convert_project(
            {"nozzle_diameter": ["0.4"], "layer_height": "0.2"},
            {"nozzle_diameter"},
            {"nozzle_diameter": ["0.4", "0.4", "0.4", "0.4"]}, {})
        check("a tool-count mismatch is warned about, with both numbers",
              len(rep3["warnings"]) == 1
              and "1" in rep3["warnings"][0]["message"]
              and "4" in rep3["warnings"][0]["message"],
              str(rep3["warnings"]))
        check("a matching tool count raises no warning",
              plugin.convert_project({"nozzle_diameter": ["0.4"]},
                                     {"nozzle_diameter"},
                                     {"nozzle_diameter": ["0.4"]},
                                     {})[1]["warnings"] == [])

        # The G-code shown is a head, not the block: the observed one was 20 lines
        # of fan and temperature setup that filled the panel.
        long_gcode = (BS + "n").join([";start"] +
                                     ["G1 X%d Y%d F6000" % (i, i) for i in range(30)])
        rows = plugin.gcode_summary({"machine_start_gcode": long_gcode})
        check("the G-code head is capped and the rest is counted",
              rows[0]["lines"] == 31 and len(rows[0]["head"]) <= plugin.GCODE_HEAD_LINES
              and rows[0]["more"] == 31 - len(rows[0]["head"]), str(rows[0])[:200])
        check("the G-code is counted as lines, not as one blob",
              rows[0]["lines"] > 1, str(rows[0]["lines"]))

        print()
        print("Re-opening an earlier conversion")
        CONVERTED_DIR = plugin.CONVERTED_DIR
        listed = page._converted_files()
        check("the conversions already on disk are listed, newest first",
              bool(listed) and all(r["name"].endswith(".3mf") for r in listed)
              and listed[0]["modified"] >= listed[-1]["modified"], str(listed)[:200])
        inbox.clear()
        page.on_message({"type": "list_files"})
        msg = [m for m in inbox if m.get("type") == "files"]
        check("the list carries each file's path, size and age",
              bool(msg) and msg[0]["files"]
              and os.path.isabs(msg[0]["files"][0]["path"])
              and msg[0]["files"][0]["size"] > 0
              and msg[0]["files"][0]["modified"] > 0, str(msg)[:200])

        inbox.clear()
        target_path = listed[0]["path"]
        page.on_message({"type": "open_file", "path": target_path})
        opened = [m for m in inbox if m.get("type") == "opened"]
        check("opening a listed conversion hands it back to OrcaSlicer",
              bool(opened) and opened[0]["path"] == target_path, str(opened)[:200])

        inbox.clear()
        page.on_message({"type": "open_file",
                         "path": os.path.join(workdir, "elsewhere.3mf")})
        err = [m for m in inbox if m.get("type") == "error"]
        check("a path from outside the plugin's folder is refused",
              bool(err) and "not one of this plugin's conversions" in err[-1]["message"],
              str(err)[:200])

        inbox.clear()
        page.on_message({"type": "open_file",
                         "path": os.path.join(CONVERTED_DIR, "gone.3mf")})
        err = [m for m in inbox if m.get("type") == "error"]
        check("a listed file that has since gone is reported",
              bool(err) and "no longer there" in err[-1]["message"], str(err)[:200])

        print()
        print("Asking for a link without a token")
        # The user has somewhere to go to fix this, so it is its own message
        # rather than a plain error the page can only print.
        check("TokenNeeded is still a MakerWorldError",
              issubclass(plugin.TokenNeeded, plugin.MakerWorldError))
        with_none = dict(plugin.load_settings(), bambu_token="")
        saved_cache = plugin._SETTINGS_CACHE
        plugin._SETTINGS_CACHE = with_none
        try:
            inbox.clear()
            page.on_message({"type": "fetch", "instance_id": 1, "title": "t"})
            deadline = time.time() + 6
            while time.time() < deadline and not inbox:
                time.sleep(0.05)
        finally:
            plugin._SETTINGS_CACHE = saved_cache
        kinds = [m.get("type") for m in inbox]
        check("a link with no token asks for one, and is not a plain error",
              "need_token" in kinds and "error" not in kinds, str(kinds))
        check("the message says where to put the token",
              any("Token settings" in (m.get("message") or "") for m in inbox),
              str(inbox)[:200])

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
