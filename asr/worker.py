"""Worker de transcrição desacoplado da captura.

Sem isto, transcrever dentro do callback do pipeline seguraria o loop de
captura por segundos a cada turno. O `pw-record` continuaria bufferizando no
pipe, então nada se perderia de imediato, mas a latência acumularia até o
Copilot ficar respondendo a perguntas de um minuto atrás.

Uma thread, uma fila. A ordem dos turnos é preservada, que é o que importa
para remontar a transcrição da aula.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable

from asr.base import Transcriber
from domain.audio import Utterance

logger = logging.getLogger(__name__)

OnTranscribed = Callable[[Utterance], None]


class TranscriptionWorker:
    """Consome `Utterance` cruas e emite as mesmas com `text` preenchido."""

    def __init__(
        self,
        transcriber: Transcriber,
        on_transcribed: OnTranscribed,
        max_queue: int = 64,
        language: str | None = None,
    ):
        self.transcriber = transcriber
        self.on_transcribed = on_transcribed
        self.language = language
        self._fila: queue.Queue[Utterance | None] = queue.Queue(maxsize=max_queue)
        self._thread: threading.Thread | None = None
        self._descartadas = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="TranscriptionWorker", daemon=True
        )
        self._thread.start()
        logger.info("Worker de transcrição iniciado")

    def submit(self, utterance: Utterance) -> None:
        """Enfileira um turno. Não bloqueia a captura."""
        try:
            self._fila.put_nowait(utterance)
        except queue.Full:
            # Melhor perder um turno e continuar ao vivo do que travar a
            # captura. Se isto aparecer no log, o modelo está lento demais
            # para o modo em uso.
            self._descartadas += 1
            logger.warning(
                "Fila de transcrição cheia; turno descartado (total: %d)",
                self._descartadas,
            )

    @property
    def pending(self) -> int:
        return self._fila.qsize()

    @property
    def dropped(self) -> int:
        return self._descartadas

    def _loop(self) -> None:
        while True:
            item = self._fila.get()
            if item is None:
                break
            try:
                self.on_transcribed(self.transcriber.transcribe(item, self.language))
            except Exception:
                logger.exception("Falha ao transcrever turno")
            finally:
                self._fila.task_done()

    def stop(self, drain: bool = True, timeout: float = 60.0) -> None:
        """Encerra o worker.

        `drain=True` espera a fila terminar — é o que o Modo Aula quer ao
        parar a gravação, para não perder o final da aula. O Copilot pode
        passar `drain=False` e descartar o que estiver pendente.
        """
        if self._thread is None:
            return
        if drain:
            self._fila.join()
        self._fila.put(None)
        self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Worker de transcrição parado")
