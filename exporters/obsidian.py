"""Exportador para o vault do Obsidian.

Herda o corpo inteiro do `MarkdownExporter` e acrescenta só o que é
específico do Obsidian: front matter YAML, checkboxes no formato que o
plugin Tasks reconhece, e a convenção de pastas `<vault>/<matéria>/<título>.md`.
Continua sendo um adaptador de saída como qualquer outro — nada aqui é
tratado como "o produto".
"""

from __future__ import annotations

import re
from pathlib import Path

from domain.session import Segment, Session, Todo
from exporters.markdown import MarkdownExporter

# Caracteres proibidos (ou problemáticos) em nomes de arquivo/pasta no Linux
# e que o Obsidian também evita por convenção (colidem com sintaxe de link).
_CARACTERES_INVALIDOS = re.compile(r'[\\/:*?"<>|\[\]#^]')


def sanitize_filename(nome: str) -> str:
    """Remove caracteres que quebram nome de arquivo ou pasta.

    Barra e dois-pontos são os que mais aparecem em títulos reais ("Aula
    3: Grafos", "Reunião 12/08") e viravam separador de caminho ou erro de
    sistema de arquivos se fossem direto para o disco.
    """
    limpo = _CARACTERES_INVALIDOS.sub("-", nome).strip()
    limpo = re.sub(r"\s+", " ", limpo)
    limpo = limpo.strip(" .")
    return limpo or "sem-titulo"


def _yaml_escape(valor: str) -> str:
    """Escapa uma string para uso como valor YAML entre aspas duplas."""
    return valor.replace("\\", "\\\\").replace('"', '\\"')


class ObsidianExporter(MarkdownExporter):
    """Markdown com front matter e checkboxes compatíveis com o plugin Tasks."""

    name = "Obsidian"
    extension = ".md"

    def render(self, session, segments, artifacts, todos) -> str:  # type: ignore[override]
        front_matter = self._render_front_matter(session)
        corpo = super().render(session, segments, artifacts, todos)
        return front_matter + corpo

    def _render_front_matter(self, session: Session) -> str:
        from exporters.markdown import format_duration

        tags = [sanitize_filename(session.subject).lower().replace(" ", "-")] if session.subject else []
        linhas = ["---"]
        linhas.append(f'title: "{_yaml_escape(session.title or "Sessão sem título")}"')
        linhas.append(f"date: {session.started_at.strftime('%Y-%m-%d')}")
        if session.subject:
            linhas.append(f'subject: "{_yaml_escape(session.subject)}"')
        linhas.append(f'duration: "{format_duration(session.duration_s)}"')
        if tags:
            linhas.append("tags: [" + ", ".join(tags) + "]")
        else:
            linhas.append("tags: []")
        linhas.append("---")
        linhas.append("")
        return "\n".join(linhas) + "\n"

    def _render_todo(self, tarefa: Todo) -> str:
        # Formato que o plugin Tasks do Obsidian reconhece: checkbox seguido
        # de metadados com emoji. `due` aqui não é data normalizada (é o que
        # a pessoa disse, ex.: "até sexta"), então o Tasks não vai agendar
        # lembrete com ele — mas continua legível e o checkbox funciona.
        caixa = "x" if tarefa.done else " "
        linha = f"- [{caixa}] {tarefa.text}"
        if tarefa.owner:
            linha += f" 👤 {tarefa.owner}"
        if tarefa.due:
            linha += f" 📅 {tarefa.due}"
        return linha

    def export(
        self,
        session: Session,
        segments: list[Segment],
        artifacts,
        todos: list[Todo],
        dest: Path,
    ) -> Path:
        """Grava em `<vault>/<matéria>/<título>.md`.

        `dest` aqui é o diretório do vault (a raiz), não o arquivo final —
        a matéria e o título já determinam o caminho completo.
        """
        conteudo = self.render(session, segments, artifacts, todos)

        pasta = dest / sanitize_filename(session.subject or "Sem matéria")
        pasta.mkdir(parents=True, exist_ok=True)

        nome_base = sanitize_filename(session.title or "Sessão sem título")
        caminho = pasta / f"{nome_base}{self.extension}"
        caminho = self._caminho_sem_colisao(caminho)

        caminho.write_text(conteudo, encoding="utf-8")
        return caminho

    def _caminho_sem_colisao(self, caminho: Path) -> Path:
        """Nunca sobrescreve: acrescenta sufixo numérico se o arquivo já existir.

        Na v1 perder a nota de uma aula anterior porque o tema se repetiu era
        um bug real — duas aulas de "Grafos" em semanas diferentes apagavam
        uma à outra.
        """
        if not caminho.exists():
            return caminho
        pasta, nome, ext = caminho.parent, caminho.stem, caminho.suffix
        contador = 2
        while True:
            candidato = pasta / f"{nome} ({contador}){ext}"
            if not candidato.exists():
                return candidato
            contador += 1
