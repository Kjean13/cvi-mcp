# ==============================================================================
#  cvi_mcp/schema.py
#  Role     Schema SQLite de l'index et migration par numero de version
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import sqlite3

# L'index est entierement regenerable depuis les sources: en cas de changement
# de schema on reconstruit au lieu de migrer.
SCHEMA_VERSION = 5

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- projet -----
-- size et mtime evitent de relire et hacher un fichier inchange a chaque
-- appel d'outil. Le hash reste l'arbitre quand l'un des deux a bouge.
CREATE TABLE IF NOT EXISTS files (
    id    INTEGER PRIMARY KEY,
    path  TEXT UNIQUE NOT NULL,
    hash  TEXT NOT NULL,
    lines INTEGER NOT NULL,
    size  INTEGER DEFAULT 0,
    mtime REAL DEFAULT 0,
    -- project: ecrit par l'equipe. generated: .h genere par CVI pour un .uir.
    -- library: en-tete dont aucun prototype n'est defini dans le projet.
    origin TEXT DEFAULT 'project',
    encoding TEXT,
    -- 1: compile (source du build ou en-tete inclus par une source du build).
    in_build INTEGER DEFAULT 1,
    reached_by TEXT,
    -- 1: liste dans le .prj (ou trouve par balayage); 0: atteint par #include.
    listed INTEGER DEFAULT 1,
    guard TEXT,
    -- Empreinte de l'etat du preprocesseur: si elle change, on reindexe.
    pp_sig TEXT,
    parse_errors INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    line_start  INTEGER NOT NULL,
    line_end    INTEGER NOT NULL,
    signature   TEXT,
    params      TEXT,
    is_static   INTEGER DEFAULT 0,
    is_callback INTEGER DEFAULT 0,
    doc_comment TEXT,
    body_hash   TEXT,
    value       TEXT,
    -- Fonctions seulement: complexite cyclomatique approchee et imbrication.
    complexity  INTEGER,
    depth       INTEGER,
    -- 0: dans un bloc #if exclu de la configuration courante.
    active      INTEGER DEFAULT 1,
    cognitive   INTEGER,
    locals      INTEGER,
    frame_bytes INTEGER,
    shape_hash  TEXT
);

CREATE TABLE IF NOT EXISTS refs (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files(id),
    line      INTEGER NOT NULL,
    col       INTEGER NOT NULL,
    name      TEXT NOT NULL,
    -- call, read, write, ref (nom passe sans etre appele), return (sortie
    -- de fonction, utile a l'analyse des verrous), cond (expression #if),
    -- macro (valeur d'une autre macro, enclosing = cette macro), dim
    -- (taille de tableau), decl (initialiseur hors fonction).
    kind      TEXT NOT NULL,
    enclosing TEXT,
    active    INTEGER DEFAULT 1
);

-- Arguments litteraux de chaque appel, indispensables pour ui_map.
CREATE TABLE IF NOT EXISTS call_args (
    ref_id INTEGER NOT NULL REFERENCES refs(id),
    pos    INTEGER NOT NULL,
    text   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS includes (
    id       INTEGER PRIMARY KEY,
    file_id  INTEGER NOT NULL REFERENCES files(id),
    included TEXT NOT NULL,
    system   INTEGER DEFAULT 0,
    line     INTEGER DEFAULT 0,
    resolved TEXT,
    how      TEXT,
    active   INTEGER DEFAULT 1
);

-- Branches #if / #elif / #else evaluees par le preprocesseur simule.
CREATE TABLE IF NOT EXISTS pp_blocks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id),
    line_start INTEGER NOT NULL,
    line_end   INTEGER NOT NULL,
    chain      INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    expr       TEXT,
    state      TEXT NOT NULL,
    confidence TEXT,
    reason     TEXT,
    names      TEXT,
    guard      INTEGER DEFAULT 0,
    varies     INTEGER DEFAULT 0
);

-- ------------------------------------------------------------------- SDK -----
CREATE TABLE IF NOT EXISTS sdk_files (
    id   INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    hash TEXT NOT NULL,
    kind TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sdk_symbols (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES sdk_files(id),
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    line      INTEGER DEFAULT 0,
    signature TEXT,
    params    TEXT,
    doc       TEXT,
    confirmed INTEGER DEFAULT 1
);

-- -------------------------------------------------------------- snapshots ----
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY,
    label        TEXT UNIQUE NOT NULL,
    created_at   REAL NOT NULL,
    root         TEXT NOT NULL,
    file_count   INTEGER NOT NULL,
    symbol_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_symbols (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    file        TEXT NOT NULL,
    signature   TEXT,
    body_hash   TEXT,
    value       TEXT,
    active      INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS snapshot_edges (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    caller      TEXT NOT NULL,
    callee      TEXT NOT NULL
);

-- ----------------------------------------------------------------- index -----
CREATE INDEX IF NOT EXISTS idx_symbols_name    ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind    ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_refs_name       ON refs(name);
CREATE INDEX IF NOT EXISTS idx_refs_kind       ON refs(kind);
CREATE INDEX IF NOT EXISTS idx_refs_enclosing  ON refs(enclosing);
CREATE INDEX IF NOT EXISTS idx_symbols_file    ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_call_args_ref   ON call_args(ref_id);
CREATE INDEX IF NOT EXISTS idx_blocks_file     ON pp_blocks(file_id);
CREATE INDEX IF NOT EXISTS idx_sdk_symbols_nam ON sdk_symbols(name);
CREATE INDEX IF NOT EXISTS idx_snap_sym        ON snapshot_symbols(snapshot_id, name);
CREATE INDEX IF NOT EXISTS idx_snap_edge       ON snapshot_edges(snapshot_id);
"""

# Tables du projet, remises a zero par un reindex force.
PROJECT_TABLES = ("call_args", "refs", "symbols", "includes", "pp_blocks", "files")

# Tout l'index, detruit lors d'un changement de version de schema.
ALL_TABLES = PROJECT_TABLES + (
    "snapshot_edges",
    "snapshot_symbols",
    "snapshots",
    "sdk_symbols",
    "sdk_files",
    "meta",
)


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Cree ou reconstruit le schema. Renvoie True si l'index a ete reconstruit."""
    rebuilt = False
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row[0]) if row else 0
    except sqlite3.OperationalError:
        current = 0

    if current and current != SCHEMA_VERSION:
        for table in ALL_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        rebuilt = True

    conn.executescript(DDL)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return rebuilt
