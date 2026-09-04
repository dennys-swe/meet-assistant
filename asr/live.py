"""Transcrição ao vivo: VAD decide os limites, o streaming preenche o texto.

Junta as duas peças que o Copilot precisa:

  - `Segmenter` (Silero VAD) diz *quando* uma pergunta terminou;
  - `StreamingTranscriber` (LocalAgreement-2) já tem o texto pronto nessa hora.

Tudo o que é caro roda numa thread própria. A captura só empurra bytes numa
fila e volta imediatamente para o `pw-record` — se a transcrição atrasar, ela
atrasa sozinha, sem furar o áudio.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from asr.streaming import StreamingConfig, StreamingTranscriber
from capture.segmenter import Segmenter, SegmenterConfig, State

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Partial:
    """Texto parcial de um turno em andamento."""

    committed: str
    pending: str

    @property
    def text(self) -> str:
        return f"{self.committed} {self.pending}".strip()


@dataclass(frozen=True)
class Turn:
    """Turno de fala encerrado — a unidade que o Copilot vai analisar."""

    text: str
    started_at: float
    ended_at: float
    latency_ms: int  # do fim da fala até o texto ficar pronto

    @property
    def duration(self) -> float:
        return self.ended_at - self.started_at


OnPartial = Callable[[Partial], None]
OnTurn = Callable[[Turn], None]

_FIM_DE_TURNO = object()
_SEM_MAIS_AUDIO = object()


class LiveTranscriber:
    """Consome PCM contínuo e emite parciais + turnos completos."""

    def __init__(
        self,
        on_partial: OnPartial | None = None,
        on_turn: OnTurn | None = None,
        streaming: StreamingConfig | None = None,
        segmenter: SegmenterConfig | None = None,
    ):
        # end_silence_ms curto: no Copilot vale mais fechar cedo e responder a
        # tempo do que esperar a frase perfeita.
        self.segmenter = Segmenter(segmenter or SegmenterConfig(end_silence_ms=500))
        self.transcriber = StreamingTranscriber(streaming)
        self.on_partial = on_partial
        self.on_turn = on_turn

        self._fila: queue.Queue = queue.Queue(maxsize=512)
        self._thread: threading.Thread | None = None

    def warmup(self) -> None:
        self.transcriber.warmup()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="LiveTranscriber", daemon=True)
        self._thread.start()

    def feed(self, pcm: bytes) -> None:
        """Chamado pela thread de captura. Não bloqueia."""
        # O segmentador é barato (uma inferência ONNX por janela de 32ms), então
        # roda aqui mesmo: precisamos do sinal de fim de turno sem atravessar a
        # fila de transcrição, que pode estar atrasada.
        falando_antes = self.segmenter.state is not State.SILENCE
        turnos = list(self.segmenter.feed(pcm))
        falando_agora = self.segmenter.state is not State.SILENCE

        # Fora da fala não enfileiramos nada. Sem isto o buffer do Whisper
        # acumula silêncio, cada passagem fica mais cara que a anterior e a
        # latência cresce sem parar — foi o que a primeira medição mostrou.
        if not falando_agora and not turnos:
            return

        try:
            self._fila.put_nowait(pcm)
        except queue.Full:
            logger.warning("Fila de áudio cheia; bloco descartado")

        if falando_agora and not falando_antes:
            logger.debug("Início de turno em %.1fs", self.segmenter.elapsed_seconds)

        for utterance in turnos:
            # Os tempos viajam junto com o marcador: ler de estado
            # compartilhado dava corrida com o início do turno seguinte.
            marcador = (_FIM_DE_TURNO, utterance.started_at, utterance.ended_at, time.perf_counter())
            try:
                self._fila.put_nowait(marcador)
            except queue.Full:
                logger.warning("Fila cheia no fim de turno")

    def _loop(self) -> None:
        """Um item por vez, mas engolindo todo o PCM acumulado de uma vez.

        Enquanto uma passagem do Whisper roda (1-2s), dezenas de blocos de 32ms
        se acumulam. Processar um por um faria a fila crescer para sempre.
        Inserir tudo e rodar **uma** passagem é a latência auto-adaptativa
        descrita no paper: quanto mais atrasados, maior o pedaço que engolimos.
        """
        while True:
            item = self._fila.get()

            try:
                # Absorve todo o áudio contíguo disponível antes de decidir.
                while isinstance(item, bytes):
                    self.transcriber.insert_audio(item)
                    try:
                        item = self._fila.get_nowait()
                    except queue.Empty:
                        item = _SEM_MAIS_AUDIO
                        break

                if item is _SEM_MAIS_AUDIO:
                    if self.transcriber.ready and self.segmenter.state is not State.SILENCE:
                        self.transcriber.process()
                        if self.on_partial:
                            estado = self.transcriber.state
                            self.on_partial(Partial(estado.committed_text, estado.pending_text))
                    continue

                if item is None:
                    return

                if isinstance(item, tuple) and item[0] is _FIM_DE_TURNO:
                    _, inicio, fim, marcado_em = item
                    texto = self.transcriber.finalize()
                    if texto and self.on_turn:
                        self.on_turn(
                            Turn(
                                text=texto,
                                started_at=inicio,
                                ended_at=fim,
                                latency_ms=int((time.perf_counter() - marcado_em) * 1000),
                            )
                        )

            except Exception:
                logger.exception("Falha no ciclo de transcrição ao vivo")

    def stop(self, timeout: float = 30.0) -> None:
        if self._thread is None:
            return
        self._fila.put(None)
        self._thread.join(timeout=timeout)
        self._thread = None
