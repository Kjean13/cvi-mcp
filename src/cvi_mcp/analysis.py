# ==============================================================================
#  cvi_mcp/analysis.py
#  Role     Analyses transverses: code mort, verrous, modules, complexite
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json
import re
from pathlib import Path

# ============================ Interne =========================================
from .queries import QueryEngine, page
from .risks import LOCK_NOTES, LOCK_PAIRS, lock_check

# ------------------------------------------------------------- code mort -------

def _unused_macros(engine: QueryEngine) -> tuple[list[dict], list[dict]]:
    """Macros du projet que rien ne cite: ni le code, ni un #if, ni une autre
    macro, ni une taille de tableau, ni un initialiseur. Les gardes
    d'inclusion sont exclues. Renvoie aussi celles citees seulement dans du
    code exclu par un #if."""
    db = engine.db
    guards = {r[0] for r in db.execute("SELECT guard FROM files WHERE guard IS NOT NULL")}
    live: set[str] = set()
    dormant: set[str] = set()
    for r in db.execute(
        "SELECT name, active, kind, enclosing FROM refs WHERE kind != 'return'"
    ):
        if r["kind"] == "macro" and r["enclosing"] == r["name"]:
            continue
        (live if r["active"] or r["kind"] == "cond" else dormant).add(r["name"])
    dead, only_excluded = [], []
    for r in db.execute(
        "SELECT s.name, s.line_start, s.active, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'macro' AND f.origin = 'project' AND LOWER(f.path) LIKE '%.h' "
        "ORDER BY f.path, s.line_start"
    ):
        name = r["name"]
        if name in guards or name in live:
            continue
        item = {"name": name, "file": engine.rel(r["path"]), "line": r["line_start"],
                "confidence": "haute"}
        if not r["active"]:
            item["defined_in_excluded_code"] = True
        (only_excluded if name in dormant else dead).append(item)
    return dead, only_excluded


def dead_code(engine: QueryEngine, uir: dict | None, panels: list[str]) -> dict:
    """Ce que rien n'atteint, lu sur le code compile.

    Les fonctions sont classees par atteignabilite depuis les points
    d'entree (main, WinMain, DllMain, callbacks cablees ou installees,
    fonctions dont l'adresse est prise, exports). Sans main ni WinMain le
    projet est traite comme une bibliotheque: toute fonction exportee est un
    point d'entree."""
    db = engine.db
    wired = set(uir["callbacks"]) if uir else set()
    functions_rows = db.execute(
        "SELECT s.name, s.line_start, s.line_end, s.is_static, s.is_callback, s.signature, "
        "s.active, f.path, f.origin FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'function' ORDER BY f.path, s.line_start"
    ).fetchall()
    active_fns = {r["name"] for r in functions_rows if r["active"]}

    calls: dict[str, set[str]] = {}
    pointer_taken: set[str] = set()
    called: set[str] = set()
    for r in db.execute(
        "SELECT name, kind, enclosing FROM refs WHERE active = 1 AND kind IN ('call', 'ref', 'decl', 'read')"
    ):
        if r["enclosing"] == r["name"]:
            continue
        if r["kind"] == "call":
            called.add(r["name"])
        elif r["name"] in active_fns:
            pointer_taken.add(r["name"])
        if r["enclosing"]:
            calls.setdefault(r["enclosing"], set()).add(r["name"])
    inactive_users = {r[0] for r in db.execute(
        "SELECT DISTINCT name FROM refs WHERE active = 0 AND kind IN ('call', 'ref', 'decl', 'read')")}

    exported = {r["name"] for r in functions_rows
                if re.search(r"\bDLL(EXPORT|STDCALL)\b|__declspec\s*\(\s*dllexport", r["signature"] or "")}
    has_main = any(r["name"] in ("main", "WinMain") for r in functions_rows if r["active"])
    roots = {"main", "WinMain", "DllMain"} | wired | pointer_taken | exported
    if not has_main:
        roots |= {r["name"] for r in functions_rows if r["active"] and not r["is_static"]}
    reached: set[str] = set()
    stack = [n for n in roots if n in active_fns]
    while stack:
        name = stack.pop()
        if name in reached:
            continue
        reached.add(name)
        stack.extend(n for n in calls.get(name, ()) if n in active_fns and n not in reached)

    functions, callbacks, address_taken = [], [], []
    for row in functions_rows:
        name = row["name"]
        if not row["active"] or row["origin"] != "project" or name in reached:
            if name in pointer_taken and name not in called and not row["is_callback"] \
                    and row["active"] and row["origin"] == "project":
                address_taken.append({"name": name, "file": engine.rel(row["path"]),
                                      "line": row["line_start"]})
            continue
        item = {"name": name, "file": engine.rel(row["path"]), "line": row["line_start"],
                "lines": row["line_end"] - row["line_start"] + 1}
        if row["is_callback"]:
            item["status"] = "only_used_in_excluded_code" if name in inactive_users else "unwired"
            item["confidence"] = "haute" if uir else "moyenne"
            callbacks.append(item)
            continue
        if name in called:
            item["status"] = "only_called_by_dead_code"
        elif name in inactive_users:
            item["status"] = "only_used_in_excluded_code"
        else:
            item["status"] = "never_called"
        item["confidence"] = "haute" if row["is_static"] else "moyenne"
        functions.append(item)

    globals_, write_only = [], []
    readers = {r[0] for r in db.execute(
        "SELECT DISTINCT name FROM refs WHERE active = 1 AND kind IN ('read', 'ref', 'call', 'dim', 'decl', 'cond', 'macro')")}
    writers = {r[0] for r in db.execute("SELECT DISTINCT name FROM refs WHERE active = 1 AND kind = 'write'")}
    inactive_globals = {r[0] for r in db.execute("SELECT DISTINCT name FROM refs WHERE active = 0")}
    for r in db.execute(
        "SELECT s.name, s.line_start, s.signature, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'global' AND s.active = 1 AND f.origin = 'project' ORDER BY f.path, s.line_start"
    ):
        if r["signature"] and r["signature"].lstrip().startswith("extern"):
            continue
        item = {"name": r["name"], "file": engine.rel(r["path"]), "line": r["line_start"]}
        if r["name"] not in readers and r["name"] not in writers:
            item["confidence"] = "haute"
            if r["name"] in inactive_globals:
                item["status"] = "only_used_in_excluded_code"
            globals_.append(item)
        elif r["name"] not in readers:
            item["confidence"] = "moyenne"
            write_only.append(item)
    macros, macros_excluded = _unused_macros(engine)
    return {
        "functions": functions,
        "callbacks": callbacks,
        "functions_address_taken": address_taken,
        "globals": globals_,
        "globals_write_only": write_only,
        "macros": macros,
        "macros_only_in_excluded_code": macros_excluded,
        "panels": _panels_status(engine, panels),
    }


