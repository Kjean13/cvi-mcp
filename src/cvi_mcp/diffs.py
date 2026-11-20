# ==============================================================================
#  cvi_mcp/diffs.py
#  Role     Instantanes nommes de l'index et comparaison entre deux versions
#  Etat     stable
#  Version  0.4.0
# ==============================================================================

# ============================ Standard ========================================
import os
import sqlite3
import tempfile
import time
from pathlib import Path

# ============================ Interne =========================================
from .indexer import Indexer
from .preproc import BuildContext, Preprocessor
from .prj import build_settings, collect_sources, display_root

CURRENT = "current"

# Seules les fonctions portent une empreinte de corps: ce sont elles qui
# changent de logique. Les macros et enumerateurs se comparent par valeur,
# sans quoi un PANEL_MAIN_START passant de 3 a 4 resterait invisible.
_TRACKED = (
    "function",
    "prototype",
    "global",
    "struct",
    "union",
    "enum",
    "enumerator",
    "typedef",
    "macro",
)


# ------------------------------------------------------------------- etat ------

def capture_state(conn: sqlite3.Connection, root: Path | None) -> dict:
    """Etat comparable d'un index: symboles par (kind, name, file), aretes
    d'appel, cles INI. Les en-tetes tiers sont laisses de cote; les
    constantes des .h generes par CVI deviennent des ui_constant."""
    from .mapping import ini_keys

    conn.row_factory = sqlite3.Row
    holders = ", ".join("?" for _ in _TRACKED)
    rows = conn.execute(
        f"SELECT s.name, s.kind, s.signature, s.body_hash, s.value, s.active, f.path, f.origin "
        f"FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.kind IN ({holders}) "
        "AND f.origin != 'library'",
        _TRACKED,
    ).fetchall()

    symbols: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        rel = _relative(row["path"], root)
        kind = "ui_constant" if row["origin"] == "generated" and row["kind"] == "macro" else row["kind"]
        symbols[(kind, row["name"], rel)] = {
            "signature": row["signature"],
            "body_hash": row["body_hash"],
            "value": row["value"],
            "active": row["active"],
        }
    for key in ini_keys(conn):
        symbols[("ini_key", key, "")] = {"signature": None, "body_hash": None, "value": None, "active": 1}

    edges = {
        (row["caller"], row["callee"])
        for row in conn.execute(
            "SELECT DISTINCT enclosing AS caller, name AS callee FROM refs "
            "WHERE kind = 'call' AND enclosing IS NOT NULL AND active = 1"
        )
    }
    files = {
        _relative(row[0], root) for row in conn.execute("SELECT path FROM files WHERE origin != 'library'")
    }
    return {"symbols": symbols, "edges": edges, "files": files}


def restrict(state: dict, pattern: str | None) -> dict:
    """Ne garde que les fichiers dont le chemin relatif correspond au motif."""
    if not pattern:
        return state
    import fnmatch

    needle = pattern.replace("\\", "/").lower()

    def keep(path: str) -> bool:
        low = path.lower()
        return fnmatch.fnmatch(low, needle) or low.startswith(needle.rstrip("/*") + "/") or needle in low

    return {
        "symbols": {k: v for k, v in state["symbols"].items() if k[2] == "" or keep(k[2])},
        "edges": state["edges"],
        "files": {f for f in state["files"] if keep(f)},
    }


def _relative(path: str, root: Path | None) -> str:
    if not root:
        return Path(path).name
    try:
        return Path(os.path.relpath(Path(path).resolve(), Path(root).resolve())).as_posix()
    except ValueError:
        return Path(path).name


# -------------------------------------------------------------- instantanes ----

