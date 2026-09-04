"""Exportador Markdown puro: sem front matter, sem convenção de vault.

Serve de base para variações (o Obsidian herda daqui) e como formato
autônomo para quem só quer um `.md` legível em qualquer editor.
"""

from __future__ import annotations

from pathlib import Path

from domain.session import Artifact, ArtifactKind, Segment, Session, Todo
from exporters.base import Exporter


def format_timestamp(seconds: float) -> str:
    """Formata segundos desde o início da sessão como `[MM:SS]`."""
    total = max(0, int(round(seconds)))
    minutos, segs = divmod(total, 60)
    return f"[{minutos:02d}:{segs:02d}]"


def format_duration(seconds: float) -> str:
    """Formata a duração total da sessão como `HHhMMmin` ou `MMmin`."""
    total = max(0, int(round(seconds)))
    horas, resto = divmod(total, 3600)
    minutos, _ = divmod(resto, 60)
    if horas:
        return f"{horas}h{minutos:02d}min"
    return f"{minutos}min"


def _artifacts_by_kind(artifacts: list[Artifact], kind: ArtifactKind) -> list[Artifact]:
    return sorted((a for a in artifacts if a.kind == kind), key=lambda a: a.order)


class MarkdownExporter(Exporter):
    """Gera um `.md` legível: resumo, insights, tópicos, tarefas e transcrição."""

    name = "Markdown"
    extension = ".md"

    def render(
        self,
        session: Session,
        segments: list[Segment],
        artifacts: list[Artifact],
        todos: list[Todo],
    ) -> str:
        partes: list[str] = []
        partes.append(self._render_header(session))
        partes.append(self._render_body(session, segments, artifacts, todos))
        return "\n".join(partes).rstrip() + "\n"

    def _render_header(self, session: Session) -> str:
        titulo = session.title or "Sessão sem título"
        return f"# {titulo}\n"

    def _render_body(
        self,
        session: Session,
        segments: list[Segment],
        artifacts: list[Artifact],
        todos: list[Todo],
    ) -> str:
        linhas: list[str] = []

        if session.subject:
            linhas.append(f"**Matéria:** {session.subject}  ")
        linhas.append(f"**Data:** {session.started_at.strftime('%d/%m/%Y %H:%M')}  ")
        linhas.append(f"**Duração:** {format_duration(session.duration_s)}")
        linhas.append("")

        resumos = _artifacts_by_kind(artifacts, ArtifactKind.SUMMARY)
        if resumos:
            linhas.append("## Resumo\n")
            for artefato in resumos:
                linhas.append(artefato.content.strip())
                linhas.append("")

        insights = _artifacts_by_kind(artifacts, ArtifactKind.INSIGHT)
        if insights:
            linhas.append("## Insights\n")
            for artefato in insights:
                linhas.append(f"- {artefato.content.strip()}")
            linhas.append("")

        topicos = _artifacts_by_kind(artifacts, ArtifactKind.TOPIC)
        if topicos:
            linhas.append("## Tópicos\n")
            for artefato in topicos:
                linhas.append(f"- {artefato.content.strip()}")
            linhas.append("")

        if todos:
            linhas.append("## Tarefas\n")
            for tarefa in todos:
                linhas.append(self._render_todo(tarefa))
            linhas.append("")

        if segments:
            linhas.append("## Transcrição\n")
            for segmento in sorted(segments, key=lambda s: s.started_at):
                linhas.append(self._render_segment(segmento))
            linhas.append("")

        return "\n".join(linhas)

    def _render_todo(self, tarefa: Todo) -> str:
        caixa = "x" if tarefa.done else " "
        extras = []
        if tarefa.owner:
            extras.append(f"responsável: {tarefa.owner}")
        if tarefa.due:
            extras.append(f"prazo: {tarefa.due}")
        sufixo = f" ({', '.join(extras)})" if extras else ""
        return f"- [{caixa}] {tarefa.text}{sufixo}"

    def _render_segment(self, segmento: Segment) -> str:
        ts = format_timestamp(segmento.started_at)
        quem = f"**{segmento.speaker}:** " if segmento.speaker else ""
        return f"{ts} {quem}{segmento.text}"

    def export(
        self,
        session: Session,
        segments: list[Segment],
        artifacts: list[Artifact],
        todos: list[Todo],
        dest: Path,
    ) -> Path:
        conteudo = self.render(session, segments, artifacts, todos)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(conteudo, encoding="utf-8")
        return dest
