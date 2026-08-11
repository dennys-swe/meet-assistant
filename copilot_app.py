#!/usr/bin/env python3
"""Meet Assistant — Copilot ao vivo.

    python copilot_app.py

Liga o pipeline inteiro: áudio do sistema → VAD → Whisper local → detecção de
pergunta → LLM do usuário → janela flutuante.

A transcrição roda local (nada sai da máquina). Só a geração da resposta usa a
chave de API que o usuário configurar na primeira execução.
"""

from __future__ import annotations

import logging
import sys
import threading

from asr.whisper_local import WhisperLocal
from asr.worker import TranscriptionWorker
from capture.multi import MultiSourcePipeline
from capture.segmenter import SegmenterConfig
from capture.sources import AudioSourceError, resolve_internal_audio
from config import settings as cfg
from domain.audio import Utterance
from llm.openai_compat import OpenAICompatClient, from_settings
from modes.copilot.engine import CopilotEngine, EngineCallbacks, EngineConfig

logger = logging.getLogger(__name__)


class CopilotApp:
    """Cola entre captura, motor e janela. Sem regra de negócio própria."""

    def __init__(self, settings: cfg.Settings):
        self.settings = settings
        self.pipeline: MultiSourcePipeline | None = None
        self.worker: TranscriptionWorker | None = None
        self.engine: CopilotEngine | None = None
        self.window = None
        self._unsubscribe = None
        # Falado por quem foi o último turno enviado para transcrição. O
        # callback `on_turn` do motor não carrega `speaker` (fora do escopo
        # mexer no motor além do necessário), então guardamos aqui para a
        # janela colorir a fala certa — seguro porque `ingest` é síncrono e
        # o worker processa um turno por vez.
        self._speaker_atual: str | None = None

    # ------------------------------------------------------------------

    def construir_janela(self):
        from ui.copilot_window import CopilotWindow

        self.window = CopilotWindow(
            settings=self.settings,
            on_settings_saved=self._ao_salvar_config,
            on_listen_toggled=self._ao_alternar_escuta,
            on_auto_toggled=lambda v: self.engine and self.engine.set_auto_answer(v),
            on_mic_toggled=self._ao_alternar_mic,
            on_answer_last=lambda: self.engine and self.engine.answer_last_turn(),
            on_context_changed=lambda t: self.engine and self.engine.update_user_context(t),
            on_test_connection=self._testar_conexao,
        )
        self.window.protocol("WM_DELETE_WINDOW", self.encerrar)
        return self.window

    @staticmethod
    def _testar_conexao(s: cfg.Settings) -> str | None:
        return OpenAICompatClient(s.base_url, s.api_key, s.model).check()

    def _ao_salvar_config(self, s: cfg.Settings) -> None:
        self.settings = s
        if self.engine is not None:
            self.engine.llm = from_settings(s)
            self.engine.update_user_context(s.user_context)
        self.iniciar_audio()

    # ------------------------------------------------------------------

    def iniciar_audio(self) -> None:
        """Prepara captura e transcrição. Idempotente."""
        if self.pipeline is not None:
            return
        if not self.settings.is_ready:
            return

        try:
            fonte = resolve_internal_audio()
        except AudioSourceError as e:
            self.window.show_error(str(e))
            return

        self.window.set_source(str(fonte))

        self.engine = CopilotEngine(
            llm=from_settings(self.settings),
            callbacks=EngineCallbacks(
                on_turn=lambda t, d: self.window.append_turn(t, d, self._speaker_atual),
                on_answer_start=self.window.start_answer,
                on_answer_chunk=self.window.append_answer_chunk,
                on_answer_done=self.window.finish_answer,
                on_error=self.window.show_error,
            ),
            config=EngineConfig(user_context=self.settings.user_context),
        )

        transcriber = WhisperLocal(
            model_size=self.settings.whisper_model,
            language=self.settings.language,
            beam_size=1,
        )
        self.worker = TranscriptionWorker(
            transcriber,
            on_transcribed=self._ao_transcrever,
            language=self.settings.language,
        )
        self.worker.start()

        # end_silence_ms curto: no Copilot, fechar o turno cedo vale mais do
        # que esperar a frase perfeita.
        #
        # max_utterance_s baixo é igualmente importante e menos óbvio. Quem
        # fala sem pausar (entrevista, palestra) nunca atinge o silêncio de
        # fim de turno, então quem manda é o corte forçado. Com os 30s do
        # padrão, a tela ficava meio minuto em branco e depois cuspia um
        # paredão de texto.
        #
        # 8s é o ajuste que dá a sensação das ferramentas de ditado: elas não
        # fazem streaming, transcrevem em lote a cada pausa — o "ao vivo"
        # vem do trecho ser curto, não do motor ser contínuo.
        segmenter_config = SegmenterConfig(end_silence_ms=450, max_utterance_s=8.0)
        self.pipeline = MultiSourcePipeline(
            system_source=fonte,
            config=segmenter_config,
            mic_config=segmenter_config,
        )

        self.window.set_status(f"Carregando Whisper '{self.settings.whisper_model}'...")
        threading.Thread(target=self._aquecer, daemon=True).start()

    def _aquecer(self) -> None:
        try:
            self.worker.transcriber.warmup()
            self.window.set_status("Pronto. Ligue “Ouvir” para começar.")
        except Exception as e:
            logger.exception("Falha ao carregar o Whisper")
            self.window.show_error(f"Não foi possível carregar o Whisper: {e}")

    def _ao_transcrever(self, utterance: Utterance) -> None:
        if utterance.text.strip() and self.engine is not None:
            self._speaker_atual = utterance.speaker
            self.engine.ingest(utterance.text, speaker=utterance.speaker)

    # ------------------------------------------------------------------

    def _ao_alternar_escuta(self, ligado: bool) -> None:
        if self.pipeline is None:
            self.iniciar_audio()
            if self.pipeline is None:
                return

        if ligado:
            # A captura só começa agora, mas uma vez ligada não para mais:
            # ligar/desligar a escuta apenas conecta e desconecta o consumidor.
            self.pipeline.start()
            self._unsubscribe = self.pipeline.subscribe(self.worker.submit)
        else:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            if self.engine is not None:
                self.engine.cancel()

    def _ao_alternar_mic(self, ligado: bool) -> None:
        if self.pipeline is None:
            self.iniciar_audio()
            if self.pipeline is None:
                return

        if ligado:
            try:
                self.pipeline.enable_microphone()
            except Exception as e:
                self.window.show_error(f"Não foi possível abrir o microfone: {e}")
        else:
            self.pipeline.disable_microphone()

    def encerrar(self) -> None:
        logger.info("Encerrando Copilot...")
        if self.engine is not None:
            self.engine.cancel()
        if self._unsubscribe is not None:
            self._unsubscribe()
        if self.pipeline is not None:
            self.pipeline.stop()
        if self.worker is not None:
            self.worker.stop(drain=False, timeout=5)
        if self.window is not None:
            self.window.destroy()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)-24s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        import customtkinter  # noqa: F401
    except ModuleNotFoundError:
        print(
            "A interface precisa do Tk, que não vem com o Python no Ubuntu.\n"
            "Instale com:\n\n    sudo apt install python3-tk\n",
            file=sys.stderr,
        )
        return 1

    app = CopilotApp(cfg.load())
    janela = app.construir_janela()
    app.iniciar_audio()  # não faz nada se ainda não há configuração válida
    janela.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
