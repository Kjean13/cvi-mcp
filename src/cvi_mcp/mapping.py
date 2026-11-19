# ==============================================================================
#  cvi_mcp/mapping.py
#  Role     Cartographie metier: domaines, sites d'appel (E/S industrielles),
#           persistance INI et SQL, bibliotheques liees
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import fnmatch
import re
import sqlite3
from pathlib import Path

# ============================ Interne =========================================
from .queries import LIBC, QueryEngine, split_words

INI_READ = re.compile(r"^Ini_(Get|ItemExists|SectionExists|NumberOfItems)")
INI_WRITE = re.compile(r"^Ini_(Put|Remove)")
SQL_START = re.compile(r"^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE)\b", re.IGNORECASE)
_SQL_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE)\s+[\[`\"]?([A-Za-z_][\w.]*)", re.IGNORECASE)
_GENERIC = frozenset(
    ["de", "du", "des", "la", "le", "les", "et", "en", "un", "une", "gestion", "config", "configuration", "fonctions", "fonction", "test", "tests", "main", "init", "commun", "common", "utils", "outils", "util", "fichier", "fichiers", "get", "set", "new", "old", "lib", "src", "include", "inc", "data", "panneau", "panel", "programme", "principal", "manager"]
)


def _unquote(text: str) -> str | None:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1]
    return None


def macro_strings(db: sqlite3.Connection) -> dict[str, str]:
    """Macros dont la valeur est une chaine litterale: cles et sections INI
    sont souvent nommees ainsi."""
    out = {}
    for name, value in db.execute("SELECT name, value FROM symbols WHERE kind = 'macro' AND value LIKE '\"%'"):
        literal = _unquote(value or "")
        if literal is not None:
            out[name] = literal
    return out


def _resolve(text: str, strings: dict[str, str]) -> tuple[str, bool]:
    literal = _unquote(text)
    if literal is not None:
        return literal, True
    if text in strings:
        return strings[text], True
    return text, False


# ---------------------------------------------------------------- domaines -----

def _library_of(engine: QueryEngine) -> dict[str, str]:
    """Fonction externe -> bibliotheque qui la declare (nom de l'en-tete)."""
    out: dict[str, str] = {}
    for r in engine.db.execute(
        "SELECT s.name, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'prototype' AND f.origin = 'library'"
    ):
        out.setdefault(r["name"], Path(r["path"]).stem)
    sdk = getattr(engine, "sdk_headers", None) or {}
    for name, header in sdk.items():
        out.setdefault(name, Path(header).stem)
    return out


# Bibliotheques standard de CVI reconnues par le nom de la fonction, quand
# l'index SDK ne dit pas quel en-tete la declare.
_CVI_FAMILIES = (
    (re.compile(r"^Cmt"), "cvi_multithreading"),
    (re.compile(r"^Ini_"), "cvi_inifile"),
    (re.compile(r"^DB[A-Z]"), "cvi_db"),
    (re.compile(r"TCP|^RegisterTCPServer|^ConnectToTCPServer"), "cvi_tcp"),
    (re.compile(r"^(OpenCom|CloseCom|Com[A-Z]|FlushInQ|FlushOutQ|GetInQLen)"), "cvi_rs232"),
    (re.compile(r"^imaq"), "ni_vision"),
    (re.compile(r"^(Set|Get|Insert|Delete|Replace|Clear|Display|Hide|Discard|Load|New|Duplicate|"
                r"Canvas|Plot|Process|Run|Quit|Install|Remove|Message|Confirm|Prompt|Generic|"
                r"FileSelect|DirSelect)(Ctrl|Panel|Menu|List|Table|Graph|Axis|Bitmap|Popup|Tree|"
                r"Active|Image|Cursor|Plot|Draw|System|UserInterface|DeferredCall|Batch|Start|End|"
                r"Rect|Text|Line|Point|Oval|Arc|Polygon|Clear|Y|XY|X)"), "cvi_userint"),
    (re.compile(r"^(Fmt|Scan|OpenFile|CloseFile|ReadFile|WriteFile|ReadLine|WriteLine|"
                r"FileExists|GetFileInfo|DeleteFile|CopyFile|MakeDir|GetDir|SetDir)"), "cvi_formatio"),
    (re.compile(r"^(Timer|Delay|SyncWait|DateStr|TimeStr|GetSystemDate|GetSystemTime|"
                r"NewAsyncTimer|DiscardAsyncTimer|SetAsyncTimerAttribute)"), "cvi_utility"),
)
_VERBS = frozenset(["Set", "Get", "Insert", "Delete", "Replace", "Clear", "Display", "Hide", "Discard", "Load", "New", "Make", "Is", "Has", "Init", "Open", "Close", "Read", "Write", "Update", "Check", "Do", "Free"])


def _prefix(name: str) -> str:
    """Famille d'une fonction externe a defaut d'en-tete: Pylon, imaq, DB..."""
    if name in LIBC:
        return "libc"
    for pattern, family in _CVI_FAMILIES:
        if pattern.search(name):
            return family
    match = re.match(r"([a-z]+|[A-Z][a-z]+|[A-Z]+(?=[A-Z][a-z]|_|$))", name)
    prefix = match.group(1) if match else name
    return "libc" if prefix in _VERBS else prefix


def domains(engine: QueryEngine, settings: dict | None = None) -> list[dict]:
    """Fichiers du projet regroupes par domaine. Ordre de decision: regles
    "domains" de .cvi-mcp.json (motifs sur nom de fichier, de fonction ou de
    bibliotheque), puis mot significatif partage par plusieurs noms de
    fichier. Chaque domaine porte les bibliotheques externes qu'il utilise
    le plus: un indice fort (Pylon -> camera, Modbus -> automate)."""
    rules: dict[str, list[str]] = {k: list(v) for k, v in ((settings or {}).get("domains") or {}).items()}
    files = engine.db.execute(
        "SELECT f.id, f.path, f.lines, (SELECT COUNT(*) FROM symbols s WHERE s.file_id = f.id "
        "AND s.kind = 'function' AND s.active = 1) AS n FROM files f WHERE f.origin = 'project'"
    ).fetchall()
    defined = {r[0] for r in engine.db.execute("SELECT name FROM symbols WHERE kind = 'function'")}
    library = _library_of(engine)
    usage: dict[int, dict[str, int]] = {}
    for r in engine.db.execute(
        "SELECT file_id, name, COUNT(*) AS n FROM refs WHERE kind = 'call' AND active = 1 GROUP BY file_id, name"
    ):
        if r["name"] in defined:
            continue
        tag = library.get(r["name"]) or _prefix(r["name"])
        if tag == "libc":
            continue
        bucket = usage.setdefault(r["file_id"], {})
        bucket[tag] = bucket.get(tag, 0) + r["n"]

    stems: dict[str, dict] = {}
    for f in files:
        stem = Path(f["path"]).stem
        entry = stems.setdefault(stem, {"stem": stem, "files": [], "lines": 0, "functions": 0, "libs": {}})
        entry["files"].append(engine.rel(f["path"]))
        entry["lines"] += f["lines"] or 0
        entry["functions"] += f["n"]
        for tag, count in usage.get(f["id"], {}).items():
            entry["libs"][tag] = entry["libs"].get(tag, 0) + count

    def words(stem: str) -> set[str]:
        return {w for w in split_words(stem) if len(w) > 2 and w not in _GENERIC}

    frequency: dict[str, int] = {}
    for entry in stems.values():
        for word in words(entry["stem"]):
            frequency[word] = frequency.get(word, 0) + 1

    groups: dict[str, dict] = {}
    for entry in stems.values():
        name, source = None, "nom de fichier"
        for domain, patterns in rules.items():
            if any(fnmatch.fnmatch(entry["stem"].lower(), p.lower()) for p in patterns) or any(
                    fnmatch.fnmatch(lib.lower(), p.lower()) for p in patterns for lib in entry["libs"]):
                name, source = domain, "regle .cvi-mcp.json"
                break
        if name is None:
            shared = [w for w in words(entry["stem"]) if frequency[w] >= 2]
            if shared:
                name = max(shared, key=lambda w: (frequency[w], len(w), w))
            else:
                kept = sorted(words(entry["stem"]), key=len, reverse=True)
                name = kept[0] if kept else entry["stem"].lower()
        group = groups.setdefault(name, {"domain": name, "files": [], "lines": 0, "functions": 0,
                                         "libs": {}, "decided_by": source})
        group["files"] += entry["files"]
        group["lines"] += entry["lines"]
        group["functions"] += entry["functions"]
        for tag, count in entry["libs"].items():
            group["libs"][tag] = group["libs"].get(tag, 0) + count
    out = []
    for group in groups.values():
        libs = sorted(group.pop("libs").items(), key=lambda kv: -kv[1])
        group["external_libraries"] = [{"library": k, "calls": v} for k, v in libs[:4]]
        group["files"] = sorted(group["files"])[:10]
        out.append(group)
    return sorted(out, key=lambda g: -g["lines"])


# ------------------------------------------------------------- sites d'appel ---

def call_sites(engine: QueryEngine, pattern: str, position: int | None = None,
               env: dict | None = None) -> dict:
    """Arguments passes aux fonctions dont le nom correspond au motif
    (Modbus*, *Wago*, Ini_Get*): valeurs constantes par position, evaluees
    quand c'est une macro, et fonctions qui les passent. Sert a retrouver
    adresses, registres et bits d'E/S sans lire le code."""
    from .preproc import evaluate_value

    names = sorted({r[0] for r in engine.db.execute(
        "SELECT DISTINCT name FROM refs WHERE kind = 'call' AND active = 1")
        if fnmatch.fnmatchcase(r[0].lower(), pattern.lower())})
    if not names:
        return {"pattern": pattern, "reason": f"aucun appel ne correspond a '{pattern}'"}
    out = []
    for name in names:
        rows = engine.db.execute(
            "SELECT r.id, r.enclosing, r.line, f.path FROM refs r JOIN files f ON r.file_id = f.id "
            "WHERE r.name = ? AND r.kind = 'call' AND r.active = 1", (name,)).fetchall()
        positions: dict[int, dict[str, dict]] = {}
        for r in rows:
            for a in engine.db.execute("SELECT pos, text FROM call_args WHERE ref_id = ?", (r["id"],)):
                if position is not None and a["pos"] != position:
                    continue
                text = a["text"]
                idents = set(re.findall(r"[A-Za-z_]\w*", text))
                value = None
                if re.fullmatch(r"[\w()+\-*/ <>|&]+", text) and idents <= set(env or {}):
                    value = evaluate_value(text, env or {})
                slot = positions.setdefault(a["pos"], {})
                entry = slot.setdefault(text, {"argument": text, "uses": 0, "functions": set()})
                if value is not None and str(value) != text:
                    entry["value"] = value
                entry["uses"] += 1
                entry["functions"].add(r["enclosing"])
        detail = []
        for pos, values in sorted(positions.items()):
            constant = [v for v in values.values() if "value" in v or re.fullmatch(r"-?\d+|0x[0-9a-fA-F]+", v["argument"])
                        or v["argument"].startswith('"')]
            if not constant and position is None:
                detail.append({"position": pos, "distinct": len(values), "constants": 0})
                continue
            items = sorted(constant if position is None else values.values(),
                           key=lambda v: (v.get("value", 0) if isinstance(v.get("value"), int) else 0, v["argument"]))
            detail.append({"position": pos, "distinct": len(values), "constants": len(constant),
                           "values": [{**{k: v for k, v in i.items() if k != "functions"},
                                       "functions": sorted(f for f in i["functions"] if f)[:5]}
                                      for i in items[:60]]})
        out.append({"function": name, "calls": len(rows), "arguments": detail})
    return {"pattern": pattern, "functions": out}


# --------------------------------------------------------------- persistance ---

def ini_map(engine: QueryEngine) -> dict:
    """Sections et cles des fichiers INI (bibliotheque inifile de CVI):
    fonctions qui lisent ou ecrivent, cles ecrites jamais relues et l'inverse."""
    strings = macro_strings(engine.db)
    keys: dict[tuple[str, str], dict] = {}
    unresolved = 0
    for r in engine.db.execute(
        "SELECT r.id, r.name, r.enclosing FROM refs r WHERE r.kind = 'call' AND r.active = 1 "
        "AND r.name LIKE 'Ini\\_%' ESCAPE '\\'"
    ):
        side = "write" if INI_WRITE.match(r["name"]) else "read" if INI_READ.match(r["name"]) else None
        if side is None:
            continue
        args = {a[0]: a[1] for a in engine.db.execute(
            "SELECT pos, text FROM call_args WHERE ref_id = ?", (r["id"],))}
        section, ok_s = _resolve(args.get(1, ""), strings)
        key, ok_k = _resolve(args.get(2, ""), strings) if 2 in args else ("", True)
        if not (ok_s and ok_k):
            unresolved += 1
        entry = keys.setdefault((section, key), {"section": section, "key": key, "read_by": set(),
                                                 "written_by": set(), "resolved": ok_s and ok_k})
        entry["read_by" if side == "read" else "written_by"].add(r["enclosing"])
    items = []
    for entry in keys.values():
        items.append({**entry, "read_by": sorted(f for f in entry["read_by"] if f)[:6],
                      "written_by": sorted(f for f in entry["written_by"] if f)[:6]})
    items.sort(key=lambda e: (e["section"], e["key"]))
    sections: dict[str, int] = {}
    for item in items:
        sections[item["section"]] = sections.get(item["section"], 0) + 1
    resolved = [i for i in items if i["resolved"]]
    return {
        "sections": dict(sorted(sections.items(), key=lambda kv: -kv[1])),
        "keys_total": len(items),
        "calls_with_computed_names": unresolved,
        "written_never_read": [i for i in resolved if i["written_by"] and not i["read_by"]],
        "read_never_written": [i for i in resolved if i["read_by"] and not i["written_by"]],
        "keys": items,
    }


def ini_keys(db: sqlite3.Connection) -> set[str]:
    """Cles INI resolues, pour les instantanes."""
    strings = macro_strings(db)
    out = set()
    for ref_id, name in db.execute(
        "SELECT id, name FROM refs WHERE kind = 'call' AND name LIKE 'Ini\\_%' ESCAPE '\\'"
    ):
        if not (INI_READ.match(name) or INI_WRITE.match(name)):
            continue
        args = dict(db.execute("SELECT pos, text FROM call_args WHERE ref_id = ?", (ref_id,)).fetchall())
        section, ok_s = _resolve(args.get(1, ""), strings)
        key, ok_k = _resolve(args.get(2, ""), strings) if 2 in args else ("", True)
        if ok_s and ok_k:
            out.add(f"{section}/{key}")
    return out


def sql_map(engine: QueryEngine) -> dict:
    """Requetes SQL ecrites en chaines litterales (ou formats sprintf):
    tables touchees, par fonction et par nature de requete."""
    strings = macro_strings(engine.db)
    tables: dict[str, dict] = {}
    functions: dict[str, dict] = {}
    for r in engine.db.execute(
        "SELECT a.text, r.enclosing, r.name, r.line FROM call_args a JOIN refs r ON a.ref_id = r.id "
        "WHERE r.active = 1 AND r.kind = 'call'"
    ):
        literal, ok = _resolve(r["text"], strings)
        if not ok or not SQL_START.match(literal):
            continue
        verb = SQL_START.match(literal).group(1).upper()
        found = {m.group(1) for m in _SQL_TABLE.finditer(literal)}
        fn = functions.setdefault(r["enclosing"] or "?", {"function": r["enclosing"], "statements": {},
                                                          "tables": set()})
        fn["statements"][verb] = fn["statements"].get(verb, 0) + 1
        fn["tables"] |= found
        for table in found:
            entry = tables.setdefault(table, {"table": table, "statements": {}, "functions": set()})
            entry["statements"][verb] = entry["statements"].get(verb, 0) + 1
            entry["functions"].add(r["enclosing"])
    return {
        "tables": sorted(({**t, "functions": sorted(f for f in t["functions"] if f)[:8]}
                          for t in tables.values()), key=lambda t: t["table"]),
        "functions": sorted(({**f, "tables": sorted(f["tables"])} for f in functions.values()),
                            key=lambda f: f["function"] or ""),
    }


# ------------------------------------------------------------- bibliotheques ---

def libraries(engine: QueryEngine, prj_libs: list[dict]) -> list[dict]:
    """Bibliotheques liees par le .prj: presentes ou non sur ce poste, et
    fonctions externes appelees dont l'en-tete porte le meme nom."""
    library = _library_of(engine)
    defined = {r[0] for r in engine.db.execute("SELECT name FROM symbols WHERE kind = 'function'")}
    called: dict[str, int] = {}
    for r in engine.db.execute(
        "SELECT name, COUNT(*) FROM refs WHERE kind = 'call' AND active = 1 GROUP BY name"
    ):
        if r[0] not in defined:
            called[r[0]] = r[1]
    out = []
    for lib in prj_libs:
        stem = Path(lib["listed"].replace("\\", "/")).stem.lower()
        provides = sorted((n for n, h in library.items() if h.lower() == stem and n in called),
                          key=lambda n: -called[n])
        out.append({"library": lib["listed"], "found": bool(lib["found"]),
                    "functions_used": len(provides), "top": provides[:8]})
    return out