_LOADERS = {"LoadPanel": 2, "LoadPanelEx": 2}


def _panels_status(engine: QueryEngine, panels: list[str]) -> list[dict]:
    """Panneaux jamais cites dans le code compile. Si le code charge des
    panneaux avec un identifiant calcule (variable, tableau), un panneau non
    cite a pu etre charge par ce biais: chargement indirect probable."""
    if not panels:
        return []
    db = engine.db
    cited = {r[0] for r in db.execute("SELECT DISTINCT name FROM refs WHERE active = 1")}
    cited_inactive = {r[0] for r in db.execute("SELECT DISTINCT name FROM refs WHERE active = 0")}
    known = set(panels)
    dynamic = []
    for r in db.execute(
        "SELECT r.name, r.line, r.enclosing, a.text, f.path FROM refs r "
        "JOIN call_args a ON a.ref_id = r.id JOIN files f ON r.file_id = f.id "
        "WHERE r.kind = 'call' AND r.active = 1 AND r.name IN ('LoadPanel', 'LoadPanelEx') AND a.pos = 2"
    ):
        text = r["text"].strip()
        if text not in known and not re.fullmatch(r"-?\d+", text):
            dynamic.append({"call": r["name"], "function": r["enclosing"],
                            "file": engine.rel(r["path"]), "line": r["line"], "panel_argument": text})
    out = []
    for panel in panels:
        if panel in cited:
            continue
        item = {"panel": panel}
        if panel in cited_inactive:
            item["status"] = "only_in_excluded_code"
            item["confidence"] = "haute"
        elif dynamic:
            item["status"] = "indirect_load_possible"
            item["confidence"] = "faible"
            item["evidence"] = dynamic[:3]
        else:
            item["status"] = "never_loaded"
            item["confidence"] = "haute"
        out.append(item)
    return out


