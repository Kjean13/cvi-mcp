# ==============================================================================
#  cvi_mcp/prj.py
#  Role     Lecture tolerante des fichiers projet .prj et espace de travail .cws
#  Etat     stable, valide sur projets CVI 2019 reels
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import configparser
import os
import re
from pathlib import Path

# ============================ Interne =========================================
from .cparse import read_source

_SOURCE_EXT = (".c", ".h")
_BINARY_EXT = (".lib", ".dll", ".obj")

# Chemin absolu Windows: lettre de lecteur ou partage UNC.
_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\]{2})")

# Forme MSYS que CVI ecrit dans les .prj: /c/Users/... pour C:/Users/...
_MSYS = re.compile(r"^/([A-Za-z])/(.*)$")


def _load(path: Path) -> configparser.ConfigParser:
    # strict=False: les .prj reels contiennent des cles repetees.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        text, _encoding, _raw = read_source(Path(path))
        parser.read_string(text)
    except (configparser.Error, OSError):
        pass
    return parser


def _clean(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _resolve(base: Path, value: str) -> str:
    """Chemin d'un fichier liste dans un .prj.

    Les .prj sont ecrits sous Windows: separateurs antislash, parfois chemin
    absolu avec lettre de lecteur. On accepte les deux formes.
    """
    normalized = value.replace("\\", "/")
    msys = _MSYS.match(normalized)
    if msys and os.name == "nt":
        return str(Path(f"{msys.group(1)}:/{msys.group(2)}"))
    if _ABSOLUTE.match(value) or (msys and normalized.startswith("/")):
        return str(Path(normalized))
    return str((base / normalized).resolve())


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value


def _dedup(paths: list[str]) -> list[str]:
    """Deduplication insensible a la casse: NTFS ne distingue pas main.c de
    Main.c, deux entrees du .prj ne doivent pas donner deux fichiers."""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        key = os.path.normcase(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return sorted(unique, key=os.path.normcase)


def _files_by_name(base: Path) -> dict[str, list[str]]:
    """Sources du dossier du projet par nom, pour retrouver un fichier dont
    le chemin note dans le .prj n'existe pas sur cette machine."""
    index: dict[str, list[str]] = {}
    for path in base.rglob("*"):
        if path.is_file() and path.suffix.lower() in _SOURCE_EXT:
            index.setdefault(path.name.lower(), []).append(str(path.resolve()))
    return index


def _absolute_path(items: dict[str, str]) -> str:
    """Chemin absolu d'un [File NNNN]. CVI coupe les chemins longs en
    Path Line0001, Path Line0002...: on les recolle dans l'ordre."""
    if items.get("path"):
        return _clean(items["path"])
    parts = sorted(k for k in items if k.startswith("path line"))
    return "".join(_clean(items[k]) for k in parts)


def _resolve_entry(base: Path, items: dict[str, str], by_name) -> tuple[str | None, str | None]:
    """Retrouve un [File NNNN]. Ordre: chemin relatif au projet (portable),
    chemin absolu note a l'ecriture, puis meme nom dans le dossier du projet
    si un seul fichier convient. Renvoie (chemin, nom liste si deplace)."""
    rel = _clean(items.get("path rel path", ""))
    absolute = _absolute_path(items)
    for value in (rel, absolute):
        if value:
            candidate = _resolve(base, value)
            if Path(candidate).is_file():
                return candidate, None
    listed = absolute or rel
    if not listed:
        return None, None
    hits = by_name().get(Path(listed.replace("\\", "/")).name.lower(), [])
    if len(hits) == 1:
        return hits[0], listed
    return None, listed


def parse_prj(prj_path: Path) -> dict:
    """Sources, bibliothheques et sections brutes d'un .prj.

    Le format exact varie selon la version de CVI. On ratisse donc large:
    sections [File NNNN] d'abord, puis toute valeur dont l'extension est
    reconnue, et on renvoie la liste des sections pour pouvoir diagnostiquer
    un fichier qui ne rendrait rien.
    """
    prj_path = Path(prj_path)
    base = prj_path.parent
    parser = _load(prj_path)

    sources: list[str] = []
    libs: list[str] = []
    includes: list[str] = []
    relocated: list[dict] = []
    unresolved: list[str] = []
    lib_files: list[dict] = []
    cache: dict = {}

    def by_name() -> dict[str, list[str]]:
        if "index" not in cache:
            cache["index"] = _files_by_name(base)
        return cache["index"]

    for section in parser.sections():
        if section.lower().startswith("file "):
            items = {k.lower(): v for k, v in parser.items(section)}
            listed_value = _clean(items.get("path rel path", "")) or _absolute_path(items)
            lowered = listed_value.lower()
            if lowered.endswith(_SOURCE_EXT):
                found, listed = _resolve_entry(base, items, by_name)
                if found:
                    sources.append(found)
                    if listed:
                        relocated.append({"listed": listed, "found": found})
                else:
                    # Garde le chemin note: tel quel, il reste exact sur la machine d'origine.
                    sources.append(_resolve(base, listed or listed_value))
                    # Le chemin relatif parle a l'utilisateur, l'absolu vient d'une autre machine.
                    unresolved.append(_clean(items.get("path rel path", "")) or listed)
            elif lowered.endswith(_BINARY_EXT):
                libs.append(listed_value)
                found, _listed = _resolve_entry(base, items, by_name)
                lib_files.append({"listed": _clean(items.get("path rel path", "")) or listed_value,
                                  "found": found})
            # .uir, .fp et autres: references, rien a indexer.
            continue
        for key, raw in parser.items(section):
            value = _clean(raw)
            if not value:
                continue
            lowered = value.lower()

            if lowered.endswith(_SOURCE_EXT):
                sources.append(_resolve(base, value))
            elif lowered.endswith(_BINARY_EXT):
                libs.append(value)
            elif "include" in key.lower() and _looks_like_path(value):
                includes.append(value)

    return {
        "prj_path": str(prj_path),
        "sources": _dedup(sources),
        "libs": sorted(set(libs)),
        "includes": sorted(set(includes)),
        "relocated": relocated,
        "unresolved": unresolved,
        "lib_files": lib_files,
        "sections": parser.sections(),
    }


_DEFINE_FLAG = re.compile(r'/D("[^"]*"|[^\s"]+)')


def parse_defines(raw: str) -> dict[str, str]:
    """Options /DNOM ou /DNOM=valeur de la ligne de compilation CVI."""
    out: dict[str, str] = {}
    for match in _DEFINE_FLAG.finditer(raw or ""):
        item = match.group(1).strip('"')
        name, _sep, value = item.partition("=")
        if name:
            out[name] = value if _sep else "1"
    return out


def build_settings(prj_path: Path, wanted: str | None = None) -> dict:
    """Ce que le compilateur recoit d'un .prj: chemins d'inclusion, #define,
    configuration (32 ou 64 bits, debug ou release) et version de CVI.

    wanted choisit la configuration par son nom (Debug, Release64...);
    sinon la premiere configuration Debug, sinon la premiere trouvee."""
    prj_path = Path(prj_path)
    base = prj_path.parent
    parser = _load(prj_path)
    sections = {name.lower(): name for name in parser.sections()}

    version = None
    if "project header" in sections:
        version = _clean(parser.get(sections["project header"], "version", fallback="")) or None

    include_paths: list[dict] = []
    if "include paths" in sections:
        items = {k.lower(): v for k, v in parser.items(sections["include paths"])}
        numbers = sorted({int(m.group(1)) for k in items
                          for m in [re.match(r"include path (\d+)", k)] if m})
        for number in numbers:
            prefix = f"include path {number}"
            entry = {k[len(prefix):].strip(): v for k, v in items.items()
                     if k.startswith(prefix + " ") or k == prefix}
            entry = {("path" if k == "" else k): v for k, v in entry.items()}
            rel = _clean(entry.get("rel path", ""))
            absolute = _clean(entry.get("path", "")) or "".join(
                _clean(entry[k]) for k in sorted(entry) if k.startswith("line"))
            found = None
            for value in (rel, absolute):
                if value:
                    candidate = _resolve(base, value)
                    if Path(candidate).is_dir():
                        found = candidate
                        break
            include_paths.append({"listed": rel or absolute, "found": found})

    configs: list[dict] = []
    for lowered, name in sections.items():
        if lowered.startswith(("default build config", "custom build config")):
            items = {k.lower(): _clean(v) for k, v in parser.items(name)}
            if "is 64-bit" not in items and "config name" not in items:
                continue  # liste des configurations, pas une configuration
            configs.append({
                "name": items.get("config name") or name.split()[-1],
                "is_64": items.get("is 64-bit", "").lower() == "true",
                "is_release": items.get("is release", "").lower() == "true",
                "defines": parse_defines(items.get("compiler defines", "")),
            })

    common = {}
    if "compiler defines" in sections:
        common = parse_defines(parser.get(sections["compiler defines"], "compiler defines",
                                          fallback=""))
    chosen = None
    if wanted:
        chosen = next((c for c in configs if c["name"].lower() == wanted.lower()), None)
    if chosen is None:
        chosen = next((c for c in configs if not c["is_release"] and not c["is_64"]), None)
    if chosen is None and configs:
        chosen = configs[0]

    defines = {**common, **(chosen["defines"] if chosen else {})}
    return {
        "cvi_version": version,
        "include_paths": include_paths,
        "configs": [{k: v for k, v in c.items() if k != "defines"} for c in configs],
        "config": chosen["name"] if chosen else None,
        "is_64": bool(chosen and chosen["is_64"]),
        "is_release": bool(chosen and chosen["is_release"]),
        "defines": defines,
    }


def parse_cws(cws_path: Path) -> list[str]:
    """Liste des .prj references par un espace de travail."""
    cws_path = Path(cws_path)
    base = cws_path.parent
    parser = _load(cws_path)

    projects: list[str] = []
    for section in parser.sections():
        for _key, raw in parser.items(section):
            value = _clean(raw)
            if value.lower().endswith(".prj"):
                projects.append(_resolve(base, value))
    return _dedup(projects)


def discover_sources(root: Path) -> list[str]:
    """Repli quand aucun fichier projet n'est exploitable."""
    root = Path(root)
    return sorted(
        str(path.resolve())
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in _SOURCE_EXT
    )


def collect_sources(root: Path, is_excluded=None, project_file: Path | None = None) -> list[str]:
    """Sources d'un projet: .cws puis .prj puis balayage du dossier.

    Utilise pour le projet courant comme pour un dossier compare, afin que
    les deux cotes d'un diff soient resolus de la meme facon.
    """
    root = Path(root)
    found: list[str] = []

    if project_file is not None:
        if project_file.suffix.lower() == ".cws":
            for project in parse_cws(project_file):
                found.extend(parse_prj(Path(project))["sources"])
        else:
            found.extend(parse_prj(project_file)["sources"])

    if not found:
        for workspace in sorted(root.glob("*.cws")):
            for project in parse_cws(workspace):
                found.extend(parse_prj(Path(project))["sources"])
            if found:
                break

    if not found:
        for project in sorted(root.glob("*.prj")):
            found.extend(parse_prj(project)["sources"])

    if not found:
        found = discover_sources(root)

    unique = _dedup([p for p in found if Path(p).is_file()])
    if is_excluded is None:
        return unique
    return [path for path in unique if not is_excluded(path)]


def display_root(project_path: Path, sources: list[str]) -> Path:
    """Racine commune au projet et a toutes ses sources. Les chemins sortent
    relatifs a elle: Commun/Sources/x.c plutot que ../Commun/Sources/x.c
    quand le .prj vit dans un dossier voisin du code."""
    folders = [str(Path(project_path).resolve())]
    folders += [str(Path(p).resolve().parent) for p in sources]
    try:
        return Path(os.path.commonpath(folders))
    except ValueError:  # lecteurs differents sous Windows
        return Path(project_path).resolve()


def find_uir_files(root: Path) -> list[str]:
    """Fichiers d'interface. Binaires, donc jamais analyses directement:
    le lien avec le code passe par le .h genere."""
    root = Path(root)
    return sorted(str(p.resolve()) for p in root.rglob("*.uir") if p.is_file())
