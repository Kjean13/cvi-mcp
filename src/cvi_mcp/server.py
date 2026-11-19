# ==============================================================================
#  cvi_mcp/server.py
#  Role     Serveur MCP: exposition des outils de lecture du projet CVI
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# ============================ Externe =========================================
try:  # SDK MCP v1
    from mcp.server.fastmcp import FastMCP
except (ImportError, ModuleNotFoundError):  # SDK MCP v2
    from mcp.server.mcpserver import MCPServer as FastMCP

# ============================ Interne =========================================
from . import __version__, analysis, configuration, cvienv, diffs, insight, mapping
from . import build as buildmod
from . import gitinfo as gitmod
from . import project as projectmod
from . import quality as qualitymod
from . import risks as riskmod
from . import threads as threadmod
from .audit import AuditLog
from .config import Config, named_projects, parse_define_list, set_active_project
from .indexer import Indexer
from .prj import build_settings, collect_sources, discover_sources, parse_prj
from .queries import LIBC, QueryEngine, page
from .schema import ensure_schema
from .sdk import SdkIndexer, SdkLookup, resolve_roots
from .uimap import UiMap, build_ui_map, paginate

_INSTRUCTIONS = (
    "Analyse statique en lecture seule d'un projet LabWindows/CVI. Par defaut "
    "seules les signatures et positions sortent, jamais le corps des fonctions. "
    "Le preprocesseur est simule: le code exclu par un #if est ignore sauf "
    "include_inactive=true. Les listes sont paginees: suivre next_offset. "
    "Ordre conseille: project_overview, config_map, puis find_by_topic ou "
    "find_symbol pour localiser, get_function ou impact pour comprendre, "
    "ui_map pour l'interface, risks, lock_check, dead_code et consistency pour "
    "la qualite. self_check quand un resultat surprend."
)

try:  # le mot-cle version n'existe pas sur toutes les versions du SDK
    mcp = FastMCP("cvi-mcp", instructions=_INSTRUCTIONS, version=__version__)
except TypeError:
    mcp = FastMCP("cvi-mcp", instructions=_INSTRUCTIONS)


# ------------------------------------------------------------------ socle ------

def _cfg() -> Config:
    """Configuration relue a chaque appel: l'environnement peut changer."""
    return Config()


