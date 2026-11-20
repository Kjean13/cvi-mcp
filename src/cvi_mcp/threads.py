# ==============================================================================
#  cvi_mcp/threads.py
#  Role     Contextes d'execution: thread de l'interface, pool de threads,
#           timers asynchrones. Globales partagees, flux de donnees, verrous
#           suivis ligne par ligne et d'appelant en appelant.
#  Etat     stable
#  Version  0.3.3
# ==============================================================================

# ============================ Standard ========================================
import re

# ============================ Interne =========================================
from .queries import QueryEngine
from .risks import LOCK_PAIRS, lock_map

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

# Appels dont le deuxieme argument (position 1) est la constante du controle
# touche: SetCtrlVal(panneau, CONTROLE, ...). Les Canvas* s'y ajoutent par prefixe.
UI_CONTROL_CALLS = frozenset(
    ["SetCtrlVal", "GetCtrlVal", "SetCtrlAttribute", "GetCtrlAttribute", "InsertListItem",
     "DeleteListItem", "ClearListCtrl", "PlotY", "PlotXY", "DeleteGraphPlot", "SetCtrlIndex"]
)
_CONTROL_CONSTANT = re.compile(r"[A-Z][A-Z0-9_]*")

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
    """Fonctions qui prennent un verrou quelque part (comptage par contexte)."""
    marks = ",".join("?" for _ in LOCK_PAIRS)
    return {r[0] for r in engine.db.execute(
        f"SELECT DISTINCT enclosing FROM refs WHERE kind = 'call' AND active = 1 AND name IN ({marks})",
        list(LOCK_PAIRS))}


# ----------------------------------------------------------- verrous tenus --------

class LockMap:
    """Ou un verrou est tenu: ligne par ligne, puis d'appelant en appelant.

    Deux questions, deux reponses.

    holding(fonction, ligne): les verrous tenus a coup sur a cet endroit. Ils
    viennent de la fonction elle-meme (risks.lock_map suit les prises et
    liberations chemin par chemin, retours anticipes compris) et de ses
    appelants: la fonction herite des verrous communs a tous ses appels
    (Seuillage appelle LabelFunction sous giVerrouVinci: LabelFunction et tout
    ce qu'elle appelle tiennent giVerrouVinci).

    protected(fonction, ligne): un verrou quelconque est-il tenu? Vrai aussi
    quand chaque appel de la fonction se fait sous un verrou, meme different
    d'un appel a l'autre (aucun verrou commun, donc rien dans holding).

    L'heritage est un plus petit point fixe: une boucle d'appels ne se protege
    pas elle-meme. Une fonction que rien n'appelle dans le projet, un callback,
    une fonction lancee dans un thread, main, ou dont l'adresse est prise, peut
    etre appelee de l'exterieur: elle n'herite de rien."""

    def __init__(self, engine: QueryEngine, entries: list[dict]) -> None:
        self.lines = lock_map(engine)
        self.inherited, self.covered = self._inherit(engine, entries)

    def own(self, function: str, line: int, col: int | None = None) -> frozenset[str]:
        """Verrous pris par la fonction elle-meme autour de cette position."""
        held = self.lines.get(function)
        return held.at(line, col) if held else frozenset()

    def holding(self, function: str, line: int, col: int | None = None) -> frozenset[str]:
        return self.own(function, line, col) | self.inherited.get(function, frozenset())

    def protected(self, function: str, line: int, col: int | None = None) -> bool:
        return bool(self.holding(function, line, col)) or function in self.covered

    def _inherit(self, engine: QueryEngine, entries: list[dict]) -> tuple[dict, set]:
        functions = {r[0] for r in engine.db.execute(
            "SELECT name FROM symbols WHERE kind = 'function' AND active = 1")}
        # Appelables de l'exterieur: points d'entree, callbacks, main, adresse prise.
        open_to_outside = {e["function"] for e in entries} | {
            r[0] for r in engine.db.execute(
                "SELECT name FROM symbols WHERE kind = 'function' AND active = 1 "
                "AND (is_callback = 1 OR name IN ('main', 'WinMain'))")}
        callers: dict[str, list[tuple[str, int, int]]] = {}
        for r in engine.db.execute(
            "SELECT name, kind, enclosing, line, col FROM refs WHERE active = 1 "
            "AND kind IN ('call', 'ref') AND name IN (SELECT name FROM symbols "
            "WHERE kind = 'function' AND active = 1)"
        ):
            if r["kind"] == "ref":
                open_to_outside.add(r["name"])
            elif r["enclosing"] in functions and r["enclosing"] != r["name"]:
                callers.setdefault(r["name"], []).append((r["enclosing"], r["line"], r["col"]))
        inherited: dict[str, frozenset[str]] = {}
        covered: set[str] = set()
        changed = True
        while changed:
            changed = False
            for function, sites in callers.items():
                if function in open_to_outside:
                    continue
                held_at_calls, every_call_locked = [], True
                for caller, line, col in sites:
                    held = self.own(caller, line, col) | inherited.get(caller, frozenset())
                    held_at_calls.append(held)
                    every_call_locked = every_call_locked and bool(held or caller in covered)
                if not every_call_locked:
                    continue
                common = frozenset.intersection(*held_at_calls)
                if function not in covered or inherited.get(function) != common:
                    covered.add(function)
                    inherited[function] = common
                    changed = True
        return {f: held for f, held in inherited.items() if held}, covered


