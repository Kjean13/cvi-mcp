# ==============================================================================
#  cvi_mcp/threads.py
#  Role     Contextes d'execution: thread de l'interface, pool de threads,
#           timers asynchrones. Globales partagees, flux de donnees.
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Interne =========================================
from .queries import QueryEngine
from .risks import LOCK_PAIRS

# Fonction qui lance du code ailleurs: position de la fonction lancee et
# contexte ou elle s'execute.
LAUNCHERS = {
    "CmtScheduleThreadPoolFunction": (1, "thread_pool"),
    "CmtScheduleThreadPoolFunctionAdv": (1, "thread_pool"),
    "NewAsyncTimer": (3, "async_timer"),
    "NewAsyncTimerWithPriority": (3, "async_timer"),
    "CreateThread": (2, "os_thread"),
    "_beginthread": (0, "os_thread"),
    "_beginthreadex": (2, "os_thread"),
    "PostDeferredCall": (0, "ui"),
    "PostDeferredCallToThread": (0, "deferred"),
    "PostDeferredCallToThreadAndWait": (0, "deferred"),
}

# Appels d'interface a eviter hors du thread qui possede les panneaux.
UI_SENSITIVE = frozenset(
    ["LoadPanel", "LoadPanelEx", "NewPanel", "DuplicatePanel", "DiscardPanel", "DisplayPanel", "HidePanel", "InstallPopup", "RemovePopup", "MessagePopup", "ConfirmPopup", "PromptPopup", "GenericMessagePopup", "FileSelectPopup", "FileSelectPopupEx", "DirSelectPopup", "RunUserInterface", "QuitUserInterface", "SetActivePanel", "LoadMenuBar", "DiscardMenuBar"]
)
UI_ANY = frozenset(
    ["SetCtrlVal", "GetCtrlVal", "SetCtrlAttribute", "GetCtrlAttribute", "SetPanelAttribute", "GetPanelAttribute", "InsertListItem", "DeleteListItem", "ClearListCtrl", "PlotY", "PlotXY", "DeleteGraphPlot", "SetCtrlIndex", "CanvasDrawBitmap", "ProcessDrawEvents", "ProcessSystemEvents"]
)

_SECONDARY = ("thread_pool", "async_timer", "os_thread")


def entry_points(engine: QueryEngine) -> list[dict]:
    """Fonctions lancees dans un autre contexte, avec le lieu du lancement."""
    marks = ",".join("?" for _ in LAUNCHERS)
    functions = {r[0] for r in engine.db.execute(
        "SELECT name FROM symbols WHERE kind = 'function' AND active = 1")}
    out = []
    for r in engine.db.execute(
        "SELECT r.id, r.name, r.line, r.enclosing, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        f"WHERE r.kind = 'call' AND r.active = 1 AND r.name IN ({marks})", list(LAUNCHERS),
    ):
        index, context = LAUNCHERS[r["name"]]
        row = engine.db.execute(
            "SELECT text FROM call_args WHERE ref_id = ? AND pos = ?", (r["id"], index)).fetchone()
        target = row[0].lstrip("&").strip() if row else None
        if target in functions:
            entry = {"function": target, "context": context, "via": r["name"],
                     "launched_in": r["enclosing"], "file": engine.rel(r["path"]), "line": r["line"]}
            if context == "async_timer":
                interval = engine.db.execute(
                    "SELECT text FROM call_args WHERE ref_id = ? AND pos = 0", (r["id"],)).fetchone()
                if interval:
                    entry["interval_s"] = interval[0]
            out.append(entry)
    return out


def ui_timers(engine: QueryEngine) -> list[dict]:
    """Controles timer des .uir: ils tournent dans le thread de l'interface."""
    from .uimap import UiMap

    uir = UiMap(engine).uir
    if not uir:
        return []
    grouped: dict[str, dict] = {}
    for name, info in sorted(uir["controls"].items()):
        if (info.get("type") or "").lower() != "timer":
            continue
        key = info.get("callback") or "(aucune)"
        entry = grouped.setdefault(key, {"callback": key, "controls": 0, "panels": []})
        entry["controls"] += 1
        panel = uir["owners"].get(name)
        if panel and panel not in entry["panels"]:
            entry["panels"].append(panel)
    return sorted(grouped.values(), key=lambda g: -g["controls"])