def _index_info(cfg: Config) -> dict:
    """Etat de l'index joint a chaque reponse: on sait d'ou viennent les
    donnees et de quand elles datent."""
    info: dict = {"server_version": __version__}
    if not cfg.db_path.is_file():
        info["state"] = "absent, construit au premier outil qui lit le projet"
        return info
    try:
        # as_uri: file:///C:/... sous Windows, espaces et # encodes.
        db = sqlite3.connect(f"{cfg.db_path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
            info["files"] = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error:
        info["state"] = "illisible"
        return info
    stamp = meta.get("indexed_at")
    if stamp:
        info["indexed_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(stamp)))
    info["schema"] = meta.get("schema_version")
    info["state"] = "a jour" if info["files"] else "vide"
    if meta.get("root"):
        info["root"] = meta["root"]
    if meta.get("previous_root") and meta.get("previous_root") != meta.get("root"):
        info["root_changed"] = (f"la racine etait {meta['previous_root']}: les chemins "
                                "relatifs n'ont plus la meme base")
    if _LAST.get("project"):
        info["build_config"] = _LAST["project"].ctx.config_name
    if _LAST.get("reindexed"):
        info["reindexed_this_call"] = _LAST["reindexed"]
    return info


def _reply(tool: str, args: dict, payload: dict, code_lines: int = 0,
           fmt: str | None = None) -> str:
    """Reponse JSON: etat de l'index joint, budget de taille respecte (la
    plus grosse liste est raccourcie, next_offset ajuste), format compact
    sur demande, puis trace dans le journal d'audit."""
    cfg = _cfg()
    payload = {**payload, "index_info": _index_info(cfg)}
    if (fmt or cfg.output_format) == "compact":
        payload = _compact(payload)
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > cfg.max_chars:
        payload = _shrink(payload, cfg.max_chars)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
    AuditLog(cfg.audit_log_path).record(tool, args, len(text.encode("utf-8")), code_lines,
                                        hide_paths=not cfg.audit_paths)
    return text


def _compact(value):
    """Listes d'objets de memes cles en colonnes + lignes: deux a trois fois
    moins de jetons pour une liste de symboles."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()}
    if isinstance(value, list) and len(value) >= 3 and all(isinstance(i, dict) for i in value):
        columns: list[str] = []
        for item in value:
            for key in item:
                if key not in columns:
                    columns.append(key)
        if len(columns) <= 12:
            return {"columns": columns,
                    "rows": [[_compact(item.get(c)) for c in columns] for item in value]}
    if isinstance(value, list):
        return [_compact(v) for v in value]
    return value


def _largest_list(value, path=()):
    """Chemin et taille de la plus longue liste (en caracteres JSON)."""
    best = (0, None)
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "index_info":
                continue
            found = _largest_list(child, (*path, key))
            if found[0] > best[0]:
                best = found
    elif isinstance(value, list) and len(value) > 1:
        size = len(json.dumps(value, ensure_ascii=False))
        best = (size, path)
        for index, child in enumerate(value):
            found = _largest_list(child, (*path, index))
            if found[0] > best[0] and found[0] > size * 0.8:
                best = found
    return best


def _shrink(payload: dict, budget: int) -> dict:
    """Coupe la plus grosse liste jusqu'a tenir dans le budget. Si elle
    appartient a une page (items, offset, next_offset), la page suivante
    reste accessible."""
    notes = []
    for _round in range(12):
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if len(text) <= budget:
            break
        _size, path = _largest_list(payload)
        if not path:
            break
        parent = payload
        for key in path[:-1]:
            parent = parent[key]
        items = parent[path[-1]]
        keep = max(1, int(len(items) * budget / len(text) * 0.9))
        if keep >= len(items):
            keep = len(items) - 1
        parent[path[-1]] = items[:keep]
        if isinstance(parent, dict) and path[-1] == "items" and "offset" in parent:
            parent["next_offset"] = parent["offset"] + keep
            parent["limit"] = keep
        notes.append({"list": "/".join(str(p) for p in path), "kept": keep, "had": len(items)})
    if notes:
        payload["truncated"] = {"budget_chars": budget, "lists": notes,
                                "hint": "reduire limit ou suivre next_offset"}
    return payload


def _sources(cfg: Config) -> list[str]:
    return collect_sources(
        cfg.project_path, lambda p: cfg.is_excluded(cfg.relative(p)), cfg.project_file
    )


def _engine(cfg: Config) -> QueryEngine:
    """Index mis a jour puis moteur de lecture. Chaque outil passe par ici:
    un fichier modifie est reindexe avant la reponse, rien a lancer a la main.
    Les chemins sortent relatifs a la racine commune du projet et de ses
    sources."""
    proj = projectmod.load(cfg)
    indexer = Indexer(cfg.db_path)
    try:
        report = indexer.index_files(proj.files, pp=proj.pp)
        indexer.set_root(str(proj.root))
    finally:
        indexer.close()
    _LAST["project"] = proj
    _LAST["reindexed"] = report["indexed"]
    engine = QueryEngine(cfg.db_path, proj.root)
    engine.project = proj
    return engine


# Dernier projet assemble, pour les outils qui rejouent le preprocesseur.
_LAST: dict = {}


def _snippet(
    cfg: Config, path: str, start: int, end: int, wanted: int | None = None
) -> tuple[str | None, int, str | None]:
    """Extrait de code, seulement si la configuration l'autorise. wanted
    reduit encore le plafond pour un simple coup d'oeil."""
    if not cfg.allow_body:
        return None, 0, "corps non transmis: CVI_MCP_ALLOW_BODY=0"
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, 0, "fichier illisible"

    cap = cfg.max_snippet_lines if not wanted else min(max(wanted, 1), cfg.max_snippet_lines)
    chunk = lines[max(start - 1, 0) : min(end, len(lines))]
    truncated = len(chunk) > cap
    if truncated:
        chunk = chunk[:cap]
    body = "\n".join(chunk)
    note = f"tronque a {cap} lignes sur {end - start + 1}" if truncated else None
    return body, len(chunk), note


# ----------------------------------------------------- environnement -----------

@mcp.tool()
def cvi_environment() -> str:
    """Diagnostic de la machine: version du serveur, plateforme, installations
    CVI detectees quel que soit le millesime, dossiers d'en-tetes du SDK,
    executables de build, reglages actifs et etat de l'index. A lancer en
    premier pour savoir ce que l'installation locale permet reellement."""
    cfg = _cfg()
    payload = cvienv.describe(cfg)
    payload["cvi_mcp_version"] = __version__

    index: dict = {}
    try:
        engine = _engine(cfg)
        try:
            counts = engine.overview()
            index = {
                "files": counts["total_files"],
                "lines": counts["total_lines"],
                "by_origin": {
                    k: {"files": v["files"], "lines": v["lines"]}
                    for k, v in counts["by_origin"].items()
                },
            }
            index["snapshots"] = [s["label"] for s in diffs.list_snapshots(engine.db)]
        finally:
            engine.close()
        lookup = SdkLookup(cfg.sdk_db_path)
        try:
            index["sdk"] = lookup.status()
        finally:
            lookup.close()
    except sqlite3.Error:
        index = {"state": "index illisible, lancer reindex(force=true)"}
    payload["index"] = index

    return _reply("cvi_environment", {}, payload)


# --------------------------------------------------------------- lecture -------

_OVERVIEW_SECTIONS = (
    "files", "domains", "callbacks", "static_functions", "externals", "entry_points", "libraries",
)


@mcp.tool()
def project_overview(
    section: str | None = None,
    scope: str = "all",
    contains: str | None = None,
    limit: int = 15,
    offset: int = 0,
) -> str:
    """Vue d'ensemble. Sans section: totaux par origine (project: code de
    l'equipe, generated: .h generes par CVI, library: en-tetes tiers), points
    d'entree, et le haut de chaque liste. section: files, domains (fichiers regroupes
    par domaine: noms, bibliotheques utilisees, regles "domains" de
    .cvi-mcp.json), libraries (bibliotheques du .prj presentes ou non et
    fonctions qu'elles fournissent), callbacks,
    static_functions, externals ou entry_points, paginee par limit et offset,
    filtree par contains. scope: all, project, generated, library (files)."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        lists = {
            "files": lambda: engine.files(scope),
            "domains": lambda: mapping.domains(engine, cfg.settings),
            "modules": lambda: mapping.domains(engine, cfg.settings),
            "libraries": lambda: mapping.libraries(engine, _prj_libs(cfg)),
            "callbacks": engine.callbacks,
            "static_functions": engine.static_functions,
            "externals": lambda: _categorized(cfg, engine.external_calls()),
            "entry_points": lambda: engine.overview()["entry_points"],
        }
        args = {"section": section, "scope": scope, "contains": contains,
                "limit": limit, "offset": offset}
        if section:
            if section not in lists:
                payload = {"error": f"section inconnue: {section}",
                           "sections": list(_OVERVIEW_SECTIONS)}
                return _reply("project_overview", args, payload)
            items = _filter(lists[section](), contains)
            payload = {"section": section, **page(items, offset, limit)}
            return _reply("project_overview", args, payload)

        payload = engine.overview()
        payload["root"] = str(engine.root)
        for name in ("callbacks", "static_functions", "externals"):
            items = lists[name]()
            payload[name] = {"total": len(items), "top": items[:limit]}
            if name == "externals":
                counts: dict[str, int] = {}
                for item in items:
                    counts[item["category"]] = counts.get(item["category"], 0) + 1
                payload[name]["by_category"] = counts
                if counts.get("sdk", 0) == 0 and counts.get("unknown", 0):
                    payload[name]["note"] = (
                        "index SDK vide ou sans ces fonctions: les appels CVI restent unknown. "
                        "Lancer sdk_index ou verifier CVI_MCP_SDK_PATHS"
                    )
        payload["sections"] = list(_OVERVIEW_SECTIONS)
    finally:
        engine.close()

    for project in cfg.project_files():
        info = parse_prj(project)
        payload["project_file"] = project.name
        payload["linked_libs"] = info["libs"]
        notes = []
        if not info["sources"]:
            notes.append(
                f"aucune source lue dans {project.name}, balayage du dossier utilise. "
                f"Sections presentes: {info['sections']}"
            )
        if info["relocated"]:
            notes.append(
                f"{len(info['relocated'])} fichier(s) absent(s) du chemin note dans le .prj, "
                "retrouves par leur nom dans le dossier du projet"
            )
        if info["unresolved"]:
            folders: dict[str, int] = {}
            for listed in info["unresolved"]:
                folder = str(Path(listed.replace("\\", "/")).parent)
                folders[folder] = folders.get(folder, 0) + 1
            detail = ", ".join(f"{name} ({count})" for name, count in sorted(folders.items()))
            notes.append(
                f"{len(info['unresolved'])} source(s) du .prj introuvables, dossiers attendus: "
                f"{detail}"
            )
        if notes:
            payload["project_file_note"] = " | ".join(notes)
        break

    return _reply("project_overview", args, payload)


def _prj_libs(cfg) -> list[dict]:
    out: list[dict] = []
    for project in cfg.project_files():
        out += parse_prj(project).get("lib_files", [])
    return out


def _categorized(cfg, calls: list[dict]) -> list[dict]:
    """Ajoute la categorie de chaque appel externe: libc, sdk (prototype dans
    l'index SDK) ou unknown (ni l'un ni l'autre: DLL tierce, macro, toolbox
    absente de CVI_MCP_SDK_PATHS)."""
    _ensure_sdk(cfg)
    known: dict[str, str] = {}
    names = [c["name"] for c in calls if c["name"] not in LIBC]
    lookup = SdkLookup(cfg.sdk_db_path)
    try:
        for start in range(0, len(names), 400):
            known.update(lookup.signatures_for(names[start : start + 400]))
    finally:
        lookup.close()
    for call in calls:
        call["category"] = (
            "libc" if call["name"] in LIBC else "sdk" if call["name"] in known else "unknown"
        )
        if call["name"] in known:
            call["signature"] = known[call["name"]]
    return calls


def _ensure_sdk(cfg) -> dict | None:
    """Indexe le SDK au premier besoin si une installation CVI est trouvee
    et que l'index SDK est vide. Une seule tentative par processus."""
    if _LAST.get("sdk_attempted"):
        return None
    _LAST["sdk_attempted"] = True
    lookup = SdkLookup(cfg.sdk_db_path)
    try:
        status = lookup.status()
    except sqlite3.Error:
        status = {}
    finally:
        lookup.close()
    if status.get("files"):
        return None
    roots = resolve_roots(cfg.sdk_paths)
    if not roots:
        return None
    indexer = SdkIndexer(cfg.sdk_db_path)
    try:
        return indexer.index(roots)
    finally:
        indexer.close()


def _filter(items: list[dict], contains: str | None) -> list[dict]:
    """Sous-chaine, sans casse, sur le nom ou le chemin."""
    if not contains:
        return items
    needle = contains.lower()
    return [
        i for i in items
        if needle in str(i.get("name", "")).lower() or needle in str(i.get("path", i.get("file", ""))).lower()
    ]


_KINDS = ("function", "prototype", "global", "struct", "union", "enum", "enumerator",
          "typedef", "macro")


def _why_empty(engine: QueryEngine, name: str | None = None, file: str | None = None,
               kind: str | None = None) -> dict:
    """Explique un resultat vide: filtre invalide, nom inconnu, symbole
    present seulement dans du code exclu, ou nom vu seulement dans un #if."""
    out: dict = {}
    if file and not engine.match_files(file):
        out["reason"] = f"aucun fichier indexe ne correspond a '{file}'"
        out["hint"] = "accepte un nom, un chemin partiel, un dossier ou un motif (Dossier/*.h)"
        return out
    if kind and kind not in _KINDS:
        out["reason"] = f"type inconnu: {kind}"
        out["kinds"] = list(_KINDS)
        return out
    if not name:
        out["reason"] = "aucun symbole ne correspond a ces filtres"
        return out
    hidden = engine.db.execute(
        "SELECT COUNT(*) FROM symbols WHERE name = ? AND active = 0", (name,)
    ).fetchone()[0]
    if hidden and not engine.include_inactive:
        out["reason"] = f"'{name}' n'existe que dans un bloc #if exclu de la configuration courante"
        out["hint"] = "include_inactive=true pour le voir"
        return out
    cond = engine.db.execute(
        "SELECT COUNT(*) FROM refs WHERE name = ? AND kind = 'cond'", (name,)
    ).fetchone()[0]
    if cond:
        out["reason"] = (f"'{name}' n'est defini nulle part mais teste dans {cond} directive(s) "
                         "#if: option de compilation desactivee")
        out["hint"] = f"macro_usages(name='{name}')"
        return out
    out["reason"] = f"aucun symbole indexe ne s'appelle '{name}'"
    suggestions = engine.suggest(name)
    if suggestions:
        out["did_you_mean"] = suggestions
    else:
        out["hint"] = "search_text pour chercher aussi dans les chaines et commentaires"
    return out


@mcp.tool()
def list_symbols(
    kind: str | None = None,
    file: str | None = None,
    name_contains: str | None = None,
    limit: int = 100,
    offset: int = 0,
    scope: str = "all",
    include_inactive: bool = False,
    format: str | None = None,
) -> str:
    """Liste les symboles. kind: function, prototype, global, struct, union,
    enum, enumerator, typedef, macro. file accepte un nom, un chemin partiel,
    un dossier ou un motif (Commun/Includes/*). scope: all, project,
    generated, library. include_inactive ajoute le code exclu par un #if.
    format=compact: colonnes + lignes, moins de jetons."""
    cfg = _cfg()
    engine = _engine(cfg)
    engine.include_inactive = include_inactive
    try:
        rows = engine.list_symbols(kind, file, name_contains, -1, 0, scope)
        payload = page(rows, offset, limit)
        if not rows:
            payload.update(_why_empty(engine, None, file, kind))
    finally:
        engine.close()
    args = {"kind": kind, "file": file, "name_contains": name_contains,
            "limit": limit, "offset": offset, "scope": scope, "include_inactive": include_inactive}
    return _reply("list_symbols", args, payload, fmt=format)


@mcp.tool()
def find_symbol(name: str, exact: bool = False, kind: str | None = None,
                include_inactive: bool = False) -> str:
    """Localise un symbole. Insensible a la casse; plusieurs mots ("turbine
    max") doivent tous apparaitre dans le nom. Sans resultat, propose des
    noms proches et dit pourquoi."""
    cfg = _cfg()
    engine = _engine(cfg)
    engine.include_inactive = include_inactive
    try:
        rows = engine.find_symbol(name, exact, kind)
        payload: dict = {"count": len(rows), "results": rows[:200]}
        if len(rows) > 200:
            payload["note"] = f"{len(rows)} resultats, 200 premiers: preciser la recherche"
        if not rows:
            payload.update(_why_empty(engine, name.strip(), None, kind))
    finally:
        engine.close()
    return _reply("find_symbol", {"name": name, "exact": exact, "kind": kind}, payload)


@mcp.tool()
def get_function(name: str, include_body: bool = False, lines: str | int | None = None,
                 skeleton: bool = False) -> str:
    """Detail d'une fonction: signature, parametres, commentaire, taille,
    complexite, appelants, appeles avec signature SDK pour les appels externes
    connus, controles qui la declenchent. include_body ne renvoie le corps que
    si CVI_MCP_ALLOW_BODY=1; lines limite l'extrait aux N premieres lignes,
    dans la limite de CVI_MCP_MAX_SNIPPET_LINES, ou "a-b" une plage relative
    au debut de la fonction. skeleton donne la structure (if, boucles, switch,
    retours, appels) sans aucune expression: lisible sans CVI_MCP_ALLOW_BODY.
    Le resume liste aussi globales lues et ecrites, verrous, panneaux et
    controles touches, cles INI."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        data = engine.get_function(name)
        if data is not None:
            data["touches"] = insight.function_summary(engine, name)
            if skeleton:
                data["skeleton"] = insight.skeleton(engine, data["path"], data["line_start"],
                                                     data["line_end"])
        if data is not None and data.get("is_callback"):
            # Une callback cablee dans l'editeur d'interface n'a pas d'appelant
            # dans le code: le controle qui la declenche est dans le .h genere.
            uir = UiMap(engine).uir
            if uir and name in uir["callbacks"]:
                data["wired_in_uir"] = sorted(uir["callbacks"][name])
    finally:
        engine.close()

    if data is None:
        engine = QueryEngine(cfg.db_path, _LAST["project"].root if _LAST else None)
        try:
            payload = {"error": f"fonction '{name}' introuvable", **_why_empty(engine, name)}
        finally:
            engine.close()
        return _reply("get_function", {"name": name}, payload)

    externals = [c["name"] for c in data["callees"] if c["external"]]
    if externals:
        _ensure_sdk(cfg)
        lookup = SdkLookup(cfg.sdk_db_path)
        try:
            known = lookup.signatures_for(externals)
        except sqlite3.Error:
            known = {}
        finally:
            lookup.close()
        for callee in data["callees"]:
            if callee["name"] in known:
                callee["sdk_signature"] = known[callee["name"]]

    path = data.pop("path")
    code_lines = 0
    if include_body:
        start, end, wanted = data["line_start"], data["line_end"], None
        if isinstance(lines, str) and "-" in lines:
            first, _sep, last = lines.partition("-")
            try:
                start = data["line_start"] + max(int(first) - 1, 0)
                end = min(data["line_start"] + int(last) - 1, data["line_end"])
            except ValueError:
                pass
        elif lines:
            try:
                wanted = int(lines)
            except ValueError:
                wanted = None
        body, code_lines, note = _snippet(cfg, path, start, end, wanted)
        if body is not None:
            data["body"] = body
        if note:
            data["body_note"] = note

    args = {"name": name, "include_body": include_body, "lines": lines}
    return _reply("get_function", args, data, code_lines)


@mcp.tool()
def find_usages(name: str, kind: str | None = None, limit: int = 200, file: str | None = None,
                group_by: str | None = None, include_inactive: bool = False,
                format: str | None = None) -> str:
    """Usages d'un nom. kind: call, read, write, ref (passe sans appel), cond
    (expression #if), macro (valeur d'une autre macro), dim (taille de
    tableau), decl (initialiseur hors fonction). group_by: function ou file.
    Le code exclu par un #if est compte a part sauf include_inactive.
    format=compact: colonnes + lignes."""
    cfg = _cfg()
    engine = _engine(cfg)
    engine.include_inactive = include_inactive
    try:
        data = engine.find_usages(name, kind, 0, file)
        payload: dict = {k: v for k, v in data.items() if k != "items"}
        items = data["items"]
        if group_by in ("function", "file"):
            groups: dict[str, dict] = {}
            for item in items:
                key = (item.get("in_function") or item.get("in_macro") or item.get("array")
                       or item.get("in_initializer_of") or "(hors fonction)") \
                    if group_by == "function" else item["file"]
                entry = groups.setdefault(key, {group_by: key, "count": 0, "kinds": {}})
                entry["count"] += 1
                entry["kinds"][item["kind"]] = entry["kinds"].get(item["kind"], 0) + 1
                if group_by == "function":
                    entry.setdefault("file", item["file"])
            ordered = sorted(groups.values(), key=lambda g: -g["count"])
            payload["groups"] = page(ordered, 0, limit)
        else:
            payload["usages"] = page(items, 0, limit)
        if not items:
            payload.update(_why_empty(engine, name, file))
            if data["inactive_excluded"]:
                payload["reason"] = (f"{data['inactive_excluded']} usage(s), tous dans du code "
                                     "exclu par un #if")
                payload["hint"] = "include_inactive=true pour les voir"
    finally:
        engine.close()
    args = {"name": name, "kind": kind, "limit": limit, "file": file, "group_by": group_by}
    return _reply("find_usages", args, payload, fmt=format)


@mcp.tool()
def call_graph(
    function: str,
    direction: str = "callees",
    depth: int = 2,
    include_external: bool = False,
    include_inactive: bool = False,
) -> str:
    """Graphe d'appels. direction: callees ou callers. include_external ajoute
    les appels hors projet (SDK, libc). Une arete via pointeur ou table de
    pointeurs porte confidence: moyenne."""
    cfg = _cfg()
    engine = _engine(cfg)
    engine.include_inactive = include_inactive
    try:
        graph = engine.call_graph(function, direction, depth, include_external)
        extra = {} if graph.get(function) else _why_empty(engine, function)
        if graph.get(function) == [] and engine.find_symbol(function, True):
            extra = {"reason": f"'{function}' n'a aucun {'appelant' if direction == 'callers' else 'appel interne'}"
                     + ("" if include_external or direction == "callers" else
                        ": include_external=true pour voir les appels SDK")}
    finally:
        engine.close()
    args = {
        "function": function,
        "direction": direction,
        "depth": depth,
        "include_external": include_external,
    }
    return _reply("call_graph", args, {"graph": graph, **extra})


@mcp.tool()
def file_outline(file: str) -> str:
    """Structure d'un fichier dans l'ordre des lignes: includes et symboles."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        data = engine.file_outline(file)
    finally:
        engine.close()
    payload = data or {"error": f"aucun fichier indexe ne correspond a '{file}'"}
    return _reply("file_outline", {"file": file}, payload)


@mcp.tool()
def reindex(force: bool = False) -> str:
    """Reindexe le projet. force=true reconstruit tout l'index projet."""
    cfg = _cfg()
    proj = projectmod.load(cfg)
    indexer = Indexer(cfg.db_path)
    try:
        report = indexer.index_files(proj.files, force=force, pp=proj.pp)
        indexer.set_root(str(proj.root))
    finally:
        indexer.close()
    report["sources_resolved"] = len(proj.sources)
    report["headers_reached"] = len(proj.files) - len(proj.sources) - len(proj.unlisted)
    report["root"] = str(proj.root)
    report["build_config"] = proj.ctx.config_name
    return _reply("reindex", {"force": force}, report)


# ----------------------------------------------------------------- projets ------

@mcp.tool()
def use_project(name_or_path: str | None = None) -> str:
    """Change de projet sans relancer le serveur. name_or_path: un nom de
    CVI_MCP_PROJECTS ("CL150=C:/...;Autre=D:/...") ou un chemin de dossier,
    de .prj ou de .cws. Sans argument: projet courant et projets connus
    (nommes, ou deja indexes sur ce poste). Chaque projet a son propre index."""
    current = _cfg()
    known = []
    for name, path in named_projects().items():
        known.append({"name": name, "path": path})
    home = current.db_path.parent
    for db_file in sorted(home.glob("index-*.db")):
        try:
            db = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True)
            try:
                row = db.execute("SELECT value FROM meta WHERE key = 'root'").fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            continue
        if row and not any(k.get("path") == row[0] for k in known):
            known.append({"path": row[0], "indexed": True})
    if not name_or_path:
        payload = {"current": str(current.project_file or current.project_path), "known": known}
        return _reply("use_project", {"name_or_path": None}, payload)
    target = named_projects().get(name_or_path, name_or_path)
    if not Path(target).exists():
        return _reply("use_project", {"name_or_path": name_or_path},
                      {"error": f"introuvable: {target}", "known": known})
    set_active_project(target)
    _LAST.clear()
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        overview = engine.overview()
    finally:
        engine.close()
    payload = {"current": str(cfg.project_file or cfg.project_path), "files": overview["total_files"],
               "lines": overview["total_lines"], "note": "valable jusqu'au redemarrage du serveur"}
    return _reply("use_project", {"name_or_path": name_or_path}, payload)


# En-tetes livres avec CVI et Windows: introuvables dans le projet, c'est normal.
_STANDARD_HEADERS = frozenset(
    ["cvidef.h", "cvirte.h", "userint.h", "ansi_c.h", "utility.h", "formatio.h", "asynctmr.h", "inifile.h", "toolbox.h", "tcpsupp.h", "rs232.h", "cvi_db.h", "analysis.h", "advanlys.h", "pwctrl.h", "radiogroup.h", "cvintwrk.h", "easytab.h", "progressbar.h", "localui.h", "nivision.h", "niimaq.h", "visa.h", "nidaqmx.h", "windows.h", "winsock.h", "winsock2.h", "stdio.h", "stdlib.h", "string.h", "math.h", "time.h", "ctype.h", "stdarg.h", "stddef.h", "limits.h", "float.h", "errno.h", "signal.h", "setjmp.h", "assert.h", "locale.h", "stdint.h", "stdbool.h"]
)


@mcp.tool()
def self_check() -> str:
    """Diagnostic de l'analyse: index, lecture du .prj (sources, chemins
    d'inclusion, bibliotheques), includes non resolus, fichiers qui ne
    s'analysent pas bien, encodages, sources du dossier hors build, CVI,
    SDK et reglages. A lancer quand un resultat surprend."""
    cfg = _cfg()
    engine = _engine(cfg)
    proj = engine.project
    try:
        db = engine.db
        parse = [{"file": engine.rel(r["path"]), "errors": r["parse_errors"]}
                 for r in db.execute("SELECT path, parse_errors FROM files WHERE parse_errors > 0 "
                                     "AND origin = 'project' ORDER BY parse_errors DESC LIMIT 15")]
        encodings = {r[0] or "?": r[1] for r in db.execute(
            "SELECT encoding, COUNT(*) FROM files GROUP BY encoding")}
        missing: dict[str, dict] = {}
        for r in db.execute(
            "SELECT i.included, i.line, f.path FROM includes i JOIN files f ON i.file_id = f.id "
            "WHERE i.resolved IS NULL AND i.system = 0 AND i.active = 1"
        ):
            entry = missing.setdefault(r["included"], {"include": r["included"], "count": 0,
                                                       "first": f"{engine.rel(r['path'])}:{r['line']}"})
            entry["count"] += 1
        system = db.execute("SELECT COUNT(DISTINCT included) FROM includes WHERE system = 1").fetchone()[0]
        blocks = {r[0]: r[1] for r in db.execute(
            "SELECT state, COUNT(*) FROM pp_blocks WHERE guard = 0 GROUP BY state")}
        unknown = [{"file": engine.rel(r["path"]), "line": r["line_start"], "directive": r["expr"],
                    "reason": r["reason"]}
                   for r in db.execute(
                       "SELECT b.line_start, b.expr, b.reason, f.path FROM pp_blocks b JOIN files f "
                       "ON b.file_id = f.id WHERE b.state = 'unknown' LIMIT 10")]
        not_built = [engine.rel(p) for p in proj.unreached][:15]
    finally:
        engine.close()

    known = {os.path.normcase(p) for p in proj.files}
    outside = [p for p in discover_sources(proj.root) if os.path.normcase(p) not in known
               and not cfg.is_excluded(cfg.relative(p))]
    sdk_names = set(_STANDARD_HEADERS)
    try:
        sdk_db = sqlite3.connect(str(cfg.sdk_db_path))
        try:
            sdk_names |= {Path(r[0]).name.lower() for r in sdk_db.execute("SELECT path FROM sdk_files")}
        finally:
            sdk_db.close()
    except sqlite3.Error:
        pass
    for entry in missing.values():
        entry["likely"] = "sdk_ou_systeme" if entry["include"].lower().rsplit("/", 1)[-1] in sdk_names \
            else "introuvable"
    prj = {}
    for project in cfg.project_files():
        info = parse_prj(project)
        prj = {"file": project.name, "sources": len(info["sources"]),
               "relocated": len(info["relocated"]), "unresolved": info["unresolved"][:10],
               "libraries_missing": [lib["listed"] for lib in info.get("lib_files", []) if not lib["found"]],
               "include_paths": proj.settings.get("include_paths", []),
               "configuration": proj.ctx.config_name, "configurations":
                   [c["name"] for c in proj.settings.get("configs", [])]}
        break
    lookup = SdkLookup(cfg.sdk_db_path)
    try:
        sdk = lookup.status()
    except sqlite3.Error:
        sdk = {}
    finally:
        lookup.close()
    payload = {
        "project": prj or {"note": "aucun .prj: balayage du dossier"},
        "settings_file": bool(cfg.settings),
        "forced_defines": cfg.extra_defines,
        "files_indexed": len(proj.files),
        "headers_reached_outside_prj": len(proj.files) - len(proj.sources) - len(proj.unlisted),
        "indexed_but_not_in_build": not_built,
        "sources_in_folder_not_indexed": {"count": len(outside),
                                          "sample": [Path(os.path.relpath(p, proj.root)).as_posix() for p in outside[:10]],
                                          "hint": "CVI_MCP_INDEX_UNLISTED=1 pour les indexer (in_build: false)"},
        "unresolved_includes": sorted(missing.values(),
                                      key=lambda m: (m["likely"] != "introuvable", -m["count"]))[:15],
        "system_includes": system,
        "preprocessor_branches": blocks,
        "undecided_branches": unknown,
        "parse_errors": parse,
        "encodings": encodings,
        "sdk_index": sdk,
        "cvi_installs": [i.get("name") for i in cvienv.find_installs()],
        "build_command": buildmod.resolve_command(cfg.build_cmd, cvienv.find_build_commands()),
    }
    return _reply("self_check", {}, payload)


# -------------------------------------------------------------- recherche ------

@mcp.tool()
def search_text(pattern: str, scope: str = "all", regex: bool = False, file: str | None = None,
                origin: str = "project", case_sensitive: bool = False,
                limit: int = 50, offset: int = 0, format: str | None = None) -> str:
    """Recherche dans le texte des sources: scope code, comments, strings ou
    all. Renvoie fichier, ligne, zone, fonction englobante et le seul texte
    qui correspond (la chaine entiere pour une chaine litterale), jamais la
    ligne de code. origin: project (defaut), generated, library, all."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        data = insight.search_text(engine, pattern, scope, regex, file, origin, case_sensitive)
    finally:
        engine.close()
    if "hits" in data:
        data["hits"] = page(data["hits"], offset, limit)
    args = {"pattern": pattern, "scope": scope, "regex": regex, "file": file, "origin": origin}
    return _reply("search_text", args, data, fmt=format)


@mcp.tool()
def find_by_topic(topic: str, limit: int = 10) -> str:
    """Tout ce qui touche a un theme ("turbines", "camera", "alarme"):
    fichiers, fonctions, callbacks, globales, macros, types, panneaux,
    controles et chaines (cles INI, messages), classes par pertinence."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = insight.find_by_topic(engine, topic, limit)
    finally:
        engine.close()
    return _reply("find_by_topic", {"topic": topic, "limit": limit}, payload)


@mcp.tool()
def related(name: str, limit: int = 15) -> str:
    """Symboles apparentes a un nom: meme suite numerotee (X1..Xn), utilises
    ensemble dans les memes fonctions, voisins dans le fichier, noms proches."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = insight.related(engine, name, limit)
    finally:
        engine.close()
    return _reply("related", {"name": name, "limit": limit}, payload)


@mcp.tool()
def series(name: str | None = None, min_members: int = 3, limit: int = 30, offset: int = 0) -> str:
    """Suites numerotees (gstrNOM1..gstrNOM14, "CLE_1".."CLE_8") dans les
    identifiants et chaines: etendue, trous, et fichiers ou la suite s'arrete
    avant le maximum vu ailleurs. name restreint a la suite d'un membre."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = insight.series(engine, name, min_members, limit, offset)
    finally:
        engine.close()
    return _reply("series", {"name": name, "min_members": min_members}, payload)


@mcp.tool()
def impact(name: str, limit: int = 25) -> str:
    """Checklist pour modifier ou etendre un symbole (macro, fonction,
    globale, panneau, controle): definitions, directives #if, tableaux
    dimensionnes, macros derivees, fonctions concernees avec leurs panneaux
    et cles INI, suites numerotees a prolonger."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = insight.impact(engine, cfg, name, limit)
    finally:
        engine.close()
    return _reply("impact", {"name": name, "limit": limit}, payload)


@mcp.tool()
def peek(file: str, line: int, before: int = 3, after: int = 3) -> str:
    """Quelques lignes autour d'une position (un #define, une declaration,
    une signature sur plusieurs lignes). Exige CVI_MCP_ALLOW_PEEK=1 ou
    CVI_MCP_ALLOW_BODY=1, plafonne a CVI_MCP_MAX_SNIPPET_LINES, journalise."""
    cfg = _cfg()
    args = {"file": file, "line": line, "before": before, "after": after}
    if not (cfg.allow_peek or cfg.allow_body):
        return _reply("peek", args, {"error": "apercu desactive: CVI_MCP_ALLOW_PEEK=0",
                                     "hint": "activer CVI_MCP_ALLOW_PEEK=1 pour des extraits courts"})
    engine = _engine(cfg)
    try:
        ids = engine.match_files(file)
        row = engine.db.execute(
            f"SELECT path FROM files WHERE id IN ({','.join('?' for _ in ids)}) "
            "ORDER BY LENGTH(path) LIMIT 1", ids).fetchone() if ids else None
        rel = engine.rel(row["path"]) if row else None
    finally:
        engine.close()
    if row is None:
        return _reply("peek", args, {"error": f"aucun fichier indexe ne correspond a '{file}'"})
    rows, first, last = insight.peek_lines(row["path"], line, before, after, cfg.max_snippet_lines)
    payload = {"file": rel, "from": first, "to": last, "text": "\n".join(rows)}
    if len(ids) > 1:
        payload["note"] = f"{len(ids)} fichiers correspondent, le plus court retenu"
    return _reply("peek", args, payload, len(rows))


# ------------------------------------------------------------ configuration ----

def _defines_arg(defines) -> dict[str, str]:
    """{"NOM": "12"} ou "NOM=12;AUTRE" ou "NOM=12,AUTRE"."""
    if isinstance(defines, dict):
        return {str(k): str(v) for k, v in defines.items()}
    return parse_define_list(str(defines or ""))


@mcp.tool()
def macro_usages(name: str, defines: dict | str | None = None, limit: int = 30) -> str:
    """Tout ce qui depend d'une macro: definitions (valeur brute et evaluee,
    active ou non), definitions mises en commentaire, directives #if qui la
    testent avec leur etat, fonctions qui l'utilisent, tableaux qu'elle
    dimensionne, macros et initialiseurs qui la citent. defines simule une
    autre valeur ({"NOM": "12"}) sans toucher aux fichiers."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = configuration.macro_usages(engine, cfg, name, _defines_arg(defines), limit)
    finally:
        engine.close()
    return _reply("macro_usages", {"name": name, "defines": defines, "limit": limit}, payload)


