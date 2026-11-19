"""A minimal stand-in for OrcaSlicer's embedded `orca` module.

Only the surface the plugin actually touches. Installing this into
``sys.modules`` is what lets the plugin be imported and exercised without a copy
of OrcaSlicer, so the tab's message flow can be driven end to end in a test.
"""

import json
import types
from enum import Enum


class PluginType(Enum):
    PrinterConnection = "printer-connection"
    Pages = "pages"
    Analysis = "analysis"
    Importer = "importer"
    Exporter = "exporter"
    Visualization = "visualization"
    Script = "script"
    SlicingPipeline = "slicing-pipeline"
    ResourceTexture = "resource-texture"
    Unknown = "unknown"


class PluginResult(Enum):
    Success = "success"
    Skipped = "skipped"
    RecoverableError = "recoverable-error"
    FatalError = "fatal-error"


class ExecutionResult:
    def __init__(self, status, message="", data=""):
        self.status = status
        self.message = message
        self.data = data

    @staticmethod
    def success(message="", data=""):
        return ExecutionResult(PluginResult.Success, message, data)

    @staticmethod
    def skipped(message=""):
        return ExecutionResult(PluginResult.Skipped, message)

    @staticmethod
    def failure(status, message, data=""):
        return ExecutionResult(status, message, data)


class LifecycleEvent(Enum):
    DeviceDiscovered = "device-discovered"
    DeviceSelected = "device-selected"
    DeviceOnline = "device-online"
    DeviceOffline = "device-offline"
    PrintStateChanged = "print-state-changed"


class LifecycleEvtCode(Enum):
    Ok = "ok"
    Error = "error"
    Warn = "warn"


class PythonPluginBase:
    """Root capability base. Config access is backed by a per-instance dict here."""

    _config = {}

    def get_name(self):
        raise NotImplementedError

    def get_type(self):
        return PluginType.Unknown

    def on_load(self):
        return None

    def on_unload(self):
        return None

    def on_lifecycle_event(self, event, ctx):
        return None

    def has_config_ui(self):
        return False

    def get_config_ui(self):
        return ""

    def get_default_config(self):
        return {}

    def get_config(self):
        return json.dumps(type(self)._config.get(id(self), {}))

    def get_config_version(self):
        return ""

    def save_config(self, config):
        if isinstance(config, str):
            config = json.loads(config)
        type(self)._config[id(self)] = config
        return True


# --- orca.pages ------------------------------------------------------------

class PagesPluginCapabilityBase(PythonPluginBase):
    def get_type(self):
        return PluginType.Pages

    def get_ui(self):
        raise NotImplementedError

    def get_icon(self):
        return ""

    def on_message(self, data):
        return None

    def post_message(self, data):
        """Overridden in tests to capture what the tab would receive."""
        return None


# --- orca.slicing ----------------------------------------------------------

class Step(Enum):
    posSlice = "pos-slice"
    posPerimeters = "pos-perimeters"
    posPrepareInfill = "pos-prepare-infill"
    posInfill = "pos-infill"
    posIroning = "pos-ironing"
    posContouring = "pos-contouring"
    posSupportMaterial = "pos-support-material"
    posSimplifyPath = "pos-simplify-path"
    psWipeTower = "ps-wipe-tower"
    psSkirtBrim = "ps-skirt-brim"
    psGCodePostProcess = "ps-gcode-post-process"


class SlicingPipelineContext:
    def __init__(self, **kw):
        self.orca_version = "test"
        self.step = None
        self.print = None
        self.object = None
        self.gcode_path = ""
        self.host = ""
        self.output_name = ""
        for key, value in kw.items():
            setattr(self, key, value)


class SlicingPipelineCapabilityBase(PythonPluginBase):
    def get_type(self):
        return PluginType.SlicingPipeline

    def execute(self, ctx):
        raise NotImplementedError


# --- orca.printer_agent ----------------------------------------------------

class AgentInfo:
    def __init__(self, id="", name="", version="", description=""):
        self.id = id
        self.name = name
        self.version = version
        self.description = description


class PrinterConnectionParams:
    def __init__(self, **kw):
        self.dev_id = ""
        self.host = ""
        self.port = ""
        self.username = ""
        self.password = ""
        self.use_ssl = False
        self.ca_file = ""
        for key, value in kw.items():
            setattr(self, key, value)


class PrintParams:
    def __init__(self, **kw):
        for key in ("dev_id", "dev_ip", "dev_name", "filename", "project_name",
                    "task_name", "preset_name", "password", "username",
                    "connection_type", "dst_file", "ams_mapping",
                    "ams_mapping_info", "plate_index"):
            setattr(self, key, "")
        for key, value in kw.items():
            setattr(self, key, value)


