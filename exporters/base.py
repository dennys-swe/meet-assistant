"""Interface comum a todo exportador de sessão.

Nenhum exportador é especial. O Obsidian é só mais um adaptador de saída —
se o usuário largar o vault amanhã, ele perde um formato, não o produto (o
acervo consultável vive no banco, ver `domain/session.py`). Por isso todo
exportador implementa exatamente esta interface, e nada além dela vaza para
quem chama.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from domain.session import Artifact, Segment, Session, Todo


class Exporter(ABC):
    """Converte uma sessão (e seus derivados) num formato de saída.

    `render` e `export` são separados de propósito: `render` só monta a
    string, sem tocar em disco — dá para testar o conteúdo sem I/O e, no
    futuro, para oferecer "copiar para a área de transferência" sem gravar
    nada. `export` é quem decide onde e como persistir.
    """

    name: str
    extension: str

    @abstractmethod
    def render(
        self,
        session: Session,
        segments: list[Segment],
        artifacts: list[Artifact],
        todos: list[Todo],
    ) -> str:
        """Gera o conteúdo textual da exportação."""
        raise NotImplementedError

    @abstractmethod
    def export(
        self,
        session: Session,
        segments: list[Segment],
        artifacts: list[Artifact],
        todos: list[Todo],
        dest: Path,
    ) -> Path:
        """Grava o resultado de `render` em disco e devolve o caminho final.

        `dest` é o destino pedido pelo chamador; o caminho devolvido pode
        diferir dele (ex.: sufixo para evitar sobrescrita).
        """
        raise NotImplementedError