def _concurrent(first: set[str], second: set[str]) -> bool:
    """Deux ensembles de contextes peuvent-ils s'executer en meme temps? Il faut
    deux contextes differents dont un secondaire: l'interface seule est un seul thread."""
    return any(a != b and (a != "ui" or b != "ui") for a in first for b in second)


def threads(engine: QueryEngine) -> dict:
    where, entries = contexts(engine)
    locking = _locking_functions(engine)
    locks = LockMap(engine, entries)
    summary: dict[str, dict] = {}
    for fn, labels in where.items():
        for label in labels:
            entry = summary.setdefault(label, {"context": label, "functions": 0, "ui_calls": 0,
                                               "ui_sensitive_calls": 0, "functions_taking_locks": 0})
            entry["functions"] += 1
            if fn in locking:
                entry["functions_taking_locks"] += 1
    for r in engine.db.execute(
        "SELECT enclosing, name FROM refs WHERE kind = 'call' AND active = 1 AND enclosing IS NOT NULL"
    ):
        if r["name"] in UI_ANY or r["name"] in UI_SENSITIVE:
            for label in where.get(r["enclosing"], ()):
                key = "ui_sensitive_calls" if r["name"] in UI_SENSITIVE else "ui_calls"
                summary[label][key] += 1
    shared = shared_globals(engine, where, locks)
    return {
        "entry_points": entries,
        "ui_timers": ui_timers(engine),
        "contexts": sorted(summary.values(), key=lambda c: (c["context"] != "ui", c["context"])),
        "shared_globals": shared,
    }


def _global_sites(engine: QueryEngine, names: set[str]) -> dict[str, list[dict]]:
    """Chaque lecture ou ecriture d'une globale: fonction, fichier, ligne."""
    sites: dict[str, list[dict]] = {}
    for r in engine.db.execute(
        "SELECT r.name, r.kind, r.enclosing, r.line, r.col, f.path FROM refs r "
        "JOIN files f ON r.file_id = f.id "
        "WHERE r.active = 1 AND r.kind IN ('read', 'write') AND r.enclosing IS NOT NULL"
    ):
        if r["name"] in names:
            sites.setdefault(r["name"], []).append({
                "function": r["enclosing"], "line": r["line"], "col": r["col"], "kind": r["kind"],
                "file": engine.rel(r["path"])})
    return sites


def shared_globals(engine: QueryEngine, where: dict, locks: LockMap) -> list[dict]:
    """Globales ecrites dans un contexte et lues ou ecrites dans un autre.

    Un acces est protege quand un verrou est tenu a cette ligne (voir LockMap).
    functions_without_lock liste les fonctions qui ont au moins un acces non
    protege, unprotected_accesses les premiers, avec leur ligne. Si des acces
    sont proteges par des verrous differents sans verrou commun, conflicting_locks
    donne un exemple (deux contextes qui s'executent ensemble s'excluent mal)."""
    globals_ = {r["name"]: r["signature"] or "" for r in engine.db.execute(
        "SELECT s.name, s.signature FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'global' AND f.origin = 'project'")}
    sites = _global_sites(engine, set(globals_))
    out = []
    for name, accesses in sites.items():
        writes: dict[str, set[str]] = {}
        reads: dict[str, set[str]] = {}
        for site in accesses:
            bucket = writes if site["kind"] == "write" else reads
            for label in where.get(site["function"], {"ui"}):
                bucket.setdefault(label, set()).add(site["function"])
        touched = set(writes) | set(reads)
        if not writes or len(touched) < 2 or not any(t != "ui" for t in touched):
            continue
        for site in accesses:
            site["held"] = locks.holding(site["function"], site["line"], site["col"])
            site["protected"] = locks.protected(site["function"], site["line"], site["col"])
        unprotected = sorted((s for s in accesses if not s["protected"]),
                             key=lambda s: (s["file"], s["line"]))
        signature = globals_[name]
        scalar = "[" not in signature and not any(t in signature for t in ("struct", "*"))
        item = {
            "global": name,
            "written_in": sorted(writes),
            "accessed_in": sorted(touched),
            "functions_without_lock": sorted({s["function"] for s in unprotected})[:10],
            "unprotected_accesses": [
                {"function": s["function"], "file": s["file"], "line": s["line"]}
                for s in unprotected[:5]],
            "confidence": "faible" if scalar else "moyenne",
        }
        conflict = _lock_conflict(accesses, where)
        if conflict:
            item["conflicting_locks"] = conflict
        out.append(item)
    out.sort(key=lambda g: (g["confidence"] != "moyenne", -len(g["functions_without_lock"]), g["global"]))
    return out


