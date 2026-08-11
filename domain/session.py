"""Tipos de domínio da sessão gravada.

Contrato compartilhado entre `storage/`, `modes/session/` e `exporters/`.
Nenhum I/O aqui: só a linguagem que as três camadas falam entre si.

A razão de existir uma `Session` é que o Obsidian era, na v1, a arquitetura
inteira — e um arquivo `.md` solto não responde "quais tarefas ficaram
pendentes das últimas três reuniões?". O acervo passa a ser consultável, e
exportar para o Obsidian vira um adaptador de saída, não o produto.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class SessionStatus(str, Enum):
    RECORDING = "recording"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ArtifactKind(str, Enum):
    """O que o LLM produziu a partir da transcrição."""

    SUMMARY = "summary"
    INSIGHT = "insight"
    TOPIC = "topic"


@dataclass
class Session:
    """Uma aula ou reunião gravada, do início ao fim."""

    id: int | None = None
    title: str = ""
    subject: str = ""  # matéria, projeto ou cliente
    started_at: datetime = field(default_factory=datetime.now)
    ended_at: datetime | None = None
    status: SessionStatus = SessionStatus.RECORDING
    language: str = "pt"
    audio_path: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return (self.ended_at - self.started_at).total_seconds()


@dataclass
class Segment:
    """Um trecho transcrito, ancorado no tempo da sessão.

    É a `Utterance` de `domain/audio.py` depois de persistida — sem o PCM,
    que não vale guardar no banco.
    """

    id: int | None = None
    session_id: int = 0
    text: str = ""
    started_at: float = 0.0  # segundos desde o início da sessão
    ended_at: float = 0.0
    speaker: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Artifact:
    """Resumo, insight ou tópico gerado pelo LLM."""

    id: int | None = None
    session_id: int = 0
    kind: ArtifactKind = ArtifactKind.SUMMARY
    content: str = ""
    order: int = 0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class Todo:
    """Tarefa extraída da conversa.

    `source_time` aponta para o minuto do áudio em que apareceu — é o que
    permite voltar e conferir o contexto em vez de confiar na extração.
    """

    id: int | None = None
    session_id: int = 0
    text: str = ""
    owner: str | None = None
    due: str | None = None  # como foi dito ("até sexta"), sem normalizar
    done: bool = False
    source_time: float | None = None
    created_at: datetime = field(default_factory=datetime.now)