def _graph(engine: QueryEngine) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for r in engine.db.execute(
        "SELECT DISTINCT enclosing, name FROM refs WHERE kind = 'call' AND active = 1 "
        "AND enclosing IS NOT NULL"
    ):
        edges.setdefault(r["enclosing"], set()).add(r["name"])
    return edges


def _reach(start: str, edges: dict[str, set[str]], stop: set[str]) -> set[str]:
    seen, stack = set(), [start]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(n for n in edges.get(name, ()) if n not in stop)
    return seen


def contexts(engine: QueryEngine) -> tuple[dict[str, set[str]], list[dict]]:
    """Pour chaque fonction, les contextes d'ou elle peut s'executer."""
    entries = entry_points(engine)
    edges = _graph(engine)
    launched = {e["function"] for e in entries}
    where: dict[str, set[str]] = {}
    for entry in entries:
        context = entry["context"] if entry["context"] in _SECONDARY else "ui"
        label = f"{context}:{entry['function']}" if context != "ui" else "ui"
        for fn in _reach(entry["function"], edges, launched - {entry["function"]}):
            where.setdefault(fn, set()).add(label)
    roots = {r[0] for r in engine.db.execute(
        "SELECT name FROM symbols WHERE kind = 'function' AND active = 1 "
        "AND (name IN ('main', 'WinMain') OR is_callback = 1)")} - {
            e["function"] for e in entries if e["context"] in _SECONDARY}
    for root in roots:
        for fn in _reach(root, edges, launched):
            where.setdefault(fn, set()).add("ui")
    return where, entries


def _locking_functions(engine: QueryEngine) -> set[str]:
    marks = ",".join("?" for _ in LOCK_PAIRS)
    return {r[0] for r in engine.db.execute(
        f"SELECT DISTINCT enclosing FROM refs WHERE kind = 'call' AND active = 1 AND name IN ({marks})",
        list(LOCK_PAIRS))}


def threads(engine: QueryEngine) -> dict:
    where, entries = contexts(engine)
    locking = _locking_functions(engine)
    summary: dict[str, dict] = {}
    for fn, labels in where.items():
        for label in labels:
            entry = summary.setdefault(label, {"context": label, "functions": 0, "ui_calls": 0,
                                               "ui_sensitive_calls": 0, "functions_taking_locks": 0})
            entry["functions"] += 1
            if fn in locking:
                entry["functions_taking_locks"] += 1
    ui_counts = {}
    for r in engine.db.execute(
        "SELECT enclosing, name FROM refs WHERE kind = 'call' AND active = 1 AND enclosing IS NOT NULL"
    ):
        if r["name"] in UI_ANY or r["name"] in UI_SENSITIVE:
            for label in where.get(r["enclosing"], ()):
                key = "ui_sensitive_calls" if r["name"] in UI_SENSITIVE else "ui_calls"
                summary[label][key] += 1
            ui_counts[r["enclosing"]] = ui_counts.get(r["enclosing"], 0) + 1
    shared = shared_globals(engine, where, locking)
    return {
        "entry_points": entries,
        "ui_timers": ui_timers(engine),
        "contexts": sorted(summary.values(), key=lambda c: (c["context"] != "ui", c["context"])),
        "shared_globals": shared,
    }


def shared_globals(engine: QueryEngine, where: dict, locking: set[str]) -> list[dict]:
    """Globales ecrites dans un contexte et lues ou ecrites dans un autre."""
    globals_ = {r["name"]: r["signature"] or "" for r in engine.db.execute(
        "SELECT s.name, s.signature FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'global' AND f.origin = 'project'")}
    access: dict[str, dict] = {}
    for r in engine.db.execute(
        "SELECT name, kind, enclosing FROM refs WHERE active = 1 AND kind IN ('read', 'write') "
        "AND enclosing IS NOT NULL"
    ):
        if r["name"] not in globals_:
            continue
        entry = access.setdefault(r["name"], {"writes": {}, "reads": {}})
        bucket = "writes" if r["kind"] == "write" else "reads"
        for label in where.get(r["enclosing"], {"ui"}):
            entry[bucket].setdefault(label, set()).add(r["enclosing"])
    out = []
    for name, entry in access.items():
        writers = set(entry["writes"])
        touched = writers | set(entry["reads"])
        if not writers or len(touched) < 2 or not any(t != "ui" for t in touched):
            continue
        functions = {fn for side in entry.values() for fns in side.values() for fn in fns}
        unlocked = sorted(fn for fn in functions if fn not in locking)
        signature = globals_[name]
        scalar = "[" not in signature and not any(t in signature for t in ("struct", "*"))
        out.append({
            "global": name,
            "written_in": sorted(writers),
            "accessed_in": sorted(touched),
            "functions_without_lock": unlocked[:10],
            "confidence": "faible" if scalar else "moyenne",
        })
    out.sort(key=lambda g: (g["confidence"] != "moyenne", -len(g["functions_without_lock"]), g["global"]))
    return out


