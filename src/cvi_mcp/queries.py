# ==============================================================================
#  cvi_mcp/queries.py
#  Role     Lectures sur l'index: vue d'ensemble, symboles, usages, appels
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import difflib
import fnmatch
import json
import os
import re
import sqlite3
from pathlib import Path

_FUNCTION_KINDS = ("function", "prototype")

# Origine d'un fichier, voir schema.files.origin.
ORIGINS = ("project", "generated", "library")


LIBC = frozenset(
    ["printf", "sprintf", "snprintf", "fprintf", "scanf", "sscanf", "fscanf", "vsprintf", "vsnprintf", "vprintf", "fopen", "fclose", "fread", "fwrite", "fgets", "fputs", "fputc", "fgetc", "fseek", "ftell", "rewind", "fflush", "feof", "ferror", "remove", "rename", "puts", "putchar", "getchar", "perror", "malloc", "calloc", "realloc", "free", "exit", "abort", "atexit", "atoi", "atol", "atof", "strtol", "strtoul", "strtod", "strtoll", "rand", "srand", "qsort", "bsearch", "abs", "labs", "div", "getenv", "system", "memcpy", "memmove", "memset", "memcmp", "memchr", "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp", "strchr", "strrchr", "strstr", "strlen", "strtok", "strerror", "strspn", "strcspn", "strpbrk", "stricmp", "strnicmp", "strcmpi", "strdup", "strlwr", "strupr", "isalpha", "isdigit", "isalnum", "isspace", "isupper", "islower", "ispunct", "isxdigit", "toupper", "tolower", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "sinh", "cosh", "tanh", "exp", "log", "log10", "pow", "sqrt", "ceil", "floor", "fabs", "fmod", "modf", "frexp", "ldexp", "time", "clock", "difftime", "mktime", "localtime", "gmtime", "asctime", "ctime", "strftime", "setjmp", "longjmp", "signal", "raise", "va_start", "va_end", "va_arg", "assert", "offsetof"]
)


def page(items: list, offset: int = 0, limit: int = 50) -> dict:
    """Tranche d'une liste, avec ce qu'il faut pour demander la suivante."""
    offset = max(int(offset or 0), 0)
    limit = max(int(limit or 0), 1)
    chunk = items[offset : offset + limit]
    following = offset + len(chunk)
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "next_offset": following if following < len(items) else None,
        "items": chunk,
    }


def scope_clause(scope: str | None, column: str = "f.origin") -> tuple[str, list]:
    """Filtre SQL sur l'origine des fichiers. all ou vide: aucun filtre."""
    if not scope or scope == "all":
        return "", []
    wanted = [s.strip() for s in scope.split(",") if s.strip() in ORIGINS]
    if not wanted:
        return "", []
    return f" AND {column} IN ({','.join('?' for _ in wanted)})", wanted


_CAMEL_PARTS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
# Racines abregees courantes: "nb" pour nombre, "max" pour maximum...
_ALIASES = {"nb": "nombre", "nbr": "nombre", "num": "numero", "cfg": "config",
            "conf": "config", "init": "initialisation", "max": "max", "min": "min"}


# Abreviations usuelles dans les noms C: une recherche sur l'une trouve les autres.
_SYNONYMS = (
    {"nb", "nbr", "nbre", "nombre", "num", "number", "count", "cnt"},
    {"max", "maxi", "maximum"},
    {"min", "mini", "minimum"},
    {"cfg", "conf", "config", "configuration"},
    {"init", "initialisation", "initialize"},
    {"msg", "message"},
    {"err", "erreur", "error"},
    {"temp", "temperature"},
    {"cam", "camera"},
)


def _variants(word: str) -> list[str]:
    for group in _SYNONYMS:
        if word in group:
            return sorted(group, key=len)
    return [word]


def split_words(text: str) -> list[str]:
    """MAJTableauxTurbine -> maj, tableaux, turbine; NB_TURBINE -> nombre, turbine."""
    words: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", text):
        for part in _CAMEL_PARTS.findall(chunk):
            lowered = part.lower()
            if lowered.isdigit():
                continue
            words.append(_ALIASES.get(lowered, lowered))
    return words


