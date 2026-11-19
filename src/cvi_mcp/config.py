# ==============================================================================
#  cvi_mcp/config.py
#  Role     Configuration du serveur, lue dans l'environnement
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import fnmatch
import hashlib
import json
import os
from pathlib import Path

# La detection de l'installation CVI vit dans cvienv: elle touche au disque et
# n'a donc pas sa place dans un objet reconstruit a chaque appel.


def _flag(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _number(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


# Projet choisi par use_project, prioritaire sur CVI_MCP_PROJECT pour la
# duree du processus serveur.
_ACTIVE: dict[str, str] = {}

SETTINGS_FILE = ".cvi-mcp.json"


def set_active_project(target: str | None) -> None:
    if target:
        _ACTIVE["target"] = target
    else:
        _ACTIVE.pop("target", None)


def named_projects() -> dict[str, str]:
    """CVI_MCP_PROJECTS="Nom=chemin;Autre=chemin": projets connus par nom."""
    out: dict[str, str] = {}
    for item in os.environ.get("CVI_MCP_PROJECTS", "").split(";"):
        name, sep, path = item.partition("=")
        if sep and name.strip() and path.strip():
            out[name.strip()] = path.strip()
    return out


def parse_define_list(raw: str) -> dict[str, str]:
    """"A=1;B" vers {"A": "1", "B": "1"}."""
    out: dict[str, str] = {}
    for item in (raw or "").replace(",", ";").split(";"):
        name, sep, value = item.strip().partition("=")
        if name:
            out[name.strip()] = value.strip() if sep else "1"
    return out


def _path_list(key: str) -> list[Path]:
    raw = os.environ.get(key, "")
    return [Path(p.strip()) for p in raw.split(";") if p.strip()]


class Config:
    """Instantanee de l'environnement. Recreee a chaud par le serveur."""

    def __init__(self) -> None:
        home = Path.home() / ".cvi-mcp"

        # -------------------------------------------------------- projet ------
        # Dossier du projet, ou directement son .prj / .cws.
        raw_target = _ACTIVE.get("target") or os.environ.get("CVI_MCP_PROJECT", ".")
        raw_target = named_projects().get(raw_target, raw_target)
        target = Path(raw_target).resolve()
        self.project_file = target if target.is_file() else None
        self.project_path = target.parent if target.is_file() else target
        # Un index par projet: changer de projet ne detruit pas l'index du precedent.
        digest = hashlib.sha256(os.path.normcase(str(target)).encode()).hexdigest()[:12]
        self.db_path = Path(
            os.environ.get("CVI_MCP_DB") or str(home / f"index-{digest}.db")
        )
        # Le SDK est commun a tous les projets: un seul index.
        self.sdk_db_path = Path(
            os.environ.get("CVI_MCP_SDK_DB") or os.environ.get("CVI_MCP_DB")
            or str(home / "sdk.db")
        )
        self.audit_log_path = Path(
            os.environ.get("CVI_MCP_AUDIT_LOG", str(home / "audit.jsonl"))
        )
        self.exclude_patterns = [
            p.strip() for p in os.environ.get("CVI_MCP_EXCLUDE", "").split(";") if p.strip()
        ]

        # ------------------------------------------- protection du code -------
        self.allow_body = _flag("CVI_MCP_ALLOW_BODY", False)
        self.max_snippet_lines = _number("CVI_MCP_MAX_SNIPPET_LINES", 40)

        # ----------------------------------------------------------- SDK ------
        self.sdk_paths = _path_list("CVI_MCP_SDK_PATHS")

        # ------------------------------------------------ preprocesseur -------
        # Fichier optionnel de reglages du projet, ecrit par l'utilisateur.
        self.settings = self._load_settings()
        self.build_config = (
            os.environ.get("CVI_MCP_BUILD_CONFIG", "").strip()
            or str(self.settings.get("build_config") or "")
        ) or None
        self.extra_defines = {
            **{str(k): str(v) for k, v in (self.settings.get("defines") or {}).items()},
            **parse_define_list(os.environ.get("CVI_MCP_DEFINES", "")),
        }
        self.index_unlisted = _flag("CVI_MCP_INDEX_UNLISTED", False)

        # ------------------------------------------------------ reponses -------
        self.allow_peek = _flag("CVI_MCP_ALLOW_PEEK", False)
        self.max_chars = _number("CVI_MCP_MAX_CHARS", 40000)
        self.output_format = os.environ.get("CVI_MCP_FORMAT", "json").strip().lower() or "json"
        self.audit_paths = _flag("CVI_MCP_AUDIT_PATHS", True)

        # --------------------------------------------------------- build ------
        self.build_cmd = os.environ.get("CVI_MCP_BUILD_CMD", "").strip()
        self.build_args = os.environ.get("CVI_MCP_BUILD_ARGS", "{project}").strip()
        self.build_timeout = _number("CVI_MCP_BUILD_TIMEOUT", 300)

    def _load_settings(self) -> dict:
        path = Path(os.environ.get("CVI_MCP_SETTINGS", "") or self.project_path / SETTINGS_FILE)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def is_excluded(self, rel_path: str) -> bool:
        posix = rel_path.replace("\\", "/")
        name = posix.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(name, pat)
            for pat in self.exclude_patterns
        )

    def project_files(self) -> list[Path]:
        """Fichiers .prj du projet: celui designe, ceux du .cws designe, sinon
        ceux de la racine."""
        if self.project_file and self.project_file.suffix.lower() == ".prj":
            return [self.project_file]
        if self.project_file and self.project_file.suffix.lower() == ".cws":
            from .prj import parse_cws

            return [Path(p) for p in parse_cws(self.project_file)]
        return sorted(self.project_path.glob("*.prj"))

    def relative(self, path: str | Path) -> str:
        """Chemin relatif a la racine du projet, pour que deux versions
        d'un meme projet dans deux dossiers restent comparables."""
        try:
            return Path(os.path.relpath(Path(path).resolve(), self.project_path)).as_posix()
        except ValueError:
            return Path(path).as_posix()
