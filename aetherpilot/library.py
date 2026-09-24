"""Declarative validation and stable readback models for local library metadata."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .errors import ValidationError


_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$-]{0,127}$")


def library_name(value, field="library"):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValidationError(f"Invalid {field} name", stage="plan", code="invalid_identifier")
    return value


def optional_path(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError("Library path must be a non-empty path", stage="plan", code="invalid_library_path")
    return value


@dataclass(frozen=True)
class LibraryInfo:
    name: str
    path: str | None
    technology_library: str | None
    cells: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self) | {"cells": list(self.cells)}


def validate_library_request(source, *, operation):
    if not isinstance(source, dict):
        raise ValidationError("Library request must be an object", stage="plan", code="invalid_library_request")
    if operation == "library.create":
        allowed = {"name", "path", "technology_library"}
        required = {"name"}
    elif operation in {"library.delete", "library.get"}:
        allowed = {"name", "approved"} if operation == "library.delete" else {"name"}
        required = {"name"}
    elif operation == "library.rename":
        allowed = {"name", "new_name", "approved"}
        required = {"name", "new_name"}
    elif operation == "library.set_technology":
        allowed = {"name", "technology_library", "approved"}
        required = {"name", "technology_library"}
    else:
        raise ValidationError("Unsupported library request", stage="plan", code="invalid_library_request")
    if set(source) - allowed or not required.issubset(source):
        raise ValidationError("Library request fields are invalid", stage="plan", code="invalid_library_request")
    value = dict(source)
    library_name(value["name"])
    if "new_name" in value:
        library_name(value["new_name"], "new library")
        if value["new_name"] == value["name"]:
            raise ValidationError("Library rename destination must differ", stage="plan", code="invalid_library_request")
    if "technology_library" in value and value["technology_library"] is not None:
        library_name(value["technology_library"], "technology library")
    if "path" in value:
        value["path"] = optional_path(value["path"])
    if "approved" in value and type(value["approved"]) is not bool:
        raise ValidationError("Destructive library operations require a boolean approval", stage="plan", code="approval_required")
    return value


def validate_category_request(source, *, operation):
    if not isinstance(source, dict):
        raise ValidationError("Category request must be an object", stage="plan", code="invalid_category_request")
    allowed = {
        "category.list": {"library"},
        "category.create": {"library", "category", "cells", "approved"},
        "category.delete": {"library", "category", "approved"},
        "category.cells": {"library", "category"},
        "category.add_cell": {"library", "category", "cell", "approved"},
        "category.remove_cell": {"library", "category", "cell", "approved"},
        "category.rename": {"library", "category", "new_name", "approved"},
    }.get(operation)
    if allowed is None or set(source) - allowed:
        raise ValidationError("Category request fields are invalid", stage="plan", code="invalid_category_request")
    required = {"library"}
    if operation != "category.list":
        required.add("category")
    if operation in {"category.add_cell", "category.remove_cell"}:
        required.add("cell")
    if operation == "category.rename":
        required.add("new_name")
    if not required.issubset(source):
        raise ValidationError("Category request is missing required fields", stage="plan", code="invalid_category_request")
    value = dict(source)
    for key in ("library", "category", "new_name", "cell"):
        if key in value:
            library_name(value[key], key)
    if "cells" in value:
        if not isinstance(value["cells"], list) or len(value["cells"]) > 10000:
            raise ValidationError("Category cells must be a bounded list", stage="plan", code="invalid_category_request")
        value["cells"] = [library_name(item, "cell") for item in value["cells"]]
        if len(set(value["cells"])) != len(value["cells"]):
            raise ValidationError("Category cells must be a unique list", stage="plan", code="invalid_category_request")
    if "approved" in value and type(value["approved"]) is not bool:
        raise ValidationError("Destructive category operations require a boolean approval", stage="plan", code="approval_required")
    if "new_name" in value and value["new_name"] == value["category"]:
        raise ValidationError("Category rename destination must differ", stage="plan", code="invalid_category_request")
    return value


def category_readback(library, category, cells=(), subcategories=()):
    return {
        "library": library_name(library),
        "category": library_name(category, "category"),
        "cells": sorted(set(cells)),
        "subcategories": sorted(set(subcategories)),
    }


__all__ = [
    "LibraryInfo", "library_name", "validate_library_request", "validate_category_request", "category_readback",
]