DEAD_NOTES = {
    "functions": "non atteintes depuis main, les callbacks cablees ou installees, les "
    "exports et les fonctions dont l'adresse est prise. status: never_called, "
    "only_called_by_dead_code, only_used_in_excluded_code. confidence haute si static",
    "callbacks": "ni cablees dans un .uir indexe, ni enregistrees par le code",
    "functions_address_taken": "jamais appelees directement mais passees par adresse: "
    "callbacks possibles, comptees comme vivantes",
    "globals": "variables du projet jamais lues ni ecrites dans le code compile; "
    "status only_used_in_excluded_code si seul du code exclu par un #if les utilise",
    "globals_write_only": "variables ecrites mais jamais lues: valeur calculee pour rien, "
    "sauf lecture par un debogueur ou un autre module",
    "macros": "macros des en-tetes du projet citees nulle part: code, #if, autres macros, "
    "tailles de tableaux, initialiseurs. Gardes d'inclusion exclues",
    "macros_only_in_excluded_code": "macros citees seulement dans du code exclu par un #if",
    "panels": "status: never_loaded, only_in_excluded_code, ou indirect_load_possible "
    "quand le code charge des panneaux par un identifiant calcule (evidence)",
}



DEAD_NOTES = {
    "functions": "non atteintes depuis main, les callbacks cablees ou installees, les "
    "exports et les fonctions dont l'adresse est prise. status: never_called, "
    "only_called_by_dead_code, only_used_in_excluded_code. confidence haute si static",
    "callbacks": "ni cablees dans un .uir indexe, ni enregistrees par le code",
    "functions_address_taken": "jamais appelees directement mais passees par adresse: "
    "callbacks possibles, comptees comme vivantes",
    "globals": "variables du projet jamais lues ni ecrites dans le code compile; "
    "status only_used_in_excluded_code si seul du code exclu par un #if les utilise",
    "globals_write_only": "variables ecrites mais jamais lues: valeur calculee pour rien, "
    "sauf lecture par un debogueur ou un autre module",
    "macros": "macros des en-tetes du projet citees nulle part: code, #if, autres macros, "
    "tailles de tableaux, initialiseurs. Gardes d'inclusion exclues",
    "macros_only_in_excluded_code": "macros citees seulement dans du code exclu par un #if",
    "panels": "status: never_loaded, only_in_excluded_code, ou indirect_load_possible "
    "quand le code charge des panneaux par un identifiant calcule (evidence)",
}


# --------------------------------------------------------------- modules -------

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
_GENERIC = frozenset(
    ["de", "du", "des", "la", "le", "les", "et", "en", "un", "une", "gestion", "config", "configuration", "fonctions", "fonction", "test", "tests", "main", "init", "commun", "common", "utils", "outils", "util", "fichier", "fichiers", "get", "set", "new", "old", "lib", "src", "include", "inc", "data"]
)


def modules(engine: QueryEngine) -> list[dict]:
    """Regroupe les fichiers du projet par domaine, deduit des noms: un mot
    significatif partage par au moins deux fichiers (ConfigCamera et
    GestionDeLaCamera donnent camera). Les .c et .h du meme nom forment un seul
    module; sans mot partage, le fichier forme son propre module. Heuristique, a
    verifier avec module_graph."""
    rows = engine.db.execute(
        "SELECT f.path, f.lines, "
        "(SELECT COUNT(*) FROM symbols s WHERE s.file_id = f.id AND s.kind = 'function') AS n "
        "FROM files f WHERE f.origin = 'project' ORDER BY f.path"
    ).fetchall()
    stems: dict[str, dict] = {}
    for row in rows:
        path = engine.rel(row["path"])
        key = Path(path).stem
        entry = stems.setdefault(
            key, {"stem": Path(path).stem, "folder": str(Path(path).parent), "lines": 0, "functions": 0}
        )
        entry["lines"] += row["lines"] or 0
        entry["functions"] += row["n"]

    def words(stem: str) -> set[str]:
        found = {w.lower() for w in _CAMEL.findall(stem.replace("_", " "))}
        return {w for w in found if len(w) > 2 and w not in _GENERIC and not w.isdigit()}

    frequency: dict[str, int] = {}
    for entry in stems.values():
        for word in words(entry["stem"]):
            frequency[word] = frequency.get(word, 0) + 1

    groups: dict[str, list[dict]] = {}
    for entry in stems.values():
        shared = [w for w in words(entry["stem"]) if frequency[w] >= 2]
        if shared:
            name = max(shared, key=lambda w: (frequency[w], len(w), w))
        else:
            kept = [
                w.lower() for w in _CAMEL.findall(entry["stem"].replace("_", " "))
                if len(w) > 2 and w.lower() not in _GENERIC
            ]
            name = "_".join(kept) or entry["stem"]
        groups.setdefault(name, []).append(entry)

    out = [
        {
            "module": name,
            "files": len(members),
            "lines": sum(m["lines"] for m in members),
            "functions": sum(m["functions"] for m in members),
            "stems": sorted(m["stem"] for m in members)[:8],
        }
        for name, members in groups.items()
    ]
    return sorted(out, key=lambda m: -m["lines"])



