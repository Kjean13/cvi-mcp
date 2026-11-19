# ==============================================================================
#  cvi_mcp/build.py
#  Role     Diagnostic de compilation: resolution de la commande, analyse sortie
#  Etat     parseur fiable, resolution dependante de l'installation CVI
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import re
import shlex
import shutil
import subprocess
from pathlib import Path

# Formats de diagnostic rencontres selon la version du compilateur.
_PATTERNS = (
    re.compile(
        r"^\s*(?P<file>[^\s(][^(]*?)\s*\(\s*(?P<line>\d+)\s*(?:,\s*(?P<col>\d+)\s*)?\)\s*:\s*"
        r"(?P<sev>error|warning|note|remark|fatal error)\s*(?P<code>[A-Za-z0-9_]+)?\s*:\s*(?P<msg>.+?)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?P<file>[^\s:][^:]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
        r"(?P<sev>error|warning|note|fatal error)\s*:\s*(?P<msg>.+?)\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r'^\s*"(?P<file>[^"]+)"\s*,\s*line\s+(?P<line>\d+)\s*:\s*'
        r"(?P<sev>error|warning)\s*:\s*(?P<msg>.+?)\s*$",
        re.IGNORECASE,
    ),
)

# Diagnostics sans position: edition de liens, options invalides.
_GLOBAL = re.compile(
    r"^\s*(?:(?P<who>LINK|LINKER|BUILD)\s*:\s*)?(?P<sev>error|warning|fatal error)\s*:?\s*(?P<msg>.+?)\s*$",
    re.IGNORECASE,
)

_SEVERITY = {"fatal error": "error", "error": "error", "warning": "warning"}


def resolve_command(configured: str, candidates: list[Path]) -> dict:
    """Commande de build a utiliser, et d'ou elle vient.

    La configuration explicite gagne toujours: le nom et l'emplacement de
    l'executable varient selon le millesime de CVI, on ne devine donc jamais
    a la place de l'utilisateur.
    """
    if configured:
        exe = shlex.split(configured)[0] if " " in configured else configured
        found = shutil.which(exe) or (exe if Path(exe).exists() else None)
        return {
            "command": configured,
            "resolved": found,
            "source": "CVI_MCP_BUILD_CMD",
            "available": found is not None,
        }

    if candidates:
        return {
            "command": str(candidates[0]),
            "resolved": str(candidates[0]),
            "source": "installation CVI detectee",
            "available": True,
            "other_candidates": [str(p) for p in candidates[1:]],
        }

    return {
        "command": None,
        "resolved": None,
        "source": None,
        "available": False,
        "hint": (
            "Executable de build introuvable. Definir CVI_MCP_BUILD_CMD avec le "
            "chemin complet, le nom variant selon la version de CVI."
        ),
    }


def _clean_path(raw: str, root: Path | None) -> str:
    """Chemin tel que le compilateur l'a emis, en separateurs POSIX et relatif
    au projet quand il est absolu. Garder le dossier evite de confondre deux
    fichiers homonymes."""
    path = raw.strip().replace("\\", "/")
    if root is not None:
        prefix = root.as_posix().rstrip("/") + "/"
        if path.lower().startswith(prefix.lower()):
            path = path[len(prefix):]
    return path


def parse_output(text: str, root: Path | None = None) -> dict:
    """Transforme une sortie de compilation en diagnostics structures."""
    diagnostics: list[dict] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        entry = _match_positional(line, root)
        if entry is None:
            entry = _match_global(line)
        if entry is not None:
            diagnostics.append(entry)

    errors = [d for d in diagnostics if d["severity"] == "error"]
    warnings = [d for d in diagnostics if d["severity"] == "warning"]

    by_file: dict[str, int] = {}
    for item in diagnostics:
        if item.get("file"):
            by_file[item["file"]] = by_file.get(item["file"], 0) + 1

    return {
        "diagnostics": diagnostics,
        "errors": len(errors),
        "warnings": len(warnings),
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])),
    }


def _match_positional(line: str, root: Path | None) -> dict | None:
    for pattern in _PATTERNS:
        match = pattern.match(line)
        if not match:
            continue
        groups = match.groupdict()
        severity = _SEVERITY.get(groups["sev"].lower())
        if severity is None:
            continue
        entry = {
            "severity": severity,
            "file": _clean_path(groups["file"], root),
            "line": int(groups["line"]),
            "message": groups["msg"].strip(),
        }
        if groups.get("col"):
            entry["col"] = int(groups["col"])
        if groups.get("code"):
            entry["code"] = groups["code"]
        return entry
    return None


def _match_global(line: str) -> dict | None:
    match = _GLOBAL.match(line)
    if not match:
        return None
    severity = _SEVERITY.get(match.group("sev").lower())
    if severity is None:
        return None
    return {
        "severity": severity,
        "stage": (match.group("who") or "build").lower(),
        "message": match.group("msg").strip(),
    }


# Options de compile.exe par configuration standard; les autres passent
# par -config=Nom.
_CONFIG_FLAGS = {"debug": "-debug", "release": "", "debug64": "-debug64", "release64": "-release64"}


def config_flag(name: str | None) -> str:
    if not name:
        return ""
    return _CONFIG_FLAGS.get(name.lower(), f"-config={name}")


def cvi_year(version: str | None) -> str | None:
    """Version du .prj (1900, 2000) vers le millesime de CVI (2019, 2020)."""
    if not version or not version.isdigit():
        return None
    major = int(version) // 100
    return str(2000 + major) if 8 <= major <= 99 else None


def compare_builds(previous: dict | None, current: dict) -> dict | None:
    """Diagnostics apparus et disparus depuis le build precedent."""
    if not previous:
        return None

    def keys(result: dict) -> set[tuple]:
        return {(d.get("file"), d.get("message"), d["severity"]) for d in result.get("diagnostics", [])}

    before, after = keys(previous), keys(current)
    return {
        "new": [{"file": f, "message": m, "severity": s} for f, m, s in sorted(after - before, key=str)][:30],
        "fixed": len(before - after),
        "errors_before": previous.get("errors"),
        "warnings_before": previous.get("warnings"),
    }


def run_build(
    project_file: Path,
    command: str,
    args_template: str,
    timeout: int,
    target: str | None = None,
    config: str | None = None,
) -> dict:
    """Lance le build et renvoie les diagnostics. Lecture seule sur les sources,
    mais la compilation produit ses propres fichiers de sortie."""
    flag = config_flag(config)
    template = args_template
    if flag and "{config}" not in template:
        template = template.replace("{project}", "{config} {project}")
    argv = [command] + [
        piece.replace("{project}", str(project_file)).replace("{target}", target or "")
        .replace("{config}", flag)
        for piece in shlex.split(template)
        if piece.replace("{target}", target or "").replace("{config}", flag).strip()
    ]

    try:
        completed = subprocess.run(
            argv,
            cwd=str(project_file.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "command_line": " ".join(argv),
            "status": "timeout",
            "timeout_seconds": timeout,
        }
    except OSError as exc:
        return {"command_line": " ".join(argv), "status": "echec", "reason": str(exc)}

    output = (completed.stdout or "") + "\n" + (completed.stderr or "")
    parsed = parse_output(output, project_file.parent)
    parsed.update(
        {
            "command_line": " ".join(argv),
            "exit_code": completed.returncode,
            "status": "ok" if completed.returncode == 0 and not parsed["errors"] else "erreurs",
            "output_tail": "\n".join(output.strip().splitlines()[-20:]),
        }
    )
    return parsed
