"""CopilotEngine — o motor do modo ao vivo.

Recebe turnos já transcritos, decide quais são perguntas, e produz a resposta
em streaming. Não conhece Tkinter, PipeWire nem httpx: fala com a interface
por callbacks e com o provedor pela interface `LLMClient`. É isso que permite
testá-lo inteiro sem rede, sem áudio e sem janela.

Duas decisões que valem explicação:

**Supersede.** Se uma pergunta nova chega enquanto a resposta anterior ainda
está sendo gerada, a anterior é abandonada. Numa conversa real, a última
pergunta é a única que importa — insistir na antiga entrega texto obsoleto e
ainda atrasa o que interessa.

**`[IGNORAR]`.** O detector é permissivo de propósito, então parte do que
chega aqui não pedia resposta (pergunta retórica, ou que o próprio falante
respondeu em seguida). Em vez de um segundo classificador, o próprio prompt
manda o modelo devolver `[IGNORAR]`, e o motor engole a resposta. Uma chamada
só resolve geração e filtragem.
"""

from __future__ import annotations

import itertools
import logging
import time
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from llm.base import LLMClient, LLMError
from modes.copilot.detector import Detection, QuestionDetector

logger = logging.getLogger(__name__)

MARCADOR_IGNORAR = "[IGNORAR]"

SYSTEM_PROMPT = """Você é um assistente que ajuda alguém DURANTE uma conversa ao vivo \
(reunião, entrevista ou aula). Você recebe a transcrição do que acabou de ser dito.

SUA FUNÇÃO É RESPONDER. Se houver uma pergunta no último turno, responda a ela — sempre. \
Não julgue se a pergunta era dirigida a você, se era retórica, ou se o próprio falante \
respondeu depois. Quem está lendo decide se usa ou não; seu trabalho é ter a resposta pronta.

Sua resposta será lida de relance, enquanto a conversa continua. Portanto:
- Responda em português do Brasil, direto ao ponto, no máximo 80 palavras.
- Comece pela resposta. Nunca escreva preâmbulo ("Claro!", "Boa pergunta", "Vamos lá").
- Prefira frases curtas ou 2-3 tópicos curtos. Nada de introdução e conclusão.
- Se a pergunta for técnica, dê a resposta concreta primeiro e o porquê depois.

A transcrição é automática e pode conter erros de reconhecimento. Interprete a intenção \
provável em vez de reclamar do texto.

Use {marcador} apenas no caso extremo em que o último turno **não contém pergunta \
nenhuma** — é só narração, concordância ou ruído. Na dúvida, responda."""


@dataclass
class Exchange:
    """Uma pergunta detectada e a resposta correspondente."""

    id: int
    question: str
    detection: Detection
    focus: str | None = None  # a frase interrogativa, quando o turno tem várias
    answer: str = ""
    error: str | None = None
    ignored: bool = False


@dataclass
class EngineCallbacks:
    """Ganchos para a interface. Todos opcionais."""

    on_turn: Callable[[str, Detection], None] | None = None
    on_answer_start: Callable[[Exchange], None] | None = None
    on_answer_chunk: Callable[[Exchange, str], None] | None = None
    on_answer_done: Callable[[Exchange], None] | None = None
    on_error: Callable[[str], None] | None = None


@dataclass
class EngineConfig:
    context_turns: int = 12
    max_tokens: int = 300
    temperature: float = 0.3
    user_context: str = ""

    # Responder sozinho a toda pergunta detectada. Desligado, o motor só
    # responde quando `answer_last_turn()` é chamado.
    #
    # Serve para dois casos opostos. Numa entrevista em que VOCÊ é o
    # candidato, automático é o ponto do produto. Assistindo a uma palestra
    # ou aula, quase toda pergunta é do palestrante para a plateia — responder
    # a todas gasta cota gratuita à toa (~140 chamadas/hora numa entrevista
    # de TV, medido).
    auto_answer: bool = True

    # Intervalo mínimo entre respostas automáticas. Perguntas em rajada
    # viram uma resposta só, a mais recente — que é a que importa.
    cooldown_s: float = 0.0