# ---------------------------------------------------------------- modules -------

def module_graph(engine: QueryEngine) -> dict:
    """Dependances entre fichiers sur le code compile: A depend de B quand
    une fonction de A appelle (ou passe par adresse) une fonction de B.
    Cycles (composantes fortement connexes) et aretes a couper: dans chaque
    paire mutuelle, le sens le plus leger est le plus simple a supprimer."""
    rows = engine.db.execute(
        "SELECT fc.path AS src, fd.path AS dst, COUNT(*) AS calls, "
        "COUNT(DISTINCT r.name) AS functions "
        "FROM refs r "
        "JOIN files fc ON r.file_id = fc.id "
        "JOIN symbols d ON d.name = r.name AND d.kind = 'function' AND d.active = 1 "
        "JOIN files fd ON d.file_id = fd.id "
        "WHERE r.kind IN ('call', 'ref', 'decl') AND r.active = 1 AND fc.id != fd.id "
        "AND fc.origin = 'project' AND fd.origin = 'project' "
        "GROUP BY fc.path, fd.path ORDER BY calls DESC"
    ).fetchall()

    edges = [
        {"from": engine.rel(r["src"]), "to": engine.rel(r["dst"]),
         "calls": r["calls"], "functions": r["functions"]}
        for r in rows
    ]
    modules: dict[str, dict] = {}
    for edge in edges:
        modules.setdefault(edge["from"], {"module": edge["from"], "uses": 0, "used_by": 0})
        modules.setdefault(edge["to"], {"module": edge["to"], "uses": 0, "used_by": 0})
        modules[edge["from"]]["uses"] += 1
        modules[edge["to"]]["used_by"] += 1

    weight = {(e["from"], e["to"]): e["calls"] for e in edges}
    mutual = []
    for (a, b), calls in weight.items():
        if a < b and (b, a) in weight:
            back = weight[(b, a)]
            light = (a, b, calls) if calls <= back else (b, a, back)
            mutual.append({"files": [a, b], "calls": [calls, back],
                           "cut_suggestion": {"from": light[0], "to": light[1], "calls": light[2]}})
    mutual.sort(key=lambda m: m["cut_suggestion"]["calls"])
    cycles = _strongly_connected(edges)
    return {
        "modules": sorted(modules.values(), key=lambda m: (-(m["uses"] + m["used_by"]), m["module"])),
        "edges": edges,
        "mutual": mutual,
        "cycles": cycles,
    }


