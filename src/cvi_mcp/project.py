# ==============================================================================
#  cvi_mcp/project.py
#  Role     Assemble un projet: sources, configuration de build, preprocesseur
#  Etat     stable
#  Version  0.3.3
# ==============================================================================

# ============================ Standard ========================================
import os
from dataclasses import dataclass, field
from pathlib import Path

# ============================ Interne =========================================
from .config import Config
from .preproc import BuildContext, Preprocessor
from .prj import build_settings, collect_sources, discover_sources, display_root


@dataclass
class Project:
    sources: list[str]
    units: list[str]
    files: list[str]
    root: Path
    ctx: BuildContext
    settings: dict = field(default_factory=dict)
    unlisted: list[str] = field(default_factory=list)
    unreached: list[str] = field(default_factory=list)
    pp: Preprocessor | None = None
    # Resultats d'analyses couteuses, valables tant que ce projet l'est (il est
    # remplace des qu'un fichier ou un reglage change). Cle: nom de l'analyse.
    memo: dict = field(default_factory=dict)


def _inside(path: str, root: Path) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(str(root))]) == \
            os.path.normcase(str(root))
    except ValueError:
        return False


def context(cfg: Config, forced: dict[str, str] | None = None) -> tuple[BuildContext, dict]:
    """Configuration du preprocesseur: .prj, puis .cvi-mcp.json et
    CVI_MCP_DEFINES, puis les valeurs forcees d'un scenario what-if."""
    settings: dict = {}
    for prj in cfg.project_files():
        settings = build_settings(prj, cfg.build_config)
        break
    ctx = BuildContext(
        include_paths=[p["found"] for p in settings.get("include_paths", []) if p["found"]],
        defines={**settings.get("defines", {}), **cfg.extra_defines},
        forced=dict(forced or {}),
        is_64=settings.get("is_64", False),
        is_release=settings.get("is_release", False),
        cvi_version=settings.get("cvi_version"),
        config_name=settings.get("config") or "Debug",
    )
    return ctx, settings


def load(cfg: Config, forced: dict[str, str] | None = None) -> Project:
    """Sources a indexer et etat du preprocesseur.

    Les en-tetes atteints par un #include depuis le build et situes sous la
    racine du projet sont indexes meme s'ils ne figurent pas dans le .prj:
    ils sont compiles. Ceux hors de la racine (SDK, Program Files) restent
    externes."""
    sources = collect_sources(
        cfg.project_path, lambda p: cfg.is_excluded(cfg.relative(p)), cfg.project_file
    )
    ctx, settings = context(cfg, forced)
    root = display_root(cfg.project_path, sources)
    units = [s for s in sources if s.lower().endswith(".c")]

    unlisted: list[str] = []
    if cfg.index_unlisted:
        known = {os.path.normcase(s) for s in sources}
        unlisted = [
            p for p in discover_sources(cfg.project_path)
            if os.path.normcase(p) not in known and not cfg.is_excluded(cfg.relative(p))
        ]

    files = [*sources, *unlisted]
    pp = Preprocessor(files, ctx, units)
    pp.run()
    reached = [p for p in pp.reached if _inside(p, root) and not cfg.is_excluded(cfg.relative(p))]
    if reached:
        files = [*files, *sorted(reached)]
        pp = Preprocessor(files, ctx, units)
        pp.run()

    listed = {os.path.normcase(s) for s in sources}
    for path, result in pp.results.items():
        result.listed = os.path.normcase(path) in listed
    unreached = sorted(p for p, r in pp.results.items() if not r.in_build)
    return Project(sources, units, files, root, ctx, settings, unlisted, unreached, pp)