def thread_risks(engine: QueryEngine) -> list[dict]:
    """Alertes pour l'outil risks: interface manipulee hors du thread de
    l'interface, globales partagees sans verrou."""
    where, entries = contexts(engine)
    if not entries:
        return []
    locking = _locking_functions(engine)
    out = []
    marks = ",".join("?" for _ in UI_SENSITIVE)
    for r in engine.db.execute(
        "SELECT r.name, r.line, r.enclosing, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        f"WHERE r.kind = 'call' AND r.active = 1 AND r.name IN ({marks})", list(UI_SENSITIVE),
    ):
        secondary = sorted(c for c in where.get(r["enclosing"], ()) if c != "ui")
        if secondary:
            out.append({"check": "ui_from_thread", "function": r["enclosing"],
                        "file": engine.rel(r["path"]), "line": r["line"], "call": r["name"],
                        "contexts": secondary, "confidence": "moyenne",
                        "why": f"{r['name']} appele depuis {', '.join(secondary)}: passer par "
                               "PostDeferredCall pour agir sur l'interface"})
    for item in shared_globals(engine, where, locking):
        if item["functions_without_lock"]:
            locs = engine.db.execute(
                "SELECT s.line_start, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
                "WHERE s.name = ? AND s.kind = 'global' LIMIT 1", (item["global"],)).fetchone()
            out.append({"check": "shared_global_unlocked", "function": item["functions_without_lock"][0],
                        "file": engine.rel(locs["path"]) if locs else "?",
                        "line": locs["line_start"] if locs else 0, "global": item["global"],
                        "contexts": item["accessed_in"], "confidence": item["confidence"],
                        "why": "globale ecrite et lue depuis plusieurs contextes, par des fonctions "
                               "qui ne prennent aucun verrou"})
    return out


def data_flow(engine: QueryEngine, name: str) -> dict:
    """Qui ecrit et qui lit une globale, depuis quel contexte, verrou ou non."""
    where, _entries = contexts(engine)
    locking = _locking_functions(engine)
    rows = engine.db.execute(
        "SELECT r.kind, r.enclosing, r.line, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        "WHERE r.name = ? AND r.active = 1 AND r.kind IN ('read', 'write', 'ref') "
        "AND r.enclosing IS NOT NULL ORDER BY f.path, r.line", (name,),
    ).fetchall()
    functions: dict[str, dict] = {}
    for r in rows:
        entry = functions.setdefault(r["enclosing"], {
            "function": r["enclosing"], "file": engine.rel(r["path"]), "reads": 0, "writes": 0,
            "contexts": sorted(where.get(r["enclosing"], {"ui"})),
            "takes_lock": r["enclosing"] in locking})
        entry["writes" if r["kind"] == "write" else "reads"] += 1
    items = sorted(functions.values(), key=lambda f: (-f["writes"], f["function"]))
    out = {"global": name, "writers": [f for f in items if f["writes"]],
           "readers_only": [f for f in items if not f["writes"]]}
    if not items:
        out["reason"] = f"aucune lecture ni ecriture de '{name}' dans une fonction compilee"
    return out


THREAD_NOTES = {
    "ui_from_thread": "panneau ou popup manipule depuis un thread secondaire",
    "shared_global_unlocked": "globale partagee entre contextes sans verrou dans les "
    "fonctions qui l'utilisent (verrou detecte au niveau de la fonction)",
}
