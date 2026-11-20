# ==============================================================================
#  cvi_mcp/indexer.py
#  Role     Indexation des sources C/H du projet vers SQLite
#  Etat     stable
#  Version  0.3.3
# ==============================================================================

# ============================ Standard ========================================
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

# ============================ Interne =========================================
from .cparse import (
    body_fingerprint,
    declarator_info,
    file_fingerprint,
    leading_comment,
    neutralize,
    new_parser,
    read_source,
    text_of,
    walk,
)
from .preproc import STATE_NAMES, BuildContext, FileResult, Preprocessor, confidence, strip_comments
from .schema import PROJECT_TABLES, ensure_schema

# Banniere du .h que CVI genere pour chaque .uir.
UIR_BANNER = "User Interface Resource (UIR) Include File"

# Noeuds qui ajoutent un chemin d'execution, pour la complexite cyclomatique.
_BRANCHES = {
    "if_statement",
    "for_statement",
    "while_statement",
    "do_statement",
    "conditional_expression",
}
# Noeuds qui ajoutent un niveau d'imbrication.
_NESTING = {"if_statement", "for_statement", "while_statement", "do_statement", "switch_statement"}

# Noeuds traverses sans quitter la portee fichier. Sans cela, tout ce qui est
# protege par un include guard serait vu comme imbrique donc ignore.
_TRANSPARENT = {
    "translation_unit",
    "preproc_if",
    "preproc_ifdef",
    "preproc_else",
    "preproc_elif",
    "linkage_specification",
    "declaration_list",
}


