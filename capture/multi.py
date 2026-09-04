"""Dois pipelines de captura independentes, um consumidor só.

O Copilot precisa ouvir duas fontes ao mesmo tempo — o monitor do sistema
(a outra pessoa) e, opcionalmente, o microfone (o próprio usuário) — sem
misturar os dois num stream só. Misturar jogaria fora a informação de QUEM
falou, e é justamente essa informação que evita responder às próprias
perguntas do usuário (ver `modes/copilot/engine.py`).

Por isso `MultiSourcePipeline` não é um pipeline novo: são dois
`CapturePipeline` de verdade, cada um com seu VAD e segmentador próprios,
cujas saídas são etiquetadas com `speaker` e repassadas a um único conjunto
de ouvintes. O custo é uma segunda instância do Silero VAD (~2MB, ONNX) —
aceito de propósito, não é para ser otimizado.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from capture.pipeline import CapturePipeline, Listener
from capture.segmenter import SegmenterConfig
from capture.sources import AudioSource, resolve_internal_audio, resolve_microphone
from domain.audio import Utterance

logger = logging.getLogger(__name__)

SPEAKER_SYSTEM = "sistema"
SPEAKER_USER = "voce"

PipelineFactory = Callable[[], CapturePipeline]


class MultiSourcePipeline:
    """Gerencia o pipeline do sistema (sempre ligado) e o do microfone
    (opcional, ligado/desligado em runtime), etiquetando cada `Utterance`
    com quem falou antes de repassar aos ouvintes.
    """

    def __init__(
        self,
        system_source: AudioSource | None = None,
        config: SegmenterConfig | None = None,
        mic_config: SegmenterConfig | None = None,
        system_pipeline_factory: PipelineFactory | None = None,
        mic_pipeline_factory: PipelineFactory | None = None,
    ):
        # Fábricas de pipeline, não instâncias prontas: o pipeline do
        # microfone precisa poder ser recriado a cada `enable_microphone()`,
        # e os testes injetam dublês aqui sem tocar em PipeWire de verdade.
        self._system_factory: PipelineFactory = system_pipeline_factory or (
            lambda: CapturePipeline(source=system_source or resolve_internal_audio(), config=config)
        )
        self._mic_factory: PipelineFactory = mic_pipeline_factory or (
            lambda: CapturePipeline(source=resolve_microphone(), config=mic_config or config)
        )

        self.system: CapturePipeline = self._system_factory()
        self.microphone: CapturePipeline | None = None

        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        self._system_unsubscribe: Callable[[], None] | None = None
        self._mic_unsubscribe: Callable[[], None] | None = None

    # -- assinatura ----------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Registra um consumidor. Recebe utterances das duas fontes, já
        etiquetadas. Retorna a função que o remove."""
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emitir(self, speaker: str, fala: Utterance) -> None:
        fala.speaker = speaker
        with self._lock:
            ouvintes = list(self._listeners)
        for ouvinte in ouvintes:
            try:
                ouvinte(fala)
            except Exception:
                logger.exception("Consumidor falhou ao processar utterance (%s)", speaker)

    # -- ciclo de vida ---------------------------------------------------

    def start(self) -> None:
        """Liga o pipeline do sistema. O microfone é opt-in via
        `enable_microphone()`."""
        self.system.start()
        if self._system_unsubscribe is None:
            self._system_unsubscribe = self.system.subscribe(
                lambda u: self._emitir(SPEAKER_SYSTEM, u)
            )

    def enable_microphone(self) -> None:
        """Liga a captura do microfone em runtime, sem afetar o sistema.

        Chamar duas vezes seguidas não duplica nada: se o microfone já está
        de pé, é um no-op. Se a abertura falhar (sem permissão, sem device),
        o erro sobe para quem chamou — o pipeline do sistema continua rodando
        normalmente, porque nem foi tocado.
        """
        if self.microphone is not None:
            logger.debug("Microfone já está ligado; ignorando pedido duplicado")
            return

        mic = self._mic_factory()
        try:
            mic.start()
        except Exception:
            logger.exception("Falha ao abrir o microfone")
            raise

        self.microphone = mic
        self._mic_unsubscribe = mic.subscribe(lambda u: self._emitir(SPEAKER_USER, u))
        logger.info("Captura de microfone ligada")

    def disable_microphone(self) -> None:
        """Desliga a captura do microfone. O sistema não é afetado."""
        if self.microphone is None:
            return
        if self._mic_unsubscribe is not None:
            self._mic_unsubscribe()
            self._mic_unsubscribe = None
        self.microphone.stop()
        self.microphone = None
        logger.info("Captura de microfone desligada")

    def stop(self, timeout: float = 5.0) -> None:
        """Encerra os dois pipelines de verdade, com join nas threads."""
        self.disable_microphone()
        if self._system_unsubscribe is not None:
            self._system_unsubscribe()
            self._system_unsubscribe = None
        self.system.stop(timeout=timeout)
