"""Tipos centrais do domínio de áudio.

Sem I/O, sem dependência de biblioteca externa. É o vocabulário que os dois
modos (Aula e Copilot) compartilham.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Formato canônico do pipeline inteiro. O Silero VAD exige 16 kHz mono, e a
# maioria dos motores de transcrição trabalha bem nesse formato, então
# convertemos uma única vez na captura e ninguém mais precisa pensar nisso.
SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # int16
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH


@dataclass(frozen=True)
class AudioSource:
    """Uma fonte de áudio capturável do PipeWire."""

    node_name: str
    description: str
    is_monitor: bool

    @property
    def kind(self) -> str:
        return "áudio interno" if self.is_monitor else "microfone"

    def __str__(self) -> str:
        return f"{self.description} ({self.kind})"


@dataclass
class Utterance:
    """Um turno de fala completo, delimitado por silêncio.

    Esta é a unidade que sai da captura e alimenta os dois modos. Ela existe
    justamente porque um bloco de tamanho fixo (o erro da v1) corta palavras
    no meio: aqui o corte é decidido pelo VAD, então o áudio começa e termina
    em fronteiras de fala reais.

    `text` nasce vazio — a captura não transcreve. Quem preenche é a camada
    de ASR, mais adiante no pipeline.
    """

    pcm: bytes
    started_at: float  # segundos desde o início da sessão
    ended_at: float
    text: str = ""
    speaker: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.ended_at - self.started_at

    def __str__(self) -> str:
        corpo = self.text or f"<{len(self.pcm) / BYTES_PER_SECOND:.1f}s de áudio, sem transcrição>"
        return f"[{self.started_at:6.1f}s → {self.ended_at:6.1f}s] {corpo}"
