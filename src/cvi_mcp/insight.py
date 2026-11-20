# ==============================================================================
#  cvi_mcp/insight.py
#  Role     Recherche et navigation: texte, theme, voisins, series numerotees,
#           impact d'un changement, resume et squelette de fonction
#  Etat     stable
#  Version  0.3.3
# ==============================================================================

# ============================ Standard ========================================
import bisect
import re
from pathlib import Path

# ============================ Interne =========================================
from .cparse import neutralize, new_parser, read_source, text_of, walk
from .queries import QueryEngine, page, split_words

# Fonctions de la bibliotheque inifile de CVI: (lecture ou ecriture, position
# de la section, position de la cle). Le handle est toujours l'argument 0.
INI_PREFIX = "Ini_"
SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b", re.IGNORECASE)


# ------------------------------------------------------------ utilitaires ------

def _segments(text: str) -> list[tuple[int, int, str]]:
    """Decoupe un source en zones code, comment et string (offsets)."""
    out: list[tuple[int, int, str]] = []
    i, n, start = 0, len(text), 0
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two in ("/*", "//"):
            if start < i:
                out.append((start, i, "code"))
            end = text.find("*/", i + 2) + 2 if two == "/*" else text.find("\n", i)
            end = n if end < 2 or end < 0 else end
            out.append((i, end, "comment"))
            i = start = end
        elif c in "\"'":
            if start < i:
                out.append((start, i, "code"))
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append((i, j, "string" if c == '"' else "code"))
            i = start = j
        else:
            i += 1
    if start < n:
        out.append((start, n, "code"))
    return out


def _functions_by_file(engine: QueryEngine) -> dict[str, list[tuple[int, int, str]]]:
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for r in engine.db.execute(
        "SELECT s.name, s.line_start, s.line_end, f.path FROM symbols s JOIN files f "
        "ON s.file_id = f.id WHERE s.kind = 'function' ORDER BY s.active DESC"
    ):
        spans.setdefault(r["path"], []).append((r["line_start"], r["line_end"], r["name"]))
    return spans


def _enclosing(spans: list[tuple[int, int, str]], line: int) -> str | None:
    for start, end, name in spans:
        if start <= line <= end:
            return name
    return None


def _stem(word: str) -> str:
    """Pluriel retire: turbines -> turbine, gestionnaires -> gestionnaire."""
    if len(word) > 4 and word.endswith(("es", "s", "x")) and not word.endswith("ss"):
        return word[:-1]
    return word


def _ui_constants(engine: QueryEngine) -> tuple[set[str], set[str]]:
    from .uimap import ui_constants

    return ui_constants(engine)


# --------------------------------------------------------- resume fonction -----

