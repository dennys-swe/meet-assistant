"""Exportador JSON: cópia completa e reimportável da sessão.

Ao contrário do Markdown/Obsidian, que existem para leitura humana, este
existe para round-trip — backup, migração para outro banco, ou consumo por
outra ferramenta. Por isso carrega tudo, sem selecionar o que "importa".
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

from domain.session import Artifact, Segment, Session, Todo
from exporters.base import Exporter


def _default(valor):
    if isinstance(valor, datetime):
        return valor.isoformat()
    if isinstance(valor, Enum):
        return valor.value
    raise TypeError(f"Tipo não serializável: {type(valor)!r}")


class JSONExporter(Exporter):
    """Serializa sessão, segmentos, artefatos e tarefas num único JSON."""

    name = "JSON"
    extension = ".json"

    def render(
        self,
        session: Session,
        segments: list[Segment],
        artifacts: list[Artifact],
        todos: list[Todo],
    ) -> str:
        dados = {
            "session": asdict(session),
            "segments": [asdict(s) for s in segments],
            "artifacts": [asdict(a) for a in artifacts],
            "todos": [asdict(t) for t in todos],
        }
        return json.dumps(dados, default=_default, ensure_ascii=False, indent=2)

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
