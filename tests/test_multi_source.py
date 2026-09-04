"""Testes do MultiSourcePipeline e da etiquetagem de speaker no motor.

Sem áudio real: os dois `CapturePipeline` que o `MultiSourcePipeline` gerencia
são substituídos por dublês (`FakePipeline`) injetados via
`system_pipeline_factory` / `mic_pipeline_factory`. O que se testa aqui é a
lógica de etiquetagem, ligar/desligar o microfone em runtime, e isolamento de
falha — não PipeWire.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture.multi import SPEAKER_SYSTEM, SPEAKER_USER, MultiSourcePipeline  # noqa: E402
from domain.audio import Utterance  # noqa: E402
from modes.copilot.engine import CopilotEngine, EngineCallbacks, EngineConfig  # noqa: E402
from test_engine import FakeLLM  # noqa: E402


class FakePipeline:
    """Substitui `CapturePipeline` nos testes: sem PipeWire, sem thread real."""

    def __init__(self, fail_on_start: bool = False):
        self.fail_on_start = fail_on_start
        self.started = False
        self.stopped = False
        self.start_count = 0
        self._listeners: list[Callable[[Utterance], None]] = []

    def start(self) -> None:
        self.start_count += 1
        if self.fail_on_start:
            raise RuntimeError("sem permissão para abrir o dispositivo")
        self.started = True

    def subscribe(self, listener: Callable[[Utterance], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped = True

    def emit(self, fala: Utterance) -> None:
        for ouvinte in list(self._listeners):
            ouvinte(fala)


def _utterance(texto: str = "oi") -> Utterance:
    return Utterance(pcm=b"", started_at=0.0, ended_at=1.0, text=texto)


def _montar():
    """Monta um MultiSourcePipeline com dublês, guardando as instâncias
    criadas para o teste poder disparar `emit()` nelas."""
    criados: dict[str, FakePipeline] = {}

    def fabrica_sistema():
        p = FakePipeline()
        criados["system"] = p
        return p

    mic_falha = {"on": False}

    def fabrica_mic():
        p = FakePipeline(fail_on_start=mic_falha["on"])
        criados.setdefault("mic_instances", []).append(p)  # type: ignore[arg-type]
        criados["mic"] = p
        return p

    pipeline = MultiSourcePipeline(
        system_pipeline_factory=fabrica_sistema,
        mic_pipeline_factory=fabrica_mic,
    )
    return pipeline, criados, mic_falha


# ----------------------------------------------------------------------
# Etiquetagem
# ----------------------------------------------------------------------

def test_utterance_do_sistema_recebe_speaker_sistema():
    pipeline, criados, _ = _montar()
    recebidas = []
    pipeline.subscribe(recebidas.append)
    pipeline.start()

    criados["system"].emit(_utterance("fala do sistema"))

    assert len(recebidas) == 1
    assert recebidas[0].speaker == SPEAKER_SYSTEM


def test_utterance_do_microfone_recebe_speaker_voce():
    pipeline, criados, _ = _montar()
    recebidas = []
    pipeline.subscribe(recebidas.append)
    pipeline.start()
    pipeline.enable_microphone()

    criados["mic"].emit(_utterance("fala do usuário"))

    assert len(recebidas) == 1
    assert recebidas[0].speaker == SPEAKER_USER


# ----------------------------------------------------------------------
# Ligar / desligar em runtime
# ----------------------------------------------------------------------

def test_ligar_microfone_duas_vezes_nao_duplica():
    pipeline, criados, _ = _montar()
    recebidas = []
    pipeline.subscribe(recebidas.append)
    pipeline.start()

    pipeline.enable_microphone()
    pipeline.enable_microphone()  # segunda chamada: no-op

    assert criados["mic"].start_count == 1, "não deveria abrir um segundo pipeline de mic"

    criados["mic"].emit(_utterance("uma fala só"))
    assert len(recebidas) == 1, "listener duplicado emitiria duas vezes"


def test_desligar_microfone_nao_derruba_sistema():
    pipeline, criados, _ = _montar()
    recebidas = []
    pipeline.subscribe(recebidas.append)
    pipeline.start()
    pipeline.enable_microphone()

    pipeline.disable_microphone()

    assert criados["mic"].stopped is True
    assert criados["system"].stopped is False

    criados["system"].emit(_utterance("sistema continua"))
    assert len(recebidas) == 1
    assert recebidas[0].speaker == SPEAKER_SYSTEM


def test_falha_ao_abrir_microfone_nao_derruba_sistema():
    pipeline, criados, mic_falha = _montar()
    mic_falha["on"] = True
    recebidas = []
    pipeline.subscribe(recebidas.append)
    pipeline.start()

    try:
        pipeline.enable_microphone()
        raise AssertionError("deveria ter propagado o erro de abertura do microfone")
    except RuntimeError:
        pass

    assert pipeline.microphone is None, "não deve ficar em estado 'ligado' após falhar"

    criados["system"].emit(_utterance("sistema sobrevive ao erro"))
    assert len(recebidas) == 1
    assert recebidas[0].speaker == SPEAKER_SYSTEM


def test_stop_encerra_os_dois_pipelines():
    pipeline, criados, _ = _montar()
    pipeline.start()
    pipeline.enable_microphone()

    pipeline.stop()

    assert criados["system"].stopped is True
    assert criados["mic"].stopped is True


# ----------------------------------------------------------------------
# Motor: pergunta do usuário entra no histórico mas não dispara LLM
# ----------------------------------------------------------------------

def test_pergunta_do_usuario_nao_dispara_llm_mas_entra_no_historico():
    llm = FakeLLM("resposta")
    motor = CopilotEngine(llm, config=EngineConfig())

    motor.ingest("Qual é a minha experiência com backend?", speaker=SPEAKER_USER)
    time.sleep(0.2)

    assert llm.chamadas == [], "pergunta do próprio usuário não deveria chamar o LLM"

    # Entrou no histórico: uma pergunta do sistema referenciando o contexto
    # deve conseguir puxar o turno do usuário no prompt.
    motor.ingest("E como você lidaria com isso na prática?", speaker=SPEAKER_SYSTEM)
    time.sleep(0.3)

    assert llm.chamadas, "pergunta do sistema deveria disparar o LLM"
    conteudo = llm.chamadas[-1][0]["content"]
    assert "experiência com backend" in conteudo, "turno do usuário deveria estar no histórico"


def test_pergunta_do_sistema_dispara_llm_normalmente():
    llm = FakeLLM("resposta do sistema")
    motor = CopilotEngine(llm, config=EngineConfig())

    motor.ingest("Qual é a sua maior dificuldade técnica?", speaker=SPEAKER_SYSTEM)
    time.sleep(0.3)

    assert llm.chamadas, "pergunta vinda do sistema deveria disparar o LLM"


def test_ingest_sem_speaker_continua_funcionando_como_antes():
    llm = FakeLLM("resposta")
    motor = CopilotEngine(llm, config=EngineConfig())

    motor.ingest("Qual é o seu maior desafio?")
    time.sleep(0.3)

    assert llm.chamadas, "chamada sem speaker deve manter o comportamento antigo"


if __name__ == "__main__":
    import traceback

    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    falhas = 0
    for teste in testes:
        try:
            teste()
            print(f"  ✅ {teste.__name__}")
        except Exception as e:
            falhas += 1
            print(f"  ❌ {teste.__name__}: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()

    print(f"\n{len(testes) - falhas}/{len(testes)} passaram")
    sys.exit(1 if falhas else 0)
