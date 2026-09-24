"""Local PyAether implementation of library and cell-category operations."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .artifacts import atomic_json, file_digest
from .errors import OperationError, PreflightError, ValidationError
from .library import LibraryInfo, category_readback, library_name
from .library_safety import LibraryGuard, writable_library_path


def _text(value, default=None):
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


class LibraryMetadataOps:
    def __init__(self, api, *, workspace=None):
        self.api = api
        self.guard = LibraryGuard(workspace)

    @contextmanager
    def mutation(self, name, action, *, approved=False):
        info = self.get(name)
        location = self.guard.require(name, info.path, approved=approved)
        for cell in info.cells:
            for view in self.api.dbGetAllViews(name, cell) or ():
                if self.api.dbFindCV(name, cell, str(view)) is not None:
                    raise OperationError("Library has an open view", stage="ownership", code="user_view_open")
        transaction = self.guard.backup(name, location, action)
        self.active_transaction = transaction
        try:
            yield info
        except Exception:
            self.guard.finish(transaction, success=False)
            raise
        else:
            self.guard.finish(transaction, success=True)
        finally:
            self.active_transaction = None

    def _refresh(self):
        refresh = getattr(self.api, "dbRefreshLibList", None)
        if refresh is not None:
            refresh()

    def list(self):
        values = self.api.dbGetAllLibs()
        if values is None:
            raise PreflightError("Native library enumeration returned no result", stage="preflight", code="library_query_failed")
        return sorted({_text(item) for item in values if _text(item)})

    def _library_object(self, name):
        getter = getattr(self.api, "dbGetLibIdByLibName", None)
        value = getter(name) if getter is not None else None
        if value is None:
            getter = getattr(self.api, "dmGetObj", None)
            value = getter(name) if getter is not None else None
        return value

    def _path(self, value):
        for name in ("writePath", "path", "readPath"):
            try:
                candidate = getattr(value, name)
                if candidate:
                    return str(candidate)
            except Exception:
                pass
        try:
            target = self.api.emyString()
            value.getPath(target)
            return str(target) or None
        except Exception:
            return None

    def _technology(self, name, library_object):
        getter = getattr(self.api, "techGetTechFile", None)
        if getter is None:
            return None
        tech = getter(name, "r")
        return str(tech.libName) if tech is not None and getattr(tech, "libName", None) else None

    def get(self, name):
        library_name(name)
        if name not in self.list():
            raise OperationError("Library does not exist", stage="read", code="library_missing")
        native = self._library_object(name)
        cells = getattr(self.api, "dbGetAllCells", lambda value: ()) (name) or ()
        return LibraryInfo(name=name, path=self._path(native), technology_library=self._technology(name, native),
                           cells=tuple(sorted(_text(item) for item in cells if _text(item))))

    def create(self, name, *, path=None, technology_library=None):
        library_name(name)
        destination = writable_library_path(name, path or self.guard.workspace / name, self.guard.workspace)
        if name in self.list():
            raise OperationError("Library already exists", stage="ownership", code="library_exists")
        if not destination.exists():
            destination.mkdir(mode=0o700, parents=True)
        elif not destination.is_dir() or any(destination.iterdir()):
            raise OperationError("Library destination must be an empty directory", stage="ownership", code="destination_exists")
        kwargs = {"lib": name, "path": str(destination), "splicePath": False}
        if technology_library is not None:
            if technology_library not in self.list():
                raise OperationError("Technology library does not exist", stage="preflight", code="technology_missing")
            kwargs["attachTechLib"] = technology_library
        result = self.api.dbCreateLib(**kwargs)
        if result is None:
            raise OperationError("Native library creation returned no object", stage="library", code="native_false")
        self._refresh()
        if name not in self.list():
            raise OperationError("Created library was not visible after refresh", stage="readback", code="library_create_mismatch")
        info = self.get(name)
        if Path(info.path).resolve() != destination or (technology_library is not None and info.technology_library != technology_library):
            raise OperationError("Created library path or technology differs", stage="readback", code="library_create_mismatch")
        self.guard.register(name, destination)
        return info

    def delete(self, name, *, approved=False):
        if approved is not True:
            raise OperationError("Library deletion requires explicit approval", stage="ownership", code="approval_required")
        with self.mutation(name, "delete", approved=approved) as info:
            result = self.api.dbDelLib([name], True)
            if not result:
                raise OperationError("Native library deletion failed", stage="library", code="native_false")
            self._refresh()
            if name in self.list() or Path(info.path).exists():
                raise OperationError("Deleted library is still visible after refresh", stage="readback", code="library_delete_mismatch")
            self.guard.register(name, info.path, state="deleted")
        return {"deleted": name}

    def rename(self, name, new_name, *, approved=False):
        if approved is not True:
            raise OperationError("Library rename requires explicit approval", stage="ownership", code="approval_required")
        self.get(name)
        library_name(new_name)
        writable_library_path(new_name, self.guard.workspace / new_name, self.guard.workspace)
        if new_name in self.list():
            raise OperationError("Destination library already exists", stage="ownership", code="destination_exists")
        if not hasattr(self.api, "emyLibDef"):
            raise PreflightError("Native library rename API is unavailable", stage="preflight", code="api_missing")
        namespace = getattr(self.api, "emyCdbaNS", lambda: None)()
        if namespace is None:
            namespace = getattr(self.api, "emyNativeNS", lambda: None)()
        if namespace is None:
            raise PreflightError("Native name namespace API is unavailable", stage="preflight", code="api_missing")
        source = self.api.emyScalarName(namespace, name)
        destination = self.api.emyScalarName(namespace, new_name)
        with self.mutation(name, "rename", approved=approved) as before:
            definitions = self.api.emyDMGetDefaultLibDefList("a")
            original = self.api.emyLibDef.find(definitions, source)
            if original is None:
                raise OperationError("Library definition is not in the writable local list", stage="ownership", code="library_definition_missing")
            definition_path = self.api.emyString()
            definitions.getPath(definition_path)
            definition_path = Path(str(definition_path)).absolute()
            if definition_path != definition_path.resolve() or self.guard.workspace not in definition_path.parents or not definition_path.is_file():
                raise OperationError("Library definition is not local and safe", stage="ownership", code="protected_library_path")
            directory, recovery = self.active_transaction
            mapping_backup = directory / "library-definition.backup"
            mapping_backup.write_bytes(definition_path.read_bytes())
            mapping_backup.chmod(0o600)
            recovery["library_definition"] = {"path": str(definition_path), "backup": str(mapping_backup), "sha256": file_digest(mapping_backup),
                                               "source": name, "destination": new_name, "physical_directory": "preserved"}
            atomic_json(directory / "transaction.json", recovery)
            categories = CategoryOps(self.api)
            category_before = {category: categories.read(name, category) for category in categories.list(name)}
            lib_path, write_path = self.api.emyString(), self.api.emyString()
            original.getLibPath(lib_path)
            original.getLibWritePath(write_path)
            attributes = self.api.emyDMAttrArray()
            original.getLibAttributes(attributes)
            mode = original.getLibMode()
            created = self.api.emyLibDef.create(definitions, destination, lib_path, write_path, mode, attributes, original)
            if created is None:
                raise OperationError("Native library definition creation failed", stage="library", code="native_false")
            original.destroy()
            if file_digest(definition_path) != recovery["library_definition"]["sha256"]:
                raise OperationError("Library definitions changed before rename", stage="ownership", code="baseline_changed")
            definitions.save()
            self._library_object(name).close()
            old_top = Path(before.path) / (name + ".TopCat")
            new_top = Path(before.path) / (new_name + ".TopCat")
            if new_top.exists():
                raise OperationError("Renamed top category destination already exists", stage="library", code="destination_exists")
            if old_top.exists():
                old_top.rename(new_top)
            self._refresh()
            if name in self.list() or new_name not in self.list():
                raise OperationError("Renamed library failed readback verification", stage="readback", code="library_rename_mismatch")
            actual = self.get(new_name)
            if actual.cells != before.cells or actual.technology_library != before.technology_library:
                raise OperationError("Library rename lost cells or technology", stage="readback", code="library_rename_mismatch")
            if categories.list(new_name) != sorted(category_before):
                raise OperationError("Library rename lost category membership", stage="readback", code="library_rename_mismatch")
            for category, expected in category_before.items():
                if categories.read(new_name, category) != {**expected, "library": new_name}:
                    raise OperationError("Library rename changed a category", stage="readback", code="library_rename_mismatch")
            expected_inventory = self.guard.capture(Path(before.path))
            expected_files = [dict(entry, path=(new_name + ".TopCat" if entry["path"] == name + ".TopCat" else entry["path"])) for entry in recovery["baseline"]["files"]]
            if sorted(expected_files, key=lambda entry: entry["path"]) != expected_inventory["files"] or expected_inventory["directories"] != recovery["baseline"]["directories"]:
                raise OperationError("Library rename changed native file contents", stage="readback", code="library_rename_mismatch")
            self.guard.register(new_name, actual.path)
            self.guard.register(name, before.path, state="renamed")
        return actual

    def set_technology(self, name, technology_library, *, approved=False):
        self.get(name)
        if technology_library not in self.list():
            raise OperationError("Technology library does not exist", stage="preflight", code="technology_missing")
        with self.mutation(name, "set_technology", approved=approved):
            setter = getattr(self.api, "techSetTechLibName", None)
            if setter is None or not setter(name, technology_library):
                raise OperationError("Native technology binding failed", stage="library", code="native_false")
            self._refresh()
            info = self.get(name)
            if info.technology_library != technology_library:
                raise OperationError("Technology binding failed readback verification", stage="readback", code="technology_mismatch")
        return info


class CategoryOps:
    def __init__(self, api):
        self.api = api
        self.libraries = LibraryMetadataOps(api)

    def _ensure_library(self, library):
        if not getattr(self.api, "dbLibExist", lambda *_: False)(library):
            raise OperationError("Library does not exist", stage="preflight", code="library_missing")

    def list(self, library):
        self._ensure_library(library)
        values = self.api.dbCatGetLibCats(library) or ()
        return sorted(_text(value) for value in values if _text(value))

    def _find(self, library, category):
        self._ensure_library(library)
        finder = getattr(self.api, "dbCatFind", None)
        value = finder(library, category) if finder is not None else None
        if value is None:
            raise OperationError("Category does not exist", stage="read", code="category_missing")
        return value

    def read(self, library, category):
        native = self._find(library, category)
        cells = self.api.dbCatGetCells(native) or ()
        subcategories = getattr(self.api, "dbCatGetSubCats", lambda *_: ()) (native) or ()
        return category_readback(library, category, [_text(value) for value in cells], [_text(value) for value in subcategories])

    def create(self, library, category, cells=(), *, approved=False):
        if category in self.list(library):
            raise OperationError("Category already exists", stage="ownership", code="category_exists")
        for cell in cells:
            if cell not in [str(value) for value in self.api.dbGetAllCells(library) or ()]:
                raise OperationError("Category member cell does not exist", stage="plan", code="cell_missing", details={"cell": cell})
        with self.libraries.mutation(library, "category.create", approved=approved):
            if not self.api.dbCatNew(library, name=category, cells=list(cells)):
                raise OperationError("Native category creation failed", stage="library", code="native_false")
            actual = self.read(library, category)
            if actual["cells"] != sorted(set(cells)):
                raise OperationError("Created category failed readback verification", stage="readback", code="category_create_mismatch")
        return actual

    def delete(self, library, category, *, approved=False):
        if approved is not True:
            raise OperationError("Category deletion requires explicit approval", stage="ownership", code="approval_required")
        with self.libraries.mutation(library, "category.delete", approved=approved):
            native = self._find(library, category)
            if not self.api.dbCatDelete(native):
                raise OperationError("Native category deletion failed", stage="library", code="native_false")
            if category in self.list(library):
                raise OperationError("Deleted category is still visible after readback", stage="readback", code="category_delete_mismatch")
        return {"library": library, "category": category, "deleted": True}

    def cells(self, library, category):
        return self.read(library, category)["cells"]

    def _replace_cells(self, library, category, cells, *, approved=False):
        with self.libraries.mutation(library, "category.membership", approved=approved):
            native = self._find(library, category)
            before = self.read(library, category)
            current = set(before["cells"])
            desired = set(cells)
            for cell in sorted(current - desired):
                native.removeCell(cell)
            for cell in sorted(desired - current):
                native.addCell(cell)
            if not self.api.dbCatSave(native):
                raise OperationError("Native category membership save failed", stage="library", code="native_false")
            actual = self.read(library, category)
            if actual["cells"] != sorted(desired) or actual["subcategories"] != before["subcategories"]:
                raise OperationError("Category membership failed readback verification", stage="readback", code="category_membership_mismatch")
        return actual

    def add(self, library, category, cell, *, approved=False):
        if cell not in [str(value) for value in self.api.dbGetAllCells(library) or ()]:
            raise OperationError("Category member cell does not exist", stage="plan", code="cell_missing")
        current = self.cells(library, category)
        return self._replace_cells(library, category, current + [cell], approved=approved)

    def remove(self, library, category, cell, *, approved=False):
        current = self.cells(library, category)
        if cell not in current:
            raise OperationError("Category member cell does not exist in category", stage="plan", code="category_member_missing")
        return self._replace_cells(library, category, [value for value in current if value != cell], approved=approved)

    def rename(self, library, category, new_name, *, approved=False):
        if approved is not True:
            raise OperationError("Category rename requires explicit approval", stage="ownership", code="approval_required")
        source = self.read(library, category)
        if source["subcategories"]:
            raise OperationError("Nested categories cannot be renamed by the flat V1 operation", stage="plan", code="nested_category_unsupported")
        if new_name in self.list(library):
            raise OperationError("Destination category already exists", stage="ownership", code="destination_exists")
        self.create(library, new_name, source["cells"], approved=approved)
        try:
            self.delete(library, category, approved=True)
        except Exception as exc:
            raise OperationError("Category rename partially succeeded; destination was preserved", stage="library", code="partial_success",
                                 details={"library": library, "source": category, "destination": new_name}) from exc
        return self.read(library, new_name)


__all__ = ["LibraryMetadataOps", "CategoryOps"]
