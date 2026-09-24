"""Use the actual MDE netlister, not interchange text, for native exports."""

import uuid
from pathlib import Path

from .artifacts import atomic_json, atomic_text
from .contract import digest, fields
from .errors import OperationError, ValidationError
from .maestro import _default_state, apply_maestro_action, validate_maestro_request
from .maestro_execution import MaestroExecution
from .maestro_settings import read_mde_setting, setting_text
from .native_tools import native_tool_environment
from .netlist_package import copy_native_package, package_native_netlist
from .readback import readback_is_complete
from .results import safe_input_path, safe_result_path
from .snapshot_native import design_facts


def validate_native_export(source):
    fields(source, (), ("output_dir", "model_libraries", "temperature", "netlist_format"), where="native_netlist_export")
    value = dict(source)
    if value.get("output_dir") is not None:
        value["output_dir"] = str(safe_result_path(value["output_dir"], must_exist=False))
    corner = validate_maestro_request({"action": "setup_corner", "name": "Nominal", "temperature": value.get("temperature", 27), "models": value.get("model_libraries", [])})
    value["temperature"] = corner["temperature"]
    value["model_libraries"] = corner["models"]
    value.setdefault("netlist_format", "hspice")
    if not isinstance(value["netlist_format"], str) or value["netlist_format"] not in {"hspice", "spectre"}:
        raise ValidationError("Native netlist format must be hspice or spectre", stage="netlist", code="unsupported_netlist_format")
    return value


def native_hspice_export(adapter, state_target, target, source, directory):
    state_dir = adapter.maestro._materialize_state(state_target)
    sections = read_mde_setting(state_dir / "setting")
    sections["Other"].update(netlistFormat="0", projectDirectory=str(directory), simulator="Alps")
    sections["Design"] = {"LibraryName": target["library"], "CellName": target["cell"], "ViewName": target["view"]}
    sections["Corner"] = {"size": "1", "1\\CornerName": "Nominal", "1\\CornerTemperature": str(source["temperature"])}
    sections["ModelSetup"] = {"size": str(len(source["model_libraries"]))}
    for index, model in enumerate(source["model_libraries"], 1):
        prefix = str(index) + "\\"
        sections["ModelSetup"].update({prefix + "ModelFile": model["path"], prefix + "isEnabled": "true", prefix + "Corner\\size": "1",
                                      prefix + "Corner\\1\\name": "Nominal", prefix + "Corner\\1\\value": model["section"]})
    sections["SimulatorOptions"].update(relTol="0.001", absTol="1e-9", absv="5e-6", gmin="1e-12", tnom="27", scale="1", scalem="1",
                                         post="true", probe="true", hspiceViewList="hspiceText hspice schematic symbol", hspiceStopList="hspiceText hspice symbol")
    atomic_text(state_dir / "setting", setting_text(sections))
    session = adapter.maestro._open(state_target)
    try:
        setting = session.getSetting()
        if setting.getNetlistFormat() != adapter.api.MDE_NETLIST_FORMAT_HSPICE or not setting.saveSetting(str(state_dir)):
            raise OperationError("Native HSPICE settings could not be reopened", stage="netlist", code="native_setting_write_failed")
        actual = read_mde_setting(state_dir / "setting")
        for section in ("Design", "ModelSetup", "Corner"):
            if any(actual.get(section, {}).get(key) != value for key, value in sections[section].items()):
                raise OperationError("Reopened native HSPICE setup differs", stage="netlist", code="native_state_mismatch", details={"section": section})
        atomic_json(directory / "native-settings.json", actual)
        with native_tool_environment(directory / "tools", ["emy2netlist"]):
            designs = session.createDesignNetlist(str(directory / "export"))
            if not designs:
                raise OperationError("Native HSPICE design export failed", stage="netlist", code="native_netlist_failed", details={"error": str(session.lastError())})
            simulations = session.createSimulationNetlist("Nominal", "", str(directory / "export"))
            if not simulations:
                raise OperationError("Native HSPICE simulator input export failed", stage="netlist", code="native_netlist_failed", details={"error": str(session.lastError())})
        return {"files": MaestroExecution.netlist_files([*designs, *simulations], directory)}
    finally:
        adapter.maestro._close(session)


