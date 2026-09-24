"""Native layout database readback and transaction-backed local operations."""

from __future__ import annotations

from pathlib import Path

from .artifacts import file_digest
from .contract import digest
from .errors import OperationError, ValidationError
from .layout import compare_layout_plan, diff_layout_readbacks, layout_content, layout_summary, require_complete_layout, stable_layout_readback, validate_layout_plan
from .native import LayoutPrimitives, _layout_attr, _layout_bbox, _layout_list, _layout_native_object, _layout_parameters, _layout_property, _layout_properties, _native_value, _opaque_fields, _text, require
from .transactions import ViewTransactions


class LayoutOps(LayoutPrimitives):
    domain = "layout"
    content = staticmethod(layout_content)
    diff = staticmethod(diff_layout_readbacks)

    def validate(self, plan):
        if isinstance(plan, dict) and plan.get("operation") == "layout.append":
            from .layout_append import prepare_layout_append, validate_layout_append

            value = validate_layout_append({key: item for key, item in plan.items() if key != "_baseline_content"})
            if not self.library.exists(value["target"]):
                raise OperationError("Layout append requires an existing view", stage="plan", code="target_missing")
            return prepare_layout_append(value, self.read(value["target"]))
        if isinstance(plan, dict) and plan.get("operation") == "layout.edit":
            from .layout_edit import project_layout_edits, validate_layout_edit

            value = validate_layout_edit({key: item for key, item in plan.items() if not key.startswith("_")})
            current = self.read(value["target"])
            if digest(self.content(current)) != value["base_sha256"]:
                raise OperationError("Layout edit baseline is stale", stage="plan", code="external_changes")
            value["_expected_content"] = project_layout_edits(current, value["actions"])["content"]
            return value
        return validate_layout_plan(plan)

    @staticmethod
    def compare(plan, actual):
        if plan.get("operation") == "layout.append":
            from .layout_append import compare_layout_append

            return compare_layout_append(plan, actual)
        if plan.get("operation") == "layout.edit":
            require_complete_layout(actual)
            if layout_content(actual) != plan["_expected_content"]:
                raise ValidationError("Edited layout differs from the projected native changes", stage="readback", code="layout_readback_mismatch")
            return {"ok": True, "checks": ["complete_geometry_and_connectivity", "preserved_unselected_objects", "explicit_cleanup"]}
        return compare_layout_plan(plan, actual)

    def materialize(self, plan, target, owner, artifacts):
        if plan.get("operation") == "layout.append":
            self.copy(plan["target"], target)
            design = self.open(target, "a")
            try:
                require(self.api.dbCrtProp(design, "aetherpilot_owner", owner, "string"), "dbCrtProp", "layout")
                self.populate(design, plan, reuse_nets=True)
                self.save(design)
            finally:
                self.close(design)
            return
        if plan.get("operation") == "layout.edit":
            from .layout_edit import object_match_key, project_layout_edits

            self.copy(plan["target"], target)
            design = self.open(target, "a")
            try:
                require(self.api.dbCrtProp(design, "aetherpilot_owner", owner, "string"), "dbCrtProp", "layout")
                projection = project_layout_edits(self.read_design(design, target), plan["actions"])
                references = {}
                for collection, native_collection in (("instances", "instances"), ("shapes", "shapes"), ("vias", "vias")):
                    for native in list(getattr(design, native_collection)):
                        item = _layout_native_object(self.api, native)
                        actual_collection = "mosaics" if collection == "instances" and _layout_attr(native, "rows") is not None else collection
                        references.setdefault((actual_collection, object_match_key(item)), []).append(native)
                for removed in projection["removed"]:
                    matching = references.get((removed["collection"], object_match_key(removed["object"])), [])
                    if not matching:
                        raise ValidationError("Cannot map selected geometry to a native object", stage="layout", code="ambiguous_selection")
                    require(self.api.dbDelete(matching.pop(0)), "dbDelete", "layout")
                for terminal in list(design.terminals):
                    for pin in list(terminal.pins):
                        if not list(pin.figs):
                            self.api.emyPin.destroy(pin)
                for terminal in list(design.terminals):
                    if not list(terminal.pins):
                        self.api.emyTerm.destroy(terminal)
                if any(action["operation"] == "clear" for action in plan["actions"]):
                    for net in list(design.nets):
                        require(self.api.dbDelete(net), "dbDelete", "layout")
                self.save(design)
                artifacts.write("layout-edit-effects.json", {"changes": projection["changes"], "selected": projection["selected"]})
            finally:
                self.close(design)
            return
        self.build(plan, target, owner, artifacts.path / "layout-check-report.txt")

    def read(self, target, *, include_properties=True):
        existing = self.api.dbFindCV(target["library"], target["cell"], target.get("view", "layout"))
        design = existing if existing is not None else self.open(target)
        try:
            return self.read_design(design, target, include_properties=include_properties)
        finally:
            if existing is None:
                self.close(design)

    def read_design(self, design, target, *, include_properties=True):
        issues = []
        filename = _text(_layout_attr(design, "fileName"))
        path = Path(filename) if filename else None
        before_hash = file_digest(path) if path and path.is_file() else None
        raw = {}
        for key in ("instances", "shapes", "vias", "terminals", "nets"):
            raw[key] = _layout_list(design, key)
            if raw[key] is None:
                issues.append({"kind": key, "reason": "native collection unavailable"})
                raw[key] = []
        instances = []
        mosaics = []
        for instance in raw["instances"]:
            item = _layout_native_object(self.api, instance)
            item.update(name=_text(_layout_attr(instance, "name")), master={
                "library": _text(_layout_attr(instance, "libName")), "cell": _text(_layout_attr(instance, "cellName")),
                "view": _text(_layout_attr(instance, "viewName"))}, parameters=_layout_parameters(self.api, instance, issues))
            rows = _layout_attr(instance, "rows")
            columns = _layout_attr(instance, "columns")
            if rows is not None or columns is not None:
                item.update(rows=rows, cols=columns, row_pitch=_layout_attr(instance, "deltaY"), col_pitch=_layout_attr(instance, "deltaX"))
                mosaics.append(item)
            else:
                instances.append(item)
            if not item.get("name") or any(value is None for value in item["master"].values()) or item.get("origin") is None or item.get("orientation") is None:
                issues.append({"kind": "instance", "object": item.get("name"), "reason": "missing master or transform"})
        shapes = [_layout_native_object(self.api, figure) for figure in raw["shapes"]]
        for item in shapes:
            required = {"rect": ("bbox",), "polygon": ("points",), "path": ("points", "width", "style", "begin_extension", "end_extension"),
                        "label": ("text", "position", "orientation", "height", "font", "justification")}.get(item["obj_type"])
            if required is None or any(item.get(key) is None for key in (*required, "layer", "purpose", "bbox")) or any(point is None for point in item.get("points") or []):
                issues.append({"kind": "shape", "object": item.get("name"), "native_type": item["obj_type"], "reason": "unsupported or incomplete geometry"})
        vias = []
        for via in raw["vias"]:
            item = _layout_native_object(self.api, via)
            definition = _layout_attr(via, "viaDef")
            item["definition"] = _text(_layout_attr(definition, "name"))
            item.setdefault("position", item.pop("origin", None))
            parameters = {}
            for name, attr in (("rows", "cutRow"), ("cols", "cutColumn"), ("width", "cutWidth"), ("height", "cutHeight")):
                value = _layout_attr(via, attr)
                if value is not None:
                    parameters[name] = _native_value(value)
            spacing = _layout_attr(via, "cutSpacing")
            if spacing is not None:
                spacing = _native_value(spacing)
                if isinstance(spacing, list) and len(spacing) >= 2:
                    parameters.update(xSpace=float(spacing[0]), ySpace=float(spacing[1]))
                else:
                    issues.append({"kind": "via", "object": item.get("name"), "reason": "via spacing is not a readable pair"})
            for name, prefix in (("layer1", "layer1"), ("layer2", "layer2")):
                horizontal = _layout_attr(via, prefix + "LeftRight")
                vertical = _layout_attr(via, prefix + "TopBottom")
                if horizontal is not None and vertical is not None:
                    horizontal = _native_value(horizontal)
                    vertical = _native_value(vertical)
                    if isinstance(horizontal, list) and isinstance(vertical, list) and len(horizontal) >= 2 and len(vertical) >= 2:
                        parameters[name] = [float(horizontal[0]), float(vertical[1]), float(horizontal[1]), float(vertical[0])]
                    else:
                        issues.append({"kind": "via", "object": item.get("name"), "reason": "via enclosure is not readable"})
            for name, attr in (("impL1", "implant1Enc"), ("impL2", "implant2Enc")):
                value = _layout_attr(via, attr)
                if value is not None:
                    value = _native_value(value)
                    if isinstance(value, list):
                        parameters[name] = [float(number) for number in value]
                    else:
                        issues.append({"kind": "via", "object": item.get("name"), "reason": "via implant enclosure is not readable"})
            definitions = _layout_list(via, "cdfParameter")
            if definitions is not None:
                parameters.update({str(entry.name): _native_value(entry.value) for entry in definitions})
            item["parameters"] = parameters
            if not item.get("definition") or item.get("position") is None or item.get("orientation") is None:
                issues.append({"kind": "via", "object": item.get("name"), "reason": "missing via definition or transform"})
            vias.append(item)
        pins = []
        for terminal in raw["terminals"]:
            native_pins = _layout_list(terminal, "pins")
            if native_pins is None:
                issues.append({"kind": "pin", "reason": "terminal pin collection unavailable"})
                continue
            for pin in native_pins:
                figures = _layout_list(pin, "figs")
                name = _layout_property(self.api, pin, "aetherpilot_name") or _text(_layout_attr(pin, "name"))
                item = {"name": name, "terminal": _text(_layout_attr(terminal, "name")),
                        "net": _text(_layout_attr(_layout_attr(terminal, "net"), "name")),
                        "direction": _text(_layout_attr(terminal, "direction")), "properties": _layout_properties(pin),
                        "figures": [_layout_native_object(self.api, figure) for figure in figures or []]}
                if len(item["figures"]) == 1:
                    item.update({key: item["figures"][0][key] for key in ("bbox", "layer", "purpose")})
                if not figures or not name or item["net"] is None or item["direction"] is None:
                    issues.append({"kind": "pin", "object": name, "reason": "incomplete terminal or pin geometry"})
                pins.append(item)
        nets = [{"name": _text(_layout_attr(net, "name")), "properties": _layout_properties(net)} for net in raw["nets"]]
        technology = _text(_layout_attr(_layout_attr(design, "tech"), "libName"))
        modified = _layout_attr(design, "isModified")
        if modified is None or technology is None:
            issues.append({"kind": "source", "reason": "technology or modified state unavailable"})
        after_hash = file_digest(path) if path and path.is_file() else None
        if before_hash != after_hash:
            issues.append({"kind": "source", "reason": "database changed during readback"})
        for collection in (instances, mosaics, shapes, vias, pins, nets):
            issues.extend(_opaque_fields(collection))
            for item in collection:
                if include_properties and item.get("properties") is None:
                    issues.append({"kind": "properties", "object": item.get("name"), "reason": "native properties unavailable"})
                elif not include_properties:
                    item.pop("properties", None)
        if not include_properties:
            issues.append({"kind": "properties", "reason": "excluded by request"})
        readback = {"schema_version": 1, "target": {key: target[key] for key in ("library", "cell", "view")}, "technology": technology,
                    "owner": _layout_property(self.api, design, "aetherpilot_owner"), "database_sha256": after_hash,
                    "instances": instances, "mosaics": mosaics, "shapes": shapes, "vias": vias, "pins": pins, "nets": nets,
                    "source": {"saved": not modified if modified is not None else None, "modified": modified, "file": filename},
                    "unsupported": issues, "completeness": {"status": "incomplete" if issues else "complete", "issues": issues}}
        value = stable_layout_readback(readback)
        value["layers"] = layout_summary(value)["layers"]
        return value

    def apply(self, plan, artifacts, result):
        plan = self.validate(plan)
        target = plan["target"]
        transactions = ViewTransactions(self, artifacts, result)
        if self.library.exists(target):
            transactions._guard(target)
            current = transactions._read(target)
            if not transactions._is_managed(target, current):
                raise OperationError("Direct apply only overwrites an unchanged managed view; use a candidate for imported designs", stage="ownership", code="unmanaged_target")
            try:
                self.compare(plan, current)
            except ValidationError:
                if plan["mode"] == "create":
                    raise OperationError("create cannot change an existing view", stage="ownership", code="target_exists")
            else:
                artifacts.write("readback.json", current)
                artifacts.write("validation.json", {"ok": True, "idempotent": True})
                return current
        candidate = transactions.prepare(plan)
        result["readback"] = candidate
        return transactions.publish(candidate["candidate_id"], approved=True, candidate_sha256=candidate["candidate"]["content_sha256"],
                                    base_sha256=candidate["baseline"]["content_sha256"] if candidate["baseline"] else None)

    def session(self, target, actions, *, owned):
        if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
            raise ValidationError("Layout session requires 1–100 actions", stage="plan", code="invalid_layout_actions")
        self.validate_session_actions(actions)
        current = self.api.aeGetCurDesign()
        matches = current is not None and all(_text(_layout_attr(current, name)) == target[key] for name, key in (("libName", "library"), ("cellName", "cell"), ("viewName", "view")))
        if not matches and not owned:
            raise OperationError("Attached session action requires the explicitly targeted current view", stage="session", code="user_view_mismatch")
        opened = False
        try:
            if not matches:
                require(self.api.aeOpenDesign(target["library"], target["cell"], target["view"], "readOnly"), "aeOpenDesign", "session")
                opened = True
            responses = []
            for action in actions:
                if not isinstance(action, dict) or "operation" not in action:
                    raise ValidationError("Invalid layout session action", stage="plan", code="invalid_layout_actions")
                operation = action["operation"]
                if operation == "fit" and set(action) == {"operation"}:
                    self.api.aeZoomFit(1, 0.9)
                elif operation == "active_layer" and set(action) <= {"operation", "layer", "purpose"} and "layer" in action:
                    self.api.aeSetEntryLayer(action["layer"], action.get("purpose", "drawing"))
                    actual_layer = [str(value) for value in self.api.aeGetEntryLayerPurpose()]
                    if actual_layer != [action["layer"], action.get("purpose", "drawing")]:
                        raise ValidationError("Native active layer differs from the request", stage="session", code="session_readback_mismatch")
                elif operation in {"show_layers", "hide_layers", "show_only_layers"} and set(action) == {"operation", "layers"}:
                    pairs = action["layers"]
                    if not isinstance(pairs, list) or any(not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(name, str) or not name for name in pair) for pair in pairs):
                        raise ValidationError("Layer actions require layer/purpose pairs", stage="plan", code="invalid_layout_actions")
                    if operation == "show_only_layers":
                        self.api.aeSetAllLayerVisible(False)
                    for layer, purpose in pairs:
                        self.api.aeSetLayerVisible(operation != "hide_layers", layer, purpose)
                        if bool(self.api.aeLayerIsVisible(self.api.emyString(layer), self.api.emyString(purpose))) != (operation != "hide_layers"):
                            raise ValidationError("Native layer visibility differs from the request", stage="session", code="session_readback_mismatch")
                elif operation == "select_box" and set(action) <= {"operation", "bbox", "inside", "mode"} and "bbox" in action:
                    from .layout import select_layout_objects

                    readback = self.read(target)
                    selected = select_layout_objects(readback, bbox=action["bbox"], inside=action.get("inside", True))
                    verified = self.select_native_objects(self.api.aeGetCurDesign(), readback, selected, mode=action.get("mode", "replace"))
                    responses.append({"operation": operation, "selected": selected, **verified})
                    continue
                elif operation == "highlight_net":
                    active = self.api.aeGetCurDesign()
                    figures = [figure for figure in list(active.shapes) + list(active.vias) if _text(_layout_attr(_layout_attr(figure, "net"), "name")) == action["net"]]
                    if not figures:
                        raise OperationError("Net has no native figures to highlight", stage="session", code="net_not_found")
                    area = require(self.api.aeGetDaByCV(active), "aeGetDaByCV", "session")
                    highlight = require(self.api.aeCrtHilightSet(area, action.get("layer", "M1"), "drawing"), "aeCrtHilightSet", "session")
                    for figure in figures:
                        box = _layout_bbox(_layout_attr(figure, "listBbox"))
                        if box is None:
                            raise ValidationError("Net highlight requires native geometry", stage="session", code="missing_geometry")
                        require(self.api.dbCrtRect(cv=highlight, bbox=tuple(box), layer=action.get("layer", "M1"), purpose="drawing"), "dbCrtRect", "session")
                    highlight.enable = True
                    responses.append({"operation": operation, "figures": len(figures), "scope": "net_figure_bounding_boxes"})
                    continue
                elif operation == "clear_highlights":
                    self.api.aeDeleteAllHilitSet(self.api.aeGetCurDesign())
                else:
                    raise ValidationError("Unsupported or malformed layout session action", stage="plan", code="invalid_layout_actions")
                responses.append({"operation": operation, "executed": True})
            return {"actions": responses, "scope": "explicit_local_gui_session", "persistent": not owned}
        finally:
            if opened:
                self.api.aeCloseDesign()

    def select_native_objects(self, design, readback, selected, *, mode="replace"):
        from collections import Counter
        from .layout_edit import object_match_key

        if mode not in {"replace", "add", "subtract", "sub", "remove"}:
            raise ValidationError("Unsupported selection mode", stage="session", code="invalid_layout_actions")
        normalized_mode = "subtract" if mode in {"sub", "remove"} else mode
        selected_before = list(self.api.aeGetSelectedSet(design))

        def clear_selection():
            clear = getattr(self.api, "aeDeselectAll", None)
            if callable(clear):
                clear()
                return
            clear = getattr(self.api, "aeDeselectAllFig", None)
            if callable(clear):
                clear(design)
                return
            raise ValidationError("Native API does not expose selection clearing", stage="session", code="unsupported_native_api")

        native_by_key = {}
        for collection in ("instances", "shapes", "vias"):
            for native in getattr(design, collection):
                actual_collection = "mosaics" if collection == "instances" and _layout_attr(native, "rows") is not None else collection
                key = object_match_key(_layout_native_object(self.api, native))
                native_by_key.setdefault((actual_collection, key), []).append(native)
        objects = []
        expected_items = []
        omitted = []
        for reference in selected:
            collection = reference["collection"]
            field = "name" if collection in {"instances", "mosaics"} else "id"
            matches = [item for item in readback[collection] if item[field] == reference["key"]]
            if len(matches) != 1:
                raise ValidationError("Selection reference is ambiguous", stage="session", code="ambiguous_selection")
            item = matches[0]
            native_matches = native_by_key.get((collection, object_match_key(item)), [])
            if not native_matches:
                raise ValidationError("Selected geometry has no native object", stage="session", code="ambiguous_selection")
            native = native_matches.pop(0)
            expected_items.append((reference, item, native))
            if item.get("pin"):
                omitted.append({**reference, "reason": "pin_figure_not_exposed_by_native_selection_set"})
            elif callable(getattr(self.api, "aeLayerIsVisible", None)) and not bool(self.api.aeLayerIsVisible(self.api.emyString(item.get("layer")), self.api.emyString(item.get("purpose", "drawing")))):
                omitted.append({**reference, "reason": "layer_hidden"})
            else:
                objects.append(native)
        if normalized_mode == "replace":
            clear_selection()
        if normalized_mode in {"replace", "add"} and objects:
            require(self.api.aeSelectFigs(objects, selInside=False), "aeSelectFigs", "session")
        before = Counter(object_match_key(_layout_native_object(self.api, item)) for item in selected_before)
        requested = Counter(object_match_key(_layout_native_object(self.api, item)) for item in objects)
        if normalized_mode == "subtract":
            retained = [item for item in selected_before if object_match_key(_layout_native_object(self.api, item)) not in requested]
            clear_selection()
            if retained:
                require(self.api.aeSelectFigs(retained, selInside=False), "aeSelectFigs", "session")
        expected = requested if normalized_mode == "replace" else before | requested if normalized_mode == "add" else before - requested
        actual = Counter(object_match_key(_layout_native_object(self.api, item)) for item in self.api.aeGetSelectedSet(design))
        if actual != expected:
            raise ValidationError("Native selection differs from the requested objects", stage="session", code="selection_readback_mismatch",
                                  details={"mode": normalized_mode, "expected": list(expected.elements()), "actual": list(actual.elements())})
        return {"native_selected_count": len(objects), "native_omitted": omitted,
                "readback_verified": True, "selection_mode": normalized_mode,
                "selection_scope": "visible_selectable_layout_figures"}

    @staticmethod
    def validate_session_actions(actions):
        from .contract import identifier
        from .layout import select_layout_objects

        allowed = {"fit": ({"operation"}, set()), "active_layer": ({"operation", "layer"}, {"purpose"}),
                   "show_layers": ({"operation", "layers"}, set()), "hide_layers": ({"operation", "layers"}, set()),
                   "show_only_layers": ({"operation", "layers"}, set()), "select_box": ({"operation", "bbox"}, {"inside", "mode"}),
                   "highlight_net": ({"operation", "net"}, {"layer"}), "clear_highlights": ({"operation"}, set())}
        for action in actions:
            if not isinstance(action, dict) or action.get("operation") not in allowed:
                raise ValidationError("Unsupported layout session action", stage="plan", code="invalid_layout_actions")
            required, optional = allowed[action["operation"]]
            if not required <= set(action) or set(action) - required - optional:
                raise ValidationError("Malformed layout session action", stage="plan", code="invalid_layout_actions")
            if action["operation"] == "select_box":
                try:
                    select_layout_objects({"completeness": {"status": "complete", "issues": []}}, bbox=action["bbox"], inside=action.get("inside", True))
                except ValueError as exc:
                    raise ValidationError(str(exc), stage="plan", code="invalid_layout_actions") from exc
                if action.get("mode", "replace") not in {"replace", "add", "subtract", "sub", "remove"}:
                    raise ValidationError("Unsupported selection mode", stage="plan", code="invalid_layout_actions")
            for key in ("layer", "purpose", "net"):
                if key in action:
                    identifier(action[key])
            if "layers" in action:
                if not isinstance(action["layers"], list) or len(action["layers"]) > 1000:
                    raise ValidationError("Invalid layer list", stage="plan", code="invalid_layout_actions")
                for pair in action["layers"]:
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ValidationError("Invalid layer/purpose pair", stage="plan", code="invalid_layout_actions")
                    for value in pair:
                        identifier(value)

    def find_via(self, target, name):
        from .contract import identifier

        identifier(name, "via definition")
        design = self.open(target)
        try:
            definition = self.api.emyViaDef.find(design.tech, name)
            if definition is None:
                raise OperationError("Via definition is not present in the attached technology", stage="layout", code="via_definition_missing")
            return {"name": _text(_layout_attr(definition, "name")), "technology": _text(_layout_attr(design.tech, "libName")),
                    "native_type": _text(_layout_attr(definition, "objType")), "found": True}
        finally:
            self.close(design)

    def delete_cell(self, target, baselines, *, approved, artifacts, result):
        from .artifacts import atomic_json
        from .schematic_native import SchematicTransactionsBackend
        from .transactions import writable_target

        writable_target(target)
        if approved is not True or not isinstance(baselines, dict) or not baselines:
            raise OperationError("Cell deletion requires explicit approval and every view fingerprint", stage="ownership", code="approval_required")
        views = self.library.views(target["library"], target["cell"])
        if set(views) != set(baselines) or not views or set(views) - {"layout", "schematic"}:
            raise OperationError("Cell deletion requires a complete, supported view inventory", stage="ownership", code="view_inventory_mismatch")
        captured = {}
        for view in views:
            backend = self if view == "layout" else SchematicTransactionsBackend(self.api)
            transactions = ViewTransactions(backend, artifacts, result)
            current_target = {**target, "view": view}
            transactions._guard(current_target)
            current = transactions._read(current_target)
            if transactions.fingerprint(current)["content_sha256"] != baselines[view]:
                raise OperationError("Cell deletion baseline changed", stage="ownership", code="external_changes")
            backup = {**current_target, "cell": "APbackup_" + result["operation_id"]}
            backend.copy(current_target, backup)
            if digest(backend.content(transactions._read(backup))) != baselines[view]:
                raise ValidationError("Deletion backup differs from its source", stage="backup", code="backup_mismatch")
            captured[view] = {"target": current_target, "backup": backup, "fingerprint": transactions.fingerprint(current)}
        recovery = {"state": "prepared", "views": captured, "deleted": []}
        recovery_path = Path.cwd() / ".aetherpilot" / "deletions" / (result["operation_id"] + ".json")
        atomic_json(recovery_path, recovery)
        try:
            for view, captured_view in captured.items():
                backend = self if view == "layout" else SchematicTransactionsBackend(self.api)
                transactions = ViewTransactions(backend, artifacts, result)
                transactions._assert_baseline(captured_view["target"], captured_view["fingerprint"])
            for view in views:
                require(self.api.dbDelCV(target["library"], target["cell"], view), "dbDelCV", "layout")
                recovery["deleted"].append(view)
                atomic_json(recovery_path, recovery)
            if self.library.views(target["library"], target["cell"]):
                raise ValidationError("Deleted cell still has views", stage="readback", code="delete_mismatch")
            recovery["state"] = "deleted"
        except Exception:
            recovery["state"] = "recovery_required"
            raise
        finally:
            atomic_json(recovery_path, recovery)
            artifacts.write("cell-deletion.json", recovery)
        result["changes"].append({"type": "cell_deleted", "target": target, "backups": [item["backup"] for item in captured.values()]})
        return recovery