def save_snapshot(conn: sqlite3.Connection, label: str, root: Path | None, overwrite: bool = False) -> dict:
    if label == CURRENT:
        return {"error": f"'{CURRENT}' est reserve a l'index vivant"}

    existing = conn.execute(
        "SELECT id FROM snapshots WHERE label = ?", (label,)
    ).fetchone()
    if existing and not overwrite:
        return {"error": f"instantane '{label}' deja present, utiliser overwrite"}
    if existing:
        _drop(conn, existing[0])

    state = capture_state(conn, root)
    cursor = conn.execute(
        "INSERT INTO snapshots (label, created_at, root, file_count, symbol_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (label, time.time(), str(root or ""), len(state["files"]), len(state["symbols"])),
    )
    snapshot_id = int(cursor.lastrowid)

    conn.executemany(
        "INSERT INTO snapshot_symbols "
        "(snapshot_id, name, kind, file, signature, body_hash, value, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (snapshot_id, name, kind, file, data["signature"], data["body_hash"], data["value"],
             data.get("active", 1))
            for (kind, name, file), data in state["symbols"].items()
        ],
    )
    conn.executemany(
        "INSERT INTO snapshot_edges (snapshot_id, caller, callee) VALUES (?, ?, ?)",
        [(snapshot_id, caller, callee) for caller, callee in state["edges"]],
    )
    conn.executemany(
        "INSERT INTO snapshot_files (snapshot_id, file) VALUES (?, ?)",
        [(snapshot_id, file) for file in sorted(state["files"])],
    )
    conn.commit()

    return {
        "label": label,
        "files": len(state["files"]),
        "symbols": len(state["symbols"]),
        "call_edges": len(state["edges"]),
    }


def list_snapshots(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    return [
        {
            "label": row["label"],
            "created_at": round(row["created_at"], 0),
            "root": row["root"] or None,
            "files": row["file_count"],
            "symbols": row["symbol_count"],
        }
        for row in conn.execute("SELECT * FROM snapshots ORDER BY created_at")
    ]


def delete_snapshot(conn: sqlite3.Connection, label: str) -> dict:
    row = conn.execute("SELECT id FROM snapshots WHERE label = ?", (label,)).fetchone()
    if row is None:
        return {"error": f"instantane '{label}' introuvable"}
    _drop(conn, row[0])
    conn.commit()
    return {"deleted": label}


def _drop(conn: sqlite3.Connection, snapshot_id: int) -> None:
    conn.execute("DELETE FROM snapshot_symbols WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM snapshot_edges WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM snapshot_files WHERE snapshot_id = ?", (snapshot_id,))
    conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))


