"""Pipeline de captura: fonte → PCM → VAD → Utterances.

É o único ponto que os dois modos (Aula e Copilot) consomem. Nenhum dos dois
conhece PipeWire, ONNX ou blocos de PCM — ambos veem apenas um fluxo de turnos
de fala. Ligar ou desligar o Copilot não toca no motor de captura, que era a
origem dos bugs de estado da v1.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator

from capture.recorder import PipeWireRecorder
from capture.segmenter import Segmenter, SegmenterConfig
from capture.sources import AudioSource, resolve_internal_audio
from domain.audio import Utterance

logger = logging.getLogger(__name__)

Listener = Callable[[Utterance], None]


class CapturePipeline:
    """Captura contínua que emite `Utterance` para quem estiver ouvindo."""

    def __init__(
        self,
        source: AudioSource | None = None,
        config: SegmenterConfig | None = None,
    ):
        self.source = source or resolve_internal_audio()
        self.recorder = PipeWireRecorder(self.source)
        self.segmenter = Segmenter(config)
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._rodando = False

    # -- assinatura ----------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Registra um consumidor. Retorna a função que o remove."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emitir(self, fala: Utterance) -> None:
        with self._lock:
            ouvintes = list(self._listeners)
        for ouvinte in ouvintes:
            # Um consumidor que quebra não pode derrubar a captura nem os outros.
            try:
                ouvinte(fala)
            except Exception:
                logger.exception("Consumidor falhou ao processar utterance")

    # -- modo síncrono (CLI, testes) -----------------------------------

    def utterances(self) -> Iterator[Utterance]:
        """Itera turnos de fala no thread atual, até `stop()`."""
        self._rodando = True
        try:
            for bloco in self.recorder.stream():
                for fala in self.segmenter.feed(bloco):
                    self._emitir(fala)
                    yield fala
                if not self._rodando:
                    break
        finally:
            for fala in self.segmenter.flush():
                self._emitir(fala)
                yield fala
            self._rodando = False

    # -- modo background (GUI) -----------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Pipeline já está rodando")
            return

        def _loop() -> None:
            try:
                for _ in self.utterances():
                    pass
            except Exception:
                logger.exception("Pipeline de captura morreu")

        self._thread = threading.Thread(
            target=_loop, name="CapturePipeline", daemon=True
        )
        self._thread.start()
        logger.info("Pipeline de captura iniciado")

    def stop(self, timeout: float = 5.0) -> None:
        """Para a captura e espera a thread encerrar de verdade."""
        self._rodando = False
        self.recorder.stop()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Thread de captura não encerrou em %.1fs", timeout)
            self._thread = None
        logger.info("Pipeline de captura parado")
