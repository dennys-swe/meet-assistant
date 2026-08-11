"""Testes do CopilotEngine com um LLM falso. Sem rede, sem áudio, sem GUI."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.base import LLMClient, LLMError  # noqa: E402
from modes.copilot.engine import (  # noqa: E402
    CopilotEngine,
    EngineCallbacks,
    EngineConfig,
    Exchange,
)


class FakeLLM(LLMClient):
    def __init__(self, resposta="Resposta de teste.", atraso=0.0, erro=None):
        self.resposta = resposta
        self.atraso = atraso
        self.erro = erro
        self.chamadas: list[list[dict]] = []
        self.systems: list[str] = []

    def stream_reply(self, system, messages, max_tokens=300, temperature=0.3) -> Iterator[str]:
        self.chamadas.append(messages)
        self.systems.append(system)
        if self.erro:
            raise LLMError(self.erro)
        for palavra in self.resposta.split(" "):
            if self.atraso:
                time.sleep(self.atraso)
            yield palavra + " "

    def check(self) -> str | None:
        return None


class Coletor:
    def __init__(self):
        self.turnos = []
        self.chunks = []
        self.prontos: list[Exchange] = []
        self.erros = []
        self.evento = threading.Event()

    def callbacks(self) -> EngineCallbacks:
        return EngineCallbacks(
            on_turn=lambda t, d: self.turnos.append((t, d.is_question)),
            on_answer_chunk=lambda e, c: self.chunks.append(c),
            on_answer_done=self._done,
            on_error=self.erros.append,
        )

    def _done(self, troca: Exchange) -> None:
        self.prontos.append(troca)
        self.evento.set()

    def esperar(self, timeout=3.0) -> bool:
        ok = self.evento.wait(timeout)
        self.evento.clear()
        return ok


def montar(llm=None, **cfg):
    c = Coletor()
    motor = CopilotEngine(
        llm or FakeLLM(), callbacks=c.callbacks(), config=EngineConfig(**cfg)
    )
    return motor, c


def test_fala_comum_nao_chama_o_llm():
    llm = FakeLLM()
    motor, c = montar(llm)
    motor.ingest("Então a gente entregou o projeto em janeiro.")
    time.sleep(0.2)
    assert llm.chamadas == [], "não deveria chamar o LLM para fala comum"
    assert c.turnos and c.turnos[0][1] is False


def test_pergunta_gera_resposta():
    motor, c = montar(FakeLLM("Trabalhei cinco anos com backend."))
    motor.ingest("Qual é a sua experiência com backend?")
    assert c.esperar(), "resposta não chegou"
    assert c.prontos[0].answer.strip() == "Trabalhei cinco anos com backend."
    assert c.chunks, "deveria ter emitido chunks (streaming)"


def test_marcador_ignorar_suprime_resposta():
    motor, c = montar(FakeLLM("[IGNORAR]"))
    motor.ingest("Por que a gente faz isso mesmo? Porque é mais rápido.")
    assert c.esperar()
    troca = c.prontos[0]
    assert troca.ignored, "deveria marcar como ignorado"
    assert not troca.answer.strip(), "não deveria vazar o marcador para a tela"
    assert not any("IGNORAR" in ch for ch in c.chunks), "marcador vazou nos chunks"


def test_pergunta_nova_cancela_a_anterior():
    motor, c = montar(FakeLLM("uma resposta bem longa " * 20, atraso=0.02))
    motor.ingest("Qual é a sua experiência com sistemas distribuídos?")
    time.sleep(0.1)
    motor.ingest("E como você lida com prazos apertados?")
    assert c.esperar(timeout=5.0)
    time.sleep(0.3)
    assert len(c.prontos) == 1, f"a resposta abandonada não deveria concluir: {len(c.prontos)}"


def test_erro_do_provedor_vira_mensagem():
    motor, c = montar(FakeLLM(erro="Chave de API inválida."))
    motor.ingest("Qual é o seu maior desafio técnico?")
    assert c.esperar()
    assert c.erros == ["Chave de API inválida."]


def test_contexto_recente_vai_no_prompt():
    llm = FakeLLM()
    motor, _ = montar(llm)
    motor.ingest("Estamos falando sobre arquitetura de microsserviços.")
    motor.ingest("O sistema processa mil eventos por segundo.")
    motor.ingest("Como você garantiria a consistência disso?")
    time.sleep(0.3)

    conteudo = llm.chamadas[-1][0]["content"]
    assert "mil eventos por segundo" in conteudo, "faltou o contexto anterior"
    assert "Como você garantiria a consistência disso?" in conteudo


def test_user_context_entra_no_system():
    llm = FakeLLM()
    motor, _ = montar(llm)
    motor.update_user_context("Engenheiro de software, 5 anos com Python.")
    motor.ingest("Fala um pouco sobre a sua experiência")
    time.sleep(0.3)
    assert "5 anos com Python" in llm.systems[-1]


def test_responder_ultimo_turno_ignora_o_detector():
    llm = FakeLLM("Resposta forçada.")
    motor, c = montar(llm)
    motor.ingest("O prazo é sexta-feira.")  # não é pergunta
    time.sleep(0.2)
    assert llm.chamadas == []

    assert motor.answer_last_turn() is True
    assert c.esperar()
    assert c.prontos[0].answer.strip() == "Resposta forçada."


def test_turno_vazio_e_ignorado():
    llm = FakeLLM()
    motor, c = montar(llm)
    assert motor.ingest("   ").is_question is False
    assert llm.chamadas == []
    assert c.turnos == []


def test_modo_manual_nao_chama_o_llm():
    """Assistindo a uma aula, quase toda pergunta é do professor para a turma.
    Responder a todas queima cota gratuita sem servir para nada."""
    llm = FakeLLM("resposta")
    motor, c = montar(llm, auto_answer=False)
    motor.ingest("Qual é a sua experiência com backend?")
    time.sleep(0.2)
    assert llm.chamadas == [], "modo manual não deveria chamar o LLM"

    assert motor.answer_last_turn() is True
    assert c.esperar()
    assert len(llm.chamadas) == 1, "o botão manual deveria funcionar mesmo assim"


def test_alternar_para_auto_volta_a_responder():
    llm = FakeLLM("resposta")
    motor, c = montar(llm, auto_answer=False)
    motor.ingest("Qual é o seu maior desafio?")
    time.sleep(0.2)
    assert llm.chamadas == []

    motor.set_auto_answer(True)
    motor.ingest("E como você lida com prazos apertados?")
    assert c.esperar()
    assert len(llm.chamadas) == 1


def test_pergunta_em_turno_longo_manda_o_turno_inteiro():
    """A frase sozinha costuma ser fragmento sem referente: 'mas por que eles
    têm a necessidade?' rendeu resposta genérica quando ia sem o contexto."""
    llm = FakeLLM()
    motor, _ = montar(llm)
    bloco = ("Todos os ricos brasileiros ostentam muito no dia a dia. "
             "Mas por que eles têm essa necessidade?")
    motor.ingest(bloco)
    time.sleep(0.3)

    conteudo = llm.chamadas[-1][0]["content"]
    assert "Todos os ricos brasileiros ostentam" in conteudo, "faltou o contexto do turno"
    assert "Mas por que eles têm essa necessidade?" in conteudo


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
