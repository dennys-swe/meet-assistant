"""Persistência em SQLite, via stdlib. Sem ORM.

Guarda sessões, segmentos, artefatos e tarefas do Modo Aula (e, no futuro,
qualquer histórico do Copilot que valha reter). O `SessionRepository` é quem
define o contrato; aqui só a implementação concreta.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from domain.session import Artifact, ArtifactKind, Segment, Session, SessionStatus, Todo
from storage.base import SessionRepository

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "meet-assistant" / "sessions.db"

SCHEMA_VERSION = 1


class SQLiteRepository(SessionRepository):
    """Implementação em SQLite do acervo de sessões.

    Thread-safety: o app chama isto a partir de threads diferentes (captura,
    engine do LLM, UI). Em vez de `check_same_thread=False` com um `Lock`
    global — que serializaria toda leitura atrás de toda escrita — cada
    thread recebe sua própria conexão via `threading.local`. Conexões
    SQLite não são thread-safe entre si, mas são baratas de abrir e o
    arquivo já serializa escritores no nível do SO (WAL ajudaria ainda mais,
    mas não é necessário para o volume de uma sessão de aula/reunião).
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self._db_path = str(db_path)
        self._is_memory = self._db_path == ":memory:"
        self._local = threading.local()

        if not self._is_memory:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        # Para ":memory:" cada conexão nova seria um banco vazio e isolado —
        # aqui guardamos uma única conexão "mestre" e a reaproveitamos para
        # toda thread, aceitando o acesso concorrente controlado por um lock.
        self._memory_conn: sqlite3.Connection | None = None
        self._memory_lock = threading.Lock()

        # Detecta suporte a FTS5 em runtime: builds do SQLite variam por
        # distro (algumas compilam sem módulos de busca), então não dá para
        # assumir. Testamos criando e descartando uma tabela virtual.
        self._has_fts5 = self._detect_fts5()

        self._migrate(self._connect())

    # -- conexão ---------------------------------------------------------

    def _detect_fts5(self) -> bool:
        try:
            probe = sqlite3.connect(":memory:")
            try:
                probe.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
                return True
            except sqlite3.OperationalError:
                return False
            finally:
                probe.close()
        except sqlite3.Error:
            return False

    def _connect(self) -> sqlite3.Connection:
        if self._is_memory:
            with self._memory_lock:
                if self._memory_conn is None:
                    self._memory_conn = sqlite3.connect(
                        ":memory:", check_same_thread=False
                    )
                    self._memory_conn.row_factory = sqlite3.Row
                    self._memory_conn.execute("PRAGMA foreign_keys = ON")
                return self._memory_conn

        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def _lock(self):
        """Lock só é necessário no modo `:memory:`, onde a conexão é
        compartilhada entre threads. No modo arquivo, cada thread tem a sua."""
        return self._memory_lock if self._is_memory else _NULL_LOCK

    # -- schema / migração -------------------------------------------------

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Cria o schema se faltar e aplica migrações pendentes.

        Hoje só existe a versão 1 (criação inicial). Migrações futuras
        entram aqui como `if version < N: ...; version = N`.
        """
        with self._lock():
            version = conn.execute("PRAGMA user_version").fetchone()[0]

            if version < 1:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL DEFAULT '',
                        subject TEXT NOT NULL DEFAULT '',
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        status TEXT NOT NULL DEFAULT 'recording',
                        language TEXT NOT NULL DEFAULT 'pt',
                        audio_path TEXT,
                        meta TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE TABLE IF NOT EXISTS segments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        text TEXT NOT NULL DEFAULT '',
                        started_at REAL NOT NULL DEFAULT 0,
                        ended_at REAL NOT NULL DEFAULT 0,
                        speaker TEXT,
                        meta TEXT NOT NULL DEFAULT '{}'
                    );

                    CREATE INDEX IF NOT EXISTS idx_segments_session
                        ON segments(session_id, started_at);

                    CREATE TABLE IF NOT EXISTS artifacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        kind TEXT NOT NULL DEFAULT 'summary',
                        content TEXT NOT NULL DEFAULT '',
                        "order" INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_artifacts_session
                        ON artifacts(session_id);

                    CREATE TABLE IF NOT EXISTS todos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id INTEGER NOT NULL
                            REFERENCES sessions(id) ON DELETE CASCADE,
                        text TEXT NOT NULL DEFAULT '',
                        owner TEXT,
                        due TEXT,
                        done INTEGER NOT NULL DEFAULT 0,
                        source_time REAL,
                        created_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_todos_session
                        ON todos(session_id);
                    """
                )
                version = 1

            if self._has_fts5:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
                        text, content='segments', content_rowid='id'
                    );

                    CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
                        INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
                    END;

                    CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
                        INSERT INTO segments_fts(segments_fts, rowid, text)
                            VALUES ('delete', old.id, old.text);
                    END;

                    CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
                        INSERT INTO segments_fts(segments_fts, rowid, text)
                            VALUES ('delete', old.id, old.text);
                        INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
                    END;
                    """
                )

            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

    # -- helpers de (des)serialização ------------------------------------

    @staticmethod
    def _dt_to_str(dt: datetime | None) -> str | None:
        return dt.isoformat() if dt is not None else None

    @staticmethod
    def _str_to_dt(s: str | None) -> datetime | None:
        return datetime.fromisoformat(s) if s is not None else None

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            title=row["title"],
            subject=row["subject"],
            started_at=SQLiteRepository._str_to_dt(row["started_at"]),
            ended_at=SQLiteRepository._str_to_dt(row["ended_at"]),
            status=SessionStatus(row["status"]),
            language=row["language"],
            audio_path=row["audio_path"],
            meta=json.loads(row["meta"]),
        )

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> Segment:
        return Segment(
            id=row["id"],
            session_id=row["session_id"],
            text=row["text"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            speaker=row["speaker"],
            meta=json.loads(row["meta"]),
        )

    @staticmethod
    def _row_to_artifact(row: sqlite3.Row) -> Artifact:
        return Artifact(
            id=row["id"],
            session_id=row["session_id"],
            kind=ArtifactKind(row["kind"]),
            content=row["content"],
            order=row["order"],
            created_at=SQLiteRepository._str_to_dt(row["created_at"]),
        )

    @staticmethod
    def _row_to_todo(row: sqlite3.Row) -> Todo:
        return Todo(
            id=row["id"],
            session_id=row["session_id"],
            text=row["text"],
            owner=row["owner"],
            due=row["due"],
            done=bool(row["done"]),
            source_time=row["source_time"],
            created_at=SQLiteRepository._str_to_dt(row["created_at"]),
        )

    # -- sessões -----------------------------------------------------------

    def create_session(self, session: Session) -> Session:
        conn = self._connect()
        with self._lock():
            cur = conn.execute(
                """INSERT INTO sessions
                   (title, subject, started_at, ended_at, status, language, audio_path, meta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.title,
                    session.subject,
                    self._dt_to_str(session.started_at),
                    self._dt_to_str(session.ended_at),
                    session.status.value,
                    session.language,
                    session.audio_path,
                    json.dumps(session.meta, ensure_ascii=False),
                ),
            )
            conn.commit()
            session.id = cur.lastrowid
            return session

    def update_session(self, session: Session) -> Session:
        if session.id is None:
            raise ValueError("update_session exige session.id")
        conn = self._connect()
        with self._lock():
            conn.execute(
                """UPDATE sessions SET
                     title = ?, subject = ?, started_at = ?, ended_at = ?,
                     status = ?, language = ?, audio_path = ?, meta = ?
                   WHERE id = ?""",
                (
                    session.title,
                    session.subject,
                    self._dt_to_str(session.started_at),
                    self._dt_to_str(session.ended_at),
                    session.status.value,
                    session.language,
                    session.audio_path,
                    json.dumps(session.meta, ensure_ascii=False),
                    session.id,
                ),
            )
            conn.commit()
            return session

    def get_session(self, session_id: int) -> Session | None:
        conn = self._connect()
        with self._lock():
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[Session]:
        conn = self._connect()
        with self._lock():
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def delete_session(self, session_id: int) -> bool:
        conn = self._connect()
        with self._lock():
            cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
            return cur.rowcount > 0

    # -- conteúdo ------------------------------------------------------

    def add_segments(self, session_id: int, segments: list[Segment]) -> list[Segment]:
        if not segments:
            return []
        conn = self._connect()
        with self._lock():
            cur = conn.cursor()
            cur.executemany(
                """INSERT INTO segments
                   (session_id, text, started_at, ended_at, speaker, meta)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (
                        session_id,
                        s.text,
                        s.started_at,
                        s.ended_at,
                        s.speaker,
                        json.dumps(s.meta, ensure_ascii=False),
                    )
                    for s in segments
                ],
            )
            conn.commit()
            # `executemany` só garante o `lastrowid` do último item em builds
            # recentes do sqlite3; para não depender disso, relemos os ids
            # dos últimos N inseridos nesta sessão (ordem == ordem de inserção,
            # já que AUTOINCREMENT é estritamente crescente).
            n = len(segments)
            rows = conn.execute(
                "SELECT id FROM segments WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
            ids = [r["id"] for r in reversed(rows)]
            for seg, new_id in zip(segments, ids):
                seg.id = new_id
                seg.session_id = session_id
            return segments

    def list_segments(self, session_id: int) -> list[Segment]:
        conn = self._connect()
        with self._lock():
            rows = conn.execute(
                "SELECT * FROM segments WHERE session_id = ? ORDER BY started_at, id",
                (session_id,),
            ).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def add_artifacts(self, session_id: int, artifacts: list[Artifact]) -> list[Artifact]:
        if not artifacts:
            return []
        conn = self._connect()
        with self._lock():
            cur = conn.cursor()
            cur.executemany(
                """INSERT INTO artifacts
                   (session_id, kind, content, "order", created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        session_id,
                        a.kind.value,
                        a.content,
                        a.order,
                        self._dt_to_str(a.created_at),
                    )
                    for a in artifacts
                ],
            )
            conn.commit()
            n = len(artifacts)
            rows = conn.execute(
                "SELECT id FROM artifacts WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
            ids = [r["id"] for r in reversed(rows)]
            for art, new_id in zip(artifacts, ids):
                art.id = new_id
                art.session_id = session_id
            return artifacts

    def list_artifacts(self, session_id: int) -> list[Artifact]:
        conn = self._connect()
        with self._lock():
            rows = conn.execute(
                'SELECT * FROM artifacts WHERE session_id = ? ORDER BY "order", id',
                (session_id,),
            ).fetchall()
        return [self._row_to_artifact(r) for r in rows]

    def add_todos(self, session_id: int, todos: list[Todo]) -> list[Todo]:
        if not todos:
            return []
        conn = self._connect()
        with self._lock():
            cur = conn.cursor()
            cur.executemany(
                """INSERT INTO todos
                   (session_id, text, owner, due, done, source_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        session_id,
                        t.text,
                        t.owner,
                        t.due,
                        int(t.done),
                        t.source_time,
                        self._dt_to_str(t.created_at),
                    )
                    for t in todos
                ],
            )
            conn.commit()
            n = len(todos)
            rows = conn.execute(
                "SELECT id FROM todos WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, n),
            ).fetchall()
            ids = [r["id"] for r in reversed(rows)]
            for todo, new_id in zip(todos, ids):
                todo.id = new_id
                todo.session_id = session_id
            return todos

    def list_todos(self, session_id: int | None = None, only_open: bool = False) -> list[Todo]:
        conn = self._connect()
        clauses = []
        params: list = []
        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if only_open:
            clauses.append("done = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock():
            rows = conn.execute(
                f"SELECT * FROM todos {where} ORDER BY created_at, id", params
            ).fetchall()
        return [self._row_to_todo(r) for r in rows]

    def set_todo_done(self, todo_id: int, done: bool = True) -> bool:
        conn = self._connect()
        with self._lock():
            cur = conn.execute(
                "UPDATE todos SET done = ? WHERE id = ?", (int(done), todo_id)
            )
            conn.commit()
            return cur.rowcount > 0

    # -- busca ---------------------------------------------------------

    def search(self, query: str, limit: int = 50) -> list[Segment]:
        query = query.strip()
        if not query:
            return []
        conn = self._connect()
        with self._lock():
            if self._has_fts5:
                rows = conn.execute(
                    """SELECT s.* FROM segments s
                       JOIN segments_fts f ON f.rowid = s.id
                       WHERE segments_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (_fts_escape(query), limit),
                ).fetchall()
            else:
                # Fallback sem FTS5: builds mínimas do SQLite (algumas
                # distros Linux compilam sem os módulos de busca). `LIKE` é
                # mais lento e sem ranking, mas não exige extensão nenhuma.
                rows = conn.execute(
                    "SELECT * FROM segments WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{query}%", limit),
                ).fetchall()
        return [self._row_to_segment(r) for r in rows]

    # -- ciclo de vida ---------------------------------------------------

    def close(self) -> None:
        if self._is_memory:
            with self._memory_lock:
                if self._memory_conn is not None:
                    self._memory_conn.close()
                    self._memory_conn = None
        else:
            conn = getattr(self._local, "conn", None)
            if conn is not None:
                conn.close()
                self._local.conn = None


def _fts_escape(query: str) -> str:
    """Escapa a consulta para MATCH do FTS5 tratando-a como frase literal.

    Sem isso, caracteres como `"`, `-` ou `*` na fala transcrita (comuns em
    português coloquial) quebrariam a sintaxe de consulta do FTS5.
    """
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


class _NullLock:
    """Lock que não faz nada — usado quando cada thread já tem sua própria
    conexão (modo arquivo), então não há seção crítica a proteger."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_NULL_LOCK = _NullLock()