def export_native_netlist(adapter, target, source, artifacts, options):
    source = validate_native_export(source)
    output = Path(source["output_dir"]) if source.get("output_dir") else artifacts.path / "native-netlist-package"
    if output.exists():
        raise ValidationError("Native netlist output already exists", stage="netlist", code="target_exists")
    models = source["model_libraries"]
    for model in models:
        safe_input_path(model["path"])

    def observe():
        hierarchy = adapter.schematic.read_hierarchy(target, pdk_root=options.get("pdk_root"))
        if not readback_is_complete(hierarchy) or any(record.get("readback", {}).get("source", {}).get("modified") is not False for record in hierarchy["records"] if "readback" in record):
            raise OperationError("Native export needs a complete saved hierarchy", stage="netlist", code="incomplete_readback", details={"completeness": hierarchy["completeness"]})
        return digest(design_facts(hierarchy))

    baseline = observe()
    name = "aetherpilot_netlist_" + uuid.uuid4().hex[:16]
    state_target = {"library": name, "cell": "export", "view": "state"}
    directory = artifacts.path / "native-netlisting"
    directory.mkdir(mode=0o700)
    created = False
    try:
        adapter.library_metadata.create(name, technology_library="018um_PDK")
        created = True
        if source["netlist_format"] == "hspice":
            native = native_hspice_export(adapter, state_target, target, source, directory)
        else:
            state = _default_state(name, "export", "state")
            state, unused = apply_maestro_action(state, {"action": "create_test", "test": "export", "design_library": target["library"], "design_cell": target["cell"], "design_view": target["view"]})
            state, unused = apply_maestro_action(state, {"action": "setup_corner", "name": "Nominal", "temperature": source["temperature"], "models": models})
            adapter.maestro.apply(state_target, {"action": "restore_setup", "setup": state}, options=options)
            execution = MaestroExecution(adapter.maestro, state_target, {**options, "maestro_artifact_dir": str(directory)})
            native = execution.create_netlist(test="export", corner="Nominal")
        suffix = ".sp" if source["netlist_format"] == "hspice" else ".scs"
        inputs = [record["path"] for record in native["files"] if Path(record["path"]).suffix == suffix]
        if len(inputs) != 1:
            raise OperationError("Native export did not produce one simulator entry point", stage="netlist", code="native_netlist_invalid")
        if observe() != baseline:
            raise OperationError("Schematic hierarchy changed while netlisting", stage="netlist", code="baseline_changed")
        package = package_native_netlist(inputs[0], artifacts.path / "verified-native-package", allowed_roots=[directory, *(Path(model["path"]).parent for model in models)],
                                        max_bytes=min(options.get("max_operation_bytes", 1024 ** 3), 256 * 1024 ** 2))
    finally:
        if created:
            adapter.library_metadata.delete(name, approved=True)
    if observe() != baseline:
        raise OperationError("Schematic hierarchy changed before package publication", stage="netlist", code="baseline_changed")
    from .maestro_migration import publish_directory

    staging = Path(package["output_dir"])
    if output == artifacts.path / "native-netlist-package":
        publish_directory(staging, output)
    else:
        copy_native_package(staging, output)
    package.update(output_dir=str(output), input_file=str(output / Path(package["input_file"]).name), manifest=str(output / "manifest.json"), netlist_format=source["netlist_format"],
                   target=target, hierarchy_sha256=baseline, native_netlister="MdeSession.createSimulationNetlist", scratch_library_removed=name not in adapter.library.list())
    package["entrypoint"] = Path(package["input_file"]).name
    package["location"] = {"scope": "requested_directory"} if source.get("output_dir") else {"scope": "operation_artifacts", "path": "native-netlist-package"}
    atomic_json(artifacts.path / "native-netlist-export.json", package)
    return package
