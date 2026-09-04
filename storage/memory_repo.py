"""Persistência em memória, para testes e para rodar o app sem tocar disco.

Mesma interface do `SQLiteRepository` — mesmos testes devem passar contra os
dois. Nada aqui sobrevive ao processo.
"""

from __future__ import annotations

import copy
import itertools
import threading

from domain.session import Artifact, Segment, Session, Todo
from storage.base import SessionRepository


class InMemoryRepository(SessionRepository):
    """Guarda tudo em dicionários. Thread-safe via um único `Lock`.

    Não há gargalo de I/O a proteger (é tudo RAM), então um lock simples em
    vez de estruturas por thread é suficiente e mais fácil de auditar.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[int, Session] = {}
        self._segments: dict[int, Segment] = {}
        self._artifacts: dict[int, Artifact] = {}
        self._todos: dict[int, Todo] = {}
        self._session_ids = itertools.count(1)
        self._segment_ids = itertools.count(1)
        self._artifact_ids = itertools.count(1)
        self._todo_ids = itertools.count(1)

    # -- sessões -----------------------------------------------------------

    def create_session(self, session: Session) -> Session:
        with self._lock:
            new = copy.deepcopy(session)
            new.id = next(self._session_ids)
            self._sessions[new.id] = new
            return copy.deepcopy(new)

    def update_session(self, session: Session) -> Session:
        if session.id is None:
            raise ValueError("update_session exige session.id")
        with self._lock:
            if session.id not in self._sessions:
                raise KeyError(f"sessão {session.id} não existe")
            self._sessions[session.id] = copy.deepcopy(session)
            return copy.deepcopy(session)

    def get_session(self, session_id: int) -> Session | None:
        with self._lock:
            s = self._sessions.get(session_id)
            return copy.deepcopy(s) if s else None

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[Session]:
        with self._lock:
            ordenadas = sorted(
                self._sessions.values(), key=lambda s: s.started_at, reverse=True
            )
            fatia = ordenadas[offset : offset + limit]
            return [copy.deepcopy(s) for s in fatia]

    def delete_session(self, session_id: int) -> bool:
        with self._lock:
            if session_id not in self._sessions:
                return False
            del self._sessions[session_id]
            # cascata manual, imitando ON DELETE CASCADE
            for tabela in (self._segments, self._artifacts, self._todos):
                for item_id in [
                    i for i, item in tabela.items() if item.session_id == session_id
                ]:
                    del tabela[item_id]
            return True

    # -- conteúdo ------------------------------------------------------

    def add_segments(self, session_id: int, segments: list[Segment]) -> list[Segment]:
        if not segments:
            return []
        with self._lock:
            resultado = []
            for s in segments:
                novo = copy.deepcopy(s)
                novo.id = next(self._segment_ids)
                novo.session_id = session_id
                self._segments[novo.id] = novo
                resultado.append(copy.deepcopy(novo))
            return resultado

    def list_segments(self, session_id: int) -> list[Segment]:
        with self._lock:
            itens = [s for s in self._segments.values() if s.session_id == session_id]
            itens.sort(key=lambda s: (s.started_at, s.id))
            return [copy.deepcopy(s) for s in itens]

    def add_artifacts(self, session_id: int, artifacts: list[Artifact]) -> list[Artifact]:
        if not artifacts:
            return []
        with self._lock:
            resultado = []
            for a in artifacts:
                novo = copy.deepcopy(a)
                novo.id = next(self._artifact_ids)
                novo.session_id = session_id
                self._artifacts[novo.id] = novo
                resultado.append(copy.deepcopy(novo))
            return resultado

    def list_artifacts(self, session_id: int) -> list[Artifact]:
        with self._lock:
            itens = [a for a in self._artifacts.values() if a.session_id == session_id]
            itens.sort(key=lambda a: (a.order, a.id))
            return [copy.deepcopy(a) for a in itens]

    def add_todos(self, session_id: int, todos: list[Todo]) -> list[Todo]:
        if not todos:
            return []
        with self._lock:
            resultado = []
            for t in todos:
                novo = copy.deepcopy(t)
                novo.id = next(self._todo_ids)
                novo.session_id = session_id
                self._todos[novo.id] = novo
                resultado.append(copy.deepcopy(novo))
            return resultado

    def list_todos(self, session_id: int | None = None, only_open: bool = False) -> list[Todo]:
        with self._lock:
            itens = list(self._todos.values())
            if session_id is not None:
                itens = [t for t in itens if t.session_id == session_id]
            if only_open:
                itens = [t for t in itens if not t.done]
            itens.sort(key=lambda t: (t.created_at, t.id))
            return [copy.deepcopy(t) for t in itens]

    def set_todo_done(self, todo_id: int, done: bool = True) -> bool:
        with self._lock:
            todo = self._todos.get(todo_id)
            if todo is None:
                return False
            todo.done = done
            return True

    # -- busca ---------------------------------------------------------

    def search(self, query: str, limit: int = 50) -> list[Segment]:
        query = query.strip().lower()
        if not query:
            return []
        with self._lock:
            achados = [s for s in self._segments.values() if query in s.text.lower()]
            achados.sort(key=lambda s: s.id, reverse=True)
            return [copy.deepcopy(s) for s in achados[:limit]]

    # -- ciclo de vida ---------------------------------------------------

    def close(self) -> None:
        pass