def function_summary(engine: QueryEngine, name: str) -> dict:
    """Ce qu'une fonction touche: globales lues et ecrites, verrous pris,
    panneaux et controles cites, cles INI, requetes SQL. Rien du code."""
    from .analysis import LOCK_PAIRS

    db = engine.db
    globals_ = {r[0] for r in db.execute("SELECT name FROM symbols WHERE kind = 'global'")}
    reads, writes = set(), set()
    for r in db.execute(
        "SELECT name, kind FROM refs WHERE enclosing = ? AND kind IN ('read', 'write', 'ref') "
        "AND active = 1", (name,),
    ):
        if r["name"] in globals_:
            (writes if r["kind"] == "write" else reads).add(r["name"])
    panels, controls = _ui_constants(engine)
    cited = {r[0] for r in db.execute(
        "SELECT DISTINCT name FROM refs WHERE enclosing = ? AND active = 1", (name,))}

    from .mapping import macro_strings

    strings = macro_strings(db)

    def resolve(text: str) -> str:
        literal = _unquote(text)
        return strings.get(text, literal)

    locks = []
    ini: dict[str, set] = {"read": set(), "write": set()}
    sql = []
    for r in db.execute(
        "SELECT r.id, r.name, r.line FROM refs r WHERE r.enclosing = ? AND r.kind = 'call' "
        "AND r.active = 1 ORDER BY r.line", (name,),
    ):
        args = [a[0] for a in db.execute(
            "SELECT text FROM call_args WHERE ref_id = ? ORDER BY pos", (r["id"],))]
        if r["name"] in LOCK_PAIRS and args:
            locks.append({"lock": args[0], "via": r["name"], "line": r["line"]})
        elif r["name"].startswith(INI_PREFIX) and len(args) >= 3:
            side = "write" if r["name"].startswith(("Ini_Put", "Ini_Remove")) else "read"
            ini[side].add(f"{resolve(args[1])}/{resolve(args[2])}")
        for arg in args:
            literal = _unquote(arg)
            if literal != arg and SQL_START.match(literal):
                sql.append({"line": r["line"], "statement": SQL_START.match(literal).group(1).upper(),
                            "tables": sorted(_sql_tables(literal))})
    out = {
        "globals_read": sorted(reads - writes),
        "globals_written": sorted(writes),
        "locks": locks,
        "panels": sorted(cited & panels),
        "controls": sorted(cited & controls)[:40],
        "controls_total": len(cited & controls),
        "ini_read": sorted(ini["read"]),
        "ini_written": sorted(ini["write"]),
    }
    if sql:
        out["sql"] = sql
    return {k: v for k, v in out.items() if v or k == "controls_total"}


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return text


