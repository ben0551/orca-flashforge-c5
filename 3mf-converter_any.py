"""MakerWorld C5 — an OrcaSlicer plugin that rebuilds a MakerWorld project for
the printer you actually own.

What it does
------------
A MakerWorld ``.3mf`` is a Bambu Studio project. Opened as-is it is not a broken
file: OrcaSlicer ships the BBL profiles, so it loads cleanly and slices for the
*designer's* machine - wrong bed, wrong machine limits, wrong start and end
G-code - while everything the designer tuned is buried under a printer the user
does not own.

This plugin resolves a MakerWorld link, downloads the plate you pick, swaps every
setting the *machine* owns for the selected printer's, keeps every setting the
designer actually tuned, and hands the result to the running OrcaSlicer.

Only the Python standard library is used, so nothing has to be downloaded at
plugin load time.

The FlashForge device tab and printer agent are a **separate** plugin
(``flashforge-c5``) so the two can be installed, updated and versioned
independently.

Author: Ben Fischer.  License: MIT.
"""

# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "3mf Converter"
# description = "Rebuild a MakerWorld link or any local .3mf for the printer selected in OrcaSlicer."
# author = "Ben Fischer"
# version = "1.3.0"
# ///

import base64
import io
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

import orca

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(PLUGIN_DIR, "settings.json")
# Converted projects. Inside the plugin's own directory because the audit
# hook only grants writes under roots the plugin declared, and because the
# running OrcaSlicer has to be able to read the path we hand it.
CONVERTED_DIR = os.path.join(PLUGIN_DIR, "converted")

# --------------------------------------------------------------------------- #
# Settings (kept next to the plugin)
# --------------------------------------------------------------------------- #

_SETTINGS_LOCK = threading.Lock()
_SETTINGS_CACHE = None

_SETTINGS_KEYS = ("bambu_token",)


def _default_settings():
    return {"bambu_token": os.environ.get("MWC5_BAMBU_TOKEN", "")}

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
                for key in _SETTINGS_KEYS:
                    if stored.get(key) not in (None, ""):
                        data[key] = stored[key]
        except FileNotFoundError:
            pass
        except Exception:
            pass  # unreadable/corrupt settings must not stop the plugin loading
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
# MakerWorld -> the selected printer: project conversion
# --------------------------------------------------------------------------- #
#
# A MakerWorld .3mf is a Bambu Studio project. Opened as-is it is not a broken
# file: OrcaSlicer ships the BBL profiles, so it loads cleanly and slices for the
# *designer's* machine - wrong bed height, wrong machine limits, wrong start and
# end G-code - and everything the designer tuned is buried under a printer the
# user does not own.
#
# Converting it means swapping every setting the machine owns for the selected
# printer's, and keeping every other key exactly as the designer left it. The
# rule needs no hand-written key list: OrcaSlicer itself decides which keys belong
# to a machine, and `Preset::printer_options()` is that answer. Verified end to
# end against OrcaSlicer 2.4.2 - a converted project slices with no
# `--load-settings` at all, as the FlashForge Creator 5 Pro, with the designer's
# layer height, walls, infill, temperatures and colours intact.

MW_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
# The instance endpoint is the one that still serves a file: the profile endpoint
# reports the file name and md5 but leaves `url` empty.
MW_INSTANCE_API = ("https://makerworld.com/api/v1/design-service/"
                   "instance/{instance}/f3mf?type=download")
BAMBU_DESIGN_API = "https://api.bambulab.com/v1/design-service/design/{design}"
PROJECT_CONFIG = "Metadata/project_settings.config"

# Keys holding one value per extruder. When the target printer and the project
# disagree on how many there are, these must be resized to the project's count:
# writing a 4-entry array into a 1-extruder project segfaults OrcaSlicer 2.4.2 on
# load, and a stale `extruder_offset` from the designer's machine shifts the part.
# The list is `PrintConfigDef::extruder_option_keys()`.
EXTRUDER_KEYS = frozenset((
    "default_filament_profile", "default_nozzle_volume_type", "deretraction_speed",
    "extruder_colour", "extruder_offset", "extruder_printable_height",
    "extruder_type", "long_retractions_when_cut", "max_layer_height",
    "min_layer_height", "nozzle_diameter", "nozzle_flush_dataset", "nozzle_type",
    "nozzle_volume", "retract_after_wipe", "retract_before_wipe",
    "retract_length_toolchange", "retract_lift_above", "retract_lift_below",
    "retract_lift_enforce", "retract_restart_extra", "retract_restart_extra_toolchange",
    "retract_when_changing_layer", "retraction_distances_when_cut",
    "retraction_length", "retraction_minimum_travel", "retraction_speed",
    "travel_slope", "wipe", "wipe_distance", "z_hop", "z_hop_types",
))

# Which settings belong to a machine rather than to a print. OrcaSlicer answers this
# itself: Preset::printer_options() = s_Preset_printer_options +
# s_Preset_machine_limits_options + PrintConfigDef::extruder_option_keys().
# Read out of OrcaSlicer 2.5.0's Preset.cpp / PrintConfig.cpp. The live preset's
# config_keys() is unioned on top, so a newer build adding a key still converts.
# Connection settings sit inside OrcaSlicer's own machine-option set, so they look
# like machine keys — but they are not machine *geometry*, they are where the printer
# is and how to authenticate to it. They are never read out of a user's preset and
# never written into a converted project: a converted .3mf gets passed around, and it
# must not carry someone's printer address, user or password out with it. A project
# that arrives carrying them has them stripped (see convert_project).
CONNECTION_KEYS = frozenset((
    "print_host", "print_host_webui", "host_type", "bbl_use_printhost",
    "printhost_apikey", "printhost_password", "printhost_user",
    "printhost_cafile", "printhost_port", "printhost_authorization_type",
    "printhost_ssl_ignore_revoke",
))