@mcp.tool()
def config_map(scope: str = "project", limit: int = 40, offset: int = 0) -> str:
    """Options de compilation du projet: chaque macro testee par un #if, sa
    valeur courante (ou non definie), le nombre de blocs qu'elle pilote,
    actifs et exclus, et sa definition mise en commentaire s'il y en a une.
    Donne aussi la configuration de build, les #define du .prj et les chemins
    d'inclusion. scope: project (defaut) ou all (bibliotheques comprises)."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = configuration.config_map(engine, cfg, scope, limit, offset)
    finally:
        engine.close()
    return _reply("config_map", {"scope": scope, "limit": limit, "offset": offset}, payload)


@mcp.tool()
def config_diff(defines: dict | str, limit: int = 40) -> str:
    """What-if: effet d'autres valeurs de macros ({"NOMBRE": "12"} ou
    "OPTION=1;AUTRE") sans modifier les fichiers. Branches #if qui basculent,
    fonctions, globales et macros qui apparaissent ou disparaissent, macros
    derivees qui changent de valeur, tableaux redimensionnes."""
    cfg = _cfg()
    forced = _defines_arg(defines)
    if not forced:
        return _reply("config_diff", {"defines": defines},
                      {"error": "defines vide", "example": {"NOM_MACRO": "12"}})
    engine = _engine(cfg)
    try:
        payload = configuration.config_diff(engine, cfg, forced, limit)
    finally:
        engine.close()
    return _reply("config_diff", {"defines": defines, "limit": limit}, payload)


# ------------------------------------------------------------- interface -------

@mcp.tool()
def ui_map(
    panel: str | None = None,
    contains: str | None = None,
    sort: str = "name",
    limit: int = 25,
    offset: int = 0,
    uir: str | None = None,
) -> str:
    """Cablage de l'interface. Sans panel: une ligne par panneau (controles,
    callbacks, chargement), filtree par contains, triee par sort (name,
    controls, unused, callbacks, wired, file), paginee, uir filtre sur un fichier .uir; limit=0 renvoie tous les panneaux avec toutes
    leurs callbacks. Avec panel: chaque controle,
    son type, sa callback et ce qu'elle appelle, pagine et filtrable par
    contains. Signale aussi les callbacks enregistrees par le code (timer,
    TCP, thread, table de pointeurs), celles cablees nulle part et celles
    nommees dans un .uir sans exister dans le code; en resume, les panneaux
    variantes l'un de l'autre et les controles inutilises par type."""
    cfg = _cfg()
    engine = _engine(cfg)
    root = engine.root
    engine.close()
    payload = build_ui_map(cfg.db_path, root, panel, summary=panel is None)
    payload = paginate(payload, contains, sort, limit, offset, uir)
    args = {"panel": panel, "contains": contains, "sort": sort, "limit": limit, "offset": offset,
            "uir": uir}
    return _reply("ui_map", args, payload)


# -------------------------------------------------------------- analyses -------

@mcp.tool()
def dead_code(section: str | None = None, limit: int = 15, offset: int = 0) -> str:
    """Code probablement mort, lu par nom. Sans section: total et debut de
    chaque categorie. section: functions (non appelees; certain si static),
    callbacks (cablees nulle part), globals, macros (confiance faible),
    panels (jamais cites dans le code)."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        mapper = UiMap(engine)
        panels = sorted(mapper.uir["panels"]) if mapper.uir else []
        found = analysis.dead_code(engine, mapper.uir, panels)
    finally:
        engine.close()
    args = {"section": section, "limit": limit, "offset": offset}
    if section:
        if section not in found:
            return _reply("dead_code", args, {"error": f"section inconnue: {section}",
                                               "sections": list(found)})
        payload = {"section": section, "note": analysis.DEAD_NOTES[section],
                   **page(found[section], offset, limit)}
        return _reply("dead_code", args, payload)
    payload = {
        name: {"total": len(items), "note": analysis.DEAD_NOTES[name], "top": items[:limit]}
        for name, items in found.items()
    }
    return _reply("dead_code", args, payload)


