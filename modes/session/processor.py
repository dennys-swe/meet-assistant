"""SessionProcessor — motor do Modo Aula: transcreve, resume, extrai tarefas.

Espelha o `CopilotEngine` em espírito (callbacks, thread separada, erro nunca
vira `except: pass`), mas a forma é diferente porque o problema é diferente:
o Copilot reage a cada turno em tempo real; o Modo Aula processa uma gravação
inteira de uma vez só, ao final, e pode levar minutos.

Duas garantias que a tarefa exige e que moldam o desenho:

- **Nada já transcrito se perde se o LLM falhar depois.** Os `Segment` são
  gravados no repositório assim que cada bloco é transcrito — antes de
  qualquer chamada ao LLM. Se o resumo falhar, a sessão vira `FAILED` com a
  mensagem de erro, mas a transcrição inteira continua no banco, consultável.

- **Cancelamento não corrompe nada.** É checado entre blocos (transcrição) e
  entre partes (resumo hierárquico) — nunca no meio de uma chamada em
  andamento, que não dá para interromper de forma limpa sem tocar em
  `LLMClient`/`Transcriber`, fora do escopo deste módulo.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from asr.base import Transcriber
from domain.session import (
    Artifact,
    ArtifactKind,
    Segment,
    Session,
    SessionStatus,
    Todo,
)
from llm.base import LLMClient, LLMError
from modes.session.prompts import (
    build_map_messages,
    build_reduce_messages,
    format_part,
    normalize_map_result,
    parse_json_relaxed,
    split_into_parts,
)
from modes.session.recorder import RecordedBlock
from storage.base import SessionRepository

logger = logging.getLogger(__name__)

OnProgress = Callable[[int, int], None]  # (bloco atual, total de blocos)


class Cancelado(RuntimeError):
    """Sinaliza que `cancel()` foi chamado no meio de `process()`."""


@dataclass
class ProcessorCallbacks:
    """Ganchos para quem chama `process_async`. Todos opcionais."""

    on_progress: OnProgress | None = None
    on_done: Callable[[Session], None] | None = None
    on_error: Callable[[Session, str], None] | None = None


class SessionProcessor:
    def __init__(
        self,
        repository: SessionRepository,
        transcriber: Transcriber,
        llm: LLMClient,
        max_tokens: int = 1500,
        temperature: float = 0.2,
    ):
        self.repository = repository
        self.transcriber = transcriber
        self.llm = llm
        self.max_tokens = max_tokens
        self.temperature = temperature

        self._cancel_event = threading.Event()

    # ------------------------------------------------------------------
    # API síncrona — o trabalho de fato acontece aqui
    # ------------------------------------------------------------------

    def cancel(self) -> None:
        self._cancel_event.set()

    def process(
        self,
        session: Session,
        blocks: list[RecordedBlock],
        on_progress: OnProgress | None = None,
    ) -> Session:
        """Transcreve, resume e extrai tarefas. Devolve a sessão atualizada.

        Sempre devolve uma `Session` — nunca lança por falha de transcrição
        ou de LLM. Essas falhas viram `SessionStatus.FAILED` com a mensagem
        em `session.meta["erro"]`, seguindo a regra do projeto de erro
        legível na tela em vez de traceback.
        """
        self._cancel_event.clear()
        session.status = SessionStatus.PROCESSING
        session = self.repository.update_session(session)

        try:
            segments = self._transcrever_blocos(session, blocks, on_progress)
        except Cancelado:
            return self._falhar(session, "Processamento cancelado.")
        except Exception as e:
            logger.exception("Falha ao transcrever a sessão %s", session.id)
            return self._falhar(session, f"Falha ao transcrever: {e}")

        if not segments:
            # Sessão vazia (nenhum turno capturado): nada para resumir, mas
            # não é uma falha — só não há conteúdo.
            session.status = SessionStatus.DONE
            return self.repository.update_session(session)

        try:
            self._gerar_artefatos(session, segments)
        except Cancelado:
            return self._falhar(session, "Processamento cancelado.")
        except LLMError as e:
            logger.warning("LLM falhou ao gerar resumo da sessão %s: %s", session.id, e)
            return self._falhar(session, str(e))
        except Exception as e:
            logger.exception("Falha inesperada ao gerar resumo da sessão %s", session.id)
            return self._falhar(session, f"Falha ao gerar resumo: {e}")

        session.status = SessionStatus.DONE
        return self.repository.update_session(session)

    def _falhar(self, session: Session, mensagem: str) -> Session:
        """Marca a sessão como FAILED sem apagar o que já foi persistido."""
        session.status = SessionStatus.FAILED
        session.meta = {**session.meta, "erro": mensagem}
        return self.repository.update_session(session)

    # ------------------------------------------------------------------
    # Etapa 1: transcrição bloco a bloco
    # ------------------------------------------------------------------

    def _transcrever_blocos(
        self, session: Session, blocks: list[RecordedBlock], on_progress: OnProgress | None
    ) -> list[Segment]:
        total = len(blocks)
        segmentos_gravados: list[Segment] = []

        for i, bloco in enumerate(blocks, start=1):
            if self._cancel_event.is_set():
                raise Cancelado()

            resultado = self.transcriber.transcribe_pcm(bloco.pcm, language=session.language)
            segmento = Segment(
                session_id=session.id,
                text=resultado.text,
                started_at=bloco.started_at,
                ended_at=bloco.ended_at,
                meta={"utterance_count": bloco.utterance_count},
            )
            # Grava assim que transcreve, não no final: se o bloco seguinte
            # falhar (ou o LLM falhar depois), este já está a salvo.
            salvos = self.repository.add_segments(session.id, [segmento])
            segmentos_gravados.extend(salvos)

            self._chamar(on_progress, i, total)

        return segmentos_gravados

    # ------------------------------------------------------------------
    # Etapa 2: resumo, insights/tópicos e tarefas — redução hierárquica
    # ------------------------------------------------------------------

    def _gerar_artefatos(self, session: Session, segments: list[Segment]) -> None:
        partes = split_into_parts(segments)

        resultados_mapeados = []
        for parte in partes:
            if self._cancel_event.is_set():
                raise Cancelado()

            texto = format_part(parte)
            if not texto.strip():
                continue

            system, mensagens = build_map_messages(texto)
            bruto = self._completar(system, mensagens)
            parsed = parse_json_relaxed(bruto)
            resultados_mapeados.append(normalize_map_result(parsed, bruto))

        if not resultados_mapeados:
            return

        resumo_final = self._reduzir_resumos(resultados_mapeados)

        insights = _achatar(resultados_mapeados, "insights")
        topicos = _achatar(resultados_mapeados, "topics")
        tarefas = [t for r in resultados_mapeados for t in r["tasks"]]

        artefatos = [Artifact(kind=ArtifactKind.SUMMARY, content=resumo_final, order=0)]
        artefatos += [
            Artifact(kind=ArtifactKind.TOPIC, content=t, order=i) for i, t in enumerate(topicos)
        ]
        artefatos += [
            Artifact(kind=ArtifactKind.INSIGHT, content=i_, order=i)
            for i, i_ in enumerate(insights)
        ]
        self.repository.add_artifacts(session.id, artefatos)

        todos = [
            Todo(
                text=t["text"],
                owner=t["owner"],
                due=t["due"],
                source_time=t["source_time"],
            )
            for t in tarefas
        ]
        self.repository.add_todos(session.id, todos)

    def _reduzir_resumos(self, resultados_mapeados: list[dict]) -> str:
        resumos_parciais = [r["summary"] for r in resultados_mapeados if r["summary"]]

        if len(resumos_parciais) <= 1:
            return resumos_parciais[0] if resumos_parciais else ""

        if self._cancel_event.is_set():
            raise Cancelado()

        system, mensagens = build_reduce_messages(resumos_parciais)
        bruto = self._completar(system, mensagens)
        parsed = parse_json_relaxed(bruto)
        if parsed and isinstance(parsed.get("summary"), str) and parsed["summary"].strip():
            return parsed["summary"].strip()
        # Degrada para os resumos parciais concatenados em vez de perder tudo.
        return bruto.strip() or "\n\n".join(resumos_parciais)

    def _completar(self, system: str, mensagens: list[dict[str, str]]) -> str:
        """Consome o streaming do `LLMClient` até o fim e junta o texto.

        O Modo Aula não tem por que mostrar a resposta pedaço a pedaço — só
        interessa o resultado completo — mas a interface só tem
        `stream_reply`, então juntamos aqui.
        """
        pedacos = list(
            self.llm.stream_reply(
                system=system,
                messages=mensagens,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
            )
        )
        return "".join(pedacos)

    # ------------------------------------------------------------------
    # API assíncrona — thread separada, callbacks, no mesmo estilo do Copilot
    # ------------------------------------------------------------------

    def process_async(
        self,
        session: Session,
        blocks: list[RecordedBlock],
        callbacks: ProcessorCallbacks | None = None,
    ) -> threading.Thread:
        """Dispara `process()` numa thread e devolve o handle.

        Transcrever uma aula de 1h é longo demais para travar quem chamou
        (UI ou não). `cancel()` interrompe entre blocos/partes.
        """
        callbacks = callbacks or ProcessorCallbacks()

        def _run() -> None:
            try:
                resultado = self.process(session, blocks, on_progress=callbacks.on_progress)
            except Exception as e:  # salvaguarda: process() não deveria lançar
                logger.exception("process_async: erro inesperado")
                self._chamar(callbacks.on_error, session, str(e))
                return

            if resultado.status == SessionStatus.FAILED:
                self._chamar(callbacks.on_error, resultado, resultado.meta.get("erro", ""))
            else:
                self._chamar(callbacks.on_done, resultado)

        thread = threading.Thread(target=_run, name=f"SessionProcessor-{session.id}", daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _chamar(cb, *args) -> None:
        """Um callback que quebra não pode derrubar o processamento."""
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:
            logger.exception("Callback do SessionProcessor falhou")


def _achatar(resultados_mapeados: list[dict], chave: str) -> list[str]:
    """Concatena listas de todas as partes, removendo duplicatas exatas."""
    vistos: set[str] = set()
    saida: list[str] = []
    for r in resultados_mapeados:
        for item in r[chave]:
            if item not in vistos:
                vistos.add(item)
                saida.append(item)
    return saida
