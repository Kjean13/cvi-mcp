# ==============================================================================
#  cvi_mcp/gitinfo.py
#  Role     Historique git en lecture seule, optionnel
#  Etat     stable
#  Version  0.3.3
# ==============================================================================

# ============================ Standard ========================================
import shutil
import subprocess
from pathlib import Path

# ============================ Interne =========================================
from .queries import QueryEngine

_FORMAT = "%h%x1f%ad%x1f%an%x1f%s"

# Fichiers listes par commit: un premier commit peut en contenir des centaines.
MAX_FILES_PER_COMMIT = 15


def _git(args: list[str], cwd: Path, timeout: int = 20) -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=timeout, check=False, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _commits(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            out.append({"commit": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
    return out


def history(engine: QueryEngine, target: str | None, limit: int = 10) -> dict:
    root = engine.root or Path(".")
    top = _git(["rev-parse", "--show-toplevel"], root)
    if top is None:
        return {"available": False,
                "reason": "pas de depot git a la racine du projet, ou git absent du PATH"}
    repo = Path(top.strip())
    limit = max(1, min(limit, 50))
    if not target:
        log = _git(["log", f"-{limit}", "--date=short", f"--format={_FORMAT}", "--name-only"], repo)
        commits, current = [], None
        for line in (log or "").splitlines():
            if "\x1f" in line:
                current = _commits(line)[0]
                current["files"] = []
                commits.append(current)
            elif line.strip() and current is not None:
                current["files"].append(line.strip())
        for commit in commits:
            if len(commit["files"]) > MAX_FILES_PER_COMMIT:
                commit["files_total"] = len(commit["files"])
                commit["files"] = commit["files"][:MAX_FILES_PER_COMMIT]
        return {"available": True, "repository": str(repo), "commits": commits}

    row = engine.db.execute(
        "SELECT s.line_start, s.line_end, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.name = ? AND s.kind = 'function' ORDER BY s.active DESC LIMIT 1", (target,)).fetchone()
    if row is not None:
        rel = Path(row["path"]).resolve().relative_to(repo).as_posix()
        log = _git(["log", f"-{limit}", "--date=short", f"--format={_FORMAT}", "--no-patch",
                    f"-L{row['line_start']},{row['line_end']}:{rel}"], repo, timeout=60)
        return {"available": True, "function": target, "file": rel,
                "commits": _commits(log or "")}
    ids = engine.match_files(target)
    if not ids:
        return {"available": True, "reason": f"ni fonction ni fichier ne correspond a '{target}'"}
    path = engine.db.execute("SELECT path FROM files WHERE id = ?", (ids[0],)).fetchone()[0]
    rel = Path(path).resolve().relative_to(repo).as_posix()
    log = _git(["log", f"-{limit}", "--date=short", f"--format={_FORMAT}", "--follow", "--", rel], repo)
    return {"available": True, "file": rel, "commits": _commits(log or "")}