def _strongly_connected(edges: list[dict]) -> list[dict]:
    """Groupes de fichiers qui dependent tous les uns des autres (Tarjan)."""
    graph: dict[str, list[str]] = {}
    for e in edges:
        graph.setdefault(e["from"], []).append(e["to"])
        graph.setdefault(e["to"], [])
    index, low, stack, on, out = {}, {}, [], set(), []
    counter = [0]

    def visit(start: str) -> None:
        work = [(start, iter(graph[start]))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on.add(child)
                    work.append((child, iter(graph[child])))
                    advanced = True
                    break
                if child in on:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                group = []
                while True:
                    item = stack.pop()
                    on.discard(item)
                    group.append(item)
                    if item == node:
                        break
                if len(group) > 1:
                    out.append(sorted(group))

    for node in graph:
        if node not in index:
            visit(node)
    inner = {(e["from"], e["to"]): e["calls"] for e in edges}
    result = []
    for group in out:
        members = set(group)
        links = sum(1 for (a, b) in inner if a in members and b in members)
        result.append({"files": group, "size": len(group), "internal_edges": links})
    return sorted(result, key=lambda c: -c["size"])


def render_graph(edges: list[dict], fmt: str, limit: int = 60) -> str:
    """Graphe en Mermaid ou DOT, aretes les plus lourdes d'abord."""
    chosen = edges[:limit]
    names = sorted({e["from"] for e in chosen} | {e["to"] for e in chosen})
    ids = {n: f"m{i}" for i, n in enumerate(names)}
    if fmt == "dot":
        lines = ["digraph modules {", "  rankdir=LR;", "  node [shape=box];"]
        lines += [f'  {ids[n]} [label="{n}"];' for n in names]
        lines += [f'  {ids[e["from"]]} -> {ids[e["to"]]} [label="{e["calls"]}"];' for e in chosen]
        return "\n".join([*lines, "}"])
    lines = ["graph LR"]
    lines += [f'  {ids[n]}["{n}"]' for n in names]
    lines += [f'  {ids[e["from"]]} -->|{e["calls"]}| {ids[e["to"]]}' for e in chosen]
    return "\n".join(lines)


# ------------------------------------------------------------- complexite -------

_THRESHOLDS = {"cyclomatic": (10, 20, 50), "lines": (100, 200, 400), "cognitive": (15, 30, 60),
               "params": (5, 7, 10), "frame_bytes": (8192, 32768, 131072)}


def complexity(engine: QueryEngine, sort: str = "complexity", thresholds: dict | None = None) -> list[dict]:
    """Taille, complexite cyclomatique et cognitive, imbrication, parametres,
    variables locales et pile approchee, sur le code compile."""
    limits = {**_THRESHOLDS, **{k: tuple(v) for k, v in (thresholds or {}).items() if k in _THRESHOLDS}}
    order = {
        "complexity": "s.complexity DESC",
        "cognitive": "s.cognitive DESC",
        "lines": "(s.line_end - s.line_start) DESC",
        "depth": "s.depth DESC, s.complexity DESC",
        "frame": "s.frame_bytes DESC",
        "params": "s.params DESC",
    }.get(sort, "s.complexity DESC")
    calls = {r[0]: r[1] for r in engine.db.execute(
        "SELECT enclosing, COUNT(*) FROM refs WHERE kind = 'call' AND active = 1 GROUP BY enclosing")}
    rows = engine.db.execute(
        "SELECT s.name, s.line_start, s.line_end, s.complexity, s.cognitive, s.depth, s.locals, "
        "s.frame_bytes, s.params, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        f"WHERE s.kind = 'function' AND s.active = 1 AND f.origin = 'project' ORDER BY {order}, s.name"
    ).fetchall()
    out = []
    for r in rows:
        lines = r["line_end"] - r["line_start"] + 1
        try:
            params = len([p for p in json.loads(r["params"] or "[]") if p.strip() != "void"])
        except ValueError:
            params = 0
        values = {"cyclomatic": r["complexity"] or 0, "lines": lines, "cognitive": r["cognitive"] or 0,
                  "params": params, "frame_bytes": r["frame_bytes"] or 0}
        level = max(sum(values[k] > limit for limit in limits[k]) for k in values)
        reasons = sorted(k for k in values if values[k] > limits[k][1])
        out.append({
            "name": r["name"], "file": engine.rel(r["path"]), "line": r["line_start"],
            "lines": lines, "complexity": r["complexity"], "cognitive": r["cognitive"],
            "depth": r["depth"], "params": params, "locals": r["locals"],
            "frame_bytes": r["frame_bytes"], "calls": calls.get(r["name"], 0),
            "risk": ("faible", "modere", "eleve", "tres_eleve")[level],
            **({"over": reasons} if reasons else {}),
        })
    if sort == "params":
        out.sort(key=lambda x: -x["params"])
    return out


def duplicates(engine: QueryEngine) -> list[dict]:
    """Fonctions de meme forme (noms, nombres et chaines mis a part): copies
    adaptees d'un meme modele, a factoriser ou a corriger ensemble."""
    groups: dict[str, list] = {}
    for r in engine.db.execute(
        "SELECT s.name, s.line_start, s.line_end, s.shape_hash, f.path FROM symbols s "
        "JOIN files f ON s.file_id = f.id WHERE s.kind = 'function' AND s.active = 1 "
        "AND s.shape_hash IS NOT NULL AND f.origin = 'project'"
    ):
        groups.setdefault(r["shape_hash"], []).append(r)
    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        sizes = [m["line_end"] - m["line_start"] + 1 for m in members]
        out.append({"functions": [{"name": m["name"], "file": engine.rel(m["path"]),
                                   "line": m["line_start"], "lines": n}
                                  for m, n in zip(members, sizes, strict=True)],
                    "count": len(members), "duplicated_lines": sum(sorted(sizes)[:-1])})
    out.sort(key=lambda g: -g["duplicated_lines"])
    return out


def by_file(rows: list[dict]) -> list[dict]:
    files: dict[str, dict] = {}
    for row in rows:
        entry = files.setdefault(row["file"], {"file": row["file"], "functions": 0, "lines": 0,
                                               "max_complexity": 0, "high_risk": 0})
        entry["functions"] += 1
        entry["lines"] += row["lines"]
        entry["max_complexity"] = max(entry["max_complexity"], row["complexity"] or 0)
        entry["high_risk"] += row["risk"] in ("eleve", "tres_eleve")
    return sorted(files.values(), key=lambda f: (-f["high_risk"], -f["max_complexity"]))


__all__ = [
    "DEAD_NOTES",
    "LOCK_NOTES",
    "LOCK_PAIRS",
    "complexity",
    "dead_code",
    "lock_check",
    "module_graph",
    "page",
]
