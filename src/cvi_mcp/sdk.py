# ==============================================================================
#  cvi_mcp/sdk.py
#  Role     Index separe du SDK NI: prototypes des .h, entrees des .fp
#  Etat     .h fiable, .fp heuristique assumee
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json
import re
import sqlite3
from pathlib import Path

# ============================ Interne =========================================
from . import cvienv
from .cparse import (
    declarator_info,
    file_fingerprint,
    leading_comment,
    neutralize,
    new_parser,
    read_source,
    text_of,
    walk,
)
from .schema import ensure_schema

# Le format des .fp est proprietaire et non documente. On en extrait seulement
# les identifiants imprimables, puis on ne garde que ceux qu'un en-tete
# confirme. Un nom non confirme n'est jamais presente comme une API.
_IDENTIFIER = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{3,63}")
_PLAUSIBLE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z_])[A-Za-z_][A-Za-z0-9_]*$")

_HEADER_GLOBS = ("*.h",)
_PANEL_GLOBS = ("*.fp",)


class SdkIndexer:
    """Ecrit dans les tables sdk_*. Totalement disjoint de l'index projet."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        ensure_schema(self.db)
        self.parser = new_parser()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------- publique ---

    def index(self, roots: list[Path], force: bool = False) -> dict:
        headers, panels, missing = [], [], []
        for root in roots:
            root = Path(root)
            if not root.exists():
                missing.append(str(root))
                continue
            if root.is_file():
                bucket = panels if root.suffix.lower() == ".fp" else headers
                bucket.append(root)
                continue
            for pattern in _HEADER_GLOBS:
                headers.extend(sorted(root.rglob(pattern)))
            for pattern in _PANEL_GLOBS:
                panels.extend(sorted(root.rglob(pattern)))

        header_count = 0
        for path in headers:
            if self._index_header(path, force):
                header_count += 1

        panel_total, panel_confirmed, panel_rejected = 0, 0, 0
        for path in panels:
            confirmed, rejected = self._index_panel(path, force)
            panel_total += 1
            panel_confirmed += confirmed
            panel_rejected += rejected

        self.db.commit()
        prototypes = self.db.execute(
            "SELECT COUNT(*) FROM sdk_symbols WHERE kind = 'prototype'"
        ).fetchone()[0]

        report = {
            "roots": [str(r) for r in roots],
            "headers_indexed": header_count,
            "headers_seen": len(headers),
            "prototypes": prototypes,
            "panel_files": panel_total,
            "panel_entries_confirmed": panel_confirmed,
            "panel_candidates_discarded": panel_rejected,
        }
        if missing:
            report["missing_paths"] = missing
        if panel_rejected:
            report["panel_note"] = (
                "Le format .fp n'est pas documente: seuls les noms confirmes par "
                "un en-tete sont retenus, les autres candidats sont ecartes."
            )
        return report

    # -------------------------------------------------------------- en-tetes --

    def _index_header(self, path: Path, force: bool) -> bool:
        path = path.resolve()
        try:
            text, _encoding, raw = read_source(path)
        except OSError:
            return False
        digest = file_fingerprint(raw)

        row = self.db.execute(
            "SELECT id, hash FROM sdk_files WHERE path = ?", (str(path),)
        ).fetchone()
        if row and row[1] == digest and not force:
            return False

        lines = text.splitlines()
        source = neutralize(text).encode("utf-8")
        tree = self.parser.parse(source)

        file_id = self._upsert(row, str(path), digest, "header")
        for node in walk(tree.root_node):
            if node.type == "declaration":
                self._store_prototype(node, source, lines, file_id)
            elif node.type == "preproc_def":
                name_node = node.child_by_field_name("name")
                value_node = node.child_by_field_name("value")
                if name_node is not None:
                    self._insert(
                        file_id,
                        text_of(source, name_node),
                        "macro",
                        node.start_point[0] + 1,
                        text_of(source, value_node).strip() if value_node else None,
                        None,
                        None,
                    )
        self.db.commit()
        return True

    def _store_prototype(self, node, source: bytes, lines: list[str], file_id: int) -> None:
        declarator = node.child_by_field_name("declarator")
        if declarator is None:
            return
        if declarator.type != "function_declarator" and not any(
            child.type == "function_declarator" for child in walk(declarator)
        ):
            return
        name, params = declarator_info(declarator, source)
        if not name:
            return
        self._insert(
            file_id,
            name,
            "prototype",
            node.start_point[0] + 1,
            " ".join(text_of(source, node).split()).rstrip(";"),
            json.dumps(params, ensure_ascii=False),
            leading_comment(lines, node.start_point[0]),
        )

    # ------------------------------------------------------- panneaux .fp -----

    def _index_panel(self, path: Path, force: bool) -> tuple[int, int]:
        path = path.resolve()
        try:
            raw = path.read_bytes()
        except OSError:
            return 0, 0
        digest = file_fingerprint(raw)

        row = self.db.execute(
            "SELECT id, hash FROM sdk_files WHERE path = ?", (str(path),)
        ).fetchone()
        if row and row[1] == digest and not force:
            return 0, 0

        file_id = self._upsert(row, str(path), digest, "panel")

        candidates = {
            match.group(0).decode("ascii")
            for match in _IDENTIFIER.finditer(raw)
        }
        candidates = {name for name in candidates if _PLAUSIBLE.match(name)}

        confirmed = 0
        for name in sorted(candidates):
            known = self.db.execute(
                "SELECT 1 FROM sdk_symbols WHERE name = ? AND kind = 'prototype' LIMIT 1",
                (name,),
            ).fetchone()
            if not known:
                continue
            self._insert(file_id, name, "panel_entry", 0, None, None, None)
            confirmed += 1

        self.db.commit()
        return confirmed, len(candidates) - confirmed

    # ------------------------------------------------------------ ecritures ---

    def _upsert(self, row, path: str, digest: str, kind: str) -> int:
        if row:
            file_id = row[0]
            self.db.execute("DELETE FROM sdk_symbols WHERE file_id = ?", (file_id,))
            self.db.execute(
                "UPDATE sdk_files SET hash = ?, kind = ? WHERE id = ?",
                (digest, kind, file_id),
            )
            return file_id
        cursor = self.db.execute(
            "INSERT INTO sdk_files (path, hash, kind) VALUES (?, ?, ?)",
            (path, digest, kind),
        )
        return int(cursor.lastrowid)

    def _insert(self, file_id, name, kind, line, signature, params, doc) -> None:
        self.db.execute(
            "INSERT INTO sdk_symbols (file_id, name, kind, line, signature, params, doc) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, name, kind, line, signature, params, doc),
        )


class SdkLookup:
    """Lecture seule sur l'index SDK."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        # Un index SDK jamais construit doit repondre une liste vide avec un
        # conseil, pas une erreur de table absente.
        ensure_schema(self.db)
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def status(self) -> dict:
        files = self.db.execute(
            "SELECT kind, COUNT(*) AS total FROM sdk_files GROUP BY kind"
        ).fetchall()
        return {
            "files": {row["kind"]: row["total"] for row in files},
            "prototypes": self.db.execute(
                "SELECT COUNT(*) FROM sdk_symbols WHERE kind = 'prototype'"
            ).fetchone()[0],
            "panel_entries": self.db.execute(
                "SELECT COUNT(*) FROM sdk_symbols WHERE kind = 'panel_entry'"
            ).fetchone()[0],
        }

    def lookup(self, name: str, exact: bool = True, limit: int = 20) -> list[dict]:
        clause = "s.name = ?" if exact else "s.name LIKE ?"
        needle = name if exact else f"%{name}%"
        rows = self.db.execute(
            "SELECT s.name, s.kind, s.signature, s.params, s.doc, s.line, f.path, f.kind AS src "
            f"FROM sdk_symbols s JOIN sdk_files f ON s.file_id = f.id WHERE {clause} "
            "ORDER BY CASE s.kind WHEN 'prototype' THEN 0 WHEN 'macro' THEN 1 ELSE 2 END, s.name "
            "LIMIT ?",
            (needle, limit),
        ).fetchall()

        out: list[dict] = []
        for row in rows:
            entry = {
                "name": row["name"],
                "kind": row["kind"],
                "header": Path(row["path"]).name,
            }
            if row["signature"]:
                entry["signature"] = row["signature"]
            if row["params"]:
                try:
                    entry["params"] = json.loads(row["params"])
                except json.JSONDecodeError:
                    pass
            if row["doc"]:
                entry["doc"] = row["doc"]
            if row["kind"] == "panel_entry":
                entry["note"] = "expose dans un panneau de fonction"
            out.append(entry)
        return out

    def signatures_for(self, names: list[str]) -> dict[str, str]:
        """Signature SDK des appels externes, pour enrichir get_function."""
        if not names:
            return {}
        holders = ", ".join("?" for _ in names)
        rows = self.db.execute(
            f"SELECT name, signature FROM sdk_symbols WHERE kind = 'prototype' "
            f"AND name IN ({holders})",
            tuple(names),
        ).fetchall()
        return {row["name"]: row["signature"] for row in rows if row["signature"]}


def resolve_roots(configured: list[Path], hints: list[Path] | None = None) -> list[Path]:
    """Chemins explicites s'il y en a, sinon ce que l'installation CVI expose."""
    if configured:
        return configured
    if hints is None:
        return cvienv.find_sdk_dirs()
    return [path for path in hints if path.exists()]