@mcp.tool()
def lock_check(kind: str | None = None, limit: int = 30, offset: int = 0) -> str:
    """Verrous pris et rendus (CmtGetLock, TSV, sections critiques), suivis
    chemin par chemin dans chaque fonction. kind filtre: return_while_held
    (return avec verrou pris), held_at_end, release_without_acquire. Ne
    renvoie que des positions, jamais de code."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        report = analysis.lock_check(engine)
    finally:
        engine.close()
    findings = [f for f in report.pop("findings") if not kind or f["kind"] == kind]
    payload = {**report, "notes": analysis.LOCK_NOTES, "findings": page(findings, offset, limit)}
    return _reply("lock_check", {"kind": kind, "limit": limit, "offset": offset}, payload)


_CONFIDENCE_RANK = {"haute": 0, "moyenne": 1, "faible": 2}


@mcp.tool()
def risks(check: str | None = None, file: str | None = None, min_confidence: str = "moyenne",
          limit: int = 30, offset: int = 0) -> str:
    """Risques C classiques et fuites, sur le code compile, positions et
    explication seulement. check: unbounded_copy, format_mismatch,
    ignored_return, unchecked_alloc, leak_on_return, leak_at_end,
    double_release, ui_from_thread, shared_global_unlocked. min_confidence:
    haute, moyenne (defaut) ou faible. Paires de ressources supplementaires:
    "resource_pairs" dans .cvi-mcp.json ([["Ouvre", "Ferme", "return"]])."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        extra = cfg.settings.get("resource_pairs") or []
        checked = cfg.settings.get("checked_calls")
        items = riskmod.c_risks(engine, frozenset(checked) | riskmod.CHECKED_CALLS if checked else None)
        items += riskmod.resource_check(engine, extra)["findings"]
        items += threadmod.thread_risks(engine)
        if file:
            ids = engine.match_files(file)
            marks = ",".join("?" * len(ids))
            wanted = {engine.rel(r["path"]) for r in engine.db.execute(
                f"SELECT path FROM files WHERE id IN ({marks})", ids)} if ids else set()
            items = [i for i in items if i["file"] in wanted]
    finally:
        engine.close()
    floor = _CONFIDENCE_RANK.get(min_confidence, 1)
    hidden = sum(1 for i in items if _CONFIDENCE_RANK.get(i["confidence"], 2) > floor)
    items = [i for i in items if _CONFIDENCE_RANK.get(i["confidence"], 2) <= floor]
    counts: dict[str, dict] = {}
    for item in items:
        entry = counts.setdefault(item["check"], {"total": 0, "haute": 0, "moyenne": 0, "faible": 0})
        entry["total"] += 1
        entry[item["confidence"]] += 1
    if check:
        items = [i for i in items if i["check"] == check]
    items.sort(key=lambda i: (_CONFIDENCE_RANK.get(i["confidence"], 2), i["check"], i["file"], i["line"]))
    by_file: dict[str, int] = {}
    for item in items:
        by_file[item["file"]] = by_file.get(item["file"], 0) + 1
    payload = {
        "counts": counts,
        "below_min_confidence": hidden,
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])[:15]),
        "notes": {**riskmod.RISK_NOTES, **threadmod.THREAD_NOTES},
        "findings": page(items, offset, limit),
    }
    args = {"check": check, "file": file, "min_confidence": min_confidence, "limit": limit,
            "offset": offset}
    return _reply("risks", args, payload)


