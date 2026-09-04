"""Interface de persistência.

Contrato entre quem grava (`modes/session/`) e quem armazena (`storage/`).
Existe para que o Modo Aula não saiba que por baixo há SQLite — e para que os
testes rodem contra um repositório em memória, sem tocar em disco.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from domain.session import Artifact, Segment, Session, Todo


class SessionRepository(ABC):
    """Operações mínimas sobre o acervo de sessões."""

    # -- sessões -------------------------------------------------------

    @abstractmethod
    def create_session(self, session: Session) -> Session:
        """Persiste e devolve a sessão com `id` preenchido."""

    @abstractmethod
    def update_session(self, session: Session) -> Session: ...

    @abstractmethod
    def get_session(self, session_id: int) -> Session | None: ...

    @abstractmethod
    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[Session]:
        """Mais recentes primeiro."""

    @abstractmethod
    def delete_session(self, session_id: int) -> bool:
        """Remove a sessão e tudo que pende dela."""

    # -- conteúdo ------------------------------------------------------

    @abstractmethod
    def add_segments(self, session_id: int, segments: list[Segment]) -> list[Segment]:
        """Em lote: uma aula de 1h gera centenas de segmentos."""

    @abstractmethod
    def list_segments(self, session_id: int) -> list[Segment]:
        """Em ordem cronológica."""

    @abstractmethod
    def add_artifacts(self, session_id: int, artifacts: list[Artifact]) -> list[Artifact]: ...

    @abstractmethod
    def list_artifacts(self, session_id: int) -> list[Artifact]: ...

    @abstractmethod
    def add_todos(self, session_id: int, todos: list[Todo]) -> list[Todo]: ...

    @abstractmethod
    def list_todos(self, session_id: int | None = None, only_open: bool = False) -> list[Todo]:
        """`session_id=None` atravessa todas as sessões.

        É a consulta que justifica o banco: "o que ficou pendente das últimas
        reuniões?" não se responde com uma pasta de arquivos markdown.
        """

    @abstractmethod
    def set_todo_done(self, todo_id: int, done: bool = True) -> bool: ...

    # -- busca ---------------------------------------------------------

    @abstractmethod
    def search(self, query: str, limit: int = 50) -> list[Segment]:
        """Busca textual na transcrição, em todas as sessões."""

    @abstractmethod
    def close(self) -> None: ...