def load_snapshot(conn: sqlite3.Connection, label: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id FROM snapshots WHERE label = ?", (label,)).fetchone()
    if row is None:
        return None
    snapshot_id = row["id"]

    symbols = {
        (r["kind"], r["name"], r["file"]): {
            "signature": r["signature"],
            "body_hash": r["body_hash"],
            "value": r["value"],
            "active": r["active"],
        }
        for r in conn.execute(
            "SELECT name, kind, file, signature, body_hash, value, active FROM snapshot_symbols "
            "WHERE snapshot_id = ?",
            (snapshot_id,),
        )
    }
    edges = {
        (r["caller"], r["callee"])
        for r in conn.execute(
            "SELECT caller, callee FROM snapshot_edges WHERE snapshot_id = ?",
            (snapshot_id,),
        )
    }
    files = {r["file"] for r in conn.execute(
        "SELECT file FROM snapshot_files WHERE snapshot_id = ?", (snapshot_id,))}
    if not files:
        # Instantane ancien, pris avant snapshot_files: les fichiers des symboles.
        files = {key[2] for key in symbols if key[2]}
    return {"symbols": symbols, "edges": edges, "files": files}


# ----------------------------------------------------------------- comparaison -

def compare(before: dict, after: dict, label_a: str, label_b: str) -> dict:
    old_keys = set(before["symbols"])
    new_keys = set(after["symbols"])

    removed = old_keys - new_keys
    added = new_keys - old_keys

    # Un symbole de meme nom et meme nature present des deux cotes dans un
    # fichier different a ete deplace, pas supprime puis recree.
    moved: list[dict] = []
    for kind, name, old_file in sorted(removed):
        twins = [k for k in added if k[0] == kind and k[1] == name]
        if len(twins) != 1:
            continue
        twin = twins[0]
        moved.append({"kind": kind, "name": name, "from": old_file, "to": twin[2]})
        added.discard(twin)

    moved_keys = {(m["kind"], m["name"], m["from"]) for m in moved}
    removed -= moved_keys

    body_changed: list[dict] = []
    signature_changed: list[dict] = []
    value_changed: list[dict] = []
    activation_changed: list[dict] = []
    for key in sorted(old_keys & new_keys):
        kind, name, file = key
        old, new = before["symbols"][key], after["symbols"][key]
        if (old.get("active", 1) or 0) != (new.get("active", 1) or 0):
            activation_changed.append({"kind": kind, "name": name, "file": file,
                                       "now": "compile" if new.get("active", 1) else "exclu"})
        if (old["signature"] or "") != (new["signature"] or ""):
            signature_changed.append(
                {
                    "kind": kind,
                    "name": name,
                    "file": file,
                    "before": old["signature"],
                    "after": new["signature"],
                }
            )
        elif (old.get("value") or "") != (new.get("value") or ""):
            value_changed.append(
                {
                    "kind": kind,
                    "name": name,
                    "file": file,
                    "before": old.get("value"),
                    "after": new.get("value"),
                }
            )
        elif old["body_hash"] and new["body_hash"] and old["body_hash"] != new["body_hash"]:
            body_changed.append({"kind": kind, "name": name, "file": file})

    added_calls = after["edges"] - before["edges"]
    removed_calls = before["edges"] - after["edges"]

    def split(keys, wanted):
        return sorted(k for k in keys if k[0] == wanted)

    ini_added, ini_removed = split(added, "ini_key"), split(removed, "ini_key")
    ui_added, ui_removed = split(added, "ui_constant"), split(removed, "ui_constant")
    added = {k for k in added if k[0] not in ("ini_key", "ui_constant")}
    removed = {k for k in removed if k[0] not in ("ini_key", "ui_constant")}
    summary = {
        "activation_changed": len(activation_changed),
        "ini_keys_added": len(ini_added),
        "ini_keys_removed": len(ini_removed),
        "ui_constants_added": len(ui_added),
        "ui_constants_removed": len(ui_removed),
        "added": len(added),
        "removed": len(removed),
        "moved": len(moved),
        "signature_changed": len(signature_changed),
        "value_changed": len(value_changed),
        "body_changed": len(body_changed),
        "calls_added": len(added_calls),
        "calls_removed": len(removed_calls),
        "files_added": len(after["files"] - before["files"]),
        "files_removed": len(before["files"] - after["files"]),
    }

    return {
        "from": label_a,
        "to": label_b,
        "identical": not any(summary.values()),
        "headline": _headline(added, removed, value_changed, signature_changed, body_changed,
                              activation_changed, ui_added, ui_removed, ini_added, ini_removed,
                              after["files"] - before["files"], before["files"] - after["files"]),
        "summary": summary,
        "ui_by_panel": _ui_by_panel(ui_added, ui_removed),
        "ini_keys_added": [k[1] for k in ini_added][:50],
        "ini_keys_removed": [k[1] for k in ini_removed][:50],
        "activation_changed": activation_changed,
        "files_added": sorted(after["files"] - before["files"]),
        "files_removed": sorted(before["files"] - after["files"]),
        "added": [
            {"kind": k, "name": n, "file": f} for k, n, f in sorted(added)
        ],
        "removed": [
            {"kind": k, "name": n, "file": f} for k, n, f in sorted(removed)
        ],
        "moved": moved,
        "signature_changed": signature_changed,
        "value_changed": value_changed,
        "body_changed": body_changed,
        "calls_added": [{"caller": a, "callee": b} for a, b in sorted(added_calls)],
        "calls_removed": [{"caller": a, "callee": b} for a, b in sorted(removed_calls)],
    }


def _ui_by_panel(added, removed) -> list[dict]:
    panels: dict[str, dict] = {}
    for side, keys in (("added", added), ("removed", removed)):
        for _kind, name, file in keys:
            entry = panels.setdefault(file, {"header": file, "added": [], "removed": []})
            entry[side].append(name)
    return [{**p, "added": p["added"][:15], "removed": p["removed"][:15]}
            for p in sorted(panels.values(), key=lambda p: -(len(p["added"]) + len(p["removed"])))]


def _headline(added, removed, values, signatures, bodies, activation, ui_added, ui_removed,
              ini_added, ini_removed, files_added, files_removed) -> list[str]:
    """Le diff en quelques phrases."""
    lines = []

    def count(items, kind):
        return sum(1 for k in items if k[0] == kind)

    for kind, label in (("function", "fonction"), ("macro", "macro"), ("global", "globale")):
        a, r = count(added, kind), count(removed, kind)
        if a or r:
            lines.append(f"{label}s: {a} ajoutee(s), {r} supprimee(s)")
    for item in values[:8]:
        lines.append(f"{item['name']}: {item['before']} -> {item['after']}")
    if len(values) > 8:
        lines.append(f"... {len(values) - 8} autre(s) valeur(s) modifiee(s)")
    if signatures:
        lines.append(f"{len(signatures)} signature(s) modifiee(s)")
    if bodies:
        names = ", ".join(b["name"] for b in bodies[:5])
        lines.append(f"{len(bodies)} corps de fonction modifie(s): {names}" + (" ..." if len(bodies) > 5 else ""))
    if activation:
        lines.append(f"{len(activation)} symbole(s) passe(s) entre compile et exclu par un #if")
    panels = {k[2] for k in list(ui_added) + list(ui_removed)}
    if panels:
        lines.append(f"interface: {len(ui_added)} constante(s) ajoutee(s), {len(ui_removed)} "
                     f"supprimee(s) dans {len(panels)} en-tete(s) .uir")
    if ini_added or ini_removed:
        lines.append(f"INI: {len(ini_added)} cle(s) ajoutee(s), {len(ini_removed)} supprimee(s)")
    if files_added or files_removed:
        lines.append(f"fichiers: {len(files_added)} ajoute(s), {len(files_removed)} supprime(s)")
    return lines or ["aucune difference"]


# ----------------------------------------------------------- impact d'un changement -

# Poids d'une fonction modifiee dans le classement "a relire d'abord".
_WEIGHTS = {"thread": 5, "lock": 3, "risk": 4, "signature": 3, "caller": 0.5}


def change_impact(report: dict, before: dict, after: dict, live: dict | None = None,
                  limit: int = 15) -> dict:
    """Ce qu'un changement risque de casser, a partir de la comparaison de deux etats.

    report est la sortie de compare. Pour chaque fonction ajoutee, retiree, dont le corps ou
    la signature change, ou qui passe de compile a exclu: ses appelants dans la nouvelle
    version (aretes d'appel de l'etat), et, si live le donne, les contextes d'execution, la
    prise de verrous et les alertes de risks qui la visent. Une fonction retiree que
    quelqu'un appelle encore est un appel casse. Une macro dont la valeur change est
    rapportee avec ce qu'elle dimensionne (live).

    live (version vivante seulement): {"contexts": {fonction: [contextes]}, "locking": {fonctions},
    "risks": {fonction: [alertes]}, "macros": {macro: {"arrays_resized", "functions_using"}}}."""
    live = live or {}
    callers: dict[str, set[str]] = {}
    for caller, callee in after["edges"]:
        callers.setdefault(callee, set()).add(caller)
    still_defined = {name for (kind, name, _file) in after["symbols"] if kind == "function"}

    changes: dict[str, dict] = {}

    def note(name: str, file: str, change: str, **extra) -> None:
        entry = changes.setdefault(name, {"name": name, "file": file, "change": [], **extra})
        if change not in entry["change"]:
            entry["change"].append(change)
        entry.update(extra)

    for item in report.get("added", []):
        if item["kind"] == "function":
            note(item["name"], item["file"], "added")
    for item in report.get("body_changed", []):
        if item["kind"] == "function":
            note(item["name"], item["file"], "body")
    for item in report.get("signature_changed", []):
        if item["kind"] == "function":
            note(item["name"], item["file"], "signature",
                 signature_before=item["before"], signature_after=item["after"])
    for item in report.get("activation_changed", []):
        if item["kind"] == "function":
            note(item["name"], item["file"], "compiled" if item["now"] == "compile" else "excluded")
    removed = [i for i in report.get("removed", []) if i["kind"] == "function"]

    functions = []
    for name, entry in changes.items():
        who = sorted(callers.get(name, set()) - {name})
        entry["callers_total"] = len(who)
        entry["callers"] = who[:8]
        contexts = [c for c in live.get("contexts", {}).get(name, []) if c != "ui"]
        if contexts:
            entry["thread_contexts"] = contexts
        if name in live.get("locking", ()):
            entry["takes_lock"] = True
        alerts = live.get("risks", {}).get(name, [])
        if alerts:
            entry["risks"] = alerts[:5]
        entry["score"] = round(
            _WEIGHTS["thread"] * bool(contexts) + _WEIGHTS["lock"] * bool(entry.get("takes_lock"))
            + _WEIGHTS["risk"] * min(len(alerts), 3)
            + _WEIGHTS["signature"] * ("signature" in entry["change"])
            + _WEIGHTS["caller"] * min(len(who), 20), 1)
        functions.append(entry)
    functions.sort(key=lambda e: (-e["score"], e["name"]))

    # Retiree mais encore appelee: l'edition de liens echouera (ou un appel est devenu faux).
    broken = [{"function": i["name"], "file": i["file"], "callers": sorted(callers[i["name"]])[:8]}
              for i in removed if callers.get(i["name"]) and i["name"] not in still_defined]

    macros = []
    for item in report.get("value_changed", []):
        if item["kind"] == "macro":
            macros.append({"name": item["name"], "file": item["file"], "before": item["before"],
                           "after": item["after"], **live.get("macros", {}).get(item["name"], {})})

    review_first = [f["name"] for f in functions if f["score"] > 0][:5]
    summary = {
        "functions_changed": len(functions), "functions_removed": len(removed),
        "broken_calls": len(broken), "macros_changed": len(macros),
        "callers_to_review": len({c for f in functions for c in callers.get(f["name"], ())}),
        "in_threads": sum(1 for f in functions if f.get("thread_contexts")),
        "with_alerts": sum(1 for f in functions if f.get("risks")),
    }
    headline = []
    if broken:
        headline.append(f"{len(broken)} fonction(s) retiree(s) encore appelee(s)")
    if summary["in_threads"]:
        headline.append(f"{summary['in_threads']} fonction(s) modifiee(s) tournent dans un thread")
    if summary["with_alerts"]:
        headline.append(
            f"{summary['with_alerts']} fonction(s) modifiee(s) ont des alertes de risks")
    if macros:
        names = ", ".join(m["name"] for m in macros[:4])
        headline.append(f"{len(macros)} macro(s) changee(s): {names}")
    if not headline:
        headline.append(
            "aucun risque notable releve" if functions or removed else "aucune difference")
    return {"from": report["from"], "to": report["to"], "live": bool(live), "headline": headline,
            "summary": summary, "review_first": review_first, "broken_calls": broken[:limit],
            "functions": functions[:limit], "functions_total": len(functions),
            "macros": macros[:limit]}


def state_of_path(root: Path, is_excluded=None) -> dict:
    """Index jetable d'un autre dossier, pour comparer deux versions posees
    cote a cote sans toucher a l'index courant."""
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="cvi-mcp-diff-") as scratch:
        indexer = Indexer(Path(scratch) / "side.db")
        try:
            sources = collect_sources(root, is_excluded)
            ctx = BuildContext()
            prjs = sorted(root.glob("*.prj"))
            if prjs:
                settings = build_settings(prjs[0])
                ctx = BuildContext(
                    include_paths=[p["found"] for p in settings["include_paths"] if p["found"]],
                    defines=settings["defines"], is_64=settings["is_64"],
                    is_release=settings["is_release"], cvi_version=settings["cvi_version"])
            pp = Preprocessor(sources, ctx)
            pp.run()
            indexer.index_files(sources, pp=pp)
            return capture_state(indexer.db, display_root(root, sources))
        finally:
            indexer.close()
