"""Testes dos exportadores com uma sessão de exemplo em memória.

Sem rede, sem áudio, sem GUI — só monta `Session`/`Segment`/`Artifact`/`Todo`
e verifica o texto (ou JSON) que cada exportador produz. As escritas em
disco usam `tempfile.TemporaryDirectory`; nunca tocam no vault real nem no
home do usuário.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.session import Artifact, ArtifactKind, Segment, Session, Todo  # noqa: E402
from exporters.json_export import JSONExporter  # noqa: E402
from exporters.markdown import MarkdownExporter, format_timestamp  # noqa: E402
from exporters.obsidian import ObsidianExporter, sanitize_filename  # noqa: E402


def montar_sessao():
    """Sessão de exemplo: acentos, artefatos fora de ordem, tarefas mistas."""
    session = Session(
        id=1,
        title="Aula 3: Grafos e Árvores",
        subject="Estruturas de Dados",
        started_at=datetime(2026, 8, 10, 14, 0, 0),
        ended_at=datetime(2026, 8, 10, 15, 30, 0),
        language="pt",
    )
    segments = [
        Segment(
            id=1, session_id=1, text="Bem-vindos à aula de hoje.",
            started_at=0.0, ended_at=4.0, speaker="Professor",
        ),
        Segment(
            id=2, session_id=1, text="Vamos falar sobre árvores binárias.",
            started_at=65.0, ended_at=70.0, speaker="Professor",
        ),
    ]
    # Fora de ordem de propósito: order=2 vem antes de order=0 na lista.
    artifacts = [
        Artifact(id=1, session_id=1, kind=ArtifactKind.SUMMARY,
                  content="Resumo final da aula sobre grafos.", order=2),
        Artifact(id=2, session_id=1, kind=ArtifactKind.SUMMARY,
                  content="Introdução rápida ao tema.", order=0),
        Artifact(id=3, session_id=1, kind=ArtifactKind.INSIGHT,
                  content="Alunos confundem árvore com grafo geral.", order=0),
        Artifact(id=4, session_id=1, kind=ArtifactKind.TOPIC,
                  content="Árvores binárias", order=1),
        Artifact(id=5, session_id=1, kind=ArtifactKind.TOPIC,
                  content="Percurso em profundidade", order=0),
    ]
    todos = [
        Todo(id=1, session_id=1, text="Enviar exercício de grafos",
             owner="Professor", due="até sexta", done=False, source_time=120.0),
        Todo(id=2, session_id=1, text="Revisar slides da aula passada",
             owner=None, due=None, done=True, source_time=30.0),
    ]
    return session, segments, artifacts, todos


# ---------------------------------------------------------------------------
# MarkdownExporter
# ---------------------------------------------------------------------------

def test_markdown_ordena_artifacts_por_order():
    session, segments, artifacts, todos = montar_sessao()
    conteudo = MarkdownExporter().render(session, segments, artifacts, todos)

    pos_intro = conteudo.index("Introdução rápida ao tema.")
    pos_resumo_final = conteudo.index("Resumo final da aula sobre grafos.")
    assert pos_intro < pos_resumo_final, "order=0 deveria vir antes de order=2"

    pos_percurso = conteudo.index("Percurso em profundidade")
    pos_arvores = conteudo.index("Árvores binárias")
    assert pos_percurso < pos_arvores, "tópicos deveriam respeitar Artifact.order"


def test_markdown_timestamp_formatado():
    assert format_timestamp(0) == "[00:00]"
    assert format_timestamp(65) == "[01:05]"
    assert format_timestamp(59.6) == "[01:00]"

    session, segments, artifacts, todos = montar_sessao()
    conteudo = MarkdownExporter().render(session, segments, artifacts, todos)
    assert "[00:00]" in conteudo
    assert "[01:05]" in conteudo


def test_markdown_checkbox_tarefas():
    session, segments, artifacts, todos = montar_sessao()
    conteudo = MarkdownExporter().render(session, segments, artifacts, todos)

    assert "- [ ] Enviar exercício de grafos" in conteudo
    assert "- [x] Revisar slides da aula passada" in conteudo
    assert "responsável: Professor" in conteudo
    assert "prazo: até sexta" in conteudo


def test_markdown_cabecalho_com_titulo_materia_e_duracao():
    session, segments, artifacts, todos = montar_sessao()
    conteudo = MarkdownExporter().render(session, segments, artifacts, todos)

    assert "# Aula 3: Grafos e Árvores" in conteudo
    assert "Estruturas de Dados" in conteudo
    assert "1h30min" in conteudo


def test_markdown_export_grava_arquivo(tmp=None):
    session, segments, artifacts, todos = montar_sessao()
    with tempfile.TemporaryDirectory() as tmpdir:
        destino = Path(tmpdir) / "saida.md"
        caminho = MarkdownExporter().export(session, segments, artifacts, todos, destino)
        assert caminho == destino
        assert caminho.exists()
        assert "Árvores binárias" in caminho.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ObsidianExporter
# ---------------------------------------------------------------------------

def test_obsidian_front_matter():
    session, segments, artifacts, todos = montar_sessao()
    conteudo = ObsidianExporter().render(session, segments, artifacts, todos)

    assert conteudo.startswith("---\n")
    front, _, resto = conteudo[4:].partition("\n---\n")
    assert 'title: "Aula 3: Grafos e Árvores"' in front
    assert "date: 2026-08-10" in front
    assert 'subject: "Estruturas de Dados"' in front
    assert 'duration: "1h30min"' in front
    assert "tags: [" in front
    assert resto  # o corpo (herdado do Markdown) continua presente


def test_obsidian_checkbox_formato_tasks():
    session, segments, artifacts, todos = montar_sessao()
    conteudo = ObsidianExporter().render(session, segments, artifacts, todos)

    assert "- [ ] Enviar exercício de grafos 👤 Professor 📅 até sexta" in conteudo
    assert "- [x] Revisar slides da aula passada" in conteudo


def test_obsidian_colisao_de_nome_nao_sobrescreve():
    session, segments, artifacts, todos = montar_sessao()
    with tempfile.TemporaryDirectory() as vault:
        exporter = ObsidianExporter()
        caminho1 = exporter.export(session, segments, artifacts, todos, Path(vault))

        # Mesma matéria, mesmo título (tema repetido em outra data) —
        # não pode apagar a nota anterior.
        session2, segments2, artifacts2, todos2 = montar_sessao()
        session2.started_at = datetime(2026, 9, 1, 10, 0, 0)
        caminho2 = exporter.export(session2, segments2, artifacts2, todos2, Path(vault))

        assert caminho1 != caminho2
        assert caminho1.exists()
        assert caminho2.exists()
        assert caminho2.name == "Aula 3- Grafos e Árvores (2).md"

        # o conteúdo do primeiro arquivo não foi tocado pela segunda exportação
        assert "2026-08-10" in caminho1.read_text(encoding="utf-8")
        assert "2026-09-01" in caminho2.read_text(encoding="utf-8")


def test_obsidian_export_grava_em_vault_materia_titulo():
    session, segments, artifacts, todos = montar_sessao()
    with tempfile.TemporaryDirectory() as vault:
        caminho = ObsidianExporter().export(session, segments, artifacts, todos, Path(vault))
        assert caminho.parent.parent == Path(vault)
        assert caminho.parent.name == sanitize_filename(session.subject)
        assert caminho.suffix == ".md"


def test_obsidian_sanitiza_nome_com_barra_e_dois_pontos():
    assert "/" not in sanitize_filename("Aula 5/Revisão: Provas")
    assert ":" not in sanitize_filename("Aula 5/Revisão: Provas")

    session, segments, artifacts, todos = montar_sessao()
    session.title = "Aula 5/Revisão: Provas"
    session.subject = "Cálculo I/II"
    with tempfile.TemporaryDirectory() as vault:
        caminho = ObsidianExporter().export(session, segments, artifacts, todos, Path(vault))
        # o arquivo precisa existir dentro do vault (nenhum componente de
        # caminho pode ter "escapado" via "/" não sanitizado)
        assert vault in str(caminho)
        assert caminho.exists()
        assert "/" not in caminho.stem
        assert "/" not in caminho.parent.name


# ---------------------------------------------------------------------------
# JSONExporter
# ---------------------------------------------------------------------------

def test_json_round_trip():
    session, segments, artifacts, todos = montar_sessao()
    conteudo = JSONExporter().render(session, segments, artifacts, todos)

    dados = json.loads(conteudo)  # não pode lançar: precisa ser JSON válido

    assert dados["session"]["title"] == "Aula 3: Grafos e Árvores"
    assert dados["session"]["subject"] == "Estruturas de Dados"
    assert dados["session"]["status"] == "recording"
    assert datetime.fromisoformat(dados["session"]["started_at"]) == session.started_at

    assert len(dados["segments"]) == len(segments)
    assert dados["segments"][1]["text"] == "Vamos falar sobre árvores binárias."

    assert len(dados["artifacts"]) == len(artifacts)
    kinds = {a["kind"] for a in dados["artifacts"]}
    assert kinds == {"summary", "insight", "topic"}

    assert len(dados["todos"]) == len(todos)
    tarefa_concluida = next(t for t in dados["todos"] if t["text"].startswith("Revisar"))
    assert tarefa_concluida["done"] is True
    tarefa_aberta = next(t for t in dados["todos"] if t["text"].startswith("Enviar"))
    assert tarefa_aberta["done"] is False
    assert tarefa_aberta["owner"] == "Professor"


def test_json_export_grava_arquivo_legivel():
    session, segments, artifacts, todos = montar_sessao()
    with tempfile.TemporaryDirectory() as tmpdir:
        destino = Path(tmpdir) / "sessao.json"
        caminho = JSONExporter().export(session, segments, artifacts, todos, destino)
        assert caminho.exists()
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        assert dados["session"]["title"] == "Aula 3: Grafos e Árvores"
        # ensure_ascii=False: acento vai literal no arquivo, não escapado
        assert "Árvores" in caminho.read_text(encoding="utf-8")


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