PRESET_PRINTER_KEYS = frozenset((
    "adaptive_bed_mesh_margin", "auxiliary_fan", "bbl_use_printhost",
    "bed_custom_model", "bed_custom_texture", "bed_exclude_area",
    "bed_exclude_volume_mode", "bed_exclude_volumes", "bed_mesh_max", "bed_mesh_min",
    "bed_mesh_probe_distance", "bed_temperature_formula", "before_layer_change_gcode",
    "belt_frame_tilt_angle", "belt_frame_tilt_decouple", "belt_printer",
    "belt_printer_infinite_y", "belt_slice_rotation", "belt_slice_rotation_angle",
    "belt_support_floor_offset", "best_object_pos", "build_plate_tilt_x",
    "build_plate_tilt_y", "change_extrusion_role_gcode", "change_filament_gcode",
    "cooling_filter_enabled", "cooling_tube_length", "cooling_tube_retraction",
    "default_bed_type", "default_filament_profile", "default_nozzle_volume_type",
    "default_print_profile", "deretract_speed_extruder_change", "deretraction_speed",
    "disable_m73", "emit_machine_limits_to_gcode", "enable_belt_purge_tower",
    "enable_filament_ramming", "enable_long_retraction_when_cut",
    "enable_power_loss_recovery", "enable_pre_heating", "extra_loading_move",
    "extruder_bed_exclude_volumes", "extruder_clearance_dist_to_rod",
    "extruder_clearance_height_to_lid", "extruder_clearance_height_to_rod",
    "extruder_clearance_radius", "extruder_colour", "extruder_max_nozzle_count",
    "extruder_offset", "extruder_printable_area", "extruder_printable_height",
    "extruder_type", "extruder_variant_list", "fan_direction", "fan_kickstart",
    "fan_speedup_overhangs", "fan_speedup_time", "farthest_point_timelapse",
    "file_start_gcode", "flashforge_serial_number", "gcode_flavor", "gcode_remap_x",
    "gcode_remap_y", "gcode_remap_z", "gcode_skip_config_block", "grab_length",
    "group_algo_with_time", "head_wrap_detect_zone", "high_current_on_filament_swap",
    "host_type", "hotend_cooling_rate", "hotend_heating_rate", "imex_carriage_margin",
    "imex_firmware_managed_zones", "imex_gantry_count", "imex_mode_active_tools",
    "imex_mode_gcodes", "imex_mode_names", "imex_nozzle_clearance_x",
    "imex_nozzle_clearance_y", "imex_tool_layout", "imex_tools_per_gantry",
    "imex_viz_theme", "inherits", "input_shaping_damp_x", "input_shaping_damp_y",
    "input_shaping_emit", "input_shaping_freq_x", "input_shaping_freq_y",
    "input_shaping_type", "is_imex", "layer_change_gcode", "long_retractions_when_cut",
    "machine_bed_mass_Y", "machine_end_gcode", "machine_hotend_change_time",
    "machine_load_filament_time", "machine_max_acceleration_e",
    "machine_max_acceleration_extruding", "machine_max_acceleration_retracting",
    "machine_max_acceleration_travel", "machine_max_acceleration_x",
    "machine_max_acceleration_y", "machine_max_acceleration_z", "machine_max_force_Y",
    "machine_max_jerk_e", "machine_max_jerk_x", "machine_max_jerk_y",
    "machine_max_jerk_z", "machine_max_junction_deviation", "machine_max_printed_mass",
    "machine_max_speed_e", "machine_max_speed_x", "machine_max_speed_y",
    "machine_max_speed_z", "machine_min_extruding_rate", "machine_min_travel_rate",
    "machine_pause_gcode", "machine_prepare_compensation_time", "machine_start_gcode",
    "machine_tool_change_time", "machine_unload_filament_time",
    "manual_filament_change", "master_extruder_id", "max_layer_height",
    "max_resonance_avoidance_speed", "min_layer_height",
    "min_resonance_avoidance_speed", "nozzle_diameter", "nozzle_flush_dataset",
    "nozzle_height", "nozzle_hrc", "nozzle_type", "nozzle_volume",
    "parallel_printheads_bed_exclude_areas", "parallel_printheads_count",
    "parking_pos_retraction", "part_cooling_fan_min_pwm", "pellet_modded_printer",
    "physical_extruder_map", "preferred_orientation", "print_host", "print_host_webui",
    "printable_area", "printable_height", "printer_agent", "printer_extruder_id",
    "printer_extruder_variant", "printer_model", "printer_notes",
    "printer_plugin_config_overrides", "printer_structure", "printer_technology",
    "printer_variant", "printhost_apikey", "printhost_authorization_type",
    "printhost_cafile", "printhost_password", "printhost_port",
    "printhost_ssl_ignore_revoke", "printhost_user", "printing_by_object_gcode",
    "purge_in_prime_tower", "resonance_avoidance", "retract_after_wipe",
    "retract_before_wipe", "retract_length_toolchange", "retract_lift_above",
    "retract_lift_below", "retract_lift_enforce", "retract_restart_extra",
    "retract_restart_extra_toolchange", "retract_when_changing_layer",
    "retraction_distances_when_cut", "retraction_length", "retraction_minimum_travel",
    "retraction_speed", "scan_first_layer", "single_extruder_multi_material",
    "support_air_filtration", "support_chamber_temp_control", "support_cooling_filter",
    "support_fast_purge_mode", "support_multi_bed_types", "support_object_skip_flush",
    "support_parallel_printheads", "template_custom_gcode", "thumbnails",
    "thumbnails_format", "time_cost", "time_lapse_gcode", "tool_change_on_wipe_tower",
    "travel_slope", "upward_compatible_machine", "use_3mf", "use_firmware_retraction",
    "use_relative_e_distances", "wait_for_temp_on_wipe_tower", "wipe", "wipe_distance",
    "wipe_tower_type", "wrapping_detection_gcode", "wrapping_detection_layers",
    "wrapping_exclude_area", "z_hop", "z_hop_types", "z_offset",
))

# Keys that name presets rather than describe the machine. Relabelled so the
# project resolves against the user's own presets instead of the designer's.
IDENTITY_KEYS = ("printer_settings_id", "print_settings_id", "filament_settings_id",
                 "printer_model", "printer_vendor", "print_compatible_printers",
                 "default_print_profile", "default_filament_profile")


class MakerWorldError(Exception):
    """Anything the converter tab should show the user verbatim."""


class TokenNeeded(MakerWorldError):
    """A link was asked for without a token.

    Its own type so the page can *open* its token settings rather than only
    printing the message: the user has to go somewhere to fix this, and saying so
    without taking them there is the difference between a helpful error and a
    dead end.
    """


def parse_makerworld_url(url):
    """Return (normalised_url, design_id) for a MakerWorld model link."""
    text = (url or "").strip()
    match = re.search(r"makerworld\.com/(?:[a-z]{2}/)?(?:models|model)/(\d+)", text, re.I)
    if not match:
        match = re.search(r"\b(\d{5,})\b", text)      # a bare design id
    if not match:
        raise MakerWorldError("That does not look like a MakerWorld link. It should "
                              "contain /models/<number>.")
    design_id = int(match.group(1))
    return f"https://makerworld.com/en/models/{design_id}", design_id


def _http_get(url, headers=None, timeout=30, raw=False):
    request = urllib.request.Request(url, headers=dict(headers or {}, **{"User-Agent": MW_UA}))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read() if raw else response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise MakerWorldError(f"HTTP {exc.code} from {url.split('/')[2]}") from exc
    except urllib.error.URLError as exc:
        raise MakerWorldError(f"Could not reach {url.split('/')[2]}: {exc.reason}") from exc


def fetch_design(design_id):
    """The design's public metadata: title, plates, licence. No token needed."""
    body = _http_get(BAMBU_DESIGN_API.format(design=design_id))
    try:
        details = json.loads(body)
    except ValueError as exc:
        raise MakerWorldError("MakerWorld returned something that is not JSON.") from exc
    if not details.get("title"):
        raise MakerWorldError("That design has no public details - it may have been "
                              "removed, or be a private upload.")
    return details


def design_plates(details):
    """One row per printable plate, with the fields the page needs."""
    plates = []
    for instance in details.get("instances") or []:
        model_info = (instance.get("extention") or {}).get("modelInfo") or {}
        plates.append({
            "instance_id": instance.get("id"),
            "title": (instance.get("title") or details.get("title") or "").strip(),
            "filament_weight": model_info.get("weight") or 0,
            "size": (model_info.get("f3mf") or {}).get("size") or 0,
        })
    return plates


def fetch_instance_3mf(instance_id, token):
    """Download one plate's .3mf. Returns (bytes, filename)."""
    if not token:
        raise TokenNeeded("A BambuCloud token is needed to download from "
                          "MakerWorld. Open Token settings under the link box "
                          "and paste one in.")
    body = _http_get(MW_INSTANCE_API.format(instance=instance_id),
                     headers={"Authorization": "Bearer " + token})
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise MakerWorldError("MakerWorld did not return a download URL - the token "
                              "may have expired.") from exc
    url = payload.get("url")
    if not url:
        raise MakerWorldError(payload.get("error") or payload.get("message")
                              or "MakerWorld returned no download URL for that plate.")
    return _http_get(url, timeout=180, raw=True), payload.get("name") or "model.3mf"


def read_project_config(blob):
    """The project_settings.config out of a .3mf, or {} for a plain model."""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            names = archive.namelist()
            if PROJECT_CONFIG not in names:
                return {}
            return json.loads(archive.read(PROJECT_CONFIG).decode("utf-8", "replace"))
    except (zipfile.BadZipFile, ValueError, KeyError) as exc:
        raise MakerWorldError("That file is not a readable .3mf project.") from exc