class QueryEngine:
    """Acces en lecture seule. Les chemins sortent relatifs a la racine pour
    rester lisibles et comparables entre deux versions d'un projet."""

    def __init__(self, db_path: Path, root: Path | None = None) -> None:
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.root = Path(root).resolve() if root else None
        # Code exclu par un #if: ignore par defaut, comme le compilateur.
        self.include_inactive = False
        self.project = None

    def act(self, alias: str = "s") -> str:
        """Filtre SQL sur le code compile."""
        return "" if self.include_inactive else f" AND {alias}.active = 1"

    def match_files(self, pattern: str) -> list[int]:
        """Fichiers designes par un nom, un chemin partiel, un dossier ou un
        motif (Commun/Includes, Commun/Includes/*, *.h, main.c). Insensible a
        la casse et aux separateurs: les chemins Windows sont stockes en \\."""
        needle = pattern.replace("\\", "/").strip().strip("/").lower()
        if not needle:
            return []
        rows = self.db.execute("SELECT id, path FROM files").fetchall()
        rel = {r["id"]: self.rel(r["path"]).lower() for r in rows}
        if any(ch in needle for ch in "*?["):
            hits = [i for i, p in rel.items()
                    if fnmatch.fnmatch(p, needle) or fnmatch.fnmatch(p, "*/" + needle)
                    or fnmatch.fnmatch(p.rsplit("/", 1)[-1], needle)]
            return sorted(hits)
        exact = [i for i, p in rel.items() if p == needle or p.endswith("/" + needle)]
        if exact:
            return sorted(exact)
        folder = [i for i, p in rel.items() if p.startswith(needle + "/") or f"/{needle}/" in "/" + p]
        if folder:
            return sorted(folder)
        return sorted(i for i, p in rel.items() if needle in p)

    def file_ids_clause(self, pattern: str | None, column: str = "f.id") -> tuple[str, list, bool]:
        """Clause SQL pour un filtre de fichier, et False si rien ne correspond."""
        if not pattern:
            return "", [], True
        ids = self.match_files(pattern)
        if not ids:
            return " AND 0", [], False
        return f" AND {column} IN ({','.join('?' for _ in ids)})", ids, True

    def close(self) -> None:
        self.db.close()

    def rel(self, path: str) -> str:
        if not self.root:
            return path
        try:
            return Path(os.path.relpath(Path(path).resolve(), self.root)).as_posix()
        except ValueError:
            return Path(path).as_posix()

    # -------------------------------------------------------- vue d'ensemble --

    def overview(self) -> dict:
        """Totaux par origine de fichier: le code de l'equipe, les en-tetes
        generes par CVI et les bibliotheques tierces ne se melangent pas."""
        files = self.db.execute("SELECT origin, COUNT(*) AS n, SUM(lines) AS l FROM files GROUP BY origin")
        by_origin = {r["origin"]: {"files": r["n"], "lines": r["l"] or 0} for r in files}
        for row in self.db.execute(
            "SELECT f.origin, s.kind, COUNT(*) AS total FROM symbols s "
            f"JOIN files f ON s.file_id = f.id WHERE 1 = 1{self.act()} GROUP BY f.origin, s.kind"
        ):
            by_origin.setdefault(row["origin"], {"files": 0, "lines": 0}).setdefault(
                "symbols", {}
            )[row["kind"]] = row["total"]
        return {
            "root": str(self.root) if self.root else None,
            "by_origin": {k: by_origin[k] for k in ORIGINS if k in by_origin},
            "total_files": sum(v["files"] for v in by_origin.values()),
            "total_lines": sum(v["lines"] for v in by_origin.values()),
            "entry_points": self._locate("name = 'main' AND kind = 'function'"),
        }

    def files(self, scope: str | None = None) -> list[dict]:
        clause, params = scope_clause(scope)
        rows = self.db.execute(
            f"SELECT f.path, f.lines, f.origin FROM files f WHERE 1 = 1{clause} ORDER BY f.path",
            params,
        )
        return [{"path": self.rel(r["path"]), "lines": r["lines"], "origin": r["origin"]} for r in rows]

    def callbacks(self) -> list[dict]:
        """Callbacks, les plus complexes d'abord."""
        rows = self.db.execute(
            "SELECT s.name, s.line_start, s.line_end, s.complexity, f.path FROM symbols s "
            "JOIN files f ON s.file_id = f.id WHERE s.is_callback = 1 AND s.kind = 'function' "
            f"{self.act()} ORDER BY s.complexity DESC, s.name"
        )
        return [
            {"name": r["name"], "file": self.rel(r["path"]), "line": r["line_start"],
             "lines": r["line_end"] - r["line_start"] + 1, "complexity": r["complexity"]}
            for r in rows
        ]

    def static_functions(self) -> list[dict]:
        return self._locate("is_static = 1 AND kind = 'function'")

    def _locate(self, condition: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT s.name, s.line_start, f.path FROM symbols s "
            f"JOIN files f ON s.file_id = f.id WHERE {condition}{self.act()} "
            "ORDER BY f.path, s.line_start"
        ).fetchall()
        return [
            {"name": r["name"], "file": self.rel(r["path"]), "line": r["line_start"]}
            for r in rows
        ]

    def external_calls(self, limit: int = -1) -> list[dict]:
        """Appels dont la cible n'est pas definie dans le projet: SDK ou libc.
        Un prototype d'en-tete de bibliotheque ne rend pas la fonction interne."""
        rows = self.db.execute(
            "SELECT r.name, COUNT(*) AS total FROM refs r "
            f"WHERE r.kind = 'call'{self.act('r')} AND r.name NOT IN "
            "(SELECT s.name FROM symbols s JOIN files f ON s.file_id = f.id "
            " WHERE s.kind = 'function' "
            "    OR (s.kind IN ('prototype', 'macro') AND f.origin != 'library')) "
            "GROUP BY r.name ORDER BY total DESC, r.name LIMIT ?",
            (limit,),
        ).fetchall()
        return [{"name": r["name"], "calls": r["total"]} for r in rows]

    # -------------------------------------------------------------- symboles --

    def list_symbols(
        self,
        kind: str | None = None,
        file: str | None = None,
        name_contains: str | None = None,
        limit: int = 100,
        offset: int = 0,
        scope: str | None = None,
    ) -> list[dict]:
        clause, params = scope_clause(scope)
        query = (
            "SELECT s.name, s.kind, s.line_start, s.line_end, s.signature, s.value, s.active, "
            f"f.path FROM symbols s JOIN files f ON s.file_id = f.id WHERE 1 = 1{clause}{self.act()}"
        )
        if kind:
            query += " AND s.kind = ?"
            params.append(kind)
        if file:
            file_clause, ids, _ok = self.file_ids_clause(file)
            query += file_clause
            params += ids
        if name_contains:
            query += " AND s.name LIKE ?"
            params.append(f"%{name_contains}%")
        query += " ORDER BY f.path, s.line_start LIMIT ? OFFSET ?"
        params += [limit, max(offset, 0)]

        return [self._symbol_row(r) for r in self.db.execute(query, params)]

    def find_symbol(self, name: str, exact: bool = False, kind: str | None = None) -> list[dict]:
        """Recherche tolerante: insensible a la casse, sous-chaine, et requete
        en plusieurs mots ("turbine max") ou chaque mot doit apparaitre."""
        if exact:
            rows = self.db.execute(
                "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
                f"WHERE s.name = ?{self.act()} ORDER BY s.active DESC, f.path, s.line_start",
                (name,),
            ).fetchall()
        else:
            words = [w for w in re.split(r"[\s,]+", name.strip()) if w] or [name]
            groups, params = [], []
            for word in words:
                variants = _variants(word.lower().replace("_", ""))
                groups.append("(" + " OR ".join(
                    "LOWER(REPLACE(s.name, '_', '')) LIKE ?" for _ in variants) + ")")
                params += [f"%{v}%" for v in variants]
            clause = " AND ".join(groups)
            rows = self.db.execute(
                "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
                f"WHERE {clause}{self.act()} "
                "ORDER BY LENGTH(s.name), f.path, s.line_start",
                params,
            ).fetchall()
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        return [self._symbol_row(r) for r in rows]

    def suggest(self, query: str, limit: int = 8) -> list[dict]:
        """Noms proches quand une recherche ne rend rien: mots communs apres
        decoupage CamelCase / snake_case (nb compris comme nombre), mots
        colles en majuscules (NOMBRETURBINEMAX), puis distance d'edition.
        Une suite numerotee n'est proposee qu'une fois."""
        names = {r[0]: r[1] for r in self.db.execute(
            "SELECT name, kind FROM symbols WHERE active = 1 GROUP BY name")}
        wanted = [w for w in split_words(query) if len(w) >= 2]
        compact = re.sub(r"[^a-z0-9]", "", query.lower())
        scored: list[tuple[float, str]] = []
        for candidate in names:
            flat = re.sub(r"[^a-z0-9]", "", candidate.lower())
            parts = split_words(candidate)
            overlap = 0.0
            for word in wanted:
                if len(word) >= 3 and word in flat:
                    overlap += 1.0
                elif any(p.startswith(word[:4]) or word.startswith(p[:4]) for p in parts if len(p) >= 3):
                    overlap += 0.6
            ratio = difflib.SequenceMatcher(None, compact, flat).ratio()
            if overlap or ratio > 0.72:
                scored.append((overlap + ratio, candidate))
        scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
        out, families = [], set()
        for _score, candidate in scored:
            family = re.sub(r"\d+", "#", candidate)
            if family in families:
                continue
            families.add(family)
            entry = {"name": candidate, "kind": names[candidate]}
            if family != candidate:
                entry["series"] = family.replace("#", "<n>")
            out.append(entry)
            if len(out) >= limit:
                break
        return out

    def _symbol_row(self, row: sqlite3.Row) -> dict:
        keys = row.keys()
        out = {
            "name": row["name"],
            "kind": row["kind"],
            "file": self.rel(row["path"]),
            "line": row["line_start"],
        }
        if "line_end" in keys and row["line_end"] != row["line_start"]:
            out["line_end"] = row["line_end"]
        if "signature" in keys and row["signature"]:
            out["signature"] = row["signature"]
        if "value" in keys and row["value"]:
            out["value"] = row["value"]
        if "is_callback" in keys and row["is_callback"]:
            out["is_callback"] = True
        if "active" in keys and row["active"] == 0:
            out["active"] = False
        return out

    # ------------------------------------------------------------- fonctions --

    def get_function(self, name: str) -> dict | None:
        row = self.db.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE s.name = ? AND s.kind IN ('function', 'prototype') "
            "ORDER BY s.active DESC, CASE s.kind WHEN 'function' THEN 0 ELSE 1 END LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            return None

        params = row["params"]
        try:
            params = json.loads(params) if params else []
        except json.JSONDecodeError:
            params = []

        return {
            "name": row["name"],
            "kind": row["kind"],
            "file": self.rel(row["path"]),
            "path": row["path"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "signature": row["signature"],
            "params": params,
            "is_static": bool(row["is_static"]),
            "is_callback": bool(row["is_callback"]),
            "lines": row["line_end"] - row["line_start"] + 1,
            "complexity": row["complexity"],
            "depth": row["depth"],
            "cognitive": row["cognitive"],
            "locals": row["locals"],
            "frame_bytes": row["frame_bytes"],
            "active": bool(row["active"]),
            "doc": row["doc_comment"],
            "callers": self.callers(name),
            "callees": self.callees(name),
        }

    def callers(self, name: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT DISTINCT r.enclosing, r.line, r.kind, f.path FROM refs r "
            "JOIN files f ON r.file_id = f.id "
            "WHERE r.name = ? AND r.kind IN ('call', 'ref', 'decl') AND r.enclosing IS NOT NULL "
            f"{self.act('r')} ORDER BY f.path, r.line",
            (name,),
        ).fetchall()
        result = []
        for r in rows:
            entry = {"function": r["enclosing"], "file": self.rel(r["path"]), "line": r["line"]}
            if r["kind"] == "ref":
                entry["via"] = "pointer"
            elif r["kind"] == "decl":
                entry["via"] = "table"
            result.append(entry)
        return result

    def callees(self, name: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.name, MIN(r.line) AS line, COUNT(*) AS total FROM refs r "
            f"WHERE r.enclosing = ? AND r.kind = 'call'{self.act('r')} "
            "GROUP BY r.name ORDER BY line",
            (name,),
        ).fetchall()
        known = self._known_functions()
        return [
            {
                "name": r["name"],
                "line": r["line"],
                "calls": r["total"],
                "external": r["name"] not in known,
            }
            for r in rows
        ]

    def _known_functions(self) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT s.name FROM symbols s JOIN files f ON s.file_id = f.id "
                "WHERE s.kind = 'function' "
                "   OR (s.kind = 'prototype' AND f.origin != 'library')"
            )
        }

    # ---------------------------------------------------------------- usages --

    def find_usages(
        self, name: str, kind: str | None = None, limit: int = 200, file: str | None = None,
    ) -> dict:
        """Tous les usages d'un nom: code (call, read, write, ref), #if (cond),
        valeur d'une autre macro (macro), taille de tableau (dim),
        initialiseur hors fonction (decl). Le code exclu par un #if est
        compte a part, sauf include_inactive."""
        query = (
            "SELECT r.line, r.col, r.kind, r.enclosing, r.active, f.path FROM refs r "
            "JOIN files f ON r.file_id = f.id WHERE r.name = ? AND r.kind != 'return'"
        )
        params: list = [name]
        if kind:
            query += " AND r.kind = ?"
            params.append(kind)
        file_clause, ids, _ok = self.file_ids_clause(file)
        query += file_clause
        params += ids
        rows = self.db.execute(query + " ORDER BY f.path, r.line, r.col", params).fetchall()
        inactive = [r for r in rows if not r["active"] and r["kind"] != "cond"]
        kept = rows if self.include_inactive else [r for r in rows if r["active"] or r["kind"] == "cond"]
        items = []
        for r in kept:
            entry = {"file": self.rel(r["path"]), "line": r["line"], "kind": r["kind"]}
            if r["enclosing"]:
                key = {"macro": "in_macro", "dim": "array", "decl": "in_initializer_of"}.get(
                    r["kind"] if r["kind"] != "dim" or not self._is_function(r["enclosing"]) else "",
                    "in_function")
                entry[key] = r["enclosing"]
            if r["kind"] == "cond":
                entry["directive_reached"] = bool(r["active"])
            elif not r["active"]:
                entry["active"] = False
            items.append(entry)
        by_kind: dict[str, int] = {}
        for item in items:
            by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        return {"total": len(items), "by_kind": by_kind,
                "inactive_excluded": 0 if self.include_inactive else len(inactive),
                "items": items[: max(limit, 0)] if limit else items}

    def _is_function(self, name: str) -> bool:
        cache = getattr(self, "_functions_cache", None)
        if cache is None:
            cache = {r[0] for r in self.db.execute("SELECT name FROM symbols WHERE kind = 'function'")}
            self._functions_cache = cache
        return name in cache

    # ----------------------------------------------------------- call graph ---

    def call_graph(
        self,
        function: str,
        direction: str = "callees",
        depth: int = 2,
        include_external: bool = False,
    ) -> dict:
        known = self._known_functions()
        graph: dict[str, list[dict]] = {}
        self._expand(function, direction, depth, include_external, known, graph, set())
        return graph

    def _expand(self, name, direction, depth, include_external, known, graph, seen) -> None:
        if depth <= 0 or name in seen:
            return
        seen.add(name)

        # Appel direct, puis reference par pointeur (callback installe, par exemple).
        column, other = ("enclosing", "name") if direction == "callers" else ("name", "enclosing")
        rows = self.db.execute(
            f"SELECT DISTINCT {column} AS target, kind FROM refs "
            f"WHERE {other} = ? AND kind IN ('call', 'ref', 'decl') AND {column} IS NOT NULL"
            f"{'' if self.include_inactive else ' AND active = 1'}",
            (name,),
        ).fetchall()

        edges: dict[str, dict] = {}
        for row in rows:
            target = row["target"]
            internal = target in known
            if row["kind"] == "ref" and not internal:
                continue
            if not internal and not include_external:
                continue
            edge = edges.setdefault(target, {"name": target, "external": not internal})
            if row["kind"] in ("ref", "decl"):
                edge.setdefault("via", "pointer" if row["kind"] == "ref" else "table")
                edge.setdefault("confidence", "moyenne")
            else:
                edge.pop("via", None)
                edge.pop("confidence", None)
            if internal:
                self._expand(target, direction, depth - 1, include_external, known, graph, seen)

        graph[name] = sorted(edges.values(), key=lambda e: e["name"])

    # -------------------------------------------------------------- fichier ---

    def file_outline(self, needle: str) -> dict | None:
        """Structure d'un seul fichier, dans l'ordre des lignes."""
        ids = self.match_files(needle)
        if not ids:
            return None
        row = self.db.execute(
            f"SELECT * FROM files WHERE id IN ({','.join('?' for _ in ids)}) "
            "ORDER BY LENGTH(path) LIMIT 1", ids,
        ).fetchone()

        includes = []
        for r in self.db.execute(
            "SELECT included, system, line, resolved, how, active FROM includes "
            "WHERE file_id = ? ORDER BY line", (row["id"],),
        ):
            entry = {"name": r["included"], "line": r["line"], "system": bool(r["system"]),
                     "resolved": self.rel(r["resolved"]) if r["resolved"] else None, "how": r["how"]}
            if not r["active"]:
                entry["active"] = False
            includes.append(entry)
        symbols = [
            self._symbol_row(r)
            for r in self.db.execute(
                "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
                f"WHERE s.file_id = ?{self.act()} ORDER BY s.line_start",
                (row["id"],),
            )
        ]
        blocks = self.db.execute(
            "SELECT state, COUNT(*) FROM pp_blocks WHERE file_id = ? AND guard = 0 GROUP BY state",
            (row["id"],),
        ).fetchall()
        return {
            "file": self.rel(row["path"]),
            "lines": row["lines"],
            "origin": row["origin"],
            "encoding": row["encoding"],
            "in_build": bool(row["in_build"]),
            "reached_by": self.rel(row["reached_by"]) if row["reached_by"] else None,
            "include_guard": row["guard"],
            "parse_errors": row["parse_errors"],
            "preprocessor_branches": {b[0]: b[1] for b in blocks},
            "includes": includes,
            "symbols": symbols,
            "matched_files": len(ids),
        }
