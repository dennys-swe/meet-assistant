"""Testes da camada de persistência. Sem rede, sem áudio, sem GUI.

Roda a mesma bateria contra `InMemoryRepository` e `SQLiteRepository`
(sempre em `:memory:` — nunca o banco real do usuário).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.session import (  # noqa: E402
    Artifact,
    ArtifactKind,
    Segment,
    Session,
    SessionStatus,
    Todo,
)
from storage.base import SessionRepository  # noqa: E402
from storage.memory_repo import InMemoryRepository  # noqa: E402
from storage.sqlite_repo import SQLiteRepository  # noqa: E402

FABRICAS = {
    "memory": lambda: InMemoryRepository(),
    "sqlite": lambda: SQLiteRepository(":memory:"),
}


def _sessao(**kw) -> Session:
    base = dict(title="Reunião de kickoff", subject="Projeto X", language="pt")
    base.update(kw)
    return Session(**base)


# -- criar/ler/atualizar/listar sessão -----------------------------------


def test_criar_e_ler_sessao():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            criada = repo.create_session(_sessao())
            assert criada.id is not None, nome
            lida = repo.get_session(criada.id)
            assert lida is not None, nome
            assert lida.title == "Reunião de kickoff", nome
            assert lida.status == SessionStatus.RECORDING, nome
        finally:
            repo.close()


def test_atualizar_sessao():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            criada = repo.create_session(_sessao())
            criada.status = SessionStatus.DONE
            criada.ended_at = datetime(2026, 8, 11, 10, 30, 0)
            repo.update_session(criada)
            lida = repo.get_session(criada.id)
            assert lida.status == SessionStatus.DONE, nome
            assert lida.ended_at == datetime(2026, 8, 11, 10, 30, 0), nome
        finally:
            repo.close()


def test_listar_sessoes_mais_recente_primeiro():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            repo.create_session(_sessao(title="Primeira", started_at=datetime(2026, 1, 1)))
            repo.create_session(_sessao(title="Segunda", started_at=datetime(2026, 2, 1)))
            repo.create_session(_sessao(title="Terceira", started_at=datetime(2026, 3, 1)))
            listadas = repo.list_sessions()
            assert [s.title for s in listadas] == ["Terceira", "Segunda", "Primeira"], nome
        finally:
            repo.close()


def test_get_sessao_inexistente_devolve_none():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            assert repo.get_session(9999) is None, nome
        finally:
            repo.close()


# -- delete em cascata -----------------------------------------------------


def test_delete_sessao_apaga_tudo_em_cascata():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            repo.add_segments(sessao.id, [Segment(text="oi", started_at=0.0, ended_at=1.0)])
            repo.add_artifacts(
                sessao.id, [Artifact(kind=ArtifactKind.SUMMARY, content="resumo")]
            )
            repo.add_todos(sessao.id, [Todo(text="fazer algo")])

            assert repo.delete_session(sessao.id) is True, nome
            assert repo.get_session(sessao.id) is None, nome
            assert repo.list_segments(sessao.id) == [], nome
            assert repo.list_artifacts(sessao.id) == [], nome
            assert repo.list_todos(sessao.id) == [], nome
        finally:
            repo.close()


def test_delete_sessao_inexistente_devolve_false():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            assert repo.delete_session(12345) is False, nome
        finally:
            repo.close()


# -- segmentos em lote, ordem cronológica ----------------------------------


def test_add_segments_em_lote_preserva_ordem_cronologica():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            segmentos = [
                Segment(text="terceiro", started_at=20.0, ended_at=25.0),
                Segment(text="primeiro", started_at=0.0, ended_at=5.0),
                Segment(text="segundo", started_at=10.0, ended_at=15.0),
            ]
            inseridos = repo.add_segments(sessao.id, segmentos)
            assert len(inseridos) == 3, nome
            assert all(s.id is not None for s in inseridos), nome

            lidos = repo.list_segments(sessao.id)
            assert [s.text for s in lidos] == ["primeiro", "segundo", "terceiro"], nome
        finally:
            repo.close()


def test_add_segments_lista_vazia():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            assert repo.add_segments(sessao.id, []) == [], nome
        finally:
            repo.close()


# -- todos: cross-sessão, only_open, set_todo_done -------------------------


def test_list_todos_atravessa_sessoes_com_session_id_none():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            s1 = repo.create_session(_sessao(title="Aula 1"))
            s2 = repo.create_session(_sessao(title="Aula 2"))
            repo.add_todos(s1.id, [Todo(text="revisar slides")])
            repo.add_todos(s2.id, [Todo(text="enviar ata")])

            todos = repo.list_todos(session_id=None)
            textos = {t.text for t in todos}
            assert textos == {"revisar slides", "enviar ata"}, nome
        finally:
            repo.close()


def test_only_open_filtra_concluidos():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            inseridos = repo.add_todos(
                sessao.id, [Todo(text="pendente"), Todo(text="feito")]
            )
            feito_id = inseridos[1].id
            repo.set_todo_done(feito_id, True)

            abertos = repo.list_todos(session_id=sessao.id, only_open=True)
            assert [t.text for t in abertos] == ["pendente"], nome

            todos = repo.list_todos(session_id=sessao.id, only_open=False)
            assert len(todos) == 2, nome
        finally:
            repo.close()


def test_set_todo_done_inexistente_devolve_false():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            assert repo.set_todo_done(99999, True) is False, nome
        finally:
            repo.close()


def test_set_todo_done_alterna_para_falso():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            inseridos = repo.add_todos(sessao.id, [Todo(text="tarefa")])
            todo_id = inseridos[0].id
            repo.set_todo_done(todo_id, True)
            repo.set_todo_done(todo_id, False)
            (lido,) = repo.list_todos(session_id=sessao.id)
            assert lido.done is False, nome
        finally:
            repo.close()


# -- busca -------------------------------------------------------------


def test_search_acha_texto_e_ignora_o_que_nao_casa():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            repo.add_segments(
                sessao.id,
                [
                    Segment(text="vamos revisar o backlog amanhã", started_at=0.0, ended_at=2.0),
                    Segment(text="o deploy ficou estável ontem", started_at=2.0, ended_at=4.0),
                ],
            )
            achados = repo.search("backlog")
            assert len(achados) == 1, nome
            assert "backlog" in achados[0].text, nome

            assert repo.search("inexistente123") == [], nome
        finally:
            repo.close()


def test_search_string_vazia_nao_quebra():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            assert repo.search("") == [], nome
            assert repo.search("   ") == [], nome
        finally:
            repo.close()


# -- round-trip de meta e datetime -----------------------------------------


def test_round_trip_de_meta_com_acento_e_aninhamento():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            meta = {
                "professor": "José da Conceição",
                "tags": ["educação", "revisão"],
                "config": {"idioma": "pt-BR", "nível": 3},
            }
            criada = repo.create_session(_sessao(meta=meta))
            lida = repo.get_session(criada.id)
            assert lida.meta == meta, nome
        finally:
            repo.close()


def test_round_trip_de_datetime():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            quando = datetime(2026, 8, 11, 14, 5, 30, 123456)
            criada = repo.create_session(_sessao(started_at=quando))
            lida = repo.get_session(criada.id)
            assert lida.started_at == quando, nome
        finally:
            repo.close()


def test_round_trip_de_meta_em_segmento():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            meta = {"confiança": 0.87, "palavras": ["não", "é", "fácil"]}
            (inserido,) = repo.add_segments(
                sessao.id,
                [Segment(text="teste", started_at=0.0, ended_at=1.0, meta=meta)],
            )
            (lido,) = repo.list_segments(sessao.id)
            assert lido.meta == meta, nome
        finally:
            repo.close()


# -- artefatos -----------------------------------------------------------


def test_add_e_list_artifacts_em_ordem():
    for nome, fab in FABRICAS.items():
        repo: SessionRepository = fab()
        try:
            sessao = repo.create_session(_sessao())
            repo.add_artifacts(
                sessao.id,
                [
                    Artifact(kind=ArtifactKind.TOPIC, content="tópico B", order=2),
                    Artifact(kind=ArtifactKind.TOPIC, content="tópico A", order=1),
                ],
            )
            lidos = repo.list_artifacts(sessao.id)
            assert [a.content for a in lidos] == ["tópico A", "tópico B"], nome
        finally:
            repo.close()


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