class Indexer:
    """Ecrit l'index. Ne modifie jamais un fichier source."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.rebuilt = ensure_schema(self.db)
        self.parser = new_parser()

    def close(self) -> None:
        self.db.close()

    # ----------------------------------------------------------- publique ----

    def index_files(
        self,
        paths: list[str],
        force: bool = False,
        pp: Preprocessor | None = None,
    ) -> dict:
        """Indexe une liste de fichiers. Renvoie un compte-rendu.

        pp porte l'etat du preprocesseur (code actif ou exclu). Sans lui, le
        preprocesseur tourne avec les seules macros du projet."""
        if force:
            self._truncate_project()
        if pp is None:
            existing = [p for p in paths if Path(p).is_file()]
            pp = Preprocessor(existing, BuildContext())
            pp.run()
        self._pp = pp
        done, skipped, failed = 0, 0, []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                failed.append({"path": raw, "reason": "introuvable"})
                continue
            try:
                if self.index_file(path, force=force, result=pp.results.get(str(path.resolve()))):
                    done += 1
                else:
                    skipped += 1
            except (OSError, UnicodeError, sqlite3.Error) as exc:
                failed.append({"path": raw, "reason": type(exc).__name__})
        pruned = self._prune(paths)
        if done or pruned or self._meta("indexed_at") is None:
            self._classify_origins()
            self._set_meta("indexed_at", f"{time.time():.0f}")
        self.db.commit()
        return {
            "indexed": done,
            "unchanged": skipped,
            "failed": failed,
            "schema_rebuilt": self.rebuilt,
        }

    def set_root(self, root: str) -> None:
        """Racine d'affichage: si elle change d'une session a l'autre, on le
        signale, les chemins relatifs n'ont plus la meme base."""
        previous = self._meta("root")
        if previous and previous != root:
            self._set_meta("previous_root", previous)
        self._set_meta("root", root)
        self.db.commit()

    # ------------------------------------------------------------- meta ------

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def _classify_origins(self) -> None:
        """Un en-tete dont aucun prototype n'est defini dans le projet est une
        bibliotheque (NI Vision, base de donnees, driver): ses milliers de
        macros ne doivent pas noyer les statistiques du code de l'equipe."""
        self.db.execute("UPDATE files SET origin = 'project' WHERE origin = 'library'")
        self.db.execute(
            "UPDATE files SET origin = 'library' WHERE origin = 'project' "
            "AND LOWER(path) LIKE '%.h' "
            "AND EXISTS (SELECT 1 FROM symbols p WHERE p.file_id = files.id "
            "            AND p.kind = 'prototype') "
            "AND NOT EXISTS (SELECT 1 FROM symbols p JOIN symbols d ON d.name = p.name "
            "                AND d.kind = 'function' "
            "                WHERE p.file_id = files.id AND p.kind = 'prototype')"
        )
        # En-tete absent du .prj, atteint par un #include, dans un dossier sans
        # aucune source .c du projet et sans prototype implemente: tiers.
        self.db.execute(
            "UPDATE files SET origin = 'library' WHERE origin = 'project' AND listed = 0 "
            "AND LOWER(path) LIKE '%.h' "
            "AND NOT EXISTS (SELECT 1 FROM symbols p JOIN symbols d ON d.name = p.name "
            "                AND d.kind = 'function' "
            "                WHERE p.file_id = files.id AND p.kind = 'prototype')"
            "AND NOT EXISTS (SELECT 1 FROM files c WHERE LOWER(c.path) LIKE '%.c' "
            "                AND RTRIM(c.path, REPLACE(REPLACE(c.path, '/', ''), '\\', '')) "
            "                  = RTRIM(files.path, REPLACE(REPLACE(files.path, '/', ''), '\\', '')))"
        )

    def needs_reindex(self, path: Path) -> bool:
        path = Path(path).resolve()
        row = self.db.execute(
            "SELECT hash, size, mtime FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        if row is None:
            return True
        if self._unchanged_stat(path, row[1], row[2]):
            return False
        return row[0] != file_fingerprint(path.read_bytes())

    @staticmethod
    def _unchanged_stat(path: Path, size, mtime) -> bool:
        """Taille et date identiques: on evite de relire tout le fichier.

        Une modification qui preserve les deux est possible (restauration
        d'archive), le hash reste donc consulte des que l'un des deux bouge.
        """
        if not size:
            return False
        try:
            stat = path.stat()
        except OSError:
            return False
        return stat.st_size == size and abs(stat.st_mtime - (mtime or 0)) < 1e-6

    def index_file(self, path: Path, force: bool = False, result: FileResult | None = None) -> bool:
        """Indexe un fichier. Renvoie False s'il etait deja a jour.

        Deux passes: le texte ou les lignes exclues par le preprocesseur sont
        effacees donne le code compile (active=1), puis le texte brut donne ce
        qui ne vit que dans les blocs exclus (active=0). Les numeros de ligne
        et de colonne restent ceux du fichier."""
        path = path.resolve()
        if result is None:
            result = FileResult(str(path), in_build=True)
        signature = result.signature()
        existing = self.db.execute(
            "SELECT id, hash, size, mtime, pp_sig FROM files WHERE path = ?", (str(path),)
        ).fetchone()
        same_pp = existing is not None and existing[4] == signature
        if existing and not force and same_pp and self._unchanged_stat(path, existing[2], existing[3]):
            return False

        # L'empreinte porte sur les octets: elle ne depend pas de l'encodage.
        raw_text, encoding, raw_bytes = read_source(path)
        digest = file_fingerprint(raw_bytes)
        if existing and existing[1] == digest and same_pp and not force:
            return False

        lines = raw_text.splitlines()
        original = raw_text.encode("utf-8")
        neutral = neutralize(raw_text)
        blank = result.blank_lines()
        self._inactive = result.inactive_lines()

        rows = neutral.split("\n")
        for number in blank:
            if 0 < number <= len(rows):
                rows[number - 1] = " " * len(rows[number - 1])
        active_source = "\n".join(rows).encode("utf-8")
        tree = self.parser.parse(active_source)
        errors = sum(1 for node in walk(tree.root_node) if node.type == "ERROR")

        origin = "generated" if UIR_BANNER in raw_text[:800] else "project"
        file_id = self._upsert_file(
            existing, path, digest, len(lines), origin,
            extra={"encoding": encoding, "in_build": int(result.in_build),
                   "reached_by": result.reached_by, "guard": result.guard,
                   "listed": int(result.listed),
                   "pp_sig": signature, "parse_errors": errors},
        )
        self._active = 1
        self._visit(tree.root_node, active_source, original, lines, file_id)
        if self._inactive:
            self._active = 0
            raw_tree = self.parser.parse(neutral.encode("utf-8"))
            self._visit(raw_tree.root_node, neutral.encode("utf-8"), original, lines, file_id)
        self._active = 1
        self._store_preproc(result, file_id)
        self.db.commit()
        return True

    def _store_shape(self, name, file_id, line, body, source, _declarator) -> None:
        pp = getattr(self, "_pp", None)
        count, frame = _frame(body, source, pp.env if pp else {})
        self.db.execute(
            "UPDATE symbols SET cognitive = ?, locals = ?, frame_bytes = ?, shape_hash = ? "
            "WHERE file_id = ? AND name = ? AND kind = 'function' AND line_start = ?",
            (_cognitive(body, source), count, frame, _shape(body, source),
             file_id, name, line),
        )

    # ------------------------------------------------------- preprocesseur ----

    def _want(self, line: int) -> bool:
        """Passe active: tout ce qui n'est pas exclu. Passe inactive: seulement
        ce qui est exclu, pour ne rien compter deux fois."""
        inside = line in self._inactive
        return not inside if self._active else inside

    def _store_preproc(self, result: FileResult, file_id: int) -> None:
        pp = getattr(self, "_pp", None)
        forced = pp.ctx.forced if pp else {}
        anywhere = pp.defined_anywhere if pp else set()
        for branch in sorted(result.branches.values(), key=lambda b: b.line):
            level, reason = confidence(branch, anywhere, forced)
            self.db.execute(
                "INSERT INTO pp_blocks (file_id, line_start, line_end, chain, kind, expr, "
                "state, confidence, reason, names, guard, varies) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, branch.line, branch.end, branch.chain, branch.kind, branch.expr,
                 STATE_NAMES[branch.state], level, reason,
                 json.dumps(sorted(branch.names)), int(branch.guard), int(branch.varies)),
            )
            for name in sorted(branch.names):
                if name == "defined":
                    continue
                self.db.execute(
                    "INSERT INTO refs (file_id, line, col, name, kind, enclosing, active) "
                    "VALUES (?, ?, 0, ?, 'cond', NULL, ?)",
                    (file_id, branch.line, name, int(branch.evaluated)),
                )
        for include in sorted(result.includes.values(), key=lambda i: i.line):
            self.db.execute(
                "INSERT INTO includes (file_id, included, system, line, resolved, how, active) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, include.text, int(include.system), include.line, include.resolved,
                 include.how, int(include.state > 0)),
            )

    # ------------------------------------------------------------ fichiers ----

    def _upsert_file(self, existing, path: Path, digest: str, lines: int, origin: str,
                     extra: dict | None = None) -> int:
        try:
            stat = path.stat()
            size, mtime = stat.st_size, stat.st_mtime
        except OSError:
            size, mtime = 0, 0.0
        values = {"hash": digest, "lines": lines, "size": size, "mtime": mtime,
                  "origin": origin, **(extra or {})}

        if existing:
            file_id = existing[0]
            self._clear_file(file_id)
            assignments = ", ".join(f"{k} = ?" for k in values)
            self.db.execute(
                f"UPDATE files SET {assignments} WHERE id = ?", (*values.values(), file_id)
            )
            return file_id
        columns = ", ".join(["path", *values])
        holders = ", ".join("?" for _ in range(len(values) + 1))
        cursor = self.db.execute(
            f"INSERT INTO files ({columns}) VALUES ({holders})", (str(path), *values.values())
        )
        return int(cursor.lastrowid)

    def _clear_file(self, file_id: int) -> None:
        self.db.execute(
            "DELETE FROM call_args WHERE ref_id IN "
            "(SELECT id FROM refs WHERE file_id = ?)",
            (file_id,),
        )
        self.db.execute("DELETE FROM refs WHERE file_id = ?", (file_id,))
        self.db.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        self.db.execute("DELETE FROM includes WHERE file_id = ?", (file_id,))
        self.db.execute("DELETE FROM pp_blocks WHERE file_id = ?", (file_id,))

    def _truncate_project(self) -> None:
        for table in PROJECT_TABLES:
            self.db.execute(f"DELETE FROM {table}")
        self.db.commit()

    def _prune(self, keep: list[str]) -> int:
        """Retire de l'index les fichiers disparus du projet."""
        wanted = {str(Path(p).resolve()) for p in keep}
        removed = 0
        for file_id, path in self.db.execute("SELECT id, path FROM files").fetchall():
            if path not in wanted:
                self._clear_file(file_id)
                self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))
                removed += 1
        return removed

    # ------------------------------------------------------------- visite ----

    def _visit(self, root, source: bytes, original: bytes, lines: list[str], file_id: int) -> None:
        for node in walk(root):
            kind = node.type

            if kind in ("preproc_def", "preproc_function_def"):
                self._add_macro(node, source, file_id)
            elif kind == "function_definition":
                self._add_function(node, source, original, lines, file_id)
            elif kind == "declaration" and self._file_scope(node):
                self._add_declaration(node, source, lines, file_id)
            elif kind == "type_definition":
                self._add_typedef(node, source, file_id)
            elif kind in ("struct_specifier", "union_specifier"):
                self._add_record(node, source, file_id)
            elif kind == "enum_specifier":
                self._add_enum(node, source, file_id)
            elif kind == "array_declarator" and not self._in_function(node):
                self._add_dimension(node, source, file_id)

    def _file_scope(self, node) -> bool:
        parent = node.parent
        while parent is not None:
            if parent.type not in _TRANSPARENT:
                return False
            parent = parent.parent
        return True

    # ------------------------------------------------------------ symboles ----

    def _insert_symbol(self, **row) -> None:
        if not self._want(row["line_start"]):
            return
        row["active"] = self._active
        columns = ", ".join(row)
        holders = ", ".join("?" for _ in row)
        self.db.execute(
            f"INSERT INTO symbols ({columns}) VALUES ({holders})", tuple(row.values())
        )

    def _add_macro(self, node, source: bytes, file_id: int) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        value_node = node.child_by_field_name("value")
        value = " ".join(strip_comments(text_of(source, value_node)).split()) if value_node else None
        value = value or None
        name = text_of(source, name_node)
        line = node.start_point[0] + 1
        params_node = node.child_by_field_name("parameters")
        params = set(_WORD.findall(text_of(source, params_node))) if params_node else set()
        self._insert_symbol(
            file_id=file_id,
            name=name,
            kind="macro",
            line_start=line,
            line_end=node.end_point[0] + 1,
            value=value,
            params=json.dumps(sorted(params)) if params_node else None,
        )
        if value and self._want(line):
            for used in sorted(set(_WORD.findall(_strip_literals(value))) - params - {name}):
                self._ref(file_id, line, 0, used, "macro", name)

    def _add_function(self, node, source: bytes, original: bytes, lines: list[str], file_id: int) -> None:
        declarator = node.child_by_field_name("declarator")
        if declarator is None:
            return
        name, params = declarator_info(declarator, source)
        if not name:
            return

        body = node.child_by_field_name("body")
        header_end = body.start_byte if body else node.end_byte
        header = original[node.start_byte : header_end].decode("utf-8", errors="replace")

        self._insert_symbol(
            file_id=file_id,
            name=name,
            kind="function",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            signature=" ".join(header.split()).rstrip("{").strip(),
            params=json.dumps(params, ensure_ascii=False),
            is_static=1 if any(
                child.type == "storage_class_specifier"
                and text_of(source, child) == "static"
                for child in node.children
            ) else 0,
            is_callback=1 if "CVICALLBACK" in header else 0,
            doc_comment=leading_comment(lines, node.start_point[0]),
            body_hash=body_fingerprint(text_of(source, body)) if body else None,
            **(_metrics(body, source) if body is not None else {}),
        )

        if body is not None:
            self._collect_refs(body, source, file_id, name, self._locals(declarator, body, source))
            if self._active:
                self._store_shape(name, file_id, node.start_point[0] + 1, body, source, declarator)

    def _add_declaration(self, node, source: bytes, lines: list[str], file_id: int) -> None:
        """Une declaration peut en porter plusieurs: int a, b[4], *c; donne
        trois symboles. Chacun garde le type de la declaration et son propre
        declarateur, pour que sa signature se lise seule (int b[4])."""
        for index, declarator in enumerate(node.children_by_field_name("declarator")):
            self._add_declarator(node, declarator, index, source, lines, file_id)

    def _add_declarator(self, node, declarator, index: int, source: bytes, lines: list[str],
                        file_id: int) -> None:
        # Le premier declarateur garde la plage de lignes de toute la declaration,
        # les suivants la leur: on les retrouve a leur ligne.
        line_start = node.start_point[0] + 1 if index == 0 else declarator.start_point[0] + 1
        line_end = node.end_point[0] + 1 if index == 0 else declarator.end_point[0] + 1

        has_params = declarator.type == "function_declarator" or any(
            child.type == "function_declarator" for child in walk(declarator)
        )
        # Pointeur ou tableau de pointeurs de fonctions initialise: une variable.
        if declarator.type == "init_declarator" or (
            declarator.type != "function_declarator"
            and any(child.type == "parenthesized_declarator" for child in walk(declarator))
        ):
            has_params = False
        if has_params:
            name, params = declarator_info(declarator, source)
            if not name:
                return
            self._insert_symbol(
                file_id=file_id,
                name=name,
                kind="prototype",
                line_start=line_start,
                line_end=line_end,
                signature=" ".join(text_of(source, node).split()).rstrip(";"),
                params=json.dumps(params, ensure_ascii=False),
                doc_comment=leading_comment(lines, node.start_point[0]),
            )
            return

        name = _declared_name(declarator, source) or self._plain_name(text_of(source, declarator))
        if name:
            self._add_initializer_refs(declarator, source, file_id, name)
            # Type de la declaration (tout ce qui precede le premier declarateur)
            # suivi de ce declarateur seul.
            first = node.children_by_field_name("declarator")[0]
            specifiers = source[node.start_byte : first.start_byte].decode(
                "utf-8", errors="replace")
            self._insert_symbol(
                file_id=file_id,
                name=name,
                kind="global",
                line_start=line_start,
                line_end=line_end,
                signature=" ".join((specifiers + text_of(source, declarator)).split()).rstrip(";"),
                is_static=1 if any(
                    child.type == "storage_class_specifier"
                    and text_of(source, child) == "static"
                    for child in node.children
                ) else 0,
            )

    def _add_typedef(self, node, source: bytes, file_id: int) -> None:
        declarator = node.child_by_field_name("declarator")
        if declarator is None:
            return
        name = self._plain_name(text_of(source, declarator))
        if name:
            self._insert_symbol(
                file_id=file_id,
                name=name,
                kind="typedef",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )

    def _add_record(self, node, source: bytes, file_id: int) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        self._insert_symbol(
            file_id=file_id,
            name=text_of(source, name_node),
            kind="struct" if node.type == "struct_specifier" else "union",
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )

    def _add_enum(self, node, source: bytes, file_id: int) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            self._insert_symbol(
                file_id=file_id,
                name=text_of(source, name_node),
                kind="enum",
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        body = node.child_by_field_name("body")
        if body is None:
            return
        for child in body.children:
            if child.type != "enumerator":
                continue
            label = child.child_by_field_name("name")
            if label is None:
                continue
            value_node = child.child_by_field_name("value")
            self._insert_symbol(
                file_id=file_id,
                name=text_of(source, label),
                kind="enumerator",
                line_start=child.start_point[0] + 1,
                line_end=child.end_point[0] + 1,
                value=text_of(source, value_node).strip() if value_node else None,
            )

    # ------------------------------------------------------------ references ---

    def _locals(self, declarator, body, source: bytes) -> set[str]:
        """Parametres et variables locales, pour ne pas confondre une locale
        qui masque une globale avec la globale elle meme."""
        names: set[str] = set()
        for node in walk(declarator):
            if node.type == "parameter_declaration":
                inner = node.child_by_field_name("declarator")
                if inner is not None:
                    name = self._plain_name(text_of(source, inner))
                    if name:
                        names.add(name)
        for node in walk(body):
            if node.type != "declaration":
                continue
            for child in node.children:
                if child.type in ("identifier", "array_declarator", "pointer_declarator", "init_declarator"):
                    name = self._plain_name(text_of(source, child))
                    if name:
                        names.add(name)
        return names

    def _collect_refs(self, body, source: bytes, file_id: int, enclosing: str, locals_: set[str]) -> None:
        for node in walk(body):
            if node.type == "call_expression":
                self._add_call(node, source, file_id, enclosing)
            elif node.type == "return_statement":
                self._ref(file_id, node.start_point[0] + 1, node.start_point[1],
                          "return", "return", enclosing)
            elif node.type == "identifier":
                self._add_identifier(node, source, file_id, enclosing, locals_)

    def _add_call(self, node, source: bytes, file_id: int, enclosing: str) -> None:
        target = node.child_by_field_name("function")
        if target is None:
            return
        ref_id = self._ref(file_id, target.start_point[0] + 1, target.start_point[1],
                           text_of(source, target), "call", enclosing)
        if ref_id is None:
            return
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            return
        position = 0
        for child in arguments.children:
            if child.type in ("(", ")", ","):
                continue
            self.db.execute(
                "INSERT INTO call_args (ref_id, pos, text) VALUES (?, ?, ?)",
                (ref_id, position, " ".join(text_of(source, child).split())),
            )
            position += 1

    def _add_identifier(self, node, source: bytes, file_id: int, enclosing: str, locals_: set[str]) -> None:
        name = text_of(source, node)
        if name in locals_:
            return
        parent = node.parent
        if parent is None:
            return
        if parent.type == "call_expression" and node == parent.child_by_field_name("function"):
            return  # deja enregistre comme appel
        if parent.type == "field_expression" and node == parent.child_by_field_name("field"):
            return  # nom de champ, pas un symbole global

        if _in_array_size(node):
            kind = "dim"
        elif (parent.type == "assignment_expression" and node == parent.child_by_field_name("left")
              or parent.type == "update_expression"
              or parent.type in ("unary_expression", "pointer_expression")
              and text_of(source, parent).lstrip().startswith("&")):
            # &x passe a une fonction: elle peut ecrire dedans (GetCtrlVal).
            kind = "write"
        elif parent.type in ("argument_list", "initializer_list"):
            kind = "ref"
        elif parent.type == "subscript_expression" and node == parent.child_by_field_name("argument"):
            grand = parent.parent
            kind = (
                "write"
                if grand is not None
                and grand.type == "assignment_expression"
                and parent == grand.child_by_field_name("left")
                else "read"
            )
        else:
            kind = "read"

        self._ref(file_id, node.start_point[0] + 1, node.start_point[1], name, kind, enclosing)

    def _ref(self, file_id: int, line: int, col: int, name: str, kind: str,
             enclosing: str | None) -> int | None:
        if not self._want(line):
            return None
        cursor = self.db.execute(
            "INSERT INTO refs (file_id, line, col, name, kind, enclosing, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (file_id, line, col, name, kind, enclosing, self._active),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _in_function(node) -> bool:
        parent = node.parent
        while parent is not None:
            if parent.type == "function_definition":
                return True
            parent = parent.parent
        return False

    def _add_dimension(self, node, source: bytes, file_id: int) -> None:
        """Taille d'un tableau hors fonction: global, champ de structure,
        typedef. enclosing porte le nom du tableau."""
        size = node.child_by_field_name("size")
        if size is None:
            return
        inner = node.child_by_field_name("declarator")
        owner = self._plain_name(text_of(source, inner)) if inner is not None else None
        for item in [size, *walk(size)]:
            if item.type == "identifier":
                self._ref(file_id, item.start_point[0] + 1, item.start_point[1],
                          text_of(source, item), "dim", owner or None)

    def _add_initializer_refs(self, node, source: bytes, file_id: int, owner: str) -> None:
        """Noms cites dans l'initialiseur d'une globale: tables de callbacks,
        listes de panneaux, constantes."""
        # walk() ne rend que les descendants: la racine compte quand elle est
        # elle-meme l'init_declarator (un seul nom declare avec son initialiseur).
        for child in (node, *walk(node)):
            if child.type != "init_declarator":
                continue
            value = child.child_by_field_name("value")
            if value is None:
                continue
            for item in [value, *walk(value)]:
                if item.type == "identifier":
                    self._ref(file_id, item.start_point[0] + 1, item.start_point[1],
                              text_of(source, item), "decl", owner)

    # ------------------------------------------------------------- utilitaire --

    @staticmethod
    def _plain_name(text: str) -> str:
        """Nom nu d'un declarateur: pointeurs, tableaux et init retires."""
        candidate = text.split("=", 1)[0].split("[", 1)[0].split("(", 1)[0]
        return candidate.replace("*", " ").replace("&", " ").strip()


def _metrics(body, source: bytes) -> dict:
    """Complexite cyclomatique approchee (1 + points de decision) et
    profondeur d'imbrication maximale des structures de controle."""
    complexity, deepest = 1, 0
    stack = [(body, 0)]
    while stack:
        node, depth = stack.pop()
        kind = node.type
        if kind in _BRANCHES or kind == "case_statement" and node.child_by_field_name("value") is not None:
            complexity += 1
        elif kind == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is not None and text_of(source, operator) in ("&&", "||"):
                complexity += 1
        # Un "else if" prolonge la chaine sans imbriquer un niveau de plus.
        chained = kind == "if_statement" and node.parent is not None and node.parent.type == "else_clause"
        if kind in _NESTING and not chained:
            depth += 1
            deepest = max(deepest, depth)
        stack.extend((child, depth) for child in node.children)
    return {"complexity": complexity, "depth": deepest}


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_SHAPE_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\d[\w.]*|\S')
_KEYWORDS = frozenset(
    ["if", "else", "for", "while", "do", "switch", "case", "default", "break", "continue", "return", "goto", "sizeof", "int", "char", "short", "long", "float", "double", "void", "unsigned", "signed", "const", "static", "struct", "union", "enum", "typedef", "volatile", "extern", "register"]
)
_TYPE_SIZES = {"char": 1, "short": 2, "int": 4, "long": 4, "float": 4, "double": 8,
               "__int64": 8, "size_t": 4}


def _declared_name(node, source: bytes) -> str | None:
    """Nom declare, a travers pointeurs, tableaux, parentheses et init."""
    current = node
    for _depth in range(12):
        if current is None:
            return None
        if current.type == "identifier":
            return text_of(source, current)
        inner = current.child_by_field_name("declarator")
        if inner is None:
            inner = next((c for c in current.named_children if c.type.endswith("declarator")
                          or c.type == "identifier"), None)
        current = inner
    return None


def _strip_literals(text: str) -> str:
    return _LITERAL.sub(" ", text)


def _in_array_size(node) -> bool:
    """Vrai si l'identifiant sert a dimensionner un tableau."""
    child, parent = node, node.parent
    while parent is not None and parent.type not in ("declaration", "field_declaration",
                                                       "parameter_declaration", "compound_statement"):
        if parent.type == "array_declarator" and child == parent.child_by_field_name("size"):
            return True
        child, parent = parent, parent.parent
    return False


def _cognitive(body, source: bytes) -> int:
    """Complexite cognitive approchee: chaque rupture du flux compte, et
    compte d'autant plus qu'elle est imbriquee. Un else if ne coute qu'un."""
    total = 0
    stack = [(body, 0)]
    nesting_kinds = {"if_statement", "for_statement", "while_statement", "do_statement",
                     "switch_statement", "conditional_expression"}
    while stack:
        node, level = stack.pop()
        kind = node.type
        chained = kind == "if_statement" and node.parent is not None and node.parent.type == "else_clause"
        if kind in nesting_kinds:
            total += 1 if chained else 1 + level
        elif kind == "else_clause" and not any(c.type == "if_statement" for c in node.children) or kind == "goto_statement":
            total += 1
        elif kind == "binary_expression":
            operator = node.child_by_field_name("operator")
            op = text_of(source, operator) if operator is not None else ""
            if op in ("&&", "||"):
                parent = node.parent
                parent_op = None
                if parent is not None and parent.type == "binary_expression":
                    po = parent.child_by_field_name("operator")
                    parent_op = text_of(source, po) if po is not None else None
                if parent_op != op:
                    total += 1
        deeper = level + 1 if kind in nesting_kinds and not chained else level
        stack.extend((child, deeper) for child in node.children)
    return total


def _frame(body, source: bytes, env) -> tuple[int, int]:
    """Nombre de variables locales et taille approchee de la pile qu'elles
    occupent (tableaux dimensionnes par une macro evalues si possible)."""
    from .preproc import evaluate_value

    count, size = 0, 0
    for node in walk(body):
        if node.type != "declaration":
            continue
        text = text_of(source, node)
        if text.lstrip().startswith("static"):
            continue
        type_node = node.child_by_field_name("type")
        base = text_of(source, type_node) if type_node is not None else "int"
        unit = next((v for k, v in _TYPE_SIZES.items() if k in base.split()), 4)
        for child in node.children:
            if child.type not in ("identifier", "init_declarator", "array_declarator",
                                  "pointer_declarator"):
                continue
            count += 1
            item_size = 4 if "*" in text_of(source, child).split("=")[0] else unit
            for sub in [child, *walk(child)]:
                if sub.type == "array_declarator":
                    dim = sub.child_by_field_name("size")
                    if dim is not None:
                        value = evaluate_value(text_of(source, dim), env or {})
                        item_size *= value if value and value > 0 else 1
            size += item_size
    return count, size


def _shape(body, source: bytes) -> str | None:
    """Empreinte de la forme du corps: noms, nombres et chaines remplaces,
    mots-cles et operateurs gardes. Deux fonctions copiees puis adaptees
    ont la meme forme."""
    text = text_of(source, body)
    tokens = []
    for token in _SHAPE_TOKEN.findall(text):
        if token[0] in "\"'":
            tokens.append("S")
        elif token[0].isdigit():
            tokens.append("N")
        elif token[0].isalpha() or token[0] == "_":
            tokens.append(token if token in _KEYWORDS else "I")
        else:
            tokens.append(token)
    if len(tokens) < 40:
        return None  # trop court pour parler de copie
    return hashlib.sha256(" ".join(tokens).encode()).hexdigest()[:16]