def _lock_conflict(accesses: list[dict], where: dict) -> dict | None:
    """Deux acces proteges par des verrous sans rien en commun, depuis deux
    contextes qui s'executent ensemble, dont une ecriture."""
    seen = {}
    for site in accesses:
        held = site["held"]
        if held:
            seen.setdefault((site["function"], held, site["kind"]), site)
    keys = list(seen)
    for i, first in enumerate(keys):
        for second in keys[i + 1:]:
            if "write" not in (first[2], second[2]) or first[1] & second[1]:
                continue
            if _concurrent(where.get(first[0], {"ui"}), where.get(second[0], {"ui"})):
                a, b = seen[first], seen[second]
                return {"locks": sorted(first[1] | second[1]),
                        "first": {"function": a["function"], "file": a["file"], "line": a["line"],
                                  "locks": sorted(first[1])},
                        "second": {"function": b["function"], "file": b["file"], "line": b["line"],
                                   "locks": sorted(second[1])}}
    return None


def _ui_control_risks(engine: QueryEngine, where: dict, locks: LockMap) -> list[dict]:
    """Controle d'interface touche sans son verrou habituel.

    Pour chaque constante de controle (TELE_CANVAS), le verrou habituel est
    celui qui est tenu le plus souvent quand on la touche. Un acces qui ne le
    tient pas, alors que plusieurs contextes utilisent le controle, est signale:
    c'est le canvas dessine par quatre threads dont un SetCtrlAttribute oublie
    le verrou. Tenir un autre verrou ne protege pas: giVerrouVinci n'exclut pas
    l'affichage qui prend giVerrouTele."""
    marks = ",".join("?" for _ in UI_CONTROL_CALLS)
    by_control: dict[str, list[dict]] = {}
    for r in engine.db.execute(
        "SELECT r.name, r.line, r.col, r.enclosing, f.path, a.text AS control FROM refs r "
        "JOIN files f ON r.file_id = f.id JOIN call_args a ON a.ref_id = r.id AND a.pos = 1 "
        "WHERE r.kind = 'call' AND r.active = 1 AND f.origin = 'project' "
        "AND r.enclosing IS NOT NULL "
        f"AND (r.name IN ({marks}) OR r.name LIKE 'Canvas%')", list(UI_CONTROL_CALLS),
    ):
        control = r["control"].strip()
        if _CONTROL_CONSTANT.fullmatch(control):
            by_control.setdefault(control, []).append({
                "function": r["enclosing"], "line": r["line"], "col": r["col"],
                "file": engine.rel(r["path"]),
                "held": locks.holding(r["enclosing"], r["line"], r["col"]),
                "own": locks.own(r["enclosing"], r["line"], r["col"])})
    out = []
    for control, sites in by_control.items():
        # Le verrou habituel se compte en fonctions qui le prennent elles-memes
        # autour de l'acces, pas en lignes ni par heritage: un verrou herite par
        # une fonction qui touche dix fois le controle ne doit pas peser dix fois.
        users: dict[str, set[str]] = {}
        for site in sites:
            for handle in site["own"]:
                users.setdefault(handle, set()).add(site["function"])
        tally = {handle: len(functions) for handle, functions in users.items()}
        contexts_seen = set().union(*(where.get(s["function"], {"ui"}) for s in sites))
        if not tally or not any(c != "ui" for c in contexts_seen):
            continue
        usual = max(sorted(tally), key=lambda h: tally[h])
        free = [s for s in sites if usual not in s["held"]]
        for function in sorted({s["function"] for s in free}):
            here = sorted((s for s in free if s["function"] == function), key=lambda s: s["line"])
            wrong = sorted(set().union(*(s["held"] for s in here)))
            elsewhere = any(usual in s["held"] for s in sites if s["function"] == function)
            secondary = any(c != "ui" for c in where.get(function, {"ui"}))
            out.append({
                "check": "ui_control_unlocked", "function": function, "file": here[0]["file"],
                "line": here[0]["line"], "lines": [s["line"] for s in here][:10],
                "control": control,
                "locks": [usual], "contexts": sorted(contexts_seen),
                "confidence": "moyenne" if secondary or elsewhere else "faible",
                "why": f"{control} est protege par {usual} dans {tally[usual]} fonction(s): ici "
                       + (f"seul {', '.join(wrong)} est tenu" if wrong else "aucun verrou")
                       + ", un autre thread peut l'utiliser en meme temps"})
    return out