_SQL_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+[\[`\"]?([A-Za-z_][\w.]*)", re.IGNORECASE)


def _sql_tables(statement: str) -> set[str]:
    return {m.group(1) for m in _SQL_TABLE.finditer(statement)}


_SKELETON_KINDS = {
    "if_statement": "if", "else_clause": "else", "for_statement": "for",
    "while_statement": "while", "do_statement": "do", "switch_statement": "switch",
    "case_statement": "case", "return_statement": "return", "goto_statement": "goto",
    "break_statement": "break", "continue_statement": "continue",
    "labeled_statement": "label",
}


def skeleton(engine: QueryEngine, path: str, start: int, end: int, limit: int = 250) -> list[str]:
    """Structure d'une fonction: une ligne par structure de controle ou
    appel, indentee par imbrication, sans aucune expression ni valeur. Les
    lignes exclues par un #if sont omises."""
    project = getattr(engine, "project", None)
    result = project.pp.results.get(str(Path(path).resolve())) if project and project.pp else None
    hidden = result.inactive_lines() if result else set()
    try:
        text, _enc, _raw = read_source(Path(path))
    except OSError:
        return []
    source = neutralize(text).encode("utf-8")
    tree = new_parser().parse(source)
    target = None
    for node in walk(tree.root_node):
        if node.type == "function_definition" and node.start_point[0] + 1 == start:
            target = node
            break
    if target is None:
        return []
    body = target.child_by_field_name("body")
    out: list[str] = []

    def visit(node, depth: int) -> None:
        if len(out) >= limit:
            return
        label = _SKELETON_KINDS.get(node.type)
        nested = depth
        if node.start_point[0] + 1 in hidden:
            return
        if label:
            if label == "if" and node.parent is not None and node.parent.type == "else_clause":
                out[-1] = out[-1] + " if" if out and out[-1].endswith("else") else out[-1]
            else:
                out.append(f"L{node.start_point[0] + 1} {'  ' * depth}{label}")
            nested = depth + 1
        elif node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "identifier":
                out.append(f"L{node.start_point[0] + 1} {'  ' * depth}call {text_of(source, callee)}")
        for child in node.children:
            visit(child, nested)

    visit(body, 0)
    if len(out) >= limit:
        out.append(f"... tronque a {limit} lignes")
    return out


# ------------------------------------------------------------- search_text -----

def search_text(engine: QueryEngine, pattern: str, scope: str = "all", regex: bool = False,
                file: str | None = None, origin: str = "project", case: bool = False) -> dict:
    """Cherche dans le texte des sources indexees. scope: code, comments,
    strings ou all. Renvoie position, zone et fonction englobante, jamais
    la ligne de code: seulement le texte qui correspond (ou la chaine
    litterale entiere quand on cherche dans les chaines)."""
    if not pattern or not pattern.strip():
        return {"error": "motif vide: un motif vide correspondrait a chaque ligne"}
    flags = 0 if case else re.IGNORECASE
    try:
        compiled = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as exc:
        return {"error": f"expression reguliere invalide: {exc}"}
    wanted = {"code": {"code"}, "comments": {"comment"}, "strings": {"string"},
              "all": {"code", "comment", "string"}}.get(scope, {"code", "comment", "string"})
    query = "SELECT id, path FROM files f WHERE 1 = 1"
    params: list = []
    if origin and origin != "all":
        query += " AND f.origin = ?"
        params.append(origin)
    clause, ids, ok = engine.file_ids_clause(file)
    if not ok:
        return {"error": f"aucun fichier indexe ne correspond a '{file}'"}
    rows = engine.db.execute(query + clause + " ORDER BY path", params + ids).fetchall()
    spans = _functions_by_file(engine)
    hits = []
    for row in rows:
        try:
            text, _enc, _raw = read_source(Path(row["path"]))
        except OSError:
            continue
        newlines = [i for i, ch in enumerate(text) if ch == "\n"]
        segments = _segments(text)
        starts = [s[0] for s in segments]
        for match in compiled.finditer(text):
            index = bisect.bisect_right(starts, match.start()) - 1
            seg = segments[index] if index >= 0 else (0, len(text), "code")
            if seg[2] not in wanted:
                continue
            line = bisect.bisect_right(newlines, match.start() - 1) + 1
            col = match.start() - (newlines[line - 2] + 1 if line > 1 else 0)
            shown = text[seg[0]:seg[1]] if seg[2] == "string" else match.group(0)
            hit = {"file": engine.rel(row["path"]), "line": line, "col": col, "zone": seg[2],
                   "match": shown[:120]}
            function = _enclosing(spans.get(row["path"], []), line)
            if function:
                hit["function"] = function
            hits.append(hit)
    by_zone: dict[str, int] = {}
    for hit in hits:
        by_zone[hit["zone"]] = by_zone.get(hit["zone"], 0) + 1
    return {"total": len(hits), "by_zone": by_zone, "files_searched": len(rows), "hits": hits}


# ------------------------------------------------------------ find_by_topic ----

def find_by_topic(engine: QueryEngine, topic: str, limit: int = 10) -> dict:
    """Tout ce qui parle d'un theme: fichiers, fonctions, globales, macros,
    types, panneaux, controles et chaines litterales dont le nom contient
    les mots du theme (pluriel et casse ignores), classes par pertinence."""
    words = [_stem(w) for w in split_words(topic) if len(w) >= 2]
    if not words:
        return {"error": "theme vide"}

    def score(name: str) -> int:
        parts = [_stem(p) for p in split_words(name)]
        flat = name.lower().replace("_", "")
        total = 0
        for word in words:
            if word in parts:
                total += 3
            elif any(p.startswith(word) or (word.startswith(p) and len(p) >= max(3, 0.7 * len(word)))
                     for p in parts if len(p) >= 3):
                total += 2
            elif word in flat:
                total += 1
            else:
                return 0
        return total

    panels, controls = _ui_constants(engine)
    groups: dict[str, list] = {k: [] for k in (
        "files", "functions", "callbacks", "globals", "macros", "types", "panels", "controls", "strings")}
    for r in engine.db.execute("SELECT path, lines, origin FROM files WHERE origin != 'library'"):
        value = score(Path(r["path"]).stem)
        if value:
            groups["files"].append((value, {"file": engine.rel(r["path"]), "lines": r["lines"]}))
    for r in engine.db.execute(
        "SELECT s.name, s.kind, s.line_start, s.is_callback, f.path, f.origin FROM symbols s "
        "JOIN files f ON s.file_id = f.id WHERE s.active = 1 AND f.origin != 'library'"
    ):
        value = score(r["name"])
        if not value:
            continue
        item = {"name": r["name"], "file": engine.rel(r["path"]), "line": r["line_start"]}
        if r["name"] in panels:
            key = "panels"
        elif r["name"] in controls:
            key = "controls"
        elif r["kind"] == "function":
            key = "callbacks" if r["is_callback"] else "functions"
        elif r["kind"] == "global":
            key = "globals"
        elif r["kind"] in ("macro", "enumerator"):
            key = "macros"
        elif r["kind"] in ("struct", "union", "enum", "typedef"):
            key = "types"
        else:
            continue
        groups[key].append((value, item))
    seen_strings: dict[str, dict] = {}
    for r in engine.db.execute(
        "SELECT a.text, r.enclosing FROM call_args a JOIN refs r ON a.ref_id = r.id "
        "WHERE a.text LIKE '\"%' AND r.active = 1"
    ):
        literal = _unquote(r["text"])
        if len(literal) > 80 or literal in seen_strings:
            continue
        value = score(literal)
        if value:
            seen_strings[literal] = {"string": literal, "in_function": r["enclosing"]}
            groups["strings"].append((value, seen_strings[literal]))
    out: dict = {"topic": topic, "words": words, "totals": {}}
    for key, items in groups.items():
        items.sort(key=lambda x: (-x[0], x[1].get("name", x[1].get("file", x[1].get("string", "")))))
        out["totals"][key] = len(items)
        if items:
            out[key] = [i for _, i in items[:limit]]
    if not any(out["totals"].values()):
        out["reason"] = "aucun nom ne contient ces mots; essayer search_text pour les commentaires"
    return out


# ------------------------------------------------------------- series ----------

_SERIES = re.compile(r"^(.*?[A-Za-z_])(\d+)([A-Za-z_]\w*)?$")


def _series_key(name: str) -> tuple[str, int, str] | None:
    match = _SERIES.match(name)
    if not match:
        return None
    return match.group(1), int(match.group(2)), match.group(3) or ""


def series(engine: QueryEngine, name: str | None = None, min_members: int = 3,
           limit: int = 30, offset: int = 0) -> dict:
    """Suites numerotees d'identifiants (X1, X2... Xn) et de chaines
    litterales: etendue, trous, et fichiers ou la suite s'arrete plus tot
    qu'ailleurs (un endroit oublie lors du dernier ajout).

    Un fichier est juge a deux titres, separement: ses definitions (declaration,
    #define) et ses usages (appels, lectures, chaines). Un fichier qui declare
    gLabel1 a gLabel4 mais n'initialise que gLabel1 et gLabel2 s'arrete bien a 2
    cote usage, meme si sa declaration va jusqu'a 4."""
    used: dict[str, set[int]] = {}
    defined: dict[str, set[int]] = {}
    for r in engine.db.execute(
        "SELECT DISTINCT r.name, r.file_id FROM refs r WHERE r.kind NOT IN ('return', 'cond')"
    ):
        used.setdefault(r["name"], set()).add(r["file_id"])
    for r in engine.db.execute("SELECT DISTINCT name, file_id FROM symbols"):
        defined.setdefault(r["name"], set()).add(r["file_id"])
    for r in engine.db.execute(
        "SELECT DISTINCT a.text, r.file_id FROM call_args a JOIN refs r ON a.ref_id = r.id "
        "WHERE a.text LIKE '\"%'"
    ):
        literal = _unquote(r["text"])
        if re.fullmatch(r"[A-Za-z_]\w*", literal):
            used.setdefault(f'"{literal}"', set()).add(r["file_id"])

    # famille -> {"all": numero -> fichiers, "use": ..., "def": ...}
    families: dict[tuple[str, str], dict[str, dict[int, set[int]]]] = {}
    for role, names in (("use", used), ("def", defined)):
        for full, files in names.items():
            quoted = full.startswith('"')
            key = _series_key(full.strip('"'))
            if key is None:
                continue
            prefix, number, suffix = key
            family = families.setdefault((('"' if quoted else "") + prefix, suffix),
                                         {"all": {}, "use": {}, "def": {}})
            for bucket in ("all", role):
                family[bucket].setdefault(number, set()).update(files)

    paths = {r["id"]: engine.rel(r["path"]) for r in engine.db.execute("SELECT id, path FROM files")}
    generated = {r[0] for r in engine.db.execute("SELECT id FROM files WHERE origin != 'project'")}
    _panels, controls = _ui_constants(engine)
    wanted = None
    stem = ""
    if name:
        key = _series_key(name.strip('"'))
        wanted = (key[0], key[2]) if key else None
        stem = name.strip('"').rstrip("0123456789").lower()
    out = []
    for (prefix, suffix), roles in families.items():
        members = roles["all"]
        if len(members) < min_members:
            continue
        if name and not (
            (wanted and (prefix.lstrip('"'), suffix) == wanted)
            or (stem and prefix.lstrip('"').lower().startswith(stem))):
            continue   # un nom sans correspondance ne renvoie pas toutes les suites
        numbers = sorted(members)
        low, high = numbers[0], numbers[-1]
        if high - low + 1 > 2 * len(numbers):
            continue  # nombres epars (adresses, codes): pas une suite
        gaps = [n for n in range(low, high + 1) if n not in members]
        enumerating: set[int] = set()
        short: list[dict] = []
        for role, label_role in (("use", "usage"), ("def", "definition")):
            per_file: dict[int, int] = {}
            for number, files in roles[role].items():
                for fid in files:
                    per_file[fid] = max(per_file.get(fid, 0), number)
            for fid, top in per_file.items():
                # Un fichier enumere la suite quand il cite au moins deux de ses membres.
                if fid in generated or sum(1 for f in roles[role].values() if fid in f) < 2:
                    continue
                enumerating.add(fid)
                if top < high:
                    short.append({"file": paths.get(fid, "?"), "stops_at": top, "in": label_role})
        short.sort(key=lambda x: (x["stops_at"], x["file"], x["in"]))
        label = f"{prefix}<n>{suffix}"
        sample = f"{prefix.strip(chr(34))}{low}{suffix}"
        nature = "ui_controls" if sample in controls else "strings" if prefix.startswith('"') else "identifiers"
        entry = {"series": label, "kind": nature, "from": low, "to": high, "members": len(numbers),
                 "files_enumerating": len(enumerating)}
        if gaps:
            entry["gaps"] = gaps[:20]
        if short:
            entry["files_stopping_early"] = short[:10]
        out.append(entry)
    out.sort(key=lambda e: (e["kind"] == "ui_controls", -len(e.get("files_stopping_early", [])),
                            -e["members"], e["series"]))
    result = {"families": page(out, offset, limit),
              "note": "files_stopping_early: fichier qui enumere la suite mais s'arrete avant le "
                      "maximum vu ailleurs, endroit souvent oublie lors d'un ajout. in: usage "
                      "(appels, lectures, chaines) ou definition (declaration, #define). Le code "
                      "exclu par un #if compte: il est souvent ecrit d'avance pour plus d'elements"}
    if name and not out:
        result["reason"] = f"aucune suite numerotee ne correspond a '{name}'"
    return result


# ---------------------------------------------------------------- related ------

def related(engine: QueryEngine, name: str, limit: int = 15) -> dict:
    """Symboles apparentes: meme suite numerotee, utilises ensemble dans les
    memes fonctions, definis dans le meme fichier, noms proches."""
    db = engine.db
    users = {r[0] for r in db.execute(
        "SELECT DISTINCT enclosing FROM refs WHERE name = ? AND enclosing IS NOT NULL AND active = 1",
        (name,))}
    co: dict[str, int] = {}
    if users:
        holders = ",".join("?" for _ in users)
        for r in db.execute(
            f"SELECT name, COUNT(DISTINCT enclosing) AS n FROM refs WHERE enclosing IN ({holders}) "
            "AND active = 1 AND kind IN ('read', 'write', 'ref', 'call') AND name != ? GROUP BY name",
            [*users, name],
        ):
            co[r["name"]] = r["n"]
    usage = {r[0]: r[1] for r in db.execute(
        "SELECT name, COUNT(DISTINCT enclosing) FROM refs WHERE active = 1 GROUP BY name")}
    co_used = []
    own_family = re.sub(r"\d+", "#", name)
    for other, together in co.items():
        if re.sub(r"\d+", "#", other) == own_family:
            continue
        total = usage.get(other, together)
        jaccard = together / (len(users) + total - together)
        if together >= 2 and jaccard >= 0.25:
            co_used.append({"name": other, "together_in": together, "jaccard": round(jaccard, 2)})
    co_used.sort(key=lambda x: (-x["jaccard"], -x["together_in"]))

    home = db.execute(
        "SELECT s.file_id, s.line_start, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.name = ? ORDER BY s.active DESC LIMIT 1", (name,)).fetchone()
    neighbours = []
    if home:
        for r in db.execute(
            "SELECT name, kind, line_start FROM symbols WHERE file_id = ? AND name != ? "
            "AND active = 1 ORDER BY ABS(line_start - ?) LIMIT ?",
            (home["file_id"], name, home["line_start"], limit),
        ):
            neighbours.append({"name": r["name"], "kind": r["kind"], "line": r["line_start"]})

    words = set(split_words(name))
    similar = []
    if words:
        for r in db.execute("SELECT DISTINCT name, kind FROM symbols WHERE active = 1"):
            if r["name"] == name:
                continue
            shared = words & set(split_words(r["name"]))
            if len(shared) >= max(1, len(words) - 1) and len(shared) >= 1:
                similar.append({"name": r["name"], "kind": r["kind"], "shared_words": sorted(shared)})
    similar.sort(key=lambda x: (-len(x["shared_words"]), len(x["name"])))

    family = series(engine, name, min_members=2, limit=3)["families"]["items"] \
        if _series_key(name) else []
    out = {
        "name": name,
        "used_in_functions": len(users),
        "same_series": family,
        "used_together": co_used[:limit],
        "same_file": {"file": engine.rel(home["path"]), "symbols": neighbours} if home else None,
        "similar_names": similar[:limit],
    }
    if not users and not home:
        out["reason"] = f"'{name}' introuvable"
        out["did_you_mean"] = engine.suggest(name)
    return out


# ----------------------------------------------------------------- impact ------

def impact(engine: QueryEngine, cfg, name: str, limit: int = 25) -> dict:
    """Checklist de ce qu'il faut revoir pour modifier ou etendre un symbole
    (macro, fonction, globale, panneau, controle, type)."""
    from . import configuration

    db = engine.db
    kinds = [r[0] for r in db.execute(
        "SELECT DISTINCT kind FROM symbols WHERE name = ? ORDER BY active DESC", (name,))]
    panels, controls = _ui_constants(engine)
    usages = engine.find_usages(name, None, 0)
    if not kinds and not usages["total"]:
        return {"name": name, "reason": f"'{name}' introuvable", "did_you_mean": engine.suggest(name)}
    nature = ("panel" if name in panels else "control" if name in controls
              else kinds[0] if kinds else "undefined_switch")

    functions: dict[str, dict] = {}
    for item in usages["items"]:
        fn = item.get("in_function")
        if fn:
            entry = functions.setdefault(fn, {"function": fn, "file": item["file"], "uses": 0})
            entry["uses"] += 1
    if nature == "function":
        for caller in engine.callers(name):
            functions.setdefault(caller["function"], {"function": caller["function"],
                                                       "file": caller["file"], "uses": 0,
                                                       "relation": "appelle " + name})

    out: dict = {"name": name, "nature": nature, "checklist": []}
    checklist = out["checklist"]
    for r in db.execute(
        "SELECT s.kind, s.line_start, s.value, s.signature, s.active, f.path FROM symbols s "
        "JOIN files f ON s.file_id = f.id WHERE s.name = ? ORDER BY s.active DESC", (name,),
    ):
        checklist.append({"step": "definition", "file": engine.rel(r["path"]), "line": r["line_start"],
                          "kind": r["kind"], **({"value": r["value"]} if r["value"] else {}),
                          **({} if r["active"] else {"active": False})})

    if nature in ("macro", "undefined_switch"):
        detail = configuration.macro_usages(engine, cfg, name, None, limit)
        out["macro"] = detail["summary"]
        for cond in detail["conditions"]["items"]:
            checklist.append({"step": "directive #if", **cond})
        for array in detail["arrays"]["items"]:
            checklist.append({"step": "tableau dimensionne", **array})
        for derived in detail["in_macros"]["items"]:
            checklist.append({"step": "macro derivee", **derived})

    summaries = {}
    for fn in list(functions)[:60]:
        summaries[fn] = function_summary(engine, fn)
    touched_panels = sorted({p for s in summaries.values() for p in s.get("panels", [])})
    ini_keys = sorted({k for s in summaries.values() for k in s.get("ini_read", []) + s.get("ini_written", [])})
    for fn, entry in sorted(functions.items(), key=lambda kv: -kv[1]["uses"]):
        step = {"step": "fonction", **entry}
        summary = summaries.get(fn, {})
        if summary.get("ini_written") or summary.get("ini_read"):
            step["ini"] = True
        if summary.get("panels"):
            step["panels"] = summary["panels"]
        checklist.append(step)

    users = set(functions)
    families = list(series(engine, None, 3, 200)["families"]["items"]) if users else []
    relevant = []
    if users and families:
        holders = ",".join("?" for _ in users)
        used_names = {r[0] for r in db.execute(
            f"SELECT DISTINCT name FROM refs WHERE enclosing IN ({holders}) AND active = 1", list(users))}
        size = None
        if nature in ("macro", "undefined_switch"):
            from .preproc import evaluate_value

            size = evaluate_value(name, engine.project.pp.env) if engine.project else None
        for family in families:
            if family["kind"] == "ui_controls" or (size and family["to"] < size):
                continue
            prefix, _sep, suffix = family["series"].partition("<n>")
            pattern = re.compile(re.escape(prefix.strip('"')) + r"\d+" + re.escape(suffix) + "$")
            if any(pattern.match(n) for n in used_names):
                relevant.append(family)
    relevant.sort(key=lambda f: (-len(f.get("files_stopping_early", [])), -f["files_enumerating"]))
    out["series_to_extend"] = relevant[:10]

    out["summary"] = {
        "functions": len(functions),
        "panels": touched_panels,
        "ini_keys": len(ini_keys),
        "numbered_series": len(relevant),
        "checklist_items": len(checklist),
    }
    if ini_keys:
        out["ini_keys"] = ini_keys[:limit]
    order = {"definition": 0, "directive #if": 1, "macro derivee": 2, "tableau dimensionne": 3,
             "fonction": 4}
    checklist.sort(key=lambda c: order.get(c["step"], 5))
    out["checklist"] = page(checklist, 0, max(limit, 1) * 4)
    return out


# ------------------------------------------------------------------- peek ------

def peek_lines(path: str, line: int, before: int, after: int, cap: int) -> tuple[list[str], int, int]:
    text, _enc, _raw = read_source(Path(path))
    rows = text.split("\n")
    before, after = max(before, 0), max(after, 0)
    if before + after + 1 > cap:
        after = max(cap - 1 - before, 0)
        before = min(before, cap - 1)
    first = max(line - before, 1)
    last = min(line + after, len(rows))
    return rows[first - 1:last], first, last
