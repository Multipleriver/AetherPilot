"""Zero-dependency offline document index and search for agent self-help.

Builds a SQLite FTS5 index over text-extractable sources (``.py``/``.pyi``/
``.md``/``.txt``/``.rst``/``.cdl``/``.sp``) under caller-supplied roots, for
example the PyAether package tree and lab documentation.  Binary formats
(PDF/PPTX) cannot be parsed with the standard library and are reported as
skipped, never silently ignored.  Everything is local; no database or
simulator is touched.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from .errors import ValidationError

_TEXT_SUFFIXES = {".py", ".pyi", ".md", ".txt", ".rst", ".cdl", ".sp", ".spi", ".lib", ".json", ".yaml", ".yml", ".csv", ".tcl", ".il", ".ils", ".ini", ".def", ".cfg", ".log"}
_CHUNK_LINES = 40
_CHUNK_OVERLAP = 10


def _fts5_available(connection):
    try:
        connection.execute("CREATE VIRTUAL TABLE _probe USING fts5(x)")
        connection.execute("DROP TABLE _probe")
        return True
    except sqlite3.OperationalError:
        return False


def _chunks(path, text):
    lines = text.splitlines()
    step = max(1, _CHUNK_LINES - _CHUNK_OVERLAP)
    for start in range(0, len(lines), step):
        yield start + 1, "\n".join(lines[start:start + _CHUNK_LINES])


def build_index(roots, db_path, *, max_files=20000, max_file_bytes=2 * 1024 ** 2):
    """Build a fresh FTS index at db_path (atomically replaced)."""
    if isinstance(roots, (str, Path)):
        roots = [roots]
    roots = [Path(root).expanduser().resolve() for root in roots]
    for root in roots:
        if not root.is_dir():
            raise ValidationError("Document root does not exist", stage="docs", code="doc_root_missing", details={"root": str(root)})
    db_path = Path(db_path).expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    stats = {"roots": [str(root) for root in roots], "files": 0, "chunks": 0, "skipped_large": 0, "skipped_binary_suffix": 0, "skipped_symlink": 0}
    connection = sqlite3.connect(tmp)
    try:
        fts = _fts5_available(connection)
        if fts:
            connection.execute("CREATE VIRTUAL TABLE chunks USING fts5(path, body, tokenize='unicode61')")
        else:
            connection.execute("CREATE TABLE chunks (path TEXT, body TEXT)")
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
                for filename in sorted(filenames):
                    path = Path(dirpath) / filename
                    if path.is_symlink():
                        stats["skipped_symlink"] += 1
                        continue
                    if path.suffix.casefold() not in _TEXT_SUFFIXES:
                        stats["skipped_binary_suffix"] += 1
                        continue
                    if stats["files"] >= max_files or not path.is_file():
                        continue
                    try:
                        raw = path.read_bytes()
                    except OSError:
                        continue
                    if len(raw) > max_file_bytes or b"\x00" in raw[:4096]:
                        stats["skipped_large"] += 1
                        continue
                    relative = path.relative_to(root).as_posix()
                    body = raw.decode("utf-8", "replace")
                    rows = [(relative, chunk) for _, chunk in _chunks(path, body)]
                    connection.executemany("INSERT INTO chunks (path, body) VALUES (?, ?)", rows)
                    stats["files"] += 1
                    stats["chunks"] += len(rows)
        connection.commit()
        connection.execute("PRAGMA optimize") if fts else None
        connection.close()
        os.replace(tmp, db_path)
    finally:
        connection.close()
        tmp.unlink(missing_ok=True)
    stats["engine"] = "fts5" if _fts5_available(sqlite3.connect(db_path)) else "like"
    stats["db"] = str(db_path)
    return stats


def _terms(query):
    return [term for term in re.findall(r"[\w.]+", query) if term.strip(".")]


def search(db_path, query, *, limit=10):
    """Search the index; returns ranked matches with one-line context snippets."""
    db_path = Path(db_path).expanduser().resolve()
    if not db_path.is_file():
        raise ValidationError("Document index does not exist; build it first", stage="docs", code="doc_index_missing")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValidationError("Search limit out of bounds", stage="docs", code="invalid_docs_request")
    terms = _terms(query)
    if not terms:
        raise ValidationError("Search query has no searchable terms", stage="docs", code="invalid_docs_request")
    connection = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "chunks" not in tables:
            raise ValidationError("Document index is malformed", stage="docs", code="doc_index_missing")
        rows = []
        if _fts5_available(connection) and "chunks" in tables:
            try:
                match = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
                cursor = connection.execute(
                    "SELECT path, snippet(chunks, 1, '', '', ' … ', 32), rank FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                    (match, limit))
                rows = cursor.fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            like = "%"
            cursor = connection.execute(
                "SELECT path, body FROM chunks WHERE " + " AND ".join("body LIKE ?" for _ in terms) + " LIMIT ?",
                tuple(like + term + "%" for term in terms) + (limit * 20,))
            rows = [(path, re.sub(r"\s+", " ", body)[:160], None) for path, body in cursor.fetchall()][:limit]
        return {"schema_version": 1, "query": query, "terms": terms, "matches": [
            {"path": path, "snippet": snippet, "rank": rank} for path, snippet, rank in rows], "count": len(rows)}
    finally:
        connection.close()


__all__ = ["build_index", "search"]