class DetectResult:
    def __init__(self, **kw):
        for key in ("result_msg", "command", "dev_id", "model_id", "dev_name",
                    "version", "bind_state", "connect_type"):
            setattr(self, key, "")
        for key, value in kw.items():
            setattr(self, key, value)


class FilamentSyncMode(Enum):
    Subscription = "subscription"
    Pull = "pull"
    None_ = "none"


class CameraStreamMode(Enum):
    HTTP = "http"
    HTTPS = "https"
    HTTP_SNAPSHOT = "http-snapshot"
    RTSP = "rtsp"
    None_ = "none"


class PrinterAgentBase(PythonPluginBase):
    def get_type(self):
        return PluginType.PrinterConnection


# --- module assembly -------------------------------------------------------

class _Base:
    """orca.base — the package class."""

    def register_capabilities(self):
        raise NotImplementedError


# --- orca.host -------------------------------------------------------------

class FakePreset:
    """A preset stand-in: the keys it sets, as config_value() would return them."""

    def __init__(self, name="", **values):
        self.name = name
        self.values = dict(values)

    def config_value(self, key):
        return self.values.get(key)

    def config_keys(self):
        return sorted(self.values)

    def label(self, no_alias=False):
        return self.name


class FakePresetCollection:
    def __init__(self, presets):
        self._presets = list(presets)

    def size(self):
        return len(self._presets)

    def preset(self, index):
        if not 0 <= index < len(self._presets):
            raise IndexError("preset index out of range")
        return self._presets[index]

    def preset_names(self):
        return [p.name for p in self._presets]


class FakePresetBundle:
    """Stands in for PresetBundle: a printer collection plus a resolved config.

    ``current`` is the edited printer preset; ``full`` is the bundle's flattened
    configuration, which is the only place a value set on the preset's parent
    shows up.
    """

    def __init__(self, printers=None, full=None, current=None):
        self._printers = list(printers or [])
        self._full = dict(full or {})
        self._current = current

    @property
    def printers(self):
        return FakePresetCollection(self._printers)

    def current_printer_preset(self):
        if self._current is None:
            raise RuntimeError("no printer preset is selected")
        return self._current

    def full_config_value(self, key):
        return self._full.get(key)

    def full_config_keys(self):
        return sorted(self._full)


def make_module():
    module = types.ModuleType("orca")
    module.PluginType = PluginType
    module.PluginResult = PluginResult
    module.ExecutionResult = ExecutionResult
    module.LifecycleEvent = LifecycleEvent
    module.LifecycleEvtCode = LifecycleEvtCode
    module.PythonPluginBase = PythonPluginBase
    module.base = _Base

    registered = []

    def register_capability(cls):
        registered.append(cls)

    module.registered_capabilities = registered
    module.register_capability = register_capability
    module.requested_permissions = {}

    def request_permissions(**kwargs):
        module.requested_permissions.update(kwargs)

    module.request_permissions = request_permissions

    def plugin(cls):
        module.plugin_class = cls
        return cls

    module.plugin = plugin

    pages = types.ModuleType("orca.pages")
    pages.PagesPluginCapabilityBase = PagesPluginCapabilityBase
    module.pages = pages

    slicing = types.ModuleType("orca.slicing")
    slicing.Step = Step
    slicing.SlicingPipelineContext = SlicingPipelineContext
    slicing.SlicingPipelineCapabilityBase = SlicingPipelineCapabilityBase
    module.slicing = slicing

    agent = types.ModuleType("orca.printer_agent")
    agent.PrinterAgentBase = PrinterAgentBase
    agent.AgentInfo = AgentInfo
    agent.PrinterConnectionParams = PrinterConnectionParams
    agent.PrintParams = PrintParams
    agent.DetectResult = DetectResult
    agent.FilamentSyncMode = FilamentSyncMode
    agent.CameraStreamMode = CameraStreamMode
    module.printer_agent = agent

    module.host = types.ModuleType("orca.host")

    def _no_bundle():
        # What the real host does before the GUI/model is ready.
        raise RuntimeError("the GUI is not ready")

    module.host.preset_bundle = _no_bundle
    module.host.app_language = lambda: "en_US"

    def install_preset_bundle(bundle):
        """Point orca.host at a bundle, or back at the not-ready state."""
        module.host.preset_bundle = (lambda: bundle) if bundle is not None else _no_bundle

    module.install_preset_bundle = install_preset_bundle
    return module
