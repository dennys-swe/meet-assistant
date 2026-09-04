"""Interface de transcrição.

Existe para que os dois modos nunca conheçam o motor por trás. O Modo Aula
quer qualidade e não liga para latência; o Copilot quer o inverso. Ambos
pedem `transcribe(utterance)` e recebem a mesma `Utterance` com `text`
preenchido — quem escolhe o motor é a configuração, não o código do modo.

Também é o que mantém a porta aberta para trocar Whisper local por uma API
depois, sem tocar em nada acima desta camada.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from domain.audio import Utterance


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    language: str | None = None
    confidence: float | None = None
    duration_ms: int = 0


class Transcriber(ABC):
    """Contrato mínimo de um motor de transcrição."""

    @abstractmethod
    def transcribe_pcm(self, pcm: bytes, language: str | None = None) -> TranscriptionResult:
        """Transcreve PCM 16 kHz mono int16."""

    def transcribe(self, utterance: Utterance, language: str | None = None) -> Utterance:
        """Preenche `text` de uma `Utterance` in-place e a devolve."""
        resultado = self.transcribe_pcm(utterance.pcm, language)
        utterance.text = resultado.text
        utterance.meta["asr"] = {
            "language": resultado.language,
            "confidence": resultado.confidence,
            "duration_ms": resultado.duration_ms,
        }
        return utterance

    def warmup(self) -> None:
        """Carrega o modelo antecipadamente. Opcional, mas evita que o
        primeiro turno da sessão pague o custo de carregamento."""