def thread_risks(engine: QueryEngine) -> list[dict]:
    """Alertes pour l'outil risks: interface manipulee hors du thread de
    l'interface, globales partagees sans verrou ou sous des verrous differents,
    controle d'interface a moitie protege."""
    where, entries = contexts(engine)
    if not entries:
        return []
    locks = LockMap(engine, entries)
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
    for item in shared_globals(engine, where, locks):
        first = item["unprotected_accesses"][0] if item["unprotected_accesses"] else None
        if first:
            out.append({"check": "shared_global_unlocked", "function": first["function"],
                        "file": first["file"], "line": first["line"], "global": item["global"],
                        "contexts": item["accessed_in"], "confidence": item["confidence"],
                        "why": "globale ecrite et lue depuis plusieurs contextes, avec au moins "
                               "un acces sans verrou (voir unprotected_accesses)"})
        conflict = item.get("conflicting_locks")
        if conflict:
            a, b = conflict["first"], conflict["second"]
            out.append({"check": "inconsistent_lock", "function": a["function"], "file": a["file"],
                        "line": a["line"], "global": item["global"], "locks": conflict["locks"],
                        "other": {"function": b["function"], "file": b["file"], "line": b["line"]},
                        "contexts": item["accessed_in"], "confidence": "moyenne",
                        "why": f"globale protegee par {', '.join(a['locks'])} ici et par "
                               f"{', '.join(b['locks'])} dans {b['function']}: deux verrous "
                               "differents ne s'excluent pas"})
    out.extend(_ui_control_risks(engine, where, locks))
    return out


def data_flow(engine: QueryEngine, name: str) -> dict:
    """Qui ecrit et qui lit une globale, depuis quel contexte, verrou ou non.

    takes_lock dit si la fonction prend un verrou quelque part; unlocked_lines
    donne les lignes ou elle touche la globale sans en tenir un, et
    protected_by_callers qu'elle n'est jamais appelee sans verrou."""
    where, entries = contexts(engine)
    locking = _locking_functions(engine)
    locks = LockMap(engine, entries)
    rows = engine.db.execute(
        "SELECT r.kind, r.enclosing, r.line, r.col, f.path FROM refs r "
        "JOIN files f ON r.file_id = f.id "
        "WHERE r.name = ? AND r.active = 1 AND r.kind IN ('read', 'write', 'ref') "
        "AND r.enclosing IS NOT NULL ORDER BY f.path, r.line", (name,),
    ).fetchall()
    functions: dict[str, dict] = {}
    for r in rows:
        entry = functions.setdefault(r["enclosing"], {
            "function": r["enclosing"], "file": engine.rel(r["path"]), "reads": 0, "writes": 0,
            "contexts": sorted(where.get(r["enclosing"], {"ui"})),
            "takes_lock": r["enclosing"] in locking, "unlocked_lines": [],
            "protected_by_callers": r["enclosing"] in locks.covered})
        entry["writes" if r["kind"] == "write" else "reads"] += 1
        if not locks.protected(r["enclosing"], r["line"], r["col"]):
            entry["unlocked_lines"].append(r["line"])
    items = sorted(functions.values(), key=lambda f: (-f["writes"], f["function"]))
    out = {"global": name, "writers": [f for f in items if f["writes"]],
           "readers_only": [f for f in items if not f["writes"]]}
    if not items:
        out["reason"] = f"aucune lecture ni ecriture de '{name}' dans une fonction compilee"
    return out


THREAD_NOTES = {
    "ui_from_thread": "panneau ou popup manipule depuis un thread secondaire",
    "shared_global_unlocked": "globale partagee entre contextes, avec au moins un acces sans "
    "verrou: la ligne est protegee si un verrou y est tenu (chemin par chemin) ou si tous "
    "les appels de sa fonction se font verrou tenu",
    "inconsistent_lock": "globale protegee par un verrou ici et par un autre ailleurs, sans "
    "verrou commun: les deux acces peuvent s'executer en meme temps",
    "ui_control_unlocked": "controle d'interface (souvent un canvas) touche verrou tenu a un "
    "endroit et sans verrou a un autre, alors que plusieurs threads l'utilisent",
}