@mcp.tool()
def consistency(check: str | None = None, limit: int = 30, offset: int = 0) -> str:
    """Coherence du code compile: fonction definie deux fois, prototype
    different de la definition, prototype sans definition, fonction qui
    pourrait etre static, extern de type different, callback d'arite
    incorrecte, controle employe avec le handle d'un autre panneau."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        items = qualitymod.consistency(engine)
    finally:
        engine.close()
    counts: dict[str, int] = {}
    for item in items:
        counts[item["check"]] = counts.get(item["check"], 0) + 1
    if check:
        items = [i for i in items if i["check"] == check]
    items.sort(key=lambda i: (_CONFIDENCE_RANK.get(i["confidence"], 2), i["check"]))
    payload = {"counts": counts, "notes": qualitymod.CONSISTENCY_NOTES, "findings": page(items, offset, limit)}
    return _reply("consistency", {"check": check, "limit": limit, "offset": offset}, payload)


@mcp.tool()
def module_graph(file: str | None = None, format: str = "json", limit: int = 40,
                 offset: int = 0) -> str:
    """Dependances entre fichiers du projet: A depend de B quand A appelle
    une fonction definie dans B. Sans file: modules classes par nombre de
    liens, aretes les plus lourdes, cycles et paires mutuelles avec l'arete
    la plus legere a couper. Avec file: ce que ce fichier utilise et qui
    l'utilise. format: json, mermaid ou dot (graphe a coller dans un
    document)."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        graph = analysis.module_graph(engine)
        wanted = None
        if file:
            ids = set(engine.match_files(file))
            wanted = {engine.rel(r["path"]) for r in engine.db.execute("SELECT id, path FROM files")
                      if r["id"] in ids}
    finally:
        engine.close()
    args = {"file": file, "format": format, "limit": limit, "offset": offset}
    edges = graph["edges"]
    if wanted is not None:
        edges = [e for e in edges if e["from"] in wanted or e["to"] in wanted]
    if format in ("mermaid", "dot"):
        payload = {"format": format, "edges_shown": min(len(edges), limit),
                   "edges_total": len(edges), "graph": analysis.render_graph(edges, format, limit)}
        return _reply("module_graph", args, payload)
    if wanted is not None:
        if not wanted:
            return _reply("module_graph", args, {"error": f"aucun fichier indexe ne correspond a '{file}'"})
        payload = {
            "file": file,
            "uses": [e for e in edges if e["from"] in wanted],
            "used_by": [e for e in edges if e["to"] in wanted],
            "in_cycle": [c for c in graph["cycles"] if wanted & set(c["files"])],
        }
        return _reply("module_graph", args, payload)
    payload = {
        "modules": page(graph["modules"], offset, limit),
        "heaviest_edges": edges[:limit],
        "edges_total": len(edges),
        "cycles": graph["cycles"],
        "mutual": page(graph["mutual"], 0, limit),
    }
    return _reply("module_graph", args, payload)