def _fit_extruder(machine_value, project_value, count):
    """Resize a per-extruder machine value to the project's extruder count."""
    scalar = not isinstance(project_value, list)
    if isinstance(machine_value, list):
        if not machine_value:
            return machine_value
        sized = (machine_value[:count] if len(machine_value) >= count
                 else [machine_value[0]] * count)
    else:
        sized = [machine_value] * count
    return sized[0] if (scalar and count == 1) else sized


def convert_project(project, machine_keys, machine_values, identity):
    """Return (converted_config, report).

    Every key the machine owns takes the printer's value; every other key keeps
    the designer's. Keys the printer leaves at its default are dropped rather than
    left holding the designer's machine value, so OrcaSlicer's own default applies.
    """
    if not project:
        raise MakerWorldError("This file carries no project settings, so there is "
                              "nothing to convert - it is geometry only.")
    extruders = project.get("nozzle_diameter")
    count = len(extruders) if isinstance(extruders, list) and extruders else 1

    out = dict(project)
    # A project can carry the designer's own connection settings. They are not
    # settings this converter has any business moving: they are how *their* machine
    # was reached. Out they go, and the report says so, because a stripped
    # credential is worth knowing about rather than silently fixing.
    stripped = sorted(key for key in CONNECTION_KEYS if key in out)
    for key in stripped:
        out.pop(key, None)
    replaced, kept, dropped, relabelled = [], [], [], []
    for key in sorted(set(project) | set(machine_values)):
        if key in CONNECTION_KEYS:
            # Already stripped from `out` above; classifying them as either kept
            # or replaced would put them back in the report as if they mattered.
            continue
        if key in IDENTITY_KEYS and identity.get(key) not in (None, project.get(key)):
            # These name a preset rather than describing the print, and they are
            # pointed at the user's own so the project resolves against what they
            # have. Neither "kept" nor "replaced by your printer" is true of them,
            # and filing them under either would hide what actually happened.
            #
            # Only when there is a replacement to point at: with nothing to
            # relabel to, the key falls through and is reported as reset instead
            # of disappearing from the report entirely.
            relabelled.append(key)
            continue
        if key in machine_keys:
            if key not in machine_values:
                if key in out:
                    out.pop(key)
                    dropped.append(key)
                continue
            value = machine_values[key]
            if key in EXTRUDER_KEYS:
                value = _fit_extruder(value, project.get(key), count)
            out[key] = value
            # Counted whether or not it differs: the printer's answer is the one
            # that ends up in the file either way, and an audit that quietly omits
            # the settings where both sides happened to agree is not an audit —
            # it leaves keys the reader cannot find anywhere.
            replaced.append(key)
        elif key in project:
            kept.append(key)
    out.update({k: v for k, v in identity.items() if v is not None})
    # The designer's extruder count against the printer's, which is the pairing a
    # user has to check: the tools are matched in order, and a mismatch is where
    # filament ends up on the wrong one.
    source_count = len(project.get("nozzle_diameter") or []) or 1
    target_count = len(machine_values.get("nozzle_diameter") or []) or source_count
    warnings = []
    if stripped:
        warnings.append({
            "level": "check",
            "message": ("The project carried " + str(len(stripped)) +
                        " printer connection setting" +
                        ("s" if len(stripped) != 1 else "") + " (" +
                        ", ".join(stripped) + "). They were removed: they describe "
                        "the machine the project came from, not the print, and a "
                        "project file gets shared."),
        })
    if source_count != target_count:
        warnings.append({
            "level": "check",
            "message": (f"The designer's project was set up for {source_count} "
                        f"extruder{'s' if source_count != 1 else ''} and this "
                        f"printer has {target_count}. The tools are matched in "
                        "order — check the filament assignment before printing."),
        })
    report = {
        "extruders": target_count,
        "source_extruders": source_count,
        "kept": len(kept),
        "replaced": len(replaced),
        "dropped": len(dropped),
        "kept_examples": sorted(k for k in kept if k in KEEP_INTERESTING),
        "replaced_examples": replaced[:12],
        # The full lists, not only the counts: the whole point of the report is
        # being able to check one setting, and a count cannot be checked.
        "relabelled": len(relabelled),
        "relabelled_keys": sorted(relabelled),
        "kept_keys": sorted(kept),
        "replaced_keys": sorted(replaced),
        "dropped_keys": sorted(dropped),
        "stripped_keys": stripped,
        "warnings": warnings,
    }
    return out, report


# Named so the report can say what survived in the user's own language rather
# than counting keys.
KEEP_INTERESTING = frozenset((
    "layer_height", "wall_loops", "sparse_infill_density", "sparse_infill_pattern",
    "top_shell_layers", "bottom_shell_layers", "seam_position", "support_type",
    "support_threshold_angle", "elephant_foot_compensation", "nozzle_temperature",
    "filament_colour",
))

# The project's own filaments and machine G-code, so the report can show what the
# conversion *produced* rather than only how many keys moved. A MakerWorld .3mf is
# not sliced - it carries no .gcode at all - so "the G-code" here is the machine
# start/end block the conversion swapped for the printer's, alongside the
# filament/tool list the project now declares.
SLICE_INFO = "Metadata/slice_info.config"

# A dropped file arrives base64-encoded through the page's message channel, so a
# few hundred MB of model would be held in memory three times over. Real .3mf
# files are a few MB; this guards against a mis-drop rather than being a limit
# anyone reaches.
MAX_UPLOAD_B64 = 128 * 1024 * 1024


# The two characters a JSON-encoded preset value can start with.
JSON_OPENERS = ("[", '\"')


def _plain_name(value):
    """A preset name as a person reads it.

    OrcaSlicer hands several config values back JSON-encoded — sometimes a list,
    sometimes a quoted string — so a name can arrive as '["Generic PLA @FF C5"]'
    or with the quotes still on it. Either way the escaping is noise in a report
    whose whole job is to name the profile.
    """
    obj = value
    if isinstance(obj, (list, tuple)):
        obj = obj[0] if obj else ""
    text = str(obj or "").strip()
    for _ in range(3):
        if len(text) < 2 or text[0] not in JSON_OPENERS:
            break
        try:
            obj = json.loads(text)
        except ValueError:
            break
        if isinstance(obj, (list, tuple)):
            obj = obj[0] if obj else ""
        text = str(obj or "").strip()
    return text.strip('"').strip()


def _plain_text(value):
    """A multi-line value as written, not as JSON.

    The same encoding applies to G-code blocks, where the newlines arrive as the
    two characters backslash-n — so a twenty-line start G-code counts as one line
    and renders as one unreadable paragraph.
    """
    if isinstance(value, (list, tuple)):
        value = "\n".join(str(part) for part in value)
    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, str):
        text = parsed
    elif parsed is not None and not isinstance(parsed, (dict, list)):
        text = str(parsed)
    # A partly-encoded value still carries the escapes, so normalise anyway.
    if "\\n" in text:
        text = text.replace("\\r\\n", "\n").replace("\\n", "\n")
    return text


def _per_tool(value, count):
    """One entry per tool, whatever shape the config stored."""
    if isinstance(value, list):
        out = list(value)
    elif value in (None, ""):
        out = []
    else:
        out = [value]
    if not out:
        out = [None]
    while len(out) < count:
        out.append(out[-1])
    return out[:count]


def filament_summary(config):
    """The filament each tool will print, as the converted project declares it."""
    count = len(config.get("nozzle_diameter") or []) or 1
    types = _per_tool(config.get("filament_type"), count)
    colours = _per_tool(config.get("filament_colour"), count)
    profiles = _per_tool(config.get("filament_settings_id"), count)
    return [{"tool": i + 1,
             "type": _plain_name(types[i]),
             "colour": _plain_name(colours[i]),
             "profile": _plain_name(profiles[i])}
            for i in range(count)]


