"""Native Aether MDE integration for the local Maestro compatibility layer."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Mapping

from .errors import OperationError, PreflightError, ValidationError
from .maestro import MaestroStore, apply_maestro_action, maestro_readback, normalize_maestro_analysis_options, normalize_native_result, state_digest
from .artifacts import atomic_json, atomic_text, file_digest
from .contract import engineering
from .maestro_settings import empty_mde_setting, native_analysis_entries, read_mde_setting, setting_text
from .maestro_specs import spec_text
from .native_tools import native_tool_environment
from .results import safe_input_path, safe_result_path


_RETAINED_SESSIONS = {}
_EPHEMERAL_SESSIONS = {}


def _value(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    try:
        return str(value)
    except Exception:
        return default


def _member(value: Any, name: str, default: Any = None) -> Any:
    try:
        member = getattr(value, name)
        return member() if callable(member) else member
    except Exception:
        return default


def _safe_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("Maestro state directory is unsafe", stage="maestro", code="unsafe_state_path")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValidationError("Maestro state template contains a symlink", stage="maestro", code="unsafe_state_template")
        if path.is_file():
            files.append(path)
    return files


class MaestroNativeOps:
    def __init__(self, api, *, workspace: str | Path | None = None, template: str | Path | None = None):
        self.api = api
        self.workspace = Path(workspace).expanduser().resolve() if workspace else Path.cwd().resolve()
        self.template = Path(template).expanduser().resolve() if template else None

    def _library_path(self, library: str) -> Path:
        getter = getattr(self.api, "dbGetLibIdByLibName", None)
        if not callable(getter):
            raise PreflightError("Native library path API is unavailable", stage="maestro", code="native_api_missing")
        lib_obj = getter(library)
        if not lib_obj:
            raise OperationError("Maestro library does not exist", stage="maestro", code="library_missing", details={"library": library})
        dm_obj = lib_obj
        for name in ("writePath", "path", "readPath"):
            value = _member(dm_obj, name)
            if value:
                return Path(str(value)).expanduser().resolve()
        getter = getattr(dm_obj, "getPath", None)
        if callable(getter):
            try:
                target = self.api.emyString()
                getter(target)
                if str(target):
                    return Path(str(target)).expanduser().resolve()
            except Exception:
                pass
        dm_get = getattr(self.api, "dmGetObj", None)
        if callable(dm_get):
            try:
                dm_obj = dm_get(self.api.emyString(library))
                for name in ("writePath", "path", "readPath"):
                    value = _member(dm_obj, name)
                    if value:
                        return Path(str(value)).expanduser().resolve()
            except Exception:
                pass
        raise OperationError("Native library path is unavailable", stage="maestro", code="library_path_missing", details={"library": library})

    def _state_dir(self, target: Mapping[str, Any]) -> Path:
        return self._library_path(target["library"]) / target["cell"] / target.get("view", "state")

    def capability_report(self) -> dict[str, Any]:
        """Return an explicit, runtime-derived MDE capability matrix.

        The report is intentionally conservative: a class is only advertised when
        both its analysis class and setting writer/enable methods are present.
        This gives a caller a machine-readable answer instead of guessing from the
        portable vocabulary.
        """
        classes = {
            "tran": ("MdeTranAnalysis", "setTranAnalysis", "enableTranAnalysis", "disableTranAnalysis"),
            "dc": ("MdeDcAnalysis", "setDcAnalysis", "enableDcAnalysis", "disableDcAnalysis"),
            "ac": ("MdeAcAnalysis", "setAcAnalysis", "enableAcAnalysis", "disableAcAnalysis"),
            "noise": ("MdeNoiseAnalysis", "setNoiseAnalysis", "enableNoiseAnalysis", "disableNoiseAnalysis"),
            "stb": ("MdeStbAnalysis", "setStbAnalysis", "enableStbAnalysis", "disableStbAnalysis"),
            "pss": ("MdePssAnalysis", "setPssAnalysis", "enablePssAnalysis", "disablePssAnalysis"),
            "pnoise": ("MdePnoiseAnalysis", "setPnoiseAnalysis", "enablePnoiseAnalysis", "disablePnoiseAnalysis"),
            "pac": ("MdePacAnalysis", "setPacAnalysis", "enablePacAnalysis", "disablePacAnalysis"),
            "pxf": ("MdePxfAnalysis", "setPxfAnalysis", "enablePxfAnalysis", "disablePxfAnalysis"),
            "hb": ("MdeHbAnalysis", "setHbAnalysis", "enableHbAnalysis", "disableHbAnalysis"),
        }
        # Having a class and a setter is not sufficient: each analysis needs a
        # complete option projection and a readback verifier.  Keep this list
        # in one place so the capability report cannot advertise an analysis
        # that _set_native_analysis would reject later.
        implemented = {"tran", "dc", "ac", "noise", "stb"}
        option_schema = {
            "tran": {"required": ["stop"], "optional": ["accuracy"]},
            "dc": {"required": ["start", "stop"], "one_of": ["source", "variable"], "optional": ["step", "points", "sweep", "parameter"]},
            "ac": {"required": ["start", "stop"], "optional": ["step", "points", "sweep"]},
            "noise": {"required": ["start", "stop", "output_mode", "input_mode"], "optional": ["step", "points", "sweep", "probe", "positive_output", "negative_output", "input_voltage_source", "input_current_source", "input_port_source", "noise_separation"]},
            "stb": {"required": ["start", "stop", "probe", "mode_type", "mode_type_enabled", "local_ground"], "optional": ["step", "points", "sweep"]},
            "pss": {"required": [], "optional": [], "native_adapter": False}, "pnoise": {"required": [], "optional": [], "native_adapter": False},
            "pac": {"required": [], "optional": [], "native_adapter": False}, "pxf": {"required": [], "optional": [], "native_adapter": False},
            "hb": {"required": [], "optional": [], "native_adapter": False}, "op": {"required": [], "optional": [], "native_adapter": False},
        }
        result = {"schema_version": 1, "native_mde": True, "analyses": {}, "actions": {
            "session": ["open", "read", "close", "find"],
            "state": ["create_test", "delete_test", "set_test_enabled", "set_design", "set_analysis",
                      "add_output", "delete_output", "set_spec", "set_var", "delete_var", "set_parameter",
                      "set_corner", "delete_corner", "set_env_option", "set_sim_option", "clear_results", "save_setup"],
            "execution": ["create_netlist", "run", "read_results", "export_waveform", "export_output_view",
                          "snapshot_state", "write_script", "recover"],
            "jobs": ["run_simulation", "run_and_wait", "job_status", "job_wait", "job_cancel"],
        }}
        setting_type = getattr(self.api, "MdeSetting", None)
        for name, (class_name, setter, enable, disable) in classes.items():
            native_class = hasattr(self.api, class_name)
            writer = callable(getattr(setting_type, setter, None))
            enable_ready = callable(getattr(setting_type, enable, None))
            disable_ready = callable(getattr(setting_type, disable, None))
            result["analyses"][name] = {"portable": True,
                                         "native_class": native_class, "writer": writer,
                                         "enable": enable_ready, "disable": disable_ready,
                                         "native_adapter": name in implemented,
                                         "option_schema": option_schema.get(name, {})}
        op_writer = callable(getattr(setting_type, "setOpAnalysis", None))
        result["analyses"]["op"] = {"portable": True, "native_class": True, "writer": op_writer,
                                      "enable": callable(getattr(setting_type, "enableOpAnalysis", None)),
                                      "disable": callable(getattr(setting_type, "disableOpAnalysis", None)),
                                      "native_adapter": False, "option_schema": option_schema["op"]}
        result["supported_analyses"] = sorted(name for name, value in result["analyses"].items()
                                               if value["native_class"] and value["writer"] and value.get("native_adapter") and value.get("enable") and value.get("disable"))
        result["unsupported_analyses"] = sorted(name for name, value in result["analyses"].items()
                                                 if name not in result["supported_analyses"])
        for name, value in result["analyses"].items():
            if name in result["supported_analyses"]:
                value["status"] = "native_verified_adapter_surface"
            elif value.get("native_class") and value.get("writer"):
                value["status"] = "native_class_present_but_adapter_unimplemented"
            else:
                value["status"] = "portable_only_or_native_api_missing"
        return result

    def _template_path(self, options: Mapping[str, Any] | None = None) -> Path:
        options = options or {}
        candidates = []
        if options.get("maestro_template"):
            candidates.append(Path(options["maestro_template"]).expanduser())
        if self.template:
            candidates.append(self.template)
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_dir() and (candidate / "setting").is_file():
                _safe_files(candidate)
                return candidate
        raise PreflightError("No native MDE state template is configured", stage="maestro", code="maestro_template_missing")

    def _materialize_state(self, target: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> Path:
        state_dir = self._state_dir(target)
        if state_dir.exists():
            if state_dir.is_symlink() or not state_dir.is_dir():
                raise OperationError("Maestro state path is unsafe", stage="maestro", code="unsafe_state_path")
            if not (state_dir / "setting").is_file():
                raise OperationError("Maestro state is missing its native setting file", stage="maestro", code="state_corrupt")
            return state_dir
        state_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = state_dir.parent / (".state-" + os.urandom(8).hex())
        try:
            if self.template or (options or {}).get("maestro_template"):
                shutil.copytree(self._template_path(options), temporary, symlinks=False)
            else:
                temporary.mkdir(mode=0o700)
                atomic_text(temporary / "master.tag", "-- Master.tag File, Rev:1.0\nadeL2View.file\n")
                atomic_text(temporary / "adeL2View.file", "")
                if (options or {}).get("maestro_netlist_format") == "hspice":
                    from .maestro_hspice import empty_sections

                    atomic_text(temporary / "setting", setting_text(empty_sections()))
                else:
                    atomic_text(temporary / "setting", empty_mde_setting())
            _safe_files(temporary)
            os.replace(temporary, state_dir)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return state_dir

    def _require_unretained(self, target):
        identity = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        if any(record["api"] is self.api and record["target"] == identity and record["session"].isValid() for record in _RETAINED_SESSIONS.values()):
            raise OperationError("Use the retained session id for reading, or close it before changing its saved setup", stage="maestro", code="native_session_in_use")

    def _open(self, target: Mapping[str, Any], *, ensure: bool = False, options: Mapping[str, Any] | None = None):
        self._require_unretained(target)
        if ensure:
            self._materialize_state(target, options)
        session = self.api.MdeSession.open(target["library"], target["cell"], target.get("view", "state"))
        if session is None:
            error = _member(self.api.MdeSession, "staticLastError", "")
            raise OperationError("Native MDE state could not be opened", stage="maestro", code="native_state_open_failed", details={"error": str(error or "")})
        if not bool(_member(session, "isValid", False)):
            error = _member(session, "lastError", "")
            try:
                session.close()
            except Exception:
                pass
            raise OperationError("Native MDE state is invalid", stage="maestro", code="native_state_invalid", details={"error": str(error or "")})
        return session

    def _native_output(self, output: Any) -> dict[str, Any]:
        return {
            "name": str(_member(output, "name_p", _member(output, "name", "")) or ""),
            "expr": str(_member(output, "expr_p", _member(output, "expr", "")) or ""),
            "expr_type": str(_member(output, "exprType_p", _member(output, "exprType", "")) or ""),
            "eval_type": str(_member(output, "evalType", "") or ""),
            "spec": str(_member(output, "spec", "") or ""),
            "native_repr": str(output),
        }

    @staticmethod
    def _close(session):
        if not session.close():
            raise OperationError("Native MDE session could not be closed", stage="maestro", code="native_session_close_failed")
        if session.isValid():
            raise OperationError("Closed native MDE session remains valid", stage="maestro", code="native_session_close_mismatch")
        for identity, record in list(_RETAINED_SESSIONS.items()):
            if not record["session"].isValid():
                del _RETAINED_SESSIONS[identity]
        ephemeral = _EPHEMERAL_SESSIONS.get(id(session))
        if ephemeral:
            directory = ephemeral["directory"]
            actual = {path.name for path in directory.iterdir()}
            if directory.is_symlink() or actual != set(ephemeral["files"]) or any((directory / name).is_symlink() or not (directory / name).is_file() or file_digest(directory / name) != fingerprint for name, fingerprint in ephemeral["files"].items()):
                raise OperationError("Private native session files changed; cleanup refused", stage="maestro", code="recovery_required", details={"directory": str(directory)})
            for name in ephemeral["files"]:
                (directory / name).unlink()
            directory.rmdir()
            del _EPHEMERAL_SESSIONS[id(session)]

    def _native_corner(self, corner: Any) -> dict[str, Any]:
        return {
            "name": str(_member(corner, "getCornerName", "") or ""),
            "temperature": _value(_member(corner, "getTemperature", None)),
            "enabled": bool(_member(corner, "isEnabled", False)),
            "models": [{"path": str(item.getModelName()), "section": str(item.getModelSection())} for item in corner.getModels()],
            "parameters": [{"name": str(item.getParameterName()), "value": str(item.getParameterValue()), "enabled": bool(item.isEnable())} for item in corner.getParameters()],
            "native_repr": str(corner),
        }

    def _snapshot_setting(self, setting, session, state_dir, options=None):
        saved_path = state_dir / "setting"
        saved_hash = file_digest(saved_path)
        original_path = str(setting.getSettingFilePath())
        directory = Path((options or {}).get("maestro_artifact_dir", self.workspace / ".aetherpilot" / "maestro-readbacks")) / ("live-" + uuid.uuid4().hex)
        directory.mkdir(parents=True, mode=0o700)
        if not setting.saveSetting(str(directory)):
            raise OperationError("Native in-memory setting could not be captured", stage="maestro", code="native_setting_save_failed")
        if str(setting.getSettingFilePath()) != original_path or not session.isValid() or file_digest(saved_path) != saved_hash:
            raise OperationError("Native readback changed or raced with the live setting", stage="maestro", code="native_readback_changed_state", details={"snapshot": str(directory / "setting")})
        sections = read_mde_setting(directory / "setting")
        saved = read_mde_setting(saved_path)
        differences = [{"section": section, "field": field} for section in sorted(set(saved) | set(sections))
                       for field in sorted(set(saved.get(section, {})) | set(sections.get(section, {})))
                       if saved.get(section, {}).get(field) != sections.get(section, {}).get(field)]
        return {"sections": sections, "path": str(directory / "setting"), "sha256": file_digest(directory / "setting"),
                "saved_sha256": saved_hash, "differences": differences}

    def read_native(self, target: Mapping[str, Any], *, session=None, options=None) -> dict[str, Any]:
        state_dir = self._state_dir(target)
        owned = session is None
        session = session or self._open(target)
        try:
            setting = _member(session, "getSetting")
            if setting is None:
                raise OperationError("Native MDE setting is unavailable", stage="maestro", code="native_setting_missing")
            snapshot = self._snapshot_setting(setting, session, state_dir, options) if not owned else None
            outputs = []
            for output in _member(setting, "getAllSpeOutputs", ()) or ():
                outputs.append(self._native_output(output))
            corners = []
            for corner in _member(setting, "getAllSpeCorners", ()) or ():
                corners.append(self._native_corner(corner))
            native = {
                "valid": bool(_member(session, "isValid", False)),
                "library": _value(_member(session, "libName", target["library"])),
                "cell": _value(_member(session, "cellName", target["cell"])),
                "view": _value(_member(session, "viewName", target.get("view", "state"))),
                "session_id": _value(_member(session, "sessionId", _member(session, "id", None))),
                "session_object_id": _value(_member(session, "id", None)),
                "project_directory": _value(_member(setting, "getProjectDirectory", "")),
                "setting_file": _value(_member(setting, "getSettingFilePath", str(state_dir / "setting"))),
                "netlist_format": _value(_member(setting, "getNetlistFormat", None)),
                "design": _value(_member(setting, "getDesignTopCellView", ())),
                "outputs": outputs,
                "corners": corners,
                "enabled_corners": _value(_member(setting, "getEnabledCornerList", ()), []),
                "enabled_sweeps": _value(_member(setting, "getEnabledSweepList", ()), []),
                "model_files": _value(_member(setting, "getModelFiles", ()), []),
                "model_files_for_corners": _value(_member(setting, "getModelFilesForCorners", ()), []),
                "custom_keys": _value(_member(setting, "getAllCustomKeys", ()), []),
                "result_names": _value(_member(session, "getResultNames", ()), []),
                "setting_sha256": hashlib.sha256((state_dir / "setting").read_bytes()).hexdigest() if (state_dir / "setting").is_file() else None,
                "setting_sections": snapshot["sections"] if snapshot else read_mde_setting(state_dir / "setting"),
            }
            if snapshot:
                native.update(setting_source="in_memory", setting_snapshot=snapshot["path"],
                              live_setting_sha256=snapshot["sha256"], saved_setting_matches=not snapshot["differences"],
                              unsaved_fields=snapshot["differences"])
            else:
                native["setting_source"] = "saved"
            native["analyses"] = native_analysis_entries(native["setting_sections"])
            native["capabilities"] = self.capability_report()
            if native["netlist_format"] == self.api.MDE_NETLIST_FORMAT_HSPICE:
                from .maestro_hspice import native_fields

                native.update(native_fields(native["setting_sections"]))
            return native
        finally:
            if owned:
                self._close(session)

    def open_session(self, target: Mapping[str, Any], *, options: Mapping[str, Any] | None = None, persistent=False) -> dict[str, Any]:
        """Verify one native MDE lifecycle without retaining a worker-local id."""
        target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        session = self._open(target, options=options)
        session_id = _value(_member(session, "sessionId", _member(session, "id", None)), "")
        try:
            readback = self.read(target, options=options, session=session)
        except Exception:
            self._close(session)
            raise
        if persistent:
            _RETAINED_SESSIONS[(id(self.api), str(session_id))] = {"api": self.api, "target": target, "session": session}
        else:
            self._close(session)
        readback["action"] = "open_session"
        readback["session"] = {"id": session_id, "opened": True, "closed": not persistent, "persistent": persistent,
                                "scope": "explicit_in_process_attachment" if persistent else "local_operation_session"}
        return readback

    def read_session(self, target, session_id, *, options=None):
        finder = getattr(self.api.MdeSession, "find", None)
        session = finder(int(session_id)) if callable(finder) else None
        if session is None or not session.isValid():
            raise OperationError("Explicit native MDE session was not found", stage="maestro", code="native_session_not_found")
        identity = {"library": session.libName(), "cell": session.cellName(), "view": session.viewName()}
        if identity != {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}:
            raise OperationError("Session identity differs from the requested state", stage="maestro", code="native_session_target_mismatch")
        return self.read(target, options=options, session=session)

    def close_session(self, target: Mapping[str, Any], *, session_id: str = "",
                      options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Close a native local session, never removing a native lock by force."""
        target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        session = None
        requested_id = str(session_id or "")
        if requested_id:
            finder = getattr(self.api.MdeSession, "find", None)
            if callable(finder):
                try:
                    session = finder(int(requested_id))
                except (TypeError, ValueError, OverflowError):
                    session = None
            if session is None:
                raise OperationError("Requested native MDE session is not open in this worker", stage="maestro",
                                     code="native_session_not_found", details={"session_id": requested_id})
            identity = {"library": session.libName(), "cell": session.cellName(), "view": session.viewName()}
            if identity != target:
                raise OperationError("Session identity differs from the explicit target; it was not closed", stage="maestro", code="native_session_target_mismatch")
        else:
            self.read(target, options=options)
            session = self._open(target, options=options)
        actual_id = _value(_member(session, "sessionId", _member(session, "id", requested_id)), requested_id)
        valid_before = bool(_member(session, "isValid", False))
        self._close(session)
        valid_after = bool(_member(session, "isValid", False))
        return {"action": "close_session", "target": target,
                "session": {"id": actual_id, "opened_by_operation": not bool(requested_id),
                             "valid_before_close": valid_before, "closed": True,
                             "valid_after_close": valid_after, "persistent": False,
                             "scope": "local_operation_session"}}

    def find_open_session(self, *, session_id: str = "", target=None) -> dict[str, Any]:
        """Read an explicit native session id without affecting any session."""
        if target is not None:
            target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        requested_id = str(session_id or "")
        found = None
        if requested_id:
            finder = getattr(self.api.MdeSession, "find", None)
            if callable(finder):
                try:
                    found = finder(int(requested_id))
                except (TypeError, ValueError, OverflowError):
                    found = None
            else:
                raise PreflightError("Native session lookup API is unavailable", stage="maestro", code="native_api_missing")
        valid = bool(found is not None and _member(found, "isValid", False))
        retained = [record for (api_identity, identity), record in _RETAINED_SESSIONS.items()
                    if api_identity == id(self.api) and record["session"].isValid() and (target is None or record["target"] == target)]
        if not requested_id and retained:
            if len(retained) != 1:
                raise OperationError("Retained native session lookup is ambiguous", stage="maestro", code="ambiguous_session")
            found = retained[0]["session"]
            requested_id, valid = str(_member(found, "sessionId", _member(found, "id", None))), True
        if valid and target is not None and {"library": found.libName(), "cell": found.cellName(), "view": found.viewName()} != target:
            found, valid = None, False
        return {"action": "find_open_session", "session_id": requested_id or None,
                "found": valid, "session": {"id": requested_id, "valid": valid} if valid else None,
                "scope": "current_process", "persistent": valid and (id(self.api), requested_id) in _RETAINED_SESSIONS,
                "native_lookup_performed": bool(requested_id)}

    def _set_native_design(self, setting: Any, request: Mapping[str, Any]) -> None:
        if not bool(setting.setDesign(request["design_library"], request["design_cell"], request.get("design_view", "schematic"))):
            raise OperationError("Native MDE design update failed", stage="maestro", code="native_setting_write_failed")

    def _set_native_output(self, setting: Any, request: Mapping[str, Any]) -> None:
        constructor = getattr(self.api, "MdeSpeOutput", None)
        if not callable(constructor):
            raise PreflightError("Native MDE output API is unavailable", stage="maestro", code="native_api_missing")
        output_type = request.get("_native_output_type", request["output_type"])
        output = constructor(request["name"], self._native_output_expression(request),
                             output_type, request.get("eval_type", "Point"),
                             bool(request.get("save", True)), bool(request.get("plot", True)))
        if setting.outputExists(request["name"]):
            setting.removeOutput(request["name"])
        if not bool(setting.addSpeOutput(output)):
            raise OperationError("Native MDE output update failed", stage="maestro", code="native_setting_write_failed")

    @staticmethod
    def _native_output_expression(request: Mapping[str, Any]) -> str:
        expression = request.get("expr", "") or request.get("signal_name", "")
        if request.get("_native_output_type") != "Expression" or request.get("output_type") != "Signal":
            return expression
        suffix = {"ac": "F", "dc": "S", "tran": "T"}.get(request.get("_native_output_analysis"), "T")
        match = re.fullmatch(r"[vV]\(\s*([^()]+)\s*\)", expression)
        if match:
            return f"V{suffix}({match.group(1).strip()})"
        match = re.fullmatch(r"[iI]\(\s*([^()]+)\s*\)", expression)
        if match:
            return f"I{suffix}({match.group(1).strip()})"
        return expression

    @staticmethod
    def _native_output_request(output, spec):
        request = copy.deepcopy(output)
        if request["output_type"] == "Signal" and spec_text(spec):
            request["_native_output_type"] = "Expression"
            request["_native_output_analysis"] = spec.get("analysis", "tran")
        return request

    def _set_native_specs(self, setting: Any, state: Mapping[str, Any], staging: Path) -> None:
        """Persist the validated Harness spec model in native MDE's output schema."""
        native_specs = dict(state.get("specs", {}))
        if not native_specs:
            return
        output_dir = staging / ".output-specs"
        output_dir.mkdir(mode=0o700)
        if not setting.saveOutputsToDir(str(output_dir)):
            raise OperationError("Native output specification export failed", stage="maestro", code="native_setting_write_failed", details={"error": str(setting.lastError())})
        files = sorted(output_dir.glob("Outputs*.csv"))
        if len(files) != 1:
            raise OperationError("Native output specification export is ambiguous", stage="maestro", code="native_setting_write_failed")
        source = safe_result_path(files[0])
        try:
            with source.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        except (OSError, UnicodeError, csv.Error) as exc:
            raise OperationError("Native output specification export is unreadable", stage="maestro", code="native_setting_write_failed") from exc
        if not rows or len(rows[0]) < 8:
            raise OperationError("Native output specification export is malformed", stage="maestro", code="native_setting_write_failed")
        try:
            name_column = rows[0].index("Name")
            spec_column = rows[0].index("Spec")
        except ValueError as exc:
            raise OperationError("Native output specification export has no spec column", stage="maestro", code="native_setting_write_failed") from exc
        seen = set()
        signal_specs = set()
        for row in rows[1:]:
            if len(row) != len(rows[0]):
                raise OperationError("Native output specification export has an invalid row", stage="maestro", code="native_setting_write_failed")
            name = row[name_column]
            if name in native_specs:
                row[spec_column] = spec_text(native_specs[name])
                seen.add(name)
                if state.get("outputs", {}).get(name, {}).get("output_type") == "Signal":
                    signal_specs.add(name)
        missing = set(native_specs) - seen
        if missing:
            raise OperationError("Native output specification target is missing", stage="maestro", code="output_missing", details={"outputs": sorted(missing)})
        edited = staging / ".output-specs.csv"
        with edited.open("w", newline="", encoding="utf-8") as handle:
            csv.writer(handle, quoting=csv.QUOTE_ALL, lineterminator="\n").writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        mode = getattr(self.api, "MDE_LOAD_OUTPUT_OVERWRITE", None)
        if mode is None or not setting.loadOutputs(str(edited), mode):
            raise OperationError("Native output specifications could not be loaded", stage="maestro", code="native_setting_load_failed", details={"error": str(setting.lastError())})
        actual = {str(item.name_p): str(item.spec() or "") for item in setting.getAllSpeOutputs()}
        output_types = state.get("outputs", {})
        for name, spec in native_specs.items():
            expected = spec_text(spec)
            if name not in signal_specs and actual.get(name, "") != expected:
                raise OperationError("Native output specification readback differs", stage="maestro", code="native_setting_write_failed", details={"output": name, "expected": expected, "actual": actual.get(name, "")})

    def _set_native_analysis(self, setting: Any, request: Mapping[str, Any]) -> None:
        analysis = request["analysis"].lower()
        classes = {"tran": ("MdeTranAnalysis", "setTranAnalysis"), "dc": ("MdeDcAnalysis", "setDcAnalysis"), "ac": ("MdeAcAnalysis", "setAcAnalysis"), "noise": ("MdeNoiseAnalysis", "setNoiseAnalysis"), "stb": ("MdeStbAnalysis", "setStbAnalysis")}
        class_name, method_name = classes.get(analysis, (None, None))
        capability = self.capability_report().get("analyses", {}).get(analysis, {})
        if class_name is None or not capability.get("native_adapter") or not capability.get("native_class") or not capability.get("writer") or not capability.get("enable") or not capability.get("disable"):
            raise OperationError("Native MDE analysis is not supported by this Aether build", stage="maestro", code="native_analysis_unsupported", details={"analysis": analysis, "capability": capability})
        value = getattr(self.api, class_name)()
        options = normalize_maestro_analysis_options(analysis, request.get("options", {}))
        if analysis == "tran":
            if set(options) - {"stop", "accuracy"} or "stop" not in options or engineering(options["stop"]) <= 0:
                raise ValidationError("Transient requires a positive stop and optional accuracy", stage="plan", code="invalid_maestro_analysis")
            value.setStop(format(engineering(options["stop"]), ".17g"))
            if "accuracy" in options:
                levels = {"conservative": "MDE_TRAN_ACC_LEVEL_CONSERVATIVE", "moderate": "MDE_TRAN_ACC_LEVEL_MODERATE", "liberal": "MDE_TRAN_ACC_LEVEL_LIBERAL"}
                if options["accuracy"] not in levels:
                    raise ValidationError("Invalid accuracy level", stage="plan", code="invalid_maestro_analysis")
                value.setAccuracyLevel(getattr(self.api, levels[options["accuracy"]]))
        else:
            allowed = {"start", "stop", "step", "points", "sweep"}
            allowed.update({"variable", "source", "parameter"} if analysis == "dc" else {
                "output_mode", "probe", "positive_output", "negative_output", "input_mode",
                "input_voltage_source", "input_current_source", "input_port_source", "noise_separation",
            } if analysis == "noise" else {"probe", "mode_type", "mode_type_enabled", "local_ground"} if analysis == "stb" else set())
            if set(options) - allowed or not {"start", "stop"} <= options.keys():
                raise ValidationError("Sweep requires start/stop and supported options", stage="plan", code="invalid_maestro_analysis")
            if engineering(options["stop"]) <= engineering(options["start"]):
                raise ValidationError("Sweep limits must increase", stage="plan", code="invalid_maestro_analysis")
            if "step" in options and "points" in options or options.get("sweep", "linear") == "decade" and "step" in options:
                raise ValidationError("Sweep resolution must be unambiguous", stage="plan", code="invalid_maestro_analysis")
            sweep_variable = self.api.MdeSweepVariable()
            if analysis == "dc":
                if bool(options.get("source")) == bool(options.get("variable")):
                    raise ValidationError("DC requires exactly one source or variable", stage="plan", code="invalid_maestro_analysis")
                if options.get("source"):
                    sweep_variable.mode = self.api.MDE_SWEEP_VARIABLE_COMPONENT_PARAMETER
                    sweep_variable.componentName = options["source"]
                    sweep_variable.componentParameterName = options.get("parameter", "dc")
                elif options.get("variable"):
                    sweep_variable.mode = self.api.MDE_SWEEP_VARIABLE_DESIGN_VARIABLE
                    sweep_variable.variableName = options["variable"]
                else:
                    raise ValidationError("DC requires a source or variable", stage="plan", code="invalid_maestro_analysis")
                value.setSaveDcOperatingPointStatus(True)
                value.setHysteresisSweep(False)
            else:
                sweep_variable.mode = self.api.MDE_SWEEP_VARIABLE_FREQUENCY
            value.setSweepVariable(sweep_variable)
            sweep_range = self.api.MdeSweepRange()
            sweep_range.mode = self.api.MDE_SWEEP_RANGE_START_STOP
            sweep_range.start, sweep_range.stop = (format(engineering(options[name]), ".17g") for name in ("start", "stop"))
            value.setSweepRange(sweep_range)
            sweep_type = self.api.MdeSweepType()
            if options.get("sweep", "linear") == "linear":
                sweep_type.mode = self.api.MDE_SWEEP_TYPE_LINEAR
                if "step" in options:
                    if engineering(options["step"]) <= 0:
                        raise ValidationError("Sweep step must be positive", stage="plan", code="invalid_maestro_analysis")
                    if (engineering(options["stop"]) - engineering(options["start"])) / engineering(options["step"]) > 100000:
                        raise ValidationError("Sweep exceeds the point budget", stage="plan", code="resource_budget_exceeded")
                    sweep_type.linearMode = self.api.MDE_SWEEP_TYPE_LINEAR_STEP_SIZE
                    sweep_type.stepSize = format(engineering(options["step"]), ".17g")
                else:
                    if type(options.get("points")) is not int or not 2 <= options["points"] <= 100001:
                        raise ValidationError("Sweep requires bounded points or a step", stage="plan", code="invalid_maestro_analysis")
                    sweep_type.linearMode = self.api.MDE_SWEEP_TYPE_LINEAR_NUM_STEP
                    sweep_type.numberOfStepsForLinear = str(options["points"] - 1)
            elif options["sweep"] == "decade":
                if engineering(options["start"]) <= 0 or type(options.get("points")) is not int or not 1 <= options["points"] <= 10000:
                    raise ValidationError("Log sweep requires positive start and bounded points", stage="plan", code="invalid_maestro_analysis")
                if math.log10(engineering(options["stop"]) / engineering(options["start"])) * options["points"] > 100000:
                    raise ValidationError("Sweep exceeds the point budget", stage="plan", code="resource_budget_exceeded")
                sweep_type.mode = self.api.MDE_SWEEP_TYPE_LOG
                sweep_type.logMode = self.api.MDE_SWEEP_TYPE_LOG_POINTS_DEC
                sweep_type.pointsPerDec = str(options["points"])
            else:
                raise ValidationError("Unsupported sweep type", stage="plan", code="invalid_maestro_analysis")
            value.setSweepType(sweep_type)
            if analysis == "noise":
                output_mode = options["output_mode"]
                if output_mode == "probe":
                    value.setOutputNoiseMode(self.api.MDE_OUTPUT_NOISE_PROBE)
                    value.setOutputProbeInstance(options["probe"])
                else:
                    value.setOutputNoiseMode(self.api.MDE_OUTPUT_NOISE_VOLTAGE)
                    value.setPositiveOutputNode(options["positive_output"])
                    value.setNegativeOutputNode(options["negative_output"])
                input_modes = {
                    "voltage": ("MDE_INPUT_NOISE_VOLTAGE", "setInputVoltageSrc", "input_voltage_source"),
                    "current": ("MDE_INPUT_NOISE_CURRENT", "setInputCurrentSrc", "input_current_source"),
                    "port": ("MDE_INPUT_NOISE_PORT", "setInputPortSrc", "input_port_source"),
                    "none": ("MDE_INPUT_NOISE_NONE", None, None),
                }
                enum_name, setter_name, source_name = input_modes[options["input_mode"]]
                value.setInputNoiseMode(getattr(self.api, enum_name))
                if setter_name:
                    getattr(value, setter_name)(options[source_name])
                value.setNoiseSeparationStatus(options["noise_separation"])
            elif analysis == "stb":
                value.setProbeInst(options["probe"])
                value.setModeTypeStatus(options["mode_type_enabled"])
                mode_types = {"common": "MDE_STB_MODETYPE_COMMON", "differential": "MDE_STB_MODETYPE_DIFFERENTIAL"}
                value.setStbModeType(getattr(self.api, mode_types[options["mode_type"]]))
                value.setLocalGroundName(options["local_ground"])
        if not bool(getattr(setting, method_name)(value)):
            raise OperationError("Native MDE analysis update failed", stage="maestro", code="native_setting_write_failed", details={"analysis": analysis, "error": str(setting.lastError())})
        enable_method = ("enable" if request.get("enable", True) else "disable") + method_name[3:]
        if not bool(getattr(setting, enable_method)()):
            raise OperationError("Native MDE analysis enable/disable failed", stage="maestro", code="native_setting_write_failed")

    def _project_native(self, session, state, state_dir):
        from .maestro_hspice import is_hspice, project_sections

        selected = state["tests"].get(state["current_test"])
        if selected is None:
            raise OperationError("A native state requires a selected test", stage="maestro", code="test_missing")
        setting = session.getSetting()
        if is_hspice(selected):
            source = {"library": session.libName(), "cell": session.cellName(), "view": session.viewName()}
            target = {**source, "view": "aetherpilot_projection_" + uuid.uuid4().hex}
            directory = self._state_dir(target)
            directory.mkdir(mode=0o700, exist_ok=False)
            atomic_text(directory / "master.tag", "-- Master.tag File, Rev:1.0\nadeL2View.file\n")
            atomic_text(directory / "adeL2View.file", "")
            atomic_text(directory / "setting", setting_text(project_sections(state)))
            atomic_json(directory / "aetherpilot-ephemeral.json", {"target": target, "source": source, "purpose": "native_hspice_projection"})
            files = {path.name: file_digest(path) for path in directory.iterdir()}
            projected = None
            try:
                projected = self._open(target)
                _EPHEMERAL_SESSIONS[id(projected)] = {"session": projected, "directory": directory, "files": files}
                if not projected.getSetting().saveSetting(str(state_dir)):
                    raise OperationError("Native HSPICE projection could not be saved", stage="maestro", code="native_setting_write_failed")
                from .maestro_hspice import verify_sections
                from .maestro_migration import preserve_native_notes

                preserve_native_notes(state_dir / "setting", state)
                verify_sections(state, {"netlist_format": projected.getSetting().getNetlistFormat(), "setting_sections": read_mde_setting(state_dir / "setting")})
                self._close(session)
            except Exception:
                if projected is not None and projected.isValid():
                    self._close(projected)
                raise
            return projected
        if setting.getNetlistFormat() != self.api.MDE_NETLIST_FORMAT_SPECTRE:
            raise OperationError("This writer requires a native MDE L2 state", stage="maestro", code="native_state_format_mismatch")
        staging = state_dir / (".projection-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        atomic_text(staging / "setting", empty_mde_setting())
        if not setting.loadSetting(str(staging / "setting")):
            raise OperationError("Native MDE clean configuration could not be loaded", stage="maestro", code="native_setting_load_failed")
        design = selected["design"]
        self._set_native_design(setting, {"design_library": design["library"], "design_cell": design["cell"], "design_view": design["view"]})
        for name, analysis in selected["analyses"].items():
            self._set_native_analysis(setting, {"analysis": name, "options": analysis["options"], "enable": analysis["enabled"]})
        for output in selected["outputs"].values():
            native_output = self._native_output_request(output, selected["specs"].get(output["name"], {}))
            self._set_native_output(setting, native_output)
        policy = self.api.MdeLocalJobPolicy()
        policy.setMaxJobNum(1)
        if not setting.setJobPolicy(policy):
            raise OperationError("Native local job policy could not be set", stage="maestro", code="native_setting_write_failed")
        variables = {**state["parameters"], **state["variables"], **selected["variables"]}
        all_variables = set(variables)
        for definition in state["corners"].values():
            all_variables.update(definition["variables"])
        for name in sorted(all_variables):
            setting.addParameter(name)
            setting.addParameterForCorners(name)
        model_files = sorted({item["path"] for definition in state["corners"].values() for item in definition["models"]})
        for model_file in model_files:
            if not Path(model_file).is_file():
                raise ValidationError("Corner model file does not exist", stage="maestro", code="model_missing")
            safe_input_path(model_file)
            setting.addModelFile(model_file)
            setting.addModelFileForCorners(model_file)
        for name, definition in state["corners"].items():
            if not setting.cornerExists(name):
                setting.createCorner(name)
            corner = setting.getCorner(name)
            if corner is None or not corner.isValid():
                raise OperationError("Native corner creation failed", stage="maestro", code="native_setting_write_failed", details={"corner": name})
            corner.changeTemperature(str(definition["temperature"]))
            (corner.enable if definition["enabled"] else corner.disable)()
            values = {**{key: item["value"] for key, item in variables.items()}, **definition["variables"]}
            for parameter, value in values.items():
                corner.changeParameterValue(parameter, value)
                corner.enableParameter(parameter)
            for model in definition["models"]:
                corner.addModelSection(model["path"], model["section"])
        environment = selected["environment_options"]
        env_setters = {"netlist_views": "setNetlistViewList", "stop_views": "setStopViewList", "project_directory": "setProjectDirectory"}
        if set(environment) - env_setters.keys():
            raise ValidationError("Unsupported native environment option", stage="maestro", code="unsupported_environment_option")
        for name, value in environment.items():
            if not getattr(setting, env_setters[name])(value):
                raise OperationError("Native environment option was rejected", stage="maestro", code="native_setting_write_failed", details={"option": name})
        if not setting.saveSetting(str(staging)):
            raise OperationError("Native configuration staging failed", stage="maestro", code="native_setting_save_failed")
        simulator_options = selected["simulator_options"]
        allowed_options = {"absTol", "absv", "gmin", "relTol", "tnom", "scale", "scalem", "maxnotes", "maxwarns", "pivrel"}
        if set(simulator_options) - allowed_options:
            raise ValidationError("Unsupported native simulator option", stage="maestro", code="unsupported_simulator_option")
        if simulator_options:
            sections = read_mde_setting(staging / "setting")
            sections.setdefault("SimulatorOptions", {}).update({key: str(value) for key, value in simulator_options.items()})
            atomic_text(staging / "setting", setting_text(sections))
            if not setting.loadSetting(str(staging / "setting")):
                raise OperationError("Native simulator options could not be loaded", stage="maestro", code="native_setting_load_failed")
        self._set_native_specs(setting, selected, staging)
        if not setting.saveSetting(str(state_dir)):
            raise OperationError("Native configuration could not be saved", stage="maestro", code="native_setting_save_failed")
        from .maestro_migration import preserve_native_notes

        preserve_native_notes(state_dir / "setting", state)
        shutil.rmtree(staging)
        return session

    def _verify_projection(self, state, native):
        from .maestro_hspice import is_hspice, verify_legacy_semantics, verify_sections

        selected = state["tests"][state["current_test"]]
        if is_hspice(selected):
            if state.get("metadata", {}).get("native_hspice_legacy"):
                verify_legacy_semantics(state, native)
            else:
                verify_sections(state, native)
            return
        expected_design = [selected["design"][key] for key in ("library", "cell", "view")]
        errors = []
        for section, expected in state.get("metadata", {}).get("native_notes", {}).items():
            if native["setting_sections"].get(section) != expected:
                errors.append("native_notes:" + section)
        def equal_number(actual, expected):
            try:
                return math.isclose(engineering(actual), engineering(expected), rel_tol=1e-13, abs_tol=0)
            except (ValueError, TypeError, ValidationError):
                return False
        if native["design"] != expected_design:
            errors.append("design")
        actual_outputs = {item["name"]: item for item in native["outputs"]}
        if set(actual_outputs) != set(selected["outputs"]):
            errors.append("output_names")
        for name, expected in selected["outputs"].items():
            actual = actual_outputs.get(name, {})
            expected_native_request = self._native_output_request(expected, selected["specs"].get(name, {}))
            expected_native_type = expected_native_request.get("_native_output_type", expected["output_type"])
            if actual.get("expr") != self._native_output_expression(expected_native_request) or actual.get("expr_type") != expected_native_type or actual.get("eval_type") != expected["eval_type"]:
                errors.append("output:" + name)
            expected_spec = spec_text(selected["specs"].get(name)) if expected_native_type == "Expression" else ""
            if actual.get("spec", "") != expected_spec:
                errors.append("output_spec:" + name)
        output_sections = native["setting_sections"].get("Outputs", {})
        saved_outputs = {}
        for key, value in output_sections.items():
            if key.endswith("\\outputExprAlias"):
                saved_outputs[value] = key.split("\\")[0] + "\\"
        for name, expected in selected["outputs"].items():
            prefix = saved_outputs.get(name, "missing\\")
            expected_native_request = self._native_output_request(expected, selected["specs"].get(name, {}))
            expected_native_type = expected_native_request.get("_native_output_type", expected["output_type"])
            for field in ("save", "plot"):
                if output_sections.get(prefix + field) != str(expected[field]).lower():
                    errors.append("output_" + field + ":" + name)
            expected_spec = spec_text(selected["specs"].get(name)) if (expected["output_type"] != "Signal" or selected["specs"].get(name)) else ""
            if output_sections.get(prefix + "outputSpecStr", "") != expected_spec:
                errors.append("output_spec_file:" + name)
            if output_sections.get(prefix + "outputNode", "") != self._native_output_expression(expected_native_request):
                errors.append("output_expression_file:" + name)
            expected_corner_names = [corner for corner, definition in state["corners"].items() if definition["enabled"]]
            actual_corner_specs = []
            size = int(output_sections.get(prefix + "outputCornerSpec\\size", "0") or "0")
            for index in range(1, size + 1):
                corner_prefix = prefix + f"outputCornerSpec\\{index}\\"
                actual_corner_specs.append((output_sections.get(corner_prefix + "cornerName", ""), output_sections.get(corner_prefix + "cornerSpec", ""), output_sections.get(corner_prefix + "isEnabled", "")))
            expected_corner_specs = [(corner, expected_spec, "1") for corner in expected_corner_names] if expected_spec else []
            if actual_corner_specs != expected_corner_specs:
                errors.append("output_corner_spec:" + name)
        if set(native["analyses"]) != set(selected["analyses"]):
            errors.append("analysis_names")
        for name, expected in selected["analyses"].items():
            actual = native["analyses"].get(name, {})
            options = normalize_maestro_analysis_options(name, expected["options"])
            if actual.get("IsEnabled") not in ({"1", "true"} if expected["enabled"] else {"0", "false"}):
                errors.append("analysis_enabled:" + name)
            if name == "tran":
                if not equal_number(actual.get("Stop"), options["stop"]):
                    errors.append("analysis_stop:" + name)
                expected_level = {"conservative": "0", "moderate": "1", "liberal": "2"}.get(options.get("accuracy", "moderate"))
                if actual.get("AccuracyLevel") != expected_level:
                    errors.append("analysis_accuracy:" + name)
            elif name in {"dc", "ac", "noise", "stb"}:
                for field in ("start", "stop"):
                    if not equal_number(actual.get("SweepRange\\" + field.title()), options[field]):
                        errors.append("analysis_" + field + ":" + name)
                resolution = "PointsPerDec" if options.get("sweep") == "decade" else "StepSize" if "step" in options else "NumberOfStepsForStepSize"
                expected_resolution = options.get("step", options.get("points"))
                if resolution == "NumberOfStepsForStepSize":
                    expected_resolution -= 1
                if not equal_number(actual.get("SweepType\\" + resolution), expected_resolution):
                    errors.append("analysis_resolution:" + name)
                if actual.get("SweepType\\SweepType") != ("2" if options.get("sweep") == "decade" else "1"):
                    errors.append("analysis_sweep_type:" + name)
                if name == "dc":
                    key = "ComponentName" if options.get("source") else "VariableName"
                    if actual.get("SweepVariable\\" + key) != options.get("source", options.get("variable")):
                        errors.append("analysis_variable:" + name)
                    if actual.get("Hysteresis") != "false":
                        errors.append("analysis_hysteresis:" + name)
                elif name == "noise":
                    output_mode = options["output_mode"]
                    expected_output_mode = {"probe": "0", "voltage": "1"}[output_mode]
                    if actual.get("OutputNoise\\OutputNoise") != expected_output_mode:
                        errors.append("analysis_noise_output_mode:" + name)
                    if output_mode == "probe":
                        if actual.get("OutputNoise\\OutputProbeInstance") != options["probe"]:
                            errors.append("analysis_noise_probe:" + name)
                    else:
                        if actual.get("OutputNoise\\PositiveOutputNode") != options["positive_output"]:
                            errors.append("analysis_noise_positive_output:" + name)
                        if actual.get("OutputNoise\\NegativeOutputNode") != options["negative_output"]:
                            errors.append("analysis_noise_negative_output:" + name)
                    input_mode = options["input_mode"]
                    expected_input_mode = {"voltage": "0", "current": "1", "port": "2", "none": "3"}[input_mode]
                    if actual.get("InputNoise\\InputNoise") != expected_input_mode:
                        errors.append("analysis_noise_input_mode:" + name)
                    input_source_fields = {
                        "voltage": "InputVoltageSource",
                        "current": "InputCurrentSource",
                        "port": "InputPortSource",
                    }
                    input_source_names = {
                        "voltage": "input_voltage_source",
                        "current": "input_current_source",
                        "port": "input_port_source",
                    }
                    if input_mode != "none" and actual.get("InputNoise\\" + input_source_fields[input_mode]) != options[input_source_names[input_mode]]:
                        errors.append("analysis_noise_input_source:" + name)
                    if actual.get("NoiseSeparation") != str(options["noise_separation"]).lower():
                        errors.append("analysis_noise_separation:" + name)
                elif name == "stb":
                    if actual.get("ProbeInstance_Terminal") != options["probe"]:
                        errors.append("analysis_stb_probe:" + name)
                    if actual.get("ModeType\\ModeType") != {"common": "0", "differential": "1"}[options["mode_type"]]:
                        errors.append("analysis_stb_mode_type:" + name)
                    if actual.get("ModeType\\ModeTypeEnable") != ("1" if options["mode_type_enabled"] else "0"):
                        errors.append("analysis_stb_mode_type_enabled:" + name)
                    actual_local_ground = actual.get("LocalGroundName", "")
                    if actual_local_ground != options["local_ground"]:
                        errors.append("analysis_stb_local_ground:" + name)
        actual_corners = {item["name"]: item for item in native["corners"]}
        if set(actual_corners) != set(state["corners"]):
            errors.append("corner_names")
        variables = {**state["parameters"], **state["variables"], **selected["variables"]}
        for name, expected in state["corners"].items():
            actual = actual_corners.get(name, {})
            if actual.get("enabled") != expected["enabled"] or not equal_number(actual.get("temperature"), expected["temperature"]):
                errors.append("corner:" + name)
            values = {**{key: item["value"] for key, item in variables.items()}, **expected["variables"]}
            actual_values = {item["name"]: item["value"] for item in actual.get("parameters", []) if item["enabled"]}
            for parameter, expected_value in values.items():
                if actual_values.get(parameter) != expected_value:
                    errors.append("parameter:" + name + ":" + parameter)
            expected_models = {(item["path"], item["section"]) for item in expected["models"]}
            if {(item["path"], item["section"]) for item in actual.get("models", [])} != expected_models:
                errors.append("models:" + name)
        for key, value in selected["simulator_options"].items():
            if native["setting_sections"].get("SimulatorOptions", {}).get(key) != str(value):
                errors.append("simulator_option:" + key)
        if errors:
            raise ValidationError("Reopened native MDE configuration differs from the request", stage="maestro", code="native_state_mismatch", details={"fields": errors})

    def apply(self, target: Mapping[str, Any], request: Mapping[str, Any], *, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from .library_safety import writable_library_path
        from .transactions import writable_target

        target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        self._require_unretained(target)
        if request["action"] not in {"get_var", "get_parameter"}:
            writable_target(target)
            writable_library_path(target["library"], self._library_path(target["library"]), self.workspace)
        store = MaestroStore(self._library_path(target["library"]), target["library"], target["cell"], target["view"])
        if (store.directory / ".aetherpilot-transaction.json").exists():
            raise OperationError("Previous MDE publication needs recovery", stage="maestro", code="recovery_required")
        old_state = store.load()
        new_state, logical = apply_maestro_action(old_state, {**request, "view": target["view"]})
        if request["action"] in {"get_var", "get_parameter"}:
            readback = self.read(target, options=options)
            if request["action"] == "get_parameter":
                value = old_state["parameters"][request["name"]]
            else:
                variables = old_state["tests"][request["test"]]["variables"] if request.get("test") else old_state["variables"]
                value = variables[request["name"]]
            readback.update({"action": request["action"], "name": request["name"], "value": copy.deepcopy(value), "test": request.get("test", "")})
            return readback
        existed = store.directory.exists()
        if existed and request["action"] == "restore_setup":
            raise OperationError("Setup replay requires a new target view", stage="maestro", code="target_exists")
        if existed and not store.path.is_file():
            raise OperationError("Existing unmanaged MDE state cannot be overwritten", stage="maestro", code="unmanaged_state")
        if not existed and request["action"] not in {"create_test", "restore_setup"}:
            raise OperationError("Create a test before modifying an MDE state", stage="maestro", code="state_missing")
        from .maestro_hspice import is_hspice, project_sections

        if is_hspice(new_state["tests"][new_state["current_test"]]):
            project_sections(new_state)
            options = {**(options or {}), "maestro_netlist_format": "hspice"}
        state_dir = self._materialize_state(target, options)
        baseline = safe_result_path(state_dir / "setting").read_text(encoding="utf-8")
        old_logical = store.path.read_text(encoding="utf-8") if store.path.exists() else None
        expected_baseline = old_state["metadata"].get("native_setting_sha256")
        if expected_baseline and expected_baseline != file_digest(state_dir / "setting"):
            raise OperationError("Native MDE state changed outside this writer", stage="maestro", code="baseline_changed")
        transaction = state_dir / ".aetherpilot-transaction.json"
        if transaction.exists():
            raise OperationError("Previous MDE publication needs recovery", stage="maestro", code="recovery_required")
        backup = state_dir / (".setting-backup-" + uuid.uuid4().hex)
        atomic_text(backup, baseline)
        logical_backup = state_dir / (backup.name + ".json")
        if old_logical is not None:
            atomic_text(logical_backup, old_logical)
        journal = {"backup": backup.name, "native_sha256": file_digest(backup), "state_sha256": state_digest(old_state),
                   "logical_backup": logical_backup.name if old_logical is not None else None,
                   "logical_sha256": file_digest(logical_backup) if old_logical is not None else None,
                   "created_state": not existed, "target": target}
        atomic_json(transaction, journal)
        session = None
        publication = None
        try:
            session = self._open(target, ensure=False, options=options)
            publication = state_dir / (".publication-" + uuid.uuid4().hex)
            publication.mkdir(mode=0o700)
            session = self._project_native(session, new_state, publication) or session
            self._close(session)
            session = None
            candidate_setting = safe_result_path(publication / "setting", max_bytes=8 * 1024 ** 2)
            journal["next_native_sha256"] = file_digest(candidate_setting)
            new_state["metadata"]["native_setting_sha256"] = journal["next_native_sha256"]
            journal["next_state_sha256"] = state_digest(new_state)
            atomic_json(transaction, journal)
            if file_digest(state_dir / "setting") != journal["native_sha256"] or state_digest(store.load()) != journal["state_sha256"]:
                raise OperationError("MDE source changed while staging publication", stage="maestro", code="baseline_changed")
            atomic_text(state_dir / "setting", candidate_setting.read_text(encoding="utf-8"))
            native = self.read_native(target)
            self._verify_projection(new_state, native)
            new_state["metadata"]["native_setting_sha256"] = native["setting_sha256"]
            store.save(new_state)
            transaction.unlink()
        except Exception as original:
            try:
                if session is not None and session.isValid():
                    self._close(session)
                    session = None
                self.recover(target)
            except Exception as recovery_error:
                raise OperationError("MDE rollback requires explicit recovery", stage="maestro", code="recovery_required",
                                     details={"original": type(original).__name__, "rollback": type(recovery_error).__name__, "journal": str(transaction)}) from original
            raise
        finally:
            if session is not None and session.isValid():
                self._close(session)
            if publication is not None and publication.exists():
                shutil.rmtree(publication)
        readback = maestro_readback(new_state, action=request["action"], native=native)
        readback["native"]["logical_state_sha256"] = state_digest(new_state)
        return readback

    def read(self, target: Mapping[str, Any], *, options: Mapping[str, Any] | None = None, session=None) -> dict[str, Any]:
        target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        if (self._state_dir(target) / ".aetherpilot-transaction.json").exists():
            raise OperationError("MDE state needs transaction recovery", stage="maestro", code="recovery_required")
        if not (self._state_dir(target) / "setting").is_file():
            raise OperationError("Native MDE state does not exist", stage="maestro", code="state_missing")
        store = MaestroStore(self._library_path(target["library"]), target["library"], target["cell"], target["view"])
        state = store.load()
        native = self.read_native(target, session=session, options=options)
        if native.get("saved_setting_matches") is False:
            raise OperationError("The explicit session has unsaved settings; saved logical state cannot describe them", stage="maestro", code="unsaved_native_setting",
                                 details={"snapshot": native["setting_snapshot"], "fields": native["unsaved_fields"][:100], "field_count": len(native["unsaved_fields"]), "session_kept_open": True})
        if store.path.is_file():
            if state["metadata"].get("native_setting_sha256") != native["setting_sha256"]:
                raise OperationError("Native MDE state changed outside the harness", stage="maestro", code="baseline_changed")
            self._verify_projection(state, native)
        readback = maestro_readback(state, action="read_state", native=native)
        readback["managed_state"] = store.path.is_file()
        return readback

    def migrate_setup(self, source_target, target, *, test="imported", options=None):
        from .library_safety import writable_library_path
        from .maestro_exports import portable_setup
        from .maestro_hspice import verify_legacy_semantics
        from .maestro_migration import compare_native_settings, native_setup, publish_directory, resolve_hspice_model_paths
        from .maestro_hspice import opaque_sections
        from .transactions import writable_target

        target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        writable_target(target)
        writable_library_path(target["library"], self._library_path(target["library"]), self.workspace)
        destination = self._state_dir(target)
        if destination.exists() or destination.is_symlink():
            raise OperationError("Migration requires a new target state view", stage="maestro", code="target_exists")
        source_path = self._state_dir(source_target) / "setting"
        read_mde_setting(source_path)
        source_store = MaestroStore(self._library_path(source_target["library"]), source_target["library"], source_target["cell"], source_target["view"])
        source_bytes = source_path.read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_logical_hash = file_digest(source_store.path) if source_store.path.exists() else None
        source = self.read(source_target)
        native = source["native"]
        comparison_native = native
        if native.get("netlist_format") == 0 and not source["managed_state"]:
            comparison_native, model_base = resolve_hspice_model_paths(native, source_path, (options or {}).get("model_base_dir"))
            setup = native_setup(comparison_native, test=test, model_base=model_base)
            setup["metadata"]["native_hspice_legacy"] = True
        else:
            setup = portable_setup(source_store.load()) if source["managed_state"] else native_setup(native, test=test)
        setup["metadata"]["native_notes"] = {section: copy.deepcopy(native["setting_sections"][section]) for section in ("description", "NoteText") if section in native["setting_sections"]}
        if native.get("netlist_format") == 0:
            preserved = opaque_sections(native["setting_sections"])
            if preserved:
                setup["metadata"]["native_hspice_opaque_sections"] = preserved
        self._verify_projection(setup, native)
        evidence = Path((options or {}).get("maestro_artifact_dir", self.workspace / ".aetherpilot" / "migrations")) / uuid.uuid4().hex
        evidence.mkdir(mode=0o700, parents=True)
        canonical_source = evidence / "source-native"
        canonical_source.mkdir(mode=0o700)
        session = self._open(source_target)
        try:
            if not session.getSetting().saveSetting(str(canonical_source)):
                raise OperationError("Native migration source could not be serialized", stage="maestro", code="native_setting_save_failed")
        finally:
            self._close(session)
        candidate = {**target, "cell": "migrate_candidate_" + uuid.uuid4().hex}
        candidate_directory = self._state_dir(candidate)
        candidate_directory.parent.mkdir(mode=0o700, exist_ok=False)
        try:
            self.apply(candidate, {"action": "restore_setup", "setup": setup}, options=options)
            source_for_compare = comparison_native.get("setting_sections") or read_mde_setting(canonical_source / "setting")
            if setup.get("metadata", {}).get("native_hspice_legacy"):
                verify_legacy_semantics(setup, {"netlist_format": 0, "setting_sections": read_mde_setting(candidate_directory / "setting")})
            else:
                compare_native_settings(source_for_compare, read_mde_setting(candidate_directory / "setting"))
            if file_digest(source_path) != source_hash or (file_digest(source_store.path) if source_store.path.exists() else None) != source_logical_hash:
                raise OperationError("Native migration source changed", stage="maestro", code="baseline_changed")
            atomic_text(candidate_directory / "setting", source_bytes.decode("utf-8"))
            candidate_store = MaestroStore(self._library_path(candidate["library"]), candidate["library"], candidate["cell"], candidate["view"])
            candidate_state = candidate_store.load()
            candidate_state["metadata"]["native_setting_sha256"] = source_hash
            candidate_state["metadata"]["native_notes"] = setup["metadata"]["native_notes"]
            candidate_store.save(candidate_state)
            record = {"source": source_target, "target": target, "source_setting_sha256": source_hash,
                      "source_logical_sha256": source_logical_hash,
                      "source_managed": source["managed_state"], "native_fields_preserved": True,
                      "results_copied": False, "history_copied": False, "publication": "atomic_no_replace"}
            atomic_json(candidate_directory / "aetherpilot-migration.json", record)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            publish_directory(candidate_directory, destination)
            migrated = self.read(target)
            migrated["action"] = "migrate_setup"
            migrated["migration"] = record
            atomic_json(evidence / "migration.json", record)
            return migrated
        finally:
            if candidate_directory.parent.exists():
                shutil.rmtree(candidate_directory.parent)

    def recover(self, target):
        from .maestro_recovery import recover_transaction

        target = {"library": target["library"], "cell": target["cell"], "view": target.get("view", "state")}
        store = MaestroStore(self._library_path(target["library"]), target["library"], target["cell"], target["view"])
        return recover_transaction(store, target, lambda: self.read_native(target))

    def run(self, target: Mapping[str, Any], *, test: str = "", output_dir: str | Path | None = None,
            options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        from .maestro_execution import MaestroExecution

        return MaestroExecution(self, target, options).run(test=test, output_dir=output_dir)


__all__ = ["MaestroNativeOps"]