@mcp.tool()
def threads(global_name: str | None = None) -> str:
    """Contextes d'execution: fonctions lancees par CmtScheduleThreadPoolFunction,
    NewAsyncTimer, CreateThread ou PostDeferredCall, ce que chaque contexte
    atteint, ses appels d'interface, et les globales partagees entre
    contextes. global_name donne le flux d'une globale: qui l'ecrit, qui la
    lit, depuis quel contexte, avec ou sans verrou."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        if global_name:
            payload = threadmod.data_flow(engine, global_name)
        else:
            payload = threadmod.threads(engine)
            payload["shared_globals"] = page(payload["shared_globals"], 0, 30)
    finally:
        engine.close()
    return _reply("threads", {"global_name": global_name}, payload)


@mcp.tool()
def call_sites(pattern: str, position: int | None = None) -> str:
    """Valeurs passees aux fonctions dont le nom correspond au motif
    (Modbus*, *Wago*, Ini_Get*, MBT*): pour chaque position d'argument, les
    constantes utilisees (macros evaluees), leurs usages et les fonctions
    qui les passent. Retrouve adresses, registres et bits d'E/S."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = mapping.call_sites(engine, pattern, position, engine.project.pp.env)
    finally:
        engine.close()
    return _reply("call_sites", {"pattern": pattern, "position": position}, payload)