# The report is a summary, so it shows the beginning of the machine G-code and
# says how much more there is. Enough to recognise the block, not a wall of it.
GCODE_HEAD_LINES = 6
GCODE_HEAD_CHARS = 200


def gcode_summary(config):
    """The machine G-code now in the project: how much of it, and its head."""
    out = []
    for key, label in (("machine_start_gcode", "start"),
                       ("machine_end_gcode", "end")):
        text = _plain_text(config.get(key))
        if not text.strip():
            continue
        lines = [line.rstrip() for line in text.strip().splitlines()]
        head, used = [], 0
        for line in lines[:GCODE_HEAD_LINES]:
            if len(line) > GCODE_HEAD_CHARS:
                line = line[:GCODE_HEAD_CHARS] + " …"
            if used + len(line) > GCODE_HEAD_CHARS * 2:
                break
            head.append(line)
            used += len(line)
        out.append({"which": label, "lines": len(lines), "head": head,
                    "more": max(0, len(lines) - len(head))})
    return out


def plate_summary(blob):
    """Plates and the filament each uses, from the source file's slice_info.

    A MakerWorld project is unsliced but still records its plates, so this says
    how many plates are about to be sliced and which tools each needs. Absent or
    unreadable slice_info is normal for a bare model, not an error.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            if SLICE_INFO not in archive.namelist():
                return []
            root = ET.fromstring(archive.read(SLICE_INFO))
    except Exception:
        return []
    plates = []
    for plate in root.findall("plate"):
        index = ""
        for meta in plate.findall("metadata"):
            if meta.get("key") == "index":
                index = meta.get("value") or ""
        plates.append({
            "index": index or str(len(plates) + 1),
            "filaments": [{"id": f.get("id") or "",
                           "type": f.get("type") or "",
                           "colour": f.get("color") or "",
                           "used_m": f.get("used_m") or "",
                           "used_g": f.get("used_g") or ""}
                          for f in plate.findall("filament")],
        })
    return plates


def rewrite_3mf(blob, config):
    """A copy of the .3mf with only the project config replaced."""
    source = io.BytesIO(blob)
    target = io.BytesIO()
    with zipfile.ZipFile(source) as zin, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zout:
        for name in zin.namelist():
            if name == PROJECT_CONFIG:
                zout.writestr(name, json.dumps(config, indent=4, sort_keys=True))
            else:
                zout.writestr(name, zin.read(name))
    return target.getvalue()


def machine_snapshot():
    """(keys, values, printer_name) for the printer selected in OrcaSlicer.

    The key set is OrcaSlicer's own published answer to which settings a printer
    owns — PRESET_PRINTER_KEYS — minus the connection settings in CONNECTION_KEYS.
    Nothing else is read.

    An earlier version unioned that with `preset.config_keys()`, which is every key
    the selected preset happens to set. That was wrong twice over: too broad, since
    it pulls in the printer's address and credentials, and too weak, since a key a
    preset sets is not thereby owned by the machine.
    """
    try:
        bundle = orca.host.preset_bundle()
        preset = bundle.current_printer_preset()
    except Exception as exc:
        raise MakerWorldError("Could not read the selected printer from OrcaSlicer "
                              f"({exc}). Select a printer and try again.") from exc
    if preset is None or not getattr(preset, "name", ""):
        raise MakerWorldError("No printer is selected in OrcaSlicer. Pick one, then "
                              "convert - the project is rebuilt for whatever is "
                              "selected.")

    keys = set(PRESET_PRINTER_KEYS) - CONNECTION_KEYS

    values = {}
    for key in keys:
        value = None
        try:
            value = bundle.full_config_value(key)
        except Exception:
            value = None
        if value is None:
            try:
                value = preset.config_value(key)
            except Exception:
                value = None
        if value is not None:
            values[key] = value
    return keys, values, preset.name


def identity_for(printer_name, machine_values, project):
    """Preset names the converted project should resolve against."""
    filaments = project.get("filament_settings_id")
    filament_count = len(filaments) if isinstance(filaments, list) and filaments else 1
    print_profile = machine_values.get("default_print_profile")
    filament_profile = machine_values.get("default_filament_profile")
    if isinstance(filament_profile, list):
        filament_profile = _fit_extruder(filament_profile, filaments, filament_count)
    identity = {
        "printer_settings_id": printer_name,
        "printer_model": machine_values.get("printer_model"),
        "printer_vendor": machine_values.get("printer_vendor"),
        "print_compatible_printers": (machine_values.get("print_compatible_printers")
                                      or [printer_name]),
    }
    if isinstance(print_profile, str) and print_profile:
        identity["print_settings_id"] = print_profile
    if filament_profile:
        identity["filament_settings_id"] = filament_profile
    return identity


def convert_3mf(blob):
    """Convert a MakerWorld/Bambu project to the selected printer.

    Returns (converted_bytes, report, printer_name).
    """
    project = read_project_config(blob)
    keys, values, printer = machine_snapshot()
    identity = identity_for(printer, values, project)
    config, report = convert_project(project, keys, values, identity)
    report["printer"] = printer
    report["source_printer"] = project.get("printer_model") or "unknown"
    # What came out, not just how many keys moved. The plates are read from the
    # source file; the filaments and the machine G-code are from the converted
    # config, so they describe what will actually be sliced.
    report["filaments"] = filament_summary(config)
    report["gcode"] = gcode_summary(config)
    report["plates"] = plate_summary(blob)
    return rewrite_3mf(blob, config), report, printer


# --------------------------------------------------------------------------- #
# Handing a finished project to the running OrcaSlicer
# --------------------------------------------------------------------------- #
#
# The plugin host API is read-only: there is no "load this file" call. But every
# running instance already listens for the message a *second* launch would send it
# (InstanceCheck.cpp), and file paths there reach EVT_LOAD_MODEL_OTHER_INSTANCE,
# which is the plater. Each platform has its own transport.

def orca_data_dir():
    """Orca's data directory, or '' when this file is not installed as a plugin.

    Plugins live at <datadir>/orca_plugins/<name>/<file>.py and cloud-subscribed
    ones a level deeper, so walk up until orca_plugins is the child.
    """
    path = os.path.dirname(os.path.abspath(__file__))
    while True:
        parent, leaf = os.path.split(path)
        if leaf == "orca_plugins":
            return parent
        if not parent or parent == path:
            return ""
        path = parent


def instance_payload(paths):
    """The argv list instance_check sends: semicolon-separated, C-style quoted."""
    argv = ["orca-slicer"] + [str(p) for p in paths]
    return ";".join('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"') for a in argv)


def own_instance_hashes():
    """Instance hashes belonging to *this* data directory, newest first.

    instance_check names its lock file <hash>.lock under <datadir>/cache/ and the
    bus name carries the same hash, so the cache identifies which of the instances
    on the bus is the one hosting this plugin.
    """
    datadir = orca_data_dir()
    if not datadir:
        return []
    cache = os.path.join(datadir, "cache")
    try:
        locks = [f for f in os.listdir(cache) if f.endswith(".lock") and f[:-5].isdigit()]
    except OSError:
        return []
    locks.sort(key=lambda f: os.path.getmtime(os.path.join(cache, f)), reverse=True)
    return [f[:-5] for f in locks]


def _handoff_linux(paths):
    """The session-bus message a second launch would send."""
    try:
        names = subprocess.run(
            ["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.DBus",
             "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"dbus-send unavailable: {exc}"
    found = re.findall(r"com\.orcaslicer\.OrcaSlicer\.InstanceCheck\.Object(\d+)", names)
    if not found:
        return False, "no running OrcaSlicer instance on the session bus"
    own = own_instance_hashes()
    instance = next((h for h in own if h in found), None)
    if instance is None:
        if len(found) > 1:
            return False, ("%d OrcaSlicer instances are on the session bus and none "
                           "matches this data directory, so I will not guess which "
                           "should receive the file" % len(found))
        instance = found[0]
    iface = "com.orcaslicer.OrcaSlicer.InstanceCheck.Object" + instance
    try:
        done = subprocess.run(
            ["dbus-send", "--session", "--type=method_call", "--dest=" + iface,
             "/com/orcaslicer/OrcaSlicer/InstanceCheck/Object" + instance,
             iface + ".AnotherInstance", "string:" + instance_payload(paths)],
            capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        return False, (done.stderr or "").strip()[:200]
    return True, ""


def _handoff_macos(paths):
    """LaunchServices: `open -a <bundle> <file>` reaches the running instance."""
    exe = sys.executable or ""
    for _ in range(4):                       # .../Foo.app/Contents/MacOS/Foo
        candidate = os.path.dirname(exe)
        if candidate.endswith(".app"):
            exe = candidate
            break
        if not candidate or candidate == exe:
            break
        exe = candidate
    if not exe.endswith(".app"):
        return False, "not running from an .app bundle, so there is no instance to open into"
    try:
        subprocess.Popen(["/usr/bin/open", "-a", exe] + [str(p) for p in paths],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return False, f"open -a: {exc}"
    return True, ""


def _handoff_windows(paths):
    """WM_COPYDATA to this process's OrcaSlicer main window."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetPropW.restype = wintypes.HANDLE
    user32.GetPropW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
    user32.SendMessageW.restype = ctypes.c_longlong
    user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_void_p]

    class COPYDATASTRUCT(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_void_p),
                    ("cbData", wintypes.DWORD),
                    ("lpData", ctypes.c_void_p)]

    our_pid = kernel32.GetCurrentProcessId()
    found = []

    def visit(hwnd, _lparam):
        name = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, name, 256) == 0 or name.value != "wxWindowNR":
            return True
        # Both properties are set on the main frame only.
        if not user32.GetPropW(hwnd, "Instance_Hash_Minor"):
            return True
        if not user32.GetPropW(hwnd, "Instance_Hash_Major"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != our_pid:
            return True
        found.append(hwnd)
        return False

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(visit)
    user32.EnumWindows(callback, 0)
    if not found:
        return False, "no OrcaSlicer main window found in this process"
    buf = ctypes.create_unicode_buffer(instance_payload(paths))
    data = COPYDATASTRUCT(ctypes.c_void_p(1), ctypes.sizeof(buf),
                          ctypes.cast(buf, ctypes.c_void_p))
    # WM_COPYDATA blocks until the GUI thread has handled it, which is what we
    # want from a worker thread and would deadlock only on the GUI thread.
    user32.SendMessageW(found[0], 0x004A, None, ctypes.byref(data))
    return True, ""


def load_into_orca(paths):
    """Open finished projects in the running OrcaSlicer. Returns (ok, detail)."""
    if os.name == "nt":
        return _handoff_windows(paths)
    if sys.platform == "darwin":
        return _handoff_macos(paths)
    return _handoff_linux(paths)


# --------------------------------------------------------------------------- #
# The MakerWorld tab
# --------------------------------------------------------------------------- #

MAKERWORLD_PAGE = """
<style>
  body { margin: 0; padding: 14px; font-size: 13px; line-height: 1.45; }
  h2 { margin: 0 0 3px; font-size: 15px; }
  .note { margin: 0 0 12px; color: var(--orca-muted); font-size: 12px; }
  .row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-bottom: 4px; }
  label { display: block; font-size: 12px; color: var(--orca-muted); margin: 12px 0 4px; }
  input { flex: 1 1 200px; min-width: 0; max-width: 100%; padding: 5px 7px; font: inherit;
          color: var(--orca-fg); background: var(--orca-bg);
          border: 1px solid var(--orca-border); border-radius: 4px; }
  button { padding: 5px 10px; font: inherit; cursor: pointer; border-radius: 4px;
           color: var(--orca-fg); background: var(--orca-bg);
           border: 1px solid var(--orca-border); }
  button.primary { background: var(--orca-accent, #2f9e6f); border-color: transparent; color: #fff; }
  button.quiet { background: transparent; }
  button:disabled { opacity: .55; cursor: default; }
  .panel { border: 1px solid var(--orca-border); border-radius: 6px; padding: 10px; margin-bottom: 12px; }
  .plate { display: flex; gap: 8px; align-items: center; justify-content: space-between;
           padding: 6px 8px; border: 1px solid var(--orca-border);
           border-radius: 4px; margin-bottom: 5px; }
  .plate .meta { color: var(--orca-muted); font-size: 11px; white-space: nowrap; }
  #banner { display: none; margin-top: 10px; padding: 7px 9px; border-radius: 4px;
            border: 1px solid var(--orca-border); white-space: pre-wrap; }
  #banner.err { border-color: #c0567f; }
  #banner.ok { border-color: #2f9e6f; }
  #report { font-size: 12px; }
  #report .line { margin-bottom: 3px; }
  code { font-size: 11px; word-break: break-all; }
  .drop { border: 1px dashed var(--orca-border); border-radius: 6px; padding: 14px;
          text-align: center; margin-bottom: 4px; }
  .drop.over { border-color: var(--orca-accent, #2f9e6f); background: rgba(47,158,111,.08); }
  details.help { margin-top: 8px; font-size: 12px; }
  details.help summary { cursor: pointer; color: var(--orca-muted); }
  details.help ol { margin: 6px 0 6px 18px; padding: 0; }
  details.help li { margin-bottom: 2px; }
  .warn { border-left: 3px solid var(--orca-accent, #2f9e6f); padding-left: 7px;
          margin: 5px 0; }
  details.audit { margin: 5px 0; font-size: 12px; }
  details.audit summary { cursor: pointer; }
  .keys { max-height: 210px; overflow: auto; margin-top: 4px;
          border: 1px solid var(--orca-border); border-radius: 4px; padding: 5px; }
  .keys code { display: inline-block; margin: 0 4px 3px 0; }
  .meta { color: var(--orca-muted); font-size: 11px; }
</style>

<h2>3mf Converter</h2>
<p class="note">Drop a <code>.3mf</code> on this tab and it is rebuilt for the printer
selected in OrcaSlicer: the designer's layers, walls, infill, supports and temperatures
are kept, their machine is replaced. It then opens here, ready to slice. A MakerWorld
link works too &mdash; that path needs a token, a local file never does.</p>

<label for="url">MakerWorld link</label>
<div class="row">
  <input id="url" type="text" placeholder="https://makerworld.com/en/models/...">
  <button id="resolve" class="primary">Find plates</button>
</div>

<!-- Closed by default: most of what this plugin does needs no token at all, and
     a credentials field sitting open reads as a requirement. -->
<details class="panel" id="tokenPanel">
  <summary>Token settings <span class="meta">&mdash; only needed to download from a
    link, never for a file you already have</span></summary>
  <div class="row" style="margin-top:8px">
    <input id="token" type="password" placeholder="no token set">
    <button id="saveToken">Save token</button>
  </div>
  <p class="note" style="margin:0">Kept in this plugin's settings.json (mode 0600) and
  sent to MakerWorld and nowhere else.</p>
  <details class="help">
    <summary>How do I get a BambuCloud token?</summary>
    <ol>
      <li>Sign in at <code>makerworld.com</code> in your browser.</li>
      <li>Press <b>F12</b> to open Developer Tools.</li>
      <li>Open <b>Application</b> (Chrome, Edge) or <b>Storage</b> (Firefox) &rarr;
          <b>Cookies</b> &rarr; <code>makerworld.com</code>.</li>
      <li>Click the row named <code>token</code> and copy its <b>Value</b>.</li>
      <li>Paste it above and press <b>Save token</b>.</li>
    </ol>
    <p class="note" style="margin:0 0 6px">No cookie called <code>token</code>? Reload
    the page, open the <b>Network</b> tab, click any request to makerworld.com, and keep
    the <code>token=&hellip;</code> part of its <code>cookie:</code> request header.</p>
    <p class="note" style="margin:0"><b>Treat it like a password</b> &mdash; it is your
    whole Bambu Cloud session, and it lasts about 90 days. It is only needed to pull a
    <i>link</i>; a <code>.3mf</code> you already have converts without one.</p>
  </details>
</details>

<div id="pick" class="panel" hidden>
  <div id="pickTitle" style="margin-bottom:8px"></div>
  <div id="plates"></div>
</div>

<label>A .3mf on this machine</label>
<div id="drop" class="drop">
  <div><b>Drop it on this box</b>, or
    <button id="browse" class="quiet">choose a file</button></div>
  <div class="note" style="margin:6px 0 0">Read here and converted in place &mdash;
  nothing is uploaded anywhere.</div>
  <input id="file" type="file" accept=".3mf" style="display:none">
</div>

<!-- Out of the way, and reachable only if the two routes above fail. A webview
     that does not forward file drops or open a file dialog leaves a path as the
     only way in, and that is rare enough not to deserve space on the tab. -->
<p class="note" id="pathFallback" style="margin:6px 0 0">Neither working?
  <a href="#" id="showPath">Paste a path instead.</a></p>
<div class="row" id="pathRow" hidden style="margin-top:6px">
  <input id="path" type="text" placeholder="full path to a .3mf">
  <button id="convertPath">Convert &amp; open</button>
</div>

<div id="report" class="panel" hidden></div>

<div id="convertList" class="panel" hidden>
  <div style="margin-bottom:6px"><b>Already converted</b>
    <span class="meta">— kept beside the plugin; open one again without going to
    find it</span></div>
  <div id="convFiles"></div>
</div>
<div id="banner"></div>

<script>
(function () {
  var busy = false;

  function $(id) {
    var key = String(id);
    var el = document.getElementById(key.charAt(0) === "#" ? key.slice(1) : key);
    if (!el) console.log("makerworld: no element #" + key);
    return el;
  }

  function post(message) {
    try { orca.postMessage(message); }
    catch (e) { showBanner("Could not reach the plugin: " + e, "err"); }
  }

  function showBanner(text, kind) {
    var banner = $("banner");
    if (!banner) return;
    banner.textContent = text || "";
    banner.className = kind || "";
    banner.style.display = text ? "block" : "none";
  }

  function esc(value) {
    var span = document.createElement("span");
    span.textContent = value === null || value === undefined ? "" : String(value);
    return span.innerHTML;
  }

  function attr(value) {
    return esc(value).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function setBusy(on, what) {
    busy = on;
    var resolve = $("resolve"), convert = $("convertPath");
    if (resolve) resolve.disabled = on;
    if (convert) convert.disabled = on;
    if (on) showBanner(what || "Working...", "");
  }

  function renderPlates(message) {
    var box = $("plates"), pick = $("pick"), title = $("pickTitle");
    if (!box || !pick) return;
    var plates = message.plates || [];
    if (!plates.length) {
      pick.hidden = true;
      showBanner("That design has no printable plates.", "err");
      return;
    }
    title.innerHTML = "<b>" + esc(message.title) + "</b>";
    box.innerHTML = plates.map(function (plate) {
      var bits = [];
      if (plate.filament_weight) bits.push(Math.round(plate.filament_weight) + " g");
      if (plate.size) bits.push((Math.round(plate.size / 104857.6) / 10) + " MB");
      return '<div class="plate"><span>' + esc(plate.title) + '</span>' +
             '<span class="meta">' + esc(bits.join(" \\u00b7 ")) + '</span>' +
             '<button class="primary" data-instance="' + attr(plate.instance_id) +
             '" data-title="' + attr(plate.title) + '">Convert &amp; open</button></div>';
    }).join("");
    pick.hidden = false;
  }

  function sizeText(bytes) {
    if (!bytes) return "";
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1048576) return Math.round(bytes / 1024) + " kB";
    return (bytes / 1048576).toFixed(1) + " MB";
  }

  function whenText(seconds) {
    if (!seconds) return "";
    var mins = Math.round(Date.now() / 1000 - seconds) / 60;
    if (mins < 1) return "just now";
    if (mins < 60) return Math.round(mins) + " min ago";
    var hours = mins / 60;
    if (hours < 24) return Math.round(hours) + "h ago";
    return Math.round(hours / 24) + "d ago";
  }

  // Attribute values need the quote escaped too, and esc() only handles the
  // markup characters.
  function attr(text) {
    return esc(text).replace(/"/g, "&quot;");
  }

  function renderFiles(message) {
    var box = $("convFiles");
    if (!box || !$("convertList")) return;
    var files = message.files || [];
    if (!files.length) {
      box.innerHTML = "";
      $("convertList").hidden = true;
      return;
    }
    box.innerHTML = files.map(function (f) {
      return '<div class="plate"><span>' + esc(f.name) +
             ' <span class="meta">' + sizeText(f.size) + " &middot; " +
             whenText(f.modified) + "</span></span>" +
             '<button data-open="' + attr(f.path) + '">Open</button></div>';
    }).join("");
    $("convertList").hidden = false;
  }

  function keyBox(title, keys) {
    if (!keys || !keys.length) return "";
    return '<details class="audit"><summary>' + esc(title) +
           ' <span class="meta">' + keys.length + "</span></summary>" +
           '<div class="keys">' + keys.map(function (key) {
             return "<code>" + esc(key) + "</code>";
           }).join("") + "</div></details>";
  }

  function renderReport(message) {
    var box = $("report");
    if (!box) return;
    var r = message.report || {};
    var kept = (r.kept_examples || []).map(function (key) {
      return esc(key.replace(/_/g, " "));
    }).join(", ");
    var lines = [
      "<b>" + esc(r.printer) + "</b> project, rebuilt from " + esc(r.source_printer),
      "<b>" + r.kept + "</b> settings kept from the designer" +
        (kept ? " (including " + kept + ")" : ""),
      "<b>" + r.replaced + "</b> taken from your printer, <b>" + (r.relabelled || 0) +
        "</b> pointed at your own presets, <b>" + r.dropped +
        "</b> reset to OrcaSlicer's defaults"
    ];

    var plates = r.plates || [];
    if (plates.length) {
      lines.push("<b>" + plates.length + "</b> plate" +
                 (plates.length === 1 ? "" : "s") + " in the file:");
      plates.forEach(function (p) {
        var fil = (p.filaments || []).map(function (f) {
          return esc(f.type || "?") + (f.colour ? " " + esc(f.colour) : "") +
                 (f.used_g ? " (" + esc(f.used_g) + " g)" : "");
        }).join(", ");
        lines.push("&nbsp;&nbsp;plate " + esc(p.index) + (fil ? " &mdash; " + fil : ""));
      });
    }

    var filaments = r.filaments || [];
    if (filaments.length) {
      lines.push("<b>" + filaments.length + "</b> tool" +
                 (filaments.length === 1 ? "" : "s") + " after conversion:");
      filaments.forEach(function (f) {
        lines.push("&nbsp;&nbsp;tool " + f.tool + " &mdash; " + esc(f.type || "?") +
                   (f.colour ? " " + esc(f.colour) : "") +
                   (f.profile ? " <code>" + esc(f.profile) + "</code>" : ""));
      });
    }

    var gcode = r.gcode || [];
    if (gcode.length) {
      lines.push("Machine G-code, replaced with your printer's:");
      gcode.forEach(function (g) {
        var head = (g.head || []).map(function (line) {
          return esc(line);
        }).join("<br>");
        lines.push("&nbsp;&nbsp;" + esc(g.which) + " &mdash; " + g.lines + " line" +
                   (g.lines === 1 ? "" : "s") +
                   (g.more ? ", first " + g.head.length + " shown" : "") +
                   (head ? ": <code>" + head + "</code>" : ""));
      });
    }

    lines.push("Saved as <code>" + esc(message.path) + "</code>");
    var html = lines.map(function (line) {
      return '<div class="line">' + line + "</div>";
    }).join("");

    (r.warnings || []).forEach(function (w) {
      html += '<div class="warn"><b>' +
              (w.level === "check" ? "Check" : "Note") + "</b> &mdash; " +
              esc(w.message) + "</div>";
    });

    // The counts above are only trustworthy if the lists behind them can be
    // read, so every one of them opens.
    html += keyBox("Kept from the designer", r.kept_keys);
    html += keyBox("Pointed at your own presets", r.relabelled_keys);
    html += keyBox("Replaced by your printer", r.replaced_keys);
    html += keyBox("Reset to OrcaSlicer's defaults", r.dropped_keys);
    html += keyBox("Removed: printer connection settings", r.stripped_keys);

    box.innerHTML = html;
    box.hidden = false;
  }

  orca.onMessage(function (message) {
    if (!message || !message.type) return;
    switch (message.type) {
      case "state":
        if ($("token") && message.token_set) {
          $("token").placeholder = "(saved)";
          // Otherwise the confirmation is inside a closed panel, which reads as
          // nothing having been saved.
          if ($("tokenPanel")) $("tokenPanel").open = true;
        }
        if (message.printer) showBanner("Converting for: " + message.printer, "");
        break;
      case "resolved":
        setBusy(false);
        showBanner("", "");
        renderPlates(message);
        break;
      case "report":
        setBusy(false);
        renderReport(message);
        post({ type: "list_files" });
        // One message, not two: a follow-up line would replace this one before it
        // could be read.
        if (message.opened) {
          showBanner("Opened in OrcaSlicer - slice it and use Send to printer.", "ok");
        } else {
          showBanner("Converted and saved, but it could not be opened for you (" +
                     message.detail + ").\\nOpen it yourself with File \\u2192 " +
                     "Open Project...\\n" + message.path, "err");
        }
        break;
      case "files":
        renderFiles(message);
        break;
      case "need_token":
        // The user has somewhere to go, so take them there rather than only
        // naming the problem.
        setBusy(false);
        if ($("tokenPanel")) $("tokenPanel").open = true;
        showBanner(message.message, "err");
        break;
      case "opened":
        setBusy(false);
        if (message.opened) {
          showBanner("Opened in OrcaSlicer.", "ok");
        } else {
          showBanner("Converted, but it could not be opened for you (" +
                     message.detail + "). Open it yourself with File, then " +
                     "Open Project.", "err");
        }
        break;
      case "log":
        showBanner(message.message, message.level === "error" ? "err" : "");
        break;
      case "error":
        setBusy(false);
        showBanner(message.message, "err");
        break;
      default:
        break;
    }
  });

  if ($("saveToken")) $("saveToken").addEventListener("click", function () {
    post({ type: "save_token", token: $("token") ? $("token").value.trim() : "" });
  });
  if ($("resolve")) $("resolve").addEventListener("click", function () {
    var url = $("url") ? $("url").value.trim() : "";
    if (!url) { showBanner("Paste a MakerWorld link first.", "err"); return; }
    setBusy(true, "Looking up the design...");
    post({ type: "resolve", url: url });
  });
  if ($("plates")) $("plates").addEventListener("click", function (event) {
    var button = event.target.closest("button[data-instance]");
    if (!button || busy) return;
    setBusy(true, "Downloading and converting...");
    post({ type: "fetch", instance_id: button.getAttribute("data-instance"),
           title: button.getAttribute("data-title") });
  });
  function convertFromPath() {
    var path = $("path") ? $("path").value.trim() : "";
    if (!path) { showBanner("Give the full path to a .3mf file.", "err"); return; }
    setBusy(true, "Reading and converting...");
    post({ type: "convert_path", path: path });
  }

  if ($("showPath")) $("showPath").addEventListener("click", function (e) {
    e.preventDefault();
    if ($("pathRow")) $("pathRow").hidden = false;
    if ($("pathFallback")) $("pathFallback").hidden = true;
    if ($("path")) $("path").focus();
  });

  if ($("convertPath")) $("convertPath").addEventListener("click", convertFromPath);
  // A dropped file converts on its own and so does a chosen one, because each has
  // an end: the file lands, the dialog closes. A typed path has no such moment, so
  // Enter is what makes the button an alternative rather than a requirement.
  if ($("path")) $("path").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); convertFromPath(); }
  });

  // A dropped or picked file. The page can read the bytes but cannot see a path,
  // so the file is base64-encoded and handed to the plugin, which converts it
  // exactly as it would a typed path.
  function readPicked(file) {
    if (!file) return;
    if (String(file.name || "").toLowerCase().slice(-4) !== ".3mf") {
      showBanner("That is not a .3mf file: " + (file.name || "unknown"), "err");
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      var text = String(reader.result || "");
      var comma = text.indexOf(",");
      if (comma < 0) { showBanner("Could not read that file.", "err"); return; }
      setBusy(true, "Converting " + file.name + "...");
      post({ type: "convert_data", name: file.name, data: text.slice(comma + 1) });
    };
    reader.onerror = function () { showBanner("Could not read that file.", "err"); };
    try { reader.readAsDataURL(file); }
    catch (e) { showBanner("Could not read that file: " + e, "err"); }
  }

  if ($("drop")) {
    var drop = $("drop");
    ["dragenter", "dragover"].forEach(function (type) {
      drop.addEventListener(type, function (e) {
        e.preventDefault();
        drop.classList.add("over");
      });
    });
    ["dragleave", "drop"].forEach(function (type) {
      drop.addEventListener(type, function (e) {
        e.preventDefault();
        drop.classList.remove("over");
      });
    });
    drop.addEventListener("drop", function (e) {
      var dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) readPicked(dt.files[0]);
    });
  }
  if ($("browse") && $("file")) {
    $("browse").addEventListener("click", function (e) {
      e.preventDefault();
      $("file").click();
    });
    $("file").addEventListener("change", function (e) {
      var files = e.target.files;
      if (files && files.length) readPicked(files[0]);
    });
  }

  if ($("convFiles")) $("convFiles").addEventListener("click", function (e) {
    var el = e.target;
    var path = el && el.getAttribute ? el.getAttribute("data-open") : null;
    if (!path) return;
    setBusy(true, "Opening...");
    post({ type: "open_file", path: path });
  });

  // A panel wired only to setInterval renders empty on first load, so ask once now.
  showBanner("", "");
  post({ type: "hello" });
  post({ type: "list_files" });
})();
</script>
"""


def safe_filename(text, fallback="project"):
    """A filename built from a model title, with nothing the OS can object to."""
    cleaned = re.sub(r"[^A-Za-z0-9 ._-]+", "", str(text or "")).strip(" .")
    return (cleaned or fallback)[:80]


class MakerWorldPage(orca.pages.PagesPluginCapabilityBase):
    """The 'MakerWorld' tab: convert a MakerWorld project for the selected printer."""

    def get_name(self):
        return "3mf Converter"

    def get_ui(self):
        return MAKERWORLD_PAGE

    def on_load(self):
        # Directory creation is not covered by a persistent fs grant and is denied
        # outright off the main thread, so it has to happen here, before any
        # conversion worker tries to write.
        try:
            os.makedirs(CONVERTED_DIR, exist_ok=True)
        except Exception:
            pass

    # -- inbound ------------------------------------------------------------ #

    def on_message(self, msg):
        if not isinstance(msg, dict):
            return
        handler = {
            "hello": self._hello,
            "save_token": self._save_token,
            "resolve": self._resolve,
            "fetch": self._fetch,
            "convert_path": self._convert_path,
            "convert_data": self._convert_data,
            "list_files": self._list_files,
            "open_file": self._open_file,
        }.get(msg.get("type"))
        if not handler:
            return
        try:
            handler(msg)
        except TokenNeeded as exc:
            self.post_message({"type": "need_token", "message": str(exc)})
        except MakerWorldError as exc:
            self.post_message({"type": "error", "message": str(exc)})
        except Exception as exc:            # never let the tab die on a bug
            self.post_message({"type": "error",
                               "message": f"{type(exc).__name__}: {exc}"})

    # -- handlers ----------------------------------------------------------- #

    def _hello(self, msg):
        self._ensure_dirs()
        self.post_message({
            "type": "state",
            "token_set": bool(load_settings().get("bambu_token")),
            "printer": self._selected_printer(),
        })

    def _save_token(self, msg):
        token = (msg.get("token") or "").strip()
        ok, error = save_settings({"bambu_token": token})
        if not ok:
            raise MakerWorldError(f"Could not write settings.json: {error}")
        self.post_message({"type": "state", "token_set": bool(token),
                           "printer": self._selected_printer()})
        self.post_message({"type": "log",
                           "message": "Token saved." if token else "Token cleared."})

    def _resolve(self, msg):
        _, design_id = parse_makerworld_url(msg.get("url"))
        details = fetch_design(design_id)
        self.post_message({"type": "resolved",
                           "title": details.get("title") or "",
                           "plates": design_plates(details)})

    def _fetch(self, msg):
        instance_id = msg.get("instance_id")
        if instance_id is None:
            raise MakerWorldError("No plate was chosen.")
        title = msg.get("title") or "makerworld"
        token = load_settings().get("bambu_token")
        threading.Thread(target=self._fetch_worker,
                         args=(instance_id, title, token), daemon=True).start()

    def _fetch_worker(self, instance_id, title, token):
        try:
            blob, name = fetch_instance_3mf(instance_id, token)
            self._convert_and_open(blob, title or name)
        except TokenNeeded as exc:
            # This runs off the main thread, so it has to classify its own failure
            # rather than leaving it to the message handler.
            self.post_message({"type": "need_token", "message": str(exc)})
        except MakerWorldError as exc:
            self.post_message({"type": "error", "message": str(exc)})
        except Exception as exc:
            self.post_message({"type": "error",
                               "message": f"{type(exc).__name__}: {exc}"})

    def _convert_path(self, msg):
        path = (msg.get("path") or "").strip().strip('"').strip("'")
        if not path:
            raise MakerWorldError("Give the full path to a .3mf file.")
        # Read on the main thread: a path outside the plugin's own roots raises an
        # audit prompt, and one raised off the main thread is denied silently.
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError as exc:
            raise MakerWorldError(f"Could not read {path}: {exc}") from exc
        title = os.path.splitext(os.path.basename(path))[0]
        threading.Thread(target=self._convert_and_open,
                         args=(blob, title), daemon=True).start()

    def _convert_data(self, msg):
        """A file the page read for the user, by drop or by picker.

        The page can read the bytes but never sees a path, so the file arrives
        here base64-encoded and is converted exactly as a typed path is. Nothing
        leaves the machine.
        """
        data = msg.get("data") or ""
        if len(data) > MAX_UPLOAD_B64:
            raise MakerWorldError(
                f"That file is larger than {MAX_UPLOAD_B64 // (1024 * 1024)} MB, "
                "which is more than this route can carry. Save it to disk and "
                "convert it by path instead.")
        try:
            blob = base64.b64decode(data)
        except Exception as exc:
            raise MakerWorldError(f"Could not read the file: {exc}") from exc
        if not blob:
            raise MakerWorldError("That file arrived empty.")
        stem = os.path.splitext(os.path.basename(str(msg.get("name") or "project")))[0]
        self._ensure_dirs()
        threading.Thread(target=self._convert_and_open,
                         args=(blob, safe_filename(stem, "project")),
                         daemon=True).start()

    # -- shared ------------------------------------------------------------- #

    def _converted_files(self):
        """The projects this plugin has converted, newest first.

        They live inside OrcaSlicer's own data directory, several folders down,
        which is exactly why this list exists — nobody wants to go looking there.
        """
        rows = []
        try:
            names = os.listdir(CONVERTED_DIR)
        except OSError:
            return rows
        for name in names:
            if not name.lower().endswith(".3mf"):
                continue
            full = os.path.join(CONVERTED_DIR, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            rows.append({"name": name, "path": full,
                         "size": stat.st_size, "modified": stat.st_mtime})
        rows.sort(key=lambda row: row["modified"], reverse=True)
        return rows

    def _list_files(self, _msg):
        self.post_message({"type": "files", "files": self._converted_files(),
                           "dir": CONVERTED_DIR})

    def _open_file(self, msg):
        """Re-open something already converted, without hunting for the folder."""
        path = str(msg.get("path") or "")
        # Only ever hand OrcaSlicer a file this plugin wrote. The path comes from
        # the page, and a page-supplied path must not become a way to open
        # anything on the disk.
        if not path or (os.path.normcase(os.path.abspath(os.path.dirname(path)))
                        != os.path.normcase(os.path.abspath(CONVERTED_DIR))):
            raise MakerWorldError("That file is not one of this plugin's conversions.")
        if not os.path.exists(path):
            raise MakerWorldError("That file is no longer there.")
        opened, detail = load_into_orca([path])
        self.post_message({"type": "opened", "path": path,
                           "opened": bool(opened), "detail": detail})

    def _ensure_dirs(self):
        try:
            os.makedirs(CONVERTED_DIR, exist_ok=True)
        except Exception:
            pass

    def _selected_printer(self):
        try:
            preset = orca.host.preset_bundle().current_printer_preset()
            return getattr(preset, "name", "") or ""
        except Exception:
            return ""

    def _convert_and_open(self, blob, title):
        try:
            converted, report, printer = convert_3mf(blob)
            self._ensure_dirs()
            out = os.path.join(CONVERTED_DIR, safe_filename(
                f"{title} - {printer}") + ".3mf")
            with open(out, "wb") as handle:
                handle.write(converted)
            opened, detail = load_into_orca([out])
            self.post_message({"type": "report", "report": report, "path": out,
                               "opened": bool(opened), "detail": detail})
        except MakerWorldError as exc:
            self.post_message({"type": "error", "message": str(exc)})
        except Exception as exc:
            self.post_message({"type": "error",
                               "message": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- #
# Package
# --------------------------------------------------------------------------- #

@orca.plugin
class MakerWorldC5Plugin(orca.base):
    """The 3mf Converter tab: rebuild a downloaded .3mf for whichever printer is
    selected in OrcaSlicer."""

    def register_capabilities(self):
        try:
            orca.request_permissions(
                # A scheme://host entry is host-wide, so paths do not need listing.
                network_http=[
                    "https://makerworld.com",
                    "https://api.bambulab.com",
                    "https://makerworld.bblmw.com",
                ],
                fs_readwrite=[PLUGIN_DIR],
                # Handing a finished project to the running instance means running
                # the platform's own messenger. Process targets are matched
                # verbatim, so both the bare name and the usual absolute path are
                # declared; if neither matches, the tab says so and prints the path
                # to open by hand rather than failing silently.
                process=["dbus-send", "/usr/bin/dbus-send", "open", "/usr/bin/open"],
                threading=True,
            )
        except Exception:
            pass  # older builds may not expose the declaration

        orca.register_capability(MakerWorldPage)
