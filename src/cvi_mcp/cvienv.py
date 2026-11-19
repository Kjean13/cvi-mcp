# ==============================================================================
#  cvi_mcp/cvienv.py
#  Role     Detection de l'installation CVI, des includes SDK et du build
#  Etat     stable, independant du millesime
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import os
import platform
import re
import shutil
import sys
from pathlib import Path

# Racines d'installation. Le millesime n'est jamais code en dur: un glob CVI*
# retrouve 2017, 2019, 2020 comme toute version ulterieure.
PROGRAM_ROOTS = (
    r"C:\Program Files (x86)\National Instruments",
    r"C:\Program Files\National Instruments",
)

_INSTALL_GLOB = "CVI*"
_VERSION = re.compile(r"CVI\D*(\d+)", re.IGNORECASE)

# Sous-dossiers d'en-tetes a l'interieur d'une installation CVI.
_INSTALL_INCLUDES = ("include", "sdk/include", "toolslib", "toolslib/toolbox")

# En-tetes livres hors de l'installation CVI elle meme.
_SHARED_INCLUDES = (
    r"C:\Program Files (x86)\National Instruments\Shared\CVI\include",
    r"C:\Program Files\National Instruments\Shared\CVI\include",
    r"C:\Program Files (x86)\IVI Foundation\VISA\WinNT\include",
    r"C:\Program Files\IVI Foundation\VISA\Win64\include",
    r"C:\Program Files (x86)\National Instruments\NI-DAQ\DAQmx ANSI C Dev\include",
    r"C:\Program Files\National Instruments\NI-DAQ\DAQmx ANSI C Dev\include",
)

# Noms rencontres selon les versions. Le chemin exact depend de l'installation,
# d'ou CVI_MCP_BUILD_CMD pour trancher sans deviner.
BUILD_EXECUTABLES = ("compile.exe", "cvicc")

# Sous-dossiers ou l'executable de build peut se trouver.
_BUILD_SUBDIRS = ("", "bin")


def _roots(override) -> list[Path]:
    if override:
        return [Path(p) for p in override]
    return [Path(p) for p in PROGRAM_ROOTS]


def find_installs(roots=None) -> list[dict]:
    """Installations CVI presentes, triees du millesime le plus recent au plus
    ancien. Le libelle de version vient du nom de dossier, pas d'une liste."""
    found: list[dict] = []
    for root in _roots(roots):
        if not root.is_dir():
            continue
        for path in sorted(root.glob(_INSTALL_GLOB)):
            if not path.is_dir():
                continue
            match = _VERSION.search(path.name)
            found.append(
                {
                    "path": str(path),
                    "version": match.group(1) if match else None,
                    "name": path.name,
                    "has_include": (path / "include").is_dir(),
                    "has_bin": (path / "bin").is_dir(),
                }
            )
    found.sort(key=lambda item: (item["version"] or ""), reverse=True)
    return found


def find_sdk_dirs(roots=None) -> list[Path]:
    """Dossiers d'en-tetes exploitables, installation CVI puis composants
    partages. Seuls les dossiers reellement presents sont renvoyes."""
    dirs: list[Path] = []

    for install in find_installs(roots):
        base = Path(install["path"])
        for relative in _INSTALL_INCLUDES:
            candidate = base.joinpath(*relative.split("/"))
            if candidate.is_dir():
                dirs.append(candidate)

    for raw in _SHARED_INCLUDES:
        candidate = Path(raw)
        if candidate.is_dir():
            dirs.append(candidate)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in dirs:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_build_commands(roots=None) -> list[Path]:
    """Executables de build trouves, PATH d'abord puis installations."""
    found: list[Path] = []

    for name in BUILD_EXECUTABLES:
        hit = shutil.which(name)
        if hit:
            found.append(Path(hit))

    for install in find_installs(roots):
        base = Path(install["path"])
        for subdir in _BUILD_SUBDIRS:
            folder = base / subdir if subdir else base
            for name in BUILD_EXECUTABLES:
                candidate = folder / name
                if candidate.is_file():
                    found.append(candidate)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def describe(cfg, roots=None) -> dict:
    """Diagnostic complet: plateforme, installations, SDK, build, reglages.

    C'est la reponse a la question de la compatibilite avec une version
    donnee de CVI: on rapporte ce qui est reellement present sur la machine
    au lieu de supposer un millesime.
    """
    installs = find_installs(roots)
    sdk_dirs = find_sdk_dirs(roots)
    builds = find_build_commands(roots)

    warnings: list[str] = []
    if platform.system() != "Windows":
        warnings.append(
            "le serveur ne tourne pas sous Windows: l'analyse statique "
            "fonctionne, la detection CVI et le build ne peuvent rien trouver"
        )
    if not installs and platform.system() == "Windows":
        warnings.append(
            "aucune installation CVI detectee sous les racines standards, "
            "renseigner CVI_MCP_SDK_PATHS et CVI_MCP_BUILD_CMD"
        )
    if not cfg.project_path.is_dir():
        warnings.append(f"CVI_MCP_PROJECT ne designe pas un dossier: {cfg.project_path}")
    elif (
        cfg.project_file is None
        and not any(cfg.project_path.glob("*.prj"))
        and not any(cfg.project_path.glob("*.cws"))
    ):
        warnings.append(
            "ni .prj ni .cws dans la racine: les sources seront trouvees par "
            "balayage du dossier"
        )
    if not builds and not cfg.build_cmd:
        warnings.append("aucun executable de build trouve, build_check restera en diagnostic")

    return {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "python_bits": 64 if sys.maxsize > 2**32 else 32,
        },
        "cvi_installs": installs,
        "sdk_dirs_detected": [str(p) for p in sdk_dirs],
        "build_commands_detected": [str(p) for p in builds],
        "settings": {
            "project": str(cfg.project_path),
            "db": str(cfg.db_path),
            "audit_log": str(cfg.audit_log_path),
            "allow_body": cfg.allow_body,
            "max_snippet_lines": cfg.max_snippet_lines,
            "exclude": cfg.exclude_patterns,
            "sdk_paths_configured": [str(p) for p in cfg.sdk_paths],
            "build_cmd_configured": cfg.build_cmd or None,
            "build_args": cfg.build_args,
            "build_timeout": cfg.build_timeout,
        },
        "warnings": warnings,
    }