class CopilotEngine:
    def __init__(
        self,
        llm: LLMClient,
        callbacks: EngineCallbacks | None = None,
        config: EngineConfig | None = None,
        detector: QuestionDetector | None = None,
    ):
        self.llm = llm
        self.callbacks = callbacks or EngineCallbacks()
        self.config = config or EngineConfig()
        self.detector = detector or QuestionDetector()

        self._historico: deque[str] = deque(maxlen=self.config.context_turns)
        self._ids = itertools.count(1)
        self._geracao_atual = 0  # id da resposta viva; qualquer outra é abandonada
        self._lock = threading.Lock()
        self._ultimo_turno = ""
        self._ultima_resposta = 0.0

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------

    def ingest(self, texto: str) -> Detection:
        """Recebe um turno transcrito. Dispara a resposta se for pergunta."""
        limpo = (texto or "").strip()
        if not limpo:
            return Detection(False)

        # Procuramos a pergunta frase a frase, não no bloco inteiro. Quem fala
        # sem pausar gera turnos de 30s com várias frases dentro, e uma
        # pergunta enterrada no meio passaria batido se olhássemos só o todo.
        deteccao, frase = self.detector.detect_in_turn(limpo)

        self._historico.append(limpo)
        self._ultimo_turno = frase if deteccao.is_question else limpo

        self._chamar(self.callbacks.on_turn, limpo, deteccao)

        if deteccao.is_question and self._pode_responder_agora():
            # Mandamos o turno INTEIRO, não só a frase detectada. A frase
            # sozinha costuma ser um fragmento sem referente — "mas por que
            # eles têm a necessidade?" rendeu uma resposta genérica sobre
            # escassez de recursos, porque o assunto (ostentação) estava nas
            # frases anteriores do mesmo turno.
            self._responder(limpo, deteccao, foco=frase if frase != limpo else None)
        return deteccao

    def _pode_responder_agora(self) -> bool:
        if not self.config.auto_answer:
            return False
        if self.config.cooldown_s <= 0:
            return True
        agora = time.monotonic()
        if agora - self._ultima_resposta < self.config.cooldown_s:
            logger.debug("Resposta suprimida pelo cooldown")
            return False
        self._ultima_resposta = agora
        return True

    def set_auto_answer(self, ligado: bool) -> None:
        self.config.auto_answer = ligado
        logger.info("Resposta automática %s", "ligada" if ligado else "desligada")

    def answer_last_turn(self) -> bool:
        """Força resposta ao último turno, ignorando o detector.

        Existe porque o detector erra para menos de propósito. Quando ele
        perde uma pergunta, o usuário tem um botão em vez de ficar sem saída.
        """
        if not self._ultimo_turno:
            return False
        self._responder(self._ultimo_turno, Detection(True, "pedido manual"))
        return True

    def update_user_context(self, texto: str) -> None:
        self.config.user_context = texto or ""

    def cancel(self) -> None:
        """Descarta a resposta em andamento, se houver."""
        with self._lock:
            self._geracao_atual = 0

    # ------------------------------------------------------------------
    # Geração
    # ------------------------------------------------------------------

    def _responder(self, pergunta: str, deteccao: Detection, foco: str | None = None) -> None:
        troca = Exchange(id=next(self._ids), question=pergunta, detection=deteccao, focus=foco)
        with self._lock:
            self._geracao_atual = troca.id

        threading.Thread(
            target=self._gerar, args=(troca,), name=f"Copilot-{troca.id}", daemon=True
        ).start()

    def _vigente(self, troca: Exchange) -> bool:
        with self._lock:
            return self._geracao_atual == troca.id

    def _gerar(self, troca: Exchange) -> None:
        self._chamar(self.callbacks.on_answer_start, troca)

        system = SYSTEM_PROMPT.format(marcador=MARCADOR_IGNORAR)
        if self.config.user_context.strip():
            system += (
                "\n\nContexto sobre a pessoa que você está ajudando "
                f"(use quando for relevante):\n{self.config.user_context.strip()}"
            )

        try:
            partes: list[str] = []
            for pedaco in self.llm.stream_reply(
                system=system,
                messages=self._montar_mensagens(pergunta=troca.question, foco=troca.focus),
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            ):
                if not self._vigente(troca):
                    logger.debug("Resposta %d abandonada (pergunta nova)", troca.id)
                    return  # sai do `with` do httpx e corta a geração no servidor

                partes.append(pedaco)
                parcial = "".join(partes)

                # Segura a emissão até saber se é [IGNORAR]: caso contrário o
                # marcador pisca na tela antes de ser suprimido.
                if len(parcial) < len(MARCADOR_IGNORAR) and MARCADOR_IGNORAR.startswith(parcial.strip()):
                    continue
                if parcial.strip().startswith(MARCADOR_IGNORAR):
                    troca.ignored = True
                    logger.info("Turno %d ignorado pelo modelo (não era pergunta real)", troca.id)
                    self._chamar(self.callbacks.on_answer_done, troca)
                    return

                troca.answer = parcial
                self._chamar(self.callbacks.on_answer_chunk, troca, pedaco)

        except LLMError as e:
            if self._vigente(troca):
                troca.error = str(e)
                logger.warning("Falha ao gerar resposta: %s", e)
                self._chamar(self.callbacks.on_error, str(e))
        except Exception as e:
            if self._vigente(troca):
                troca.error = f"Erro inesperado: {e}"
                logger.exception("Erro inesperado ao gerar resposta")
                self._chamar(self.callbacks.on_error, troca.error)
        else:
            if not troca.answer.strip() and not troca.ignored:
                # O stream terminou sem nenhum texto. Acontece com modelos que
                # devolvem tudo num campo de raciocínio em vez de `content`
                # (visto no gpt-oss-20b). Sem esta mensagem o usuário fica
                # olhando para uma tela vazia sem saber por quê.
                troca.error = (
                    "O modelo não devolveu texto. Ele pode não ser compatível "
                    "com este app — tente outro em Configurações."
                )
                logger.warning("Modelo devolveu resposta vazia para o turno %d", troca.id)
                self._chamar(self.callbacks.on_error, troca.error)

        if self._vigente(troca):
            self._chamar(self.callbacks.on_answer_done, troca)

    def _montar_mensagens(self, pergunta: str, foco: str | None = None) -> list[dict[str, str]]:
        """Contexto recente + a pergunta em destaque.

        O histórico entra como um bloco de transcrição, não como turnos de
        chat: quem fala é sempre a outra pessoa, e tratar isso como diálogo
        faria o modelo achar que ele mesmo disse aquelas frases.
        """
        anteriores = [t for t in list(self._historico)[:-1] if t][-self.config.context_turns:]
        partes = []
        if anteriores:
            partes.append("Transcrição recente da conversa:\n" + "\n".join(f"- {t}" for t in anteriores))
        partes.append(f"Último turno (responda a isto):\n{pergunta}")
        if foco:
            partes.append(
                f"A pergunta a responder é esta, interpretada no contexto acima:\n{foco}"
            )
        return [{"role": "user", "content": "\n\n".join(partes)}]

    # ------------------------------------------------------------------

    @staticmethod
    def _chamar(cb, *args) -> None:
        """Um callback que quebra não pode derrubar a geração."""
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            logger.exception("Callback do Copilot falhou")
