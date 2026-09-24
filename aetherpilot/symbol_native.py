"""Native PyAether symbol generation, editing, and readback."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path

from .errors import OperationError, ValidationError
from .native import _native_properties, _native_value, _safe_attr, _text, require
from .symbol import SYMBOL_REVISION_PROPERTIES, compare_symbol_plan, default_symbol_side, digest, sorted_symbol_pins, symbol_content, symbol_pin_side_issues, validate_symbol_plan, validate_symbol_readback
from .snapshot_native import library_path
from .transactions import writable_target
from .library_safety import writable_library_path
from .artifacts import atomic_json


def _attr(value, *names, default=None):
    for name in names:
        item = _safe_attr(value, name)
        if item is None:
            continue
        try:
            return item() if callable(item) else item
        except Exception:
            continue
    return default


def _point(value):
    try:
        return [round(float(value[0]), 9), round(float(value[1]), 9)]
    except Exception:
        return None


def _points(value):
    if value is None:
        return None
    try:
        result = [_point(item) for item in value]
    except (TypeError, ValueError):
        return None
    return result if result and all(item is not None for item in result) else None


def _bbox(value):
    try:
        first, second = value
        first_point, second_point = _point(first), _point(second)
        if first_point is None or second_point is None:
            return None
        return [first_point[0], first_point[1], second_point[0], second_point[1]]
    except (TypeError, ValueError):
        return None


def _obj_type(item):
    return _text(_attr(item, "objType", "objectType", "type", default="unknown"), "unknown")


def _lpp(item):
    return (_text(_attr(item, "layerName", "layer", default=None)), _text(_attr(item, "purpose", default=None)))


def _shape_id(item):
    return str(item)


def _term_pin_figures(design):
    figures = set()
    for term in _attr(design, "terminals", default=()) or ():
        for pin in _attr(term, "pins", default=()) or ():
            values = _attr(pin, "figs", default=None)
            if values is None:
                values = [_attr(pin, "fig", default=None)]
            for figure in values or ():
                if figure is None:
                    continue
                figures.add(_shape_id(figure))
    return figures


def _label_record(item):
    obj_type = _obj_type(item)
    text = _text(_attr(item, "text", "theLabel", "textName", default=""), "")
    label_type = "evaluation" if obj_type.casefold() == "evaltext" else "normal"
    semantic = None
    if text in {"[@instanceName]", "[@instName]"}:
        semantic = "instance_name"
    elif text in {"[@partName]", "[@cellName]"}:
        semantic = "logical_name"
    elif _lpp(item) == ("pin", "label"):
        semantic = "pin_name"
    record = {
        "text": text,
        "label_type": label_type,
        "layer": _lpp(item)[0],
        "purpose": _lpp(item)[1],
        "position": _point(_attr(item, "origin", "xy", "point", default=None)),
        "height": _attr(item, "height", default=None),
        "orientation": _text(_attr(item, "orient", "orientation", default=None)),
        "justification": _text(_attr(item, "justify", "justification", default=None)),
        "font": _text(_attr(item, "font", default=None)),
        "semantic": semantic,
        "automatic": text.startswith("[@") or semantic == "pin_name",
        "bbox": _bbox(_attr(item, "listBbox", "bBox", "bbox", default=None)),
        "properties": _native_properties(item),
    }
    return record


def _shape_record(item, pin_figures):
    obj_type = _obj_type(item)
    normalized = obj_type.casefold()
    mapping = {"line": "line", "rect": "rect", "polygon": "polygon", "ellipse": "ellipse", "circle": "circle", "arc": "arc"}
    record = {
        "id": _shape_id(item),
        "obj_type": mapping.get(normalized, normalized),
        "layer": _lpp(item)[0],
        "purpose": _lpp(item)[1],
        "bbox": _bbox(_attr(item, "listBbox", "bBox", "bbox", default=None)),
        "points": _points(_attr(item, "points", "pointList", default=None)),
        "center": _point(_attr(item, "center", default=None)),
        "radius": _attr(item, "radius", default=None),
        "fill_style": _text(_attr(item, "fillStyle", "fillstyle", default=None)),
        "is_pin_figure": _shape_id(item) in pin_figures,
        "properties": _native_properties(item),
    }
    return record


class SymbolOps:
    """PyAether symbol operations with native readback gates."""

    def __init__(self, api, *, workspace=None):
        self.api = api
        self.workspace = Path(workspace or Path.cwd()).resolve()

    def _guard_target(self, target):
        target = {**target, "view": target.get("view", "symbol"), "technology": target.get("technology", "018um_PDK")}
        writable_target(target)
        writable_library_path(target["library"], library_path(self.api, target["library"]), self.workspace)
        return target

    def _candidate_name(self, prefix, owner):
        return prefix + "_ap_" + owner

    def _copy_view(self, source, destination, *, overwrite=False):
        opened = self.api.dbOpenCV(source["library"], source["cell"], source["view"], "r")
        if opened is None:
            raise OperationError("Native source view could not be opened", stage="symbol", code="target_missing")
        try:
            if not self.api.dbCopyCV(opened, destination["library"], destination["cell"], destination["view"], overwrite):
                raise OperationError("Native candidate view copy failed", stage="symbol", code="native_false")
        finally:
            self.api.dbCloseCV(opened)

    def _remove_view(self, target):
        try:
            if self.exists(target):
                self.api.dbDelCV(target["library"], target["cell"], target["view"])
        except Exception:
            return False
        return not self.exists(target)

    def _require_idle(self, target):
        if self.api.dbFindCV(target["library"], target["cell"], target["view"]) is not None:
            raise OperationError("Symbol target is already open", stage="symbol", code="user_view_open")
        if self.api.dbCVIsLocked(target["library"], target["cell"], target["view"], "symbol"):
            raise OperationError("Symbol target has a native lock; no lock is removed", stage="symbol", code="native_view_locked")

    @staticmethod
    def _geometry_hash(readback):
        value = symbol_content(readback)
        value.pop("target")
        return digest(value)

    def _publish_isolated(self, source, target, baseline, *, owner, report, extra_cleanup=()):
        target = self._guard_target(target)
        existing = self.exists(target)
        if existing:
            self._require_idle(target)
        candidate_cell = source["cell"]
        target_before = self.read(target) if existing else None
        if target_before and validate_symbol_readback(target_before):
            raise OperationError("Symbol replacement needs a complete baseline", stage="symbol", code="incomplete_readback")
        baseline_hash = target_before.get("content_sha256") if target_before else None
        baseline_geometry = self._geometry_hash(target_before) if target_before else None
        candidate_readback = self.read(source)
        if validate_symbol_readback(candidate_readback):
            raise OperationError("Symbol candidate readback is incomplete", stage="symbol", code="incomplete_readback")
        candidate_geometry = self._geometry_hash(candidate_readback)
        if candidate_readback.get("owner") != owner:
            raise OperationError("Symbol candidate ownership differs from this publication", stage="ownership", code="owner_mismatch")
        baseline_owner = target_before.get("owner") if target_before else None
        backup_cell = self._candidate_name("symbol_backup", owner) if existing else None
        backup = {**target, "cell": backup_cell, "view": "symbol"} if backup_cell else None
        cleanup = [*extra_cleanup]
        if backup:
            cleanup.append(backup)
        publication_started = False
        recovery_required = False
        try:
            if existing:
                if baseline is not None and baseline != baseline_hash:
                    raise OperationError("Symbol target changed before staging", stage="ownership", code="external_changes")
                self._copy_view(target, backup)
                if self._geometry_hash(self.read(backup)) != baseline_geometry:
                    raise OperationError("Symbol backup differs from the baseline", stage="symbol", code="backup_mismatch")
            if existing:
                self._require_idle(target)
                current = self.read(target)
                if current.get("content_sha256") != baseline_hash or current.get("owner") != baseline_owner:
                    raise OperationError("Symbol target changed before publication", stage="ownership", code="external_changes")
            elif self.exists(target):
                raise OperationError("Symbol target appeared during creation", stage="ownership", code="external_changes")
            source_design = self.api.dbOpenCV(source["library"], source["cell"], source["view"], "r")
            if source_design is None:
                raise OperationError("Native symbol candidate could not be opened", stage="symbol", code="target_missing")
            try:
                publication_started = True
                if not self.api.dbCopyCV(source_design, target["library"], target["cell"], target["view"], existing):
                    raise OperationError("Native symbol publication failed", stage="symbol", code="native_false")
            finally:
                self.api.dbCloseCV(source_design)
            actual = self.read(target)
            if self._geometry_hash(actual) != candidate_geometry or actual.get("owner") != owner:
                raise OperationError("Published symbol differs from the verified candidate", stage="symbol", code="publish_mismatch")
            publication = {"target": target, "baseline_sha256": baseline_hash, "result_sha256": actual["content_sha256"],
                           "target_existed": existing, "candidate_cell": candidate_cell, "verified": True}
            if isinstance(report, dict):
                report["publication"] = publication
            return actual
        except Exception as original:
            if not publication_started:
                raise
            try:
                current = self.read(target) if self.exists(target) else None
                current_geometry = self._geometry_hash(current) if current else None
                if current_geometry not in {baseline_geometry, candidate_geometry}:
                    raise OperationError("Symbol has unrecognized external content; automatic recovery will not overwrite it", stage="recovery", code="external_changes")
                if existing and current_geometry == baseline_geometry and current.get("owner") == baseline_owner:
                    pass
                elif existing and backup and self.exists(backup):
                    if current and current.get("owner") != owner:
                        raise OperationError("Symbol ownership changed; automatic recovery will not overwrite it", stage="recovery", code="owner_mismatch")
                    self._require_idle(target)
                    self._copy_view(backup, target, overwrite=True)
                    if self.read(target).get("content_sha256") != baseline_hash:
                        raise OperationError("Symbol backup restoration differs", stage="recovery", code="recovery_required")
                elif not existing and current_geometry == candidate_geometry:
                    if current.get("owner") != owner:
                        raise OperationError("New symbol no longer belongs to this publication", stage="recovery", code="owner_mismatch")
                    self._require_idle(target)
                    if not self._remove_view(target):
                        raise OperationError("Failed to remove owned new symbol", stage="recovery", code="recovery_required")
            except Exception as recovery:
                recovery_required = True
                record = {"target": target, "backup": backup, "owner": owner, "baseline_sha256": baseline_hash,
                          "candidate_geometry_sha256": candidate_geometry, "automatic_overwrite_refused": True,
                          "original": type(original).__name__, "recovery": type(recovery).__name__}
                recovery_path = self.workspace / ".aetherpilot" / "recovery" / ("symbol-" + owner + ".json")
                atomic_json(recovery_path, record)
                raise OperationError("Symbol publication requires explicit recovery", stage="recovery", code="recovery_required",
                                     details={"backup": backup, "recovery_record": str(recovery_path)}) from original
            raise
        finally:
            for item in cleanup:
                if not recovery_required or item != backup:
                    self._remove_view(item)

    def apply_isolated(self, plan, target, owner, report):
        plan = validate_symbol_plan(plan)
        target = self._guard_target(target)
        existing = self.exists(target)
        if existing:
            self._require_idle(target)
        if plan["mode"] == "modify" and not existing:
            raise OperationError("Symbol modify requires an existing view", stage="ownership", code="target_missing")
        if existing and not plan.get("overwrite", False) and plan["mode"] == "create":
            raise OperationError("Symbol target already exists", stage="ownership", code="target_exists")
        baseline = self.read(target)["content_sha256"] if existing else None
        owner = owner if isinstance(owner, str) and owner else uuid.uuid4().hex
        candidate_cell = self._candidate_name("symbol_candidate", owner)
        candidate_target = {**target, "cell": candidate_cell, "view": "symbol"}
        candidate_plan = copy.deepcopy(plan)
        candidate_plan["mode"] = "create"
        candidate_plan["overwrite"] = False
        candidate_plan["target"] = candidate_target
        native_result = {}
        try:
            staged = self.apply(candidate_plan, candidate_target, owner, report)
            native_result["candidate"] = staged
            actual = self._publish_isolated(candidate_target, target, baseline, owner=owner, report=native_result)
            native_result["readback"] = actual
            native_result["transaction"] = "isolated_candidate_then_verified_publication"
            native_result["recovery"] = "verified_backup_restore_or_absence"
            return native_result
        finally:
            self._remove_view(candidate_target)

    def append_isolated(self, plan, target, owner, report):
        from .symbol_append import compare_symbol_append, prepare_symbol_append, validate_symbol_append

        plan = validate_symbol_append(plan)
        target = self._guard_target(target)
        if not self.exists(target):
            raise OperationError("Symbol append requires an existing view", stage="symbol", code="target_missing")
        self._require_idle(target)
        baseline = self.read(target)
        prepare_symbol_append(plan, baseline)
        candidate = {**target, "cell": self._candidate_name("symbol_candidate", owner)}
        result = {}
        try:
            self._copy_view(target, candidate)
            candidate_plan = {key: copy.deepcopy(value) for key, value in plan.items() if key != "base_sha256"}
            candidate_plan.update(operation="symbol.apply", target=candidate)
            result["candidate"] = self.apply(candidate_plan, candidate, owner, report, baseline=baseline)
            result["readback"] = self._publish_isolated(candidate, target, baseline["content_sha256"], owner=owner, report=result)
            compare_symbol_append(plan, baseline, result["readback"])
            result.update(transaction="isolated_append_then_verified_publication", baseline_preserved=True)
            return result
        finally:
            self._remove_view(candidate)

    def generate_from_schematic_isolated(self, target, *, owner, report, **options):
        target = self._guard_target({**target, "view": options.get("symbol_view", "symbol")})
        schematic_target = {**target, "view": options.get("schematic_view", "schematic")}
        if not self.exists(schematic_target):
            raise OperationError("Source schematic does not exist", stage="preflight", code="target_missing")
        existing = self.exists(target)
        if existing:
            self._require_idle(target)
        baseline = self.read(target)["content_sha256"] if existing else None
        candidate_cell = self._candidate_name("symbol_candidate", owner)
        candidate_schematic = {**schematic_target, "cell": candidate_cell}
        candidate_target = {**target, "cell": candidate_cell}
        try:
            self._copy_view(schematic_target, candidate_schematic)
            generated = self.generate_from_schematic(candidate_target, **options)
            design = self.open(candidate_target, "a")
            try:
                self._call("dbCrtProp", design, "aetherpilot_owner", owner, "string")
                self._call("dbSaveCV", design)
            finally:
                self.close(design)
            generated["readback"] = self.read(candidate_target)
            native_result = {"candidate": generated}
            native_result["readback"] = self._publish_isolated(candidate_target, target, baseline, owner=owner, report=native_result,
                                                                extra_cleanup=(candidate_schematic,))
            native_result["transaction"] = "isolated_schematic_copy_symbol_generation_publication"
            return native_result
        finally:
            self._remove_view(candidate_target)
            self._remove_view(candidate_schematic)

    def _call(self, name, *args, **kwargs):
        method = getattr(self.api, name, None)
        if not callable(method):
            raise OperationError("Required symbol native API is unavailable", stage="symbol", code="api_missing", details={"api": name})
        return require(method(*args, **kwargs), name, "symbol")

    def open(self, target, mode="r"):
        return self._call("dbOpenCV", target["library"], target["cell"], target.get("view", "symbol"), mode)

    def close(self, design):
        self._call("dbCloseCV", design)

    def exists(self, target):
        views = getattr(self.api, "dbGetAllViews", lambda *_: [])(target["library"], target["cell"]) or []
        return target.get("view", "symbol") in {str(item) for item in views}

    def read(self, target):
        existing = _safe_attr(self.api, "dbFindCV")
        design = existing(target["library"], target["cell"], target.get("view", "symbol")) if callable(existing) else None
        opened = design is not None
        if design is None:
            design = self.open(target)
        try:
            counter_api = _safe_attr(self.api, "dbGetDesignCounter")
            initial_counter = counter_api(design) if callable(counter_api) else None
            issues = []
            if list(_attr(design, "instances", default=()) or ()):
                issues.append({"kind": "symbol_instances", "reason": "nested symbol geometry is not expanded"})
            pin_figures = _term_pin_figures(design)
            terms = []
            for term in _attr(design, "terminals", default=()) or ():
                pins = _attr(term, "pins", default=()) or ()
                all_figures = [item for pin in pins for item in _attr(pin, "figs", default=()) or ()]
                if len(all_figures) != 1:
                    issues.append({"kind": "pin_geometry", "name": _text(_attr(term, "name")), "reason": "multiple or missing native pin figures"})
                figure = None
                for pin in pins:
                    figures = _attr(pin, "figs", "fig", default=()) or ()
                    if figures:
                        figure = list(figures)[0]
                        break
                box = _bbox(_attr(figure, "listBbox", "bBox", "bbox", default=None)) if figure is not None else None
                position = None
                if box is not None:
                    position = [round((box[0] + box[2]) / 2, 9), round((box[1] + box[3]) / 2, 9)]
                terms.append({
                    "name": _text(_attr(term, "name", "getName", default=""), ""),
                    "direction": _text(_attr(term, "direction", default=""), ""),
                    "num_bits": int(_attr(term, "numBits", "num_bits", default=1) or 1),
                    "bbox": box,
                    "position": position,
                    "properties": _native_properties(term),
                })
            shapes = []
            labels = []
            selection_boxes = []
            for item in _attr(design, "shapes", default=()) or ():
                obj_type = _obj_type(item).casefold()
                layer, purpose = _lpp(item)
                if obj_type in {"text", "evaltext"}:
                    labels.append(_label_record(item))
                    continue
                record = _shape_record(item, pin_figures)
                if obj_type == "rect" and (layer, purpose) == ("instance", "drawing"):
                    if record["bbox"] is not None:
                        selection_boxes.append(record["bbox"])
                    continue
                if record["is_pin_figure"]:
                    continue
                shapes.append(record)
                if record["obj_type"] not in {"line", "rect", "polygon", "ellipse", "circle", "arc"} or record["bbox"] is None:
                    issues.append({"kind": "drawing", "object_type": record["obj_type"], "reason": "native geometry is not completely represented"})
            order = [item["name"] for item in terms]
            owner = self.api.dbGetPropByName(design, "aetherpilot_owner")
            final_counter = counter_api(design) if callable(counter_api) else None
            if initial_counter is None or initial_counter != final_counter:
                issues.append({"kind": "source", "reason": "native design revision is unavailable or changed during readback"})
            if any(term["position"] is None or not term["name"] or type(term["num_bits"]) is not int or term["num_bits"] < 1 for term in terms):
                issues.append({"kind": "terminal", "reason": "terminal identity or pin location is incomplete"})
            result = {
                "schema_version": 1,
                "owner": str(owner.value) if owner is not None else None,
                "properties": _native_properties(design),
                "content_ignored_revision_properties": sorted(SYMBOL_REVISION_PROPERTIES),
                "source": {"design_counter": final_counter, "modified": _attr(design, "isModified")},
                "target": {"library": str(_attr(design, "libName", default=target["library"])),
                            "cell": str(_attr(design, "cellName", default=target["cell"])),
                            "view": str(_attr(design, "viewName", default=target.get("view", "symbol")))},
                "terms": terms,
                "labels": sorted(labels, key=lambda item: (item.get("text", ""), item.get("position") or [])),
                "shapes": shapes,
                "selection_boxes": sorted(selection_boxes),
                "pin_order": order,
                "port_order": order,
                "term_order": order,
                "completeness": {"status": "partial" if issues else "complete", "issues": issues},
            }
            result["content_sha256"] = digest(symbol_content(result))
            return result
        finally:
            if not opened:
                self.close(design)

    def _open_write(self, plan, target):
        exists = self.exists(target)
        if plan["mode"] == "modify" and not exists:
            raise OperationError("Symbol modify requires an existing view", stage="ownership", code="target_missing")
        if exists and not plan.get("overwrite", False) and plan["mode"] == "create":
            raise OperationError("Symbol target already exists", stage="ownership", code="target_exists")
        if exists and _safe_attr(self.api, "dbFindCV") and self.api.dbFindCV(target["library"], target["cell"], target.get("view", "symbol")) is not None:
            raise OperationError("Symbol target is already open", stage="session", code="user_view_open")
        if exists and plan["mode"] == "create":
            method = getattr(self.api, "dbOpenCVByType", None)
            if callable(method):
                return self._call("dbOpenCVByType", target["library"], target["cell"], target.get("view", "symbol"), "symbol", "w")
            return self._call("dbOpenCV", target["library"], target["cell"], target.get("view", "symbol"), "w")
        if exists:
            return self.open(target, "a")
        method = getattr(self.api, "dbNewCV", None)
        if not callable(method):
            raise OperationError("Required symbol native API is unavailable", stage="symbol", code="api_missing", details={"api": "dbNewCV"})
        design = method(target["library"], target["cell"], target.get("view", "symbol"), "symbol")
        if not design:
            raise OperationError("Native symbol view creation returned an invalid handle", stage="symbol", code="native_false",
                                 details={"target": target, "views": [str(item) for item in self.api.dbGetAllViews(target["library"], target["cell"]) or []],
                                          "open": bool(self.api.dbFindCV(target["library"], target["cell"], target.get("view", "symbol")))})
        return design

    def _create_drawing(self, design, item):
        kind = item["kind"]
        if kind == "line":
            return self._call("dbCrtLine", cv=design, points=item["points"], layer=item["layer"], purpose=item["purpose"])
        if kind == "rect":
            return self._call("dbCrtRect", cv=design, bbox=tuple(item["bbox"]), layer=item["layer"], purpose=item["purpose"])
        if kind == "polygon":
            return self._call("dbCrtPolygon", cv=design, points=item["points"], layer=item["layer"], purpose=item["purpose"])
        if kind == "ellipse":
            return self._call("dbCrtEllipse", cv=design, box=tuple(item["bbox"]), layer=item["layer"], purpose=item["purpose"])
        if kind == "circle":
            return self._call("dbCrtCircle", cv=design, center=tuple(item["center"]), radius=item["radius"], layer=item["layer"], purpose=item["purpose"], type=item["circle_type"], quadrant=item["quadrant"], sides=item["sides"])
        if kind == "arc":
            method = "dbCrtArc" if callable(getattr(self.api, "dbCrtArc", None)) else "dbCrtSymbolArc"
            return self._call(method, cv=design, points=item["points"], layer=item["layer"], purpose=item["purpose"])
        raise OperationError("Unsupported symbol drawing", stage="symbol", code="unsupported_shape")

    def _create_label(self, design, item):
        if item.get("semantic") == "pin_name":
            return self._call("dbCrtLabel", cv=design, point=tuple(item["position"]), textName=item["text"], height=item["height"], layer="pin", purpose="label", orient=item["orientation"], justify=item["justification"], font=item["font"], draft=item["draft"])
        if item.get("semantic"):
            return self._call("dbCrtSymbolLabel", cv=design, point=tuple(item["position"]), label=item["text"], labelType=item["label_type"], height=item["height"], orient=item["orientation"], justify=item["justification"], font=item["font"], draft=item["draft"], overbar=item["overbar"])
        return self._call("dbCrtLabel", cv=design, point=tuple(item["position"]), textName=item["text"], height=item["height"], layer=item["layer"], purpose=item["purpose"], orient=item["orientation"], justify=item["justification"], font=item["font"], draft=item["draft"])

    def _create_pin(self, design, item):
        direction = item["direction"] if item["direction"] in {"input", "output", "inputOutput", "switch"} else "inputOutput"
        pin = self._call("dbCrtSymbolPin", cv=design, pinName=item["name"], point=tuple(item["position"]), dir=direction, shapeMode=item["shape_mode"], labelOffset=item["label_offset"], showName=item["show_name"], overbar=item["overbar"], orient=item["orientation"], isChanged=item["is_changed"])
        if "half_size" in item:
            horizontal, vertical = item["position"]
            half_size = item["half_size"]
            figures = list(pin.figs)
            if len(figures) != 1 or str(figures[0].objType).casefold() != "rect":
                raise OperationError("Native square pin has unexpected geometry", stage="symbol", code="native_terminal_mismatch")
            bounds = (horizontal - half_size, vertical - half_size, horizontal + half_size, vertical + half_size)
            scaled = [value * design.dbuPerUU for value in bounds]
            if any(abs(value - round(value)) > 1e-7 for value in scaled):
                raise ValidationError("Pin size is not representable on the native database grid", stage="symbol", code="invalid_geometry")
            figures[0].setBBox(self.api.emyBox(*[round(value) for value in scaled]))
        if direction != item["direction"]:
            pin.term.direction = item["direction"]
            if str(pin.term.direction) != item["direction"]:
                raise OperationError("Native symbol terminal type differs", stage="symbol", code="native_terminal_mismatch")
        return pin

    def _save(self, design, report):
        consistent = _safe_attr(design, "isConsistent")
        if callable(consistent) and not consistent():
            raise ValidationError("Native symbol is inconsistent", stage="check_save", code="native_inconsistent")
        checker = getattr(self.api, "dbCheckAndSaveDesign", None)
        if callable(checker):
            self._call("dbCheckAndSaveDesign", design, str(report))
        else:
            self._call("dbSaveCV", design)

    def apply(self, plan, target, owner, report, *, baseline=None):
        plan = validate_symbol_plan(plan)
        target = self._guard_target(target)
        design = self._open_write(plan, target)
        try:
            self._call("dbCrtProp", design, "aetherpilot_owner", owner, "string")
            by_kind = {item["name"]: item for item in plan["shapes"] if item["kind"] == "pin"}
            for item in plan["shapes"]:
                if item["kind"] in {"line", "rect", "polygon", "ellipse", "circle", "arc"}:
                    self._create_drawing(design, item)
                elif item["kind"] == "label":
                    self._create_label(design, item)
                elif item["kind"] == "selection_box":
                    self._call("dbCrtSymbolSelBox", cv=design, bbox=tuple(item["bbox"]))
            for name in plan["term_order"]:
                self._create_pin(design, by_kind[name])
            self._save(design, report)
        finally:
            self.close(design)
        actual = self.read(target)
        if isinstance(report, (str, Path)):
            atomic_json(Path(report).with_name("symbol-candidate-readback.json"), actual)
        if baseline is None:
            compare_symbol_plan(plan, actual)
        else:
            from .symbol_append import compare_symbol_append

            compare_symbol_append(plan, baseline, actual)
        return actual

    def generate_from_schematic(self, target, *, schematic_view="schematic", symbol_view="symbol", sides=None, symbol_shape="transmission", pin_space=0.125, pin_length=0.25, pin_shape="round", pin_origin="topLeft", overwrite=False, sort_pins=None):
        source = {"library": target["library"], "cell": target["cell"], "view": schematic_view}
        if not self.exists({**source, "view": schematic_view}):
            raise OperationError("Source schematic does not exist", stage="preflight", code="target_missing")
        schematic = self.open(source)
        try:
            terminals = []
            for item in _attr(schematic, "terminals", default=()) or ():
                boxes = [_bbox(_attr(figure, "listBbox", "bBox", "bbox")) for pin in _attr(item, "pins", default=()) or () for figure in _attr(pin, "figs", default=()) or ()]
                box = next((value for value in boxes if value is not None), None)
                terminals.append({"name": _text(_attr(item, "name", default=""), ""), "direction": _text(_attr(item, "direction", default="inputOutput"), "inputOutput"),
                                  "position": [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2] if box else None})
        finally:
            self.close(schematic)
        names = [item["name"] for item in terminals]
        side_map = {item["name"]: default_symbol_side(item["name"], item["direction"]) for item in terminals}
        if sides is not None:
            if not isinstance(sides, dict):
                raise ValidationError("symbol sides must be an object", stage="plan", code="invalid_symbol_sides")
            for side, side_names in sides.items():
                if side not in {"left", "right", "top", "bottom"} or not isinstance(side_names, list):
                    raise ValidationError("symbol sides are malformed", stage="plan", code="invalid_symbol_sides")
                for name in side_names:
                    if name not in names:
                        raise ValidationError("symbol side refers to an unknown terminal", stage="plan", code="invalid_symbol_sides")
                    side_map[name] = side
        grouped = {side: sorted_symbol_pins(terminals, [name for name in names if side_map[name] == side], side, sort_pins) for side in ("left", "right", "top", "bottom")}
        if self.exists({"library": target["library"], "cell": target["cell"], "view": symbol_view}) and not overwrite:
            raise OperationError("Symbol target already exists", stage="ownership", code="target_exists")
        if getattr(self.api, "dbFindCV", lambda *_: None)(target["library"], target["cell"], symbol_view) is not None:
            raise OperationError("Symbol target is already open", stage="session", code="user_view_open")
        self._call("dbCrtSymbol", target["library"], target["cell"], schematic_view, symbol_view,
                    leftPins=grouped["left"], rightPins=grouped["right"], topPins=grouped["top"], bottomPins=grouped["bottom"],
                    symbolShape=symbol_shape, pinSpace=pin_space, pinLength=pin_length, pinShape=pin_shape, pinOrigin=pin_origin,
                    overwrite=overwrite, keepPinsOrder=True)
        actual = self.read({"library": target["library"], "cell": target["cell"], "view": symbol_view})
        actual_names = [item["name"] for item in actual["terms"]]
        if actual_names != names and set(actual_names) != set(names):
            raise ValidationError("Generated symbol terminal set differs from schematic", stage="readback", code="symbol_readback_mismatch")
        actual_directions = {item["name"]: item["direction"] for item in actual["terms"]}
        expected_directions = {item["name"]: item["direction"] for item in terminals}
        if actual_directions != expected_directions:
            raise ValidationError("Generated symbol terminal directions differ from schematic", stage="readback", code="symbol_readback_mismatch")
        side_issues = symbol_pin_side_issues(actual, grouped)
        if side_issues:
            raise ValidationError("Generated symbol pin positions or labels differ from the requested sides", stage="readback",
                                  code="symbol_side_mismatch", details={"issues": side_issues[:20]})
        if sort_pins is not None:
            for side, expected in grouped.items():
                if sorted_symbol_pins(actual["terms"], expected, side, "geometric") != expected:
                    raise ValidationError("Generated pin placement differs from the requested order", stage="readback", code="symbol_readback_mismatch", details={"side": side})
        return {"action": "replaced" if overwrite else "created", "schematic_view": schematic_view, "symbol_view": symbol_view, "terminal_names": actual_names, "pin_order": actual["pin_order"],
                "sort_pins": sort_pins, "side_orders": grouped, "sorting_scope": "operation_local_no_global_environment_change", "readback": actual}
