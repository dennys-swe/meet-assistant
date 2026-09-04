"""Testes do Modo Aula (SessionRecorder + SessionProcessor). Sem rede, sem
áudio real, sem GUI — transcritor e LLM são dublês, e o repositório é o
`InMemoryRepository`."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asr.base import Transcriber, TranscriptionResult  # noqa: E402
from domain.audio import Utterance  # noqa: E402
from domain.session import ArtifactKind, Session, SessionStatus  # noqa: E402
from llm.base import LLMClient, LLMError  # noqa: E402
from modes.session import ProcessorCallbacks, RecordedBlock, SessionProcessor, SessionRecorder  # noqa: E402
from storage.memory_repo import InMemoryRepository  # noqa: E402


# ----------------------------------------------------------------------
# Dublês
# ----------------------------------------------------------------------


def utt(started_at: float, ended_at: float, pcm_len: int = 10) -> Utterance:
    """`Utterance` de teste. O tamanho do PCM não precisa bater com a duração
    — o recorder agrupa por tempo, não por bytes."""
    return Utterance(pcm=b"a" * pcm_len, started_at=started_at, ended_at=ended_at)


class FakeTranscriber(Transcriber):
    """Devolve textos fixos, em ordem, um por chamada."""

    def __init__(self, textos: list[str] | None = None, gancho=None):
        self.textos = textos or []
        self.chamadas = 0
        self.gancho = gancho  # chamado(indice) após cada transcrição, para testar cancelamento

    def transcribe_pcm(self, pcm: bytes, language: str | None = None) -> TranscriptionResult:
        i = self.chamadas
        self.chamadas += 1
        texto = self.textos[i] if i < len(self.textos) else f"texto do bloco {i}"
        if self.gancho:
            self.gancho(i)
        return TranscriptionResult(text=texto)


class FakeLLM(LLMClient):
    """Mesmo espírito do `FakeLLM` de `tests/test_engine.py`."""

    def __init__(self, resposta: str = "", erro: str | None = None):
        self.resposta = resposta
        self.erro = erro
        self.chamadas: list[list[dict]] = []

    def stream_reply(self, system, messages, max_tokens=300, temperature=0.3) -> Iterator[str]:
        self.chamadas.append(messages)
        if self.erro:
            raise LLMError(self.erro)
        yield self.resposta

    def check(self) -> str | None:
        return None


JSON_VALIDO = (
    '{"summary": "Resumo da aula.", '
    '"insights": ["A turma teve dúvida sobre recursão."], '
    '"topics": ["Recursão", "Complexidade"], '
    '"tasks": [{"text": "Enviar slides", "owner": "Maria", '
    '"due": "até sexta", "source_time": 30.0}]}'
)


def montar_repo_e_sessao() -> tuple[InMemoryRepository, Session]:
    repo = InMemoryRepository()
    sessao = repo.create_session(Session(title="Aula de estruturas de dados", subject="Algoritmos"))
    return repo, sessao


# ----------------------------------------------------------------------
# SessionRecorder — agrupamento em blocos
# ----------------------------------------------------------------------


def test_turnos_curtos_se_juntam_em_um_bloco_de_30s():
    rec = SessionRecorder(block_seconds=30.0)
    rec.start(Session())
    rec.add(utt(0, 10))
    rec.add(utt(10, 20))
    rec.add(utt(20, 31))  # soma chega a 31s aqui — bloco fecha
    rec.add(utt(31, 35))  # começa o próximo bloco

    blocos = rec.stop()

    assert len(blocos) == 2
    assert blocos[0].utterance_count == 3
    assert blocos[0].started_at == 0
    assert blocos[0].ended_at == 31
    assert blocos[1].utterance_count == 1
    assert blocos[1].started_at == 31
    assert blocos[1].ended_at == 35


def test_turno_nunca_e_partido():
    """Um bloco carrega o PCM inteiro dos turnos que contém, nunca um pedaço."""
    rec = SessionRecorder(block_seconds=30.0)
    rec.start(Session())
    u1, u2 = utt(0, 15, pcm_len=7), utt(15, 31, pcm_len=11)
    rec.add(u1)
    rec.add(u2)
    blocos = rec.stop()

    assert len(blocos) == 1
    assert blocos[0].pcm == u1.pcm + u2.pcm
    assert len(blocos[0].pcm) == 7 + 11


def test_turno_mais_longo_que_o_bloco_fica_sozinho():
    rec = SessionRecorder(block_seconds=30.0)
    rec.start(Session())
    rec.add(utt(0, 45))  # sozinho já passa da meta — não há como partir
    blocos = rec.stop()

    assert len(blocos) == 1
    assert blocos[0].ended_at - blocos[0].started_at == 45


def test_pausa_nao_conta_no_tempo():
    rec = SessionRecorder(block_seconds=30.0)
    rec.start(Session())
    rec.add(utt(0, 5))  # linha do tempo da sessão: 0 -> 5

    rec.pause()
    # Enquanto pausado, turnos são descartados mesmo que cheguem (o pipeline
    # continua rodando por trás — ver docstring do recorder).
    rec.add(utt(500, 505))
    rec.resume()

    # O pipeline "continuou" e este turno chega com um timestamp bruto de
    # 1000s — mas como não deveria contar o hiato da pausa, ele precisa
    # aparecer logo em seguida ao turno anterior na linha do tempo da sessão.
    rec.add(utt(1000, 1010))

    blocos = rec.stop()

    assert len(blocos) == 1
    assert blocos[0].utterance_count == 2  # o turno recebido durante a pausa foi descartado
    assert blocos[0].started_at == 0
    assert blocos[0].ended_at == 15  # 5 (primeiro turno) + 10 (segundo, sem o hiato de 995s)


def test_recorder_sem_start_ignora_turnos():
    rec = SessionRecorder()
    rec.add(utt(0, 5))  # não deveria estourar
    assert rec.stop() == []


# ----------------------------------------------------------------------
# SessionProcessor — transcrição, progresso, timestamps
# ----------------------------------------------------------------------


def test_progresso_e_emitido_por_bloco():
    repo, sessao = montar_repo_e_sessao()
    blocos = [
        RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0, utterance_count=2),
        RecordedBlock(pcm=b"b", started_at=30.0, ended_at=45.0, utterance_count=1),
    ]
    transcritor = FakeTranscriber(["Primeiro bloco.", "Segundo bloco."])
    processor = SessionProcessor(repo, transcritor, FakeLLM(JSON_VALIDO))

    progresso = []
    processor.process(sessao, blocos, on_progress=lambda i, n: progresso.append((i, n)))

    assert progresso == [(1, 2), (2, 2)]


def test_segmentos_gravados_com_timestamps_corretos():
    repo, sessao = montar_repo_e_sessao()
    blocos = [
        RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0),
        RecordedBlock(pcm=b"b", started_at=30.0, ended_at=45.0),
    ]
    transcritor = FakeTranscriber(["Primeiro bloco.", "Segundo bloco."])
    processor = SessionProcessor(repo, transcritor, FakeLLM(JSON_VALIDO))
    processor.process(sessao, blocos)

    segmentos = repo.list_segments(sessao.id)
    assert len(segmentos) == 2
    assert segmentos[0].started_at == 0.0 and segmentos[0].ended_at == 30.0
    assert segmentos[0].text == "Primeiro bloco."
    assert segmentos[1].started_at == 30.0 and segmentos[1].ended_at == 45.0
    assert segmentos[1].text == "Segundo bloco."


def test_artefatos_e_todos_sao_persistidos():
    repo, sessao = montar_repo_e_sessao()
    blocos = [RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0)]
    processor = SessionProcessor(repo, FakeTranscriber(["Aula sobre recursão."]), FakeLLM(JSON_VALIDO))

    resultado = processor.process(sessao, blocos)

    assert resultado.status == SessionStatus.DONE
    artefatos = repo.list_artifacts(sessao.id)
    resumos = [a for a in artefatos if a.kind == ArtifactKind.SUMMARY]
    topicos = [a for a in artefatos if a.kind == ArtifactKind.TOPIC]
    insights = [a for a in artefatos if a.kind == ArtifactKind.INSIGHT]
    assert resumos and resumos[0].content == "Resumo da aula."
    assert [t.content for t in topicos] == ["Recursão", "Complexidade"]
    assert [i.content for i in insights] == ["A turma teve dúvida sobre recursão."]

    todos = repo.list_todos(sessao.id)
    assert len(todos) == 1
    assert todos[0].text == "Enviar slides"
    assert todos[0].owner == "Maria"
    assert todos[0].due == "até sexta"  # como foi dito, sem normalizar
    assert todos[0].source_time == 30.0


def test_llm_falhando_mantem_segmentos_e_marca_sessao_failed():
    repo, sessao = montar_repo_e_sessao()
    blocos = [
        RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0),
        RecordedBlock(pcm=b"b", started_at=30.0, ended_at=45.0),
    ]
    processor = SessionProcessor(
        repo, FakeTranscriber(["Um.", "Dois."]), FakeLLM(erro="Chave de API inválida.")
    )

    resultado = processor.process(sessao, blocos)

    assert resultado.status == SessionStatus.FAILED
    assert resultado.meta["erro"] == "Chave de API inválida."
    # o que já foi transcrito não pode sumir por causa do LLM
    assert len(repo.list_segments(sessao.id)) == 2
    assert repo.list_artifacts(sessao.id) == []


def test_json_embrulhado_em_bloco_de_codigo_e_parseado():
    repo, sessao = montar_repo_e_sessao()
    resposta_embrulhada = f"Aqui está a análise:\n```json\n{JSON_VALIDO}\n```\nEspero que ajude."
    processor = SessionProcessor(
        repo, FakeTranscriber(["Aula."]), FakeLLM(resposta_embrulhada)
    )
    blocos = [RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0)]

    resultado = processor.process(sessao, blocos)

    assert resultado.status == SessionStatus.DONE
    resumos = [a for a in repo.list_artifacts(sessao.id) if a.kind == ArtifactKind.SUMMARY]
    assert resumos[0].content == "Resumo da aula."
    todos = repo.list_todos(sessao.id)
    assert todos[0].text == "Enviar slides"


def test_json_invalido_degrada_para_texto_em_vez_de_estourar():
    repo, sessao = montar_repo_e_sessao()
    processor = SessionProcessor(
        repo, FakeTranscriber(["Aula."]), FakeLLM("isso aqui não é JSON de jeito nenhum.")
    )
    blocos = [RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0)]

    resultado = processor.process(sessao, blocos)  # não pode lançar

    assert resultado.status == SessionStatus.DONE
    resumos = [a for a in repo.list_artifacts(sessao.id) if a.kind == ArtifactKind.SUMMARY]
    assert resumos[0].content == "isso aqui não é JSON de jeito nenhum."
    assert repo.list_todos(sessao.id) == []  # sem estrutura, sem tarefa — mas sem estourar


def test_cancelamento_interrompe_e_preserva_o_que_ja_foi_transcrito():
    repo, sessao = montar_repo_e_sessao()
    blocos = [
        RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0),
        RecordedBlock(pcm=b"b", started_at=30.0, ended_at=60.0),
        RecordedBlock(pcm=b"c", started_at=60.0, ended_at=90.0),
    ]

    processor_ref: dict[str, SessionProcessor] = {}

    def cancelar_apos_primeiro(indice: int) -> None:
        if indice == 0:
            processor_ref["p"].cancel()

    transcritor = FakeTranscriber(gancho=cancelar_apos_primeiro)
    processor = SessionProcessor(repo, transcritor, FakeLLM(JSON_VALIDO))
    processor_ref["p"] = processor

    resultado = processor.process(sessao, blocos)

    assert resultado.status == SessionStatus.FAILED
    assert "cancel" in resultado.meta["erro"].lower()
    assert len(repo.list_segments(sessao.id)) == 1  # só o primeiro bloco chegou a transcrever
    assert transcritor.chamadas == 1  # o terceiro bloco nunca foi transcrito


def test_process_async_chama_on_done_com_thread_separada():
    repo, sessao = montar_repo_e_sessao()
    blocos = [RecordedBlock(pcm=b"a", started_at=0.0, ended_at=30.0)]
    processor = SessionProcessor(repo, FakeTranscriber(["Aula."]), FakeLLM(JSON_VALIDO))

    evento = threading.Event()
    resultado: list[Session] = []

    def on_done(sessao_final: Session) -> None:
        resultado.append(sessao_final)
        evento.set()

    thread = processor.process_async(sessao, blocos, ProcessorCallbacks(on_done=on_done))
    assert evento.wait(timeout=3.0), "on_done não foi chamado"
    thread.join(timeout=1.0)
    assert resultado[0].status == SessionStatus.DONE


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
