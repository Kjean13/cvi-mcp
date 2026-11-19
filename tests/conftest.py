# ==============================================================================
#  tests/conftest.py
#  Role     Environnement isole par test et indexation des fixtures
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
from pathlib import Path

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp.indexer import Indexer
from cvi_mcp.prj import collect_sources

FIXTURES = Path(__file__).parent / "fixtures"
SDK_FIXTURES = FIXTURES / "sdk"

_MANAGED = (
    "CVI_MCP_PROJECT",
    "CVI_MCP_DB",
    "CVI_MCP_AUDIT_LOG",
    "CVI_MCP_ALLOW_BODY",
    "CVI_MCP_MAX_SNIPPET_LINES",
    "CVI_MCP_EXCLUDE",
    "CVI_MCP_SDK_PATHS",
    "CVI_MCP_BUILD_CMD",
    "CVI_MCP_BUILD_ARGS",
    "CVI_MCP_BUILD_CONFIG",
    "CVI_MCP_DEFINES",
    "CVI_MCP_SETTINGS",
    "CVI_MCP_ALLOW_PEEK",
    "CVI_MCP_MAX_CHARS",
    "CVI_MCP_FORMAT",
    "CVI_MCP_AUDIT_PATHS",
    "CVI_MCP_INDEX_UNLISTED",
    "CVI_MCP_PROJECTS",
    "CVI_MCP_SDK_DB",
)


@pytest.fixture(autouse=True)
def _fresh_server_state():
    """Projet actif et caches du serveur remis a zero entre deux tests."""
    from cvi_mcp import server
    from cvi_mcp.config import set_active_project

    set_active_project(None)
    server._LAST.clear()
    server._LAST["sdk_attempted"] = True
    yield
    set_active_project(None)
    server._LAST.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Projet pointe sur les fixtures, base et journal dans un dossier jetable."""
    for key in _MANAGED:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CVI_MCP_PROJECT", str(FIXTURES))
    monkeypatch.setenv("CVI_MCP_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("CVI_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    # Le dossier sdk sert a l'index SDK, il ne doit pas polluer l'index projet.
    monkeypatch.setenv("CVI_MCP_EXCLUDE", "sdk/*")
    return {
        "root": FIXTURES,
        "db": tmp_path / "index.db",
        "audit": tmp_path / "audit.jsonl",
        "tmp": tmp_path,
    }


@pytest.fixture
def indexed(env):
    """Index projet construit sur les fixtures."""
    sources = collect_sources(env["root"], lambda p: "/sdk/" in Path(p).as_posix())
    indexer = Indexer(env["db"])
    try:
        report = indexer.index_files(sources)
    finally:
        indexer.close()
    return {**env, "sources": sources, "report": report}
