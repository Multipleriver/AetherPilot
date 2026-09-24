"""Bounded native MDE setting-file inspection and clean L2 initialization."""

from __future__ import annotations

import configparser
import io
from pathlib import Path

from .errors import ValidationError


def read_mde_setting(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 ** 2:
        raise ValidationError("Native MDE setting is missing or unsafe", stage="maestro", code="state_corrupt")
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=("=",), comment_prefixes=("#", ";"))
    parser.optionxform = str
    try:
        parser.read_string(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ValidationError("Native MDE setting cannot be parsed", stage="maestro", code="state_corrupt") from exc
    return {name: dict(parser.items(name)) for name in parser.sections()}


def setting_text(sections):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for section, items in sections.items():
        parser[section] = {name: str(value) for name, value in items.items()}
    output = io.StringIO()
    parser.write(output, space_around_delimiters=False)
    return output.getvalue()


def empty_mde_setting():
    return setting_text({
        "Version": {"Version": "2.0"},
        "Other": {"netlistFormat": 2, "simulator": "Alps", "threadNumber": 1},
        "Design": {"LibraryName": "", "CellName": "", "ViewName": "schematic"},
        "Analysis": {"size": 0},
        "Outputs": {"size": 0, "HistoryNumber": 10, "IsOverwrite": 0},
        "Corner": {"1\\CornerName": "Nominal", "1\\CornerTemperature": 27, "1\\isEnabled": 1, "size": 1},
        "ModelSetup": {"size": 0},
        "Parameter": {"size": 0},
        "SweepTable": {"size": 0},
        "SimulatorOptions": {"spectreViewList": "spectreText spectre schematic symbol veriloga", "spectreStopList": "spectreText spectre symbol"},
    })


def native_analysis_entries(sections):
    section = sections.get("Analysis", {})
    try:
        count = int(section.get("size", 0))
        if not 0 <= count <= 100:
            raise ValueError("Analysis budget exceeded")
    except ValueError as exc:
        raise ValidationError("Native analysis list is malformed", stage="maestro", code="state_corrupt") from exc
    result = {}
    for index in range(1, count + 1):
        prefix = str(index) + "\\"
        entry = {name[len(prefix):]: value for name, value in section.items() if name.startswith(prefix)}
        name = entry.get("AnalysisTaskType", "").lower()
        if not name or name in result:
            raise ValidationError("Native analysis list contains missing or repeated types", stage="maestro", code="state_corrupt")
        result[name] = entry
    return result