@mcp.tool()
def persistence(kind: str = "ini", section: str | None = None, limit: int = 50,
                offset: int = 0) -> str:
    """Donnees persistees. kind=ini: sections et cles INI (bibliotheque
    inifile), fonctions qui les lisent ou ecrivent, cles ecrites jamais
    relues et l'inverse; section filtre une section. kind=sql: tables et
    requetes ecrites en chaines litterales, par fonction."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        if kind == "sql":
            data = mapping.sql_map(engine)
            payload = {"tables": data["tables"], "functions": page(data["functions"], offset, limit)}
        else:
            data = mapping.ini_map(engine)
            keys = data.pop("keys")
            if section:
                keys = [k for k in keys if section.lower() in k["section"].lower()]
            payload = {**data, "keys": page(keys, offset, limit)}
            payload["written_never_read"] = page(data["written_never_read"], 0, limit)
            payload["read_never_written"] = page(data["read_never_written"], 0, limit)
    finally:
        engine.close()
    return _reply("persistence", {"kind": kind, "section": section, "limit": limit,
                                  "offset": offset}, payload)


@mcp.tool()
def complexity(
    sort: str = "complexity",
    file: str | None = None,
    min_risk: str | None = None,
    section: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """Fonctions a risque sur le code compile: lignes, complexite cyclomatique
    et cognitive, imbrication, parametres, variables locales et pile
    approchee. sort: complexity, cognitive, lines, depth, frame, params.
    min_risk: modere, eleve, tres_eleve. section: by_file (resume par
    fichier) ou duplicates (fonctions copiees d'un meme modele). Seuils
    reglables dans .cvi-mcp.json: "thresholds": {"lines": [100, 200, 400]}."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        rows = analysis.complexity(engine, sort, cfg.settings.get("thresholds"))
        if file:
            ids = set(engine.match_files(file))
            names = {engine.rel(r["path"]) for r in engine.db.execute("SELECT id, path FROM files")
                     if r["id"] in ids}
            rows = [r for r in rows if r["file"] in names]
        dupes = analysis.duplicates(engine) if section == "duplicates" else None
    finally:
        engine.close()
    levels = ["faible", "modere", "eleve", "tres_eleve"]
    if min_risk in levels:
        rows = [r for r in rows if levels.index(r["risk"]) >= levels.index(min_risk)]
    distribution: dict[str, int] = {}
    for row in rows:
        distribution[row["risk"]] = distribution.get(row["risk"], 0) + 1
    args = {"sort": sort, "file": file, "min_risk": min_risk, "section": section,
            "limit": limit, "offset": offset}
    if section == "by_file":
        payload = {"risk_distribution": distribution, **page(analysis.by_file(rows), offset, limit)}
    elif section == "duplicates":
        payload = {"duplicated_lines": sum(d["duplicated_lines"] for d in dupes),
                   **page(dupes, offset, limit)}
    else:
        payload = {"risk_distribution": distribution, **page(rows, offset, limit)}
    return _reply("complexity", args, payload)


# ------------------------------------------------------------------- SDK -------

@mcp.tool()
def sdk_index(paths: str | None = None, force: bool = False) -> str:
    """Indexe les en-tetes et panneaux de fonction du SDK NI dans un espace
    separe. paths: chemins separes par des points-virgules, sinon
    CVI_MCP_SDK_PATHS puis les emplacements standards."""
    cfg = _cfg()
    if paths:
        roots = [Path(p.strip()) for p in paths.split(";") if p.strip()]
    else:
        roots = resolve_roots(cfg.sdk_paths)

    if not roots:
        payload = {
            "error": "aucun chemin SDK",
            "hint": "definir CVI_MCP_SDK_PATHS ou passer paths",
            "looked_at": cvienv.PROGRAM_ROOTS,
        }
        return _reply("sdk_index", {"paths": paths, "force": force}, payload)

    indexer = SdkIndexer(cfg.sdk_db_path)
    try:
        payload = indexer.index(roots, force=force)
    finally:
        indexer.close()
    return _reply("sdk_index", {"paths": paths, "force": force}, payload)


@mcp.tool()
def sdk_lookup(name: str, exact: bool = True, limit: int = 20) -> str:
    """Prototype, parametres et commentaire d'une fonction du SDK indexe."""
    cfg = _cfg()
    lookup = SdkLookup(cfg.sdk_db_path)
    try:
        rows = lookup.lookup(name, exact, limit)
        status = lookup.status()
    except sqlite3.Error as exc:
        return _reply("sdk_lookup", {"name": name}, {"error": str(exc)})
    finally:
        lookup.close()

    payload: dict = {"count": len(rows), "results": rows}
    if not rows:
        payload["index_status"] = status
        payload["hint"] = (
            "lancer sdk_index si l'index SDK est vide"
            if not status.get("prototypes")
            else "absent des en-tetes indexes: il vient peut-etre de l'installation CVI "
            "(toolbox, userint), a ajouter a CVI_MCP_SDK_PATHS"
        )
    return _reply("sdk_lookup", {"name": name, "exact": exact, "limit": limit}, payload)


# ----------------------------------------------------------------- build -------

@mcp.tool()
def build_check(target: str | None = None, dry_run: bool = False, config: str | None = None) -> str:
    """Compile le projet et renvoie erreurs et warnings structures, par
    fichier, avec ce qui est apparu ou a disparu depuis le build precedent.
    config: Debug, Release, Debug64, Release64 ou une configuration du .prj
    (defaut: CVI_MCP_BUILD_CONFIG ou celle de l'index). Signale une version
    de CVI du projet absente du poste. dry_run=true affiche seulement la
    commande. Aucune source n'est modifiee; la compilation produit ses
    fichiers de sortie habituels."""
    cfg = _cfg()
    args = {"target": target, "dry_run": dry_run, "config": config}
    resolved = buildmod.resolve_command(cfg.build_cmd, cvienv.find_build_commands())
    projects = cfg.project_files()
    if not projects:
        payload = {"error": "aucun .prj dans la racine du projet", "root": str(cfg.project_path)}
        return _reply("build_check", args, payload)

    settings = build_settings(projects[0], config or cfg.build_config)
    chosen = config or settings.get("config")
    known = [c["name"] for c in settings.get("configs", [])]
    year = buildmod.cvi_year(settings.get("cvi_version"))
    installed = [i.get("version") for i in cvienv.find_installs()]
    context = {"project_file": projects[0].name, "config": chosen, "configurations": known,
               "is_64": settings.get("is_64"), "project_cvi_version": year}
    if year and installed and not any(year == (v or "") for v in installed):
        context["version_warning"] = (f"projet enregistre avec CVI {year}, installe(s): "
                                      f"{', '.join(v or '?' for v in installed)}")
    if config and known and config not in known and config.lower() not in buildmod._CONFIG_FLAGS:
        return _reply("build_check", args, {**context, "error": f"configuration inconnue: {config}"})

    if dry_run or not resolved["available"]:
        payload = {
            **context,
            "mode": "dry_run" if dry_run else "indisponible",
            "command": resolved,
            "config_flag": buildmod.config_flag(chosen),
            "args_template": cfg.build_args,
            "timeout_seconds": cfg.build_timeout,
        }
        return _reply("build_check", args, payload)

    payload = buildmod.run_build(
        projects[0], resolved["command"], cfg.build_args, cfg.build_timeout, target, chosen
    )
    key = f"last_build:{chosen}"
    db = sqlite3.connect(str(cfg.db_path))
    try:
        ensure_schema(db)
        row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        previous = json.loads(row[0]) if row else None
        db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                   (key, json.dumps({k: payload.get(k) for k in ("diagnostics", "errors", "warnings")})))
        db.commit()
    finally:
        db.close()
    change = buildmod.compare_builds(previous, payload)
    if change:
        payload["since_previous_build"] = change
    payload["diagnostics"] = page(payload.get("diagnostics", []), 0, 60)
    return _reply("build_check", args, {**context, **payload})


# ------------------------------------------------------------------ diff -------

@mcp.tool()
def snapshot_save(label: str, overwrite: bool = False) -> str:
    """Enregistre l'etat courant de l'index sous un nom, pour pouvoir comparer
    plus tard. Aucun code n'est stocke, seulement des empreintes."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = diffs.save_snapshot(engine.db, label, engine.root, overwrite)
    finally:
        engine.close()
    return _reply("snapshot_save", {"label": label, "overwrite": overwrite}, payload)


@mcp.tool()
def snapshot_list() -> str:
    """Instantanes disponibles."""
    cfg = _cfg()
    engine = QueryEngine(cfg.db_path, cfg.project_path)
    try:
        rows = diffs.list_snapshots(engine.db)
    except sqlite3.Error as exc:
        return _reply("snapshot_list", {}, {"error": str(exc)})
    finally:
        engine.close()
    return _reply("snapshot_list", {}, {"count": len(rows), "snapshots": rows})


@mcp.tool()
def snapshot_delete(label: str) -> str:
    """Supprime un instantane."""
    cfg = _cfg()
    engine = QueryEngine(cfg.db_path, cfg.project_path)
    try:
        payload = diffs.delete_snapshot(engine.db, label)
    finally:
        engine.close()
    return _reply("snapshot_delete", {"label": label}, payload)


@mcp.tool()
def diff_versions(before: str, after: str = diffs.CURRENT, path: str | None = None,
                  files: str | None = None) -> str:
    """Compare deux etats du projet. Resume en phrases (headline), puis
    fonctions ajoutees, supprimees, deplacees, signature, valeur (macros) ou
    corps modifies, symboles passes entre compile et exclu, constantes
    d'interface et cles INI ajoutees ou retirees, appels ajoutes ou retires.
    before et after: nom d'instantane, 'current' (index vivant) ou
    'path:<dossier>' (autre version ou variante posee a cote). path est un
    raccourci pour after='path:<dossier>'. files restreint aux fichiers dont
    le chemin relatif correspond (SpecClient/*)."""
    cfg = _cfg()
    args = {"before": before, "after": after, "path": path, "files": files}
    if path:
        after = f"path:{path}"

    states: dict[str, dict] = {}
    labels = [before, after]
    if any(not label.startswith("path:") for label in labels):
        engine = _engine(cfg)
        try:
            for label in labels:
                if label.startswith("path:") or label in states:
                    continue
                if label == diffs.CURRENT:
                    states[label] = diffs.capture_state(engine.db, engine.root)
                    continue
                loaded = diffs.load_snapshot(engine.db, label)
                if loaded is None:
                    known = [s["label"] for s in diffs.list_snapshots(engine.db)]
                    return _reply("diff_versions", args,
                                  {"error": f"instantane '{label}' introuvable", "available": known})
                states[label] = loaded
        finally:
            engine.close()
    for label in labels:
        if label.startswith("path:") and label not in states:
            folder = Path(label[5:])
            if not folder.is_absolute():
                folder = (cfg.project_path / folder).resolve()
            if not folder.is_dir():
                return _reply("diff_versions", args, {"error": f"dossier '{folder}' introuvable"})
            states[label] = diffs.state_of_path(folder, lambda p: cfg.is_excluded(Path(p).name))

    payload = diffs.compare(diffs.restrict(states[before], files), diffs.restrict(states[after], files),
                            before, after)
    for key in ("added", "removed", "moved", "signature_changed", "value_changed", "body_changed",
                "calls_added", "calls_removed", "activation_changed"):
        if isinstance(payload.get(key), list) and len(payload[key]) > 100:
            payload[key] = page(payload[key], 0, 100)
    return _reply("diff_versions", args, payload)


@mcp.tool()
def history(target: str | None = None, limit: int = 10) -> str:
    """Historique git, si le projet est dans un depot (git requis, lecture
    seule). target: une fonction (commits qui ont touche ses lignes), un
    fichier, ou rien (derniers commits et fichiers modifies)."""
    cfg = _cfg()
    engine = _engine(cfg)
    try:
        payload = gitmod.history(engine, target, limit)
    finally:
        engine.close()
    return _reply("history", {"target": target, "limit": limit}, payload)


# ------------------------------------------------------------------ entree -----

def main(argv: list[str] | None = None) -> int:
    """Sans argument: serveur MCP sur stdio. Avec --check: diagnostic lisible,
    pour verifier une installation sans passer par un client."""
    parser = argparse.ArgumentParser(
        prog="cvi-mcp",
        description="Serveur MCP en lecture seule pour projets LabWindows/CVI.",
        epilog="Sans argument, le serveur demarre et parle MCP sur stdio.",
    )
    parser.add_argument("--version", action="version", version=f"cvi-mcp {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="afficher le diagnostic d'environnement puis quitter",
    )
    args = parser.parse_args(argv)

    if args.check:
        report = json.loads(cvi_environment.fn() if hasattr(cvi_environment, "fn") else cvi_environment())
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["warnings"] else 0

    mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
