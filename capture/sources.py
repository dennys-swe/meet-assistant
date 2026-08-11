"""Descoberta de fontes de áudio no PipeWire.

O ponto central: para capturar o áudio *interno* (Meet, Zoom, YouTube) no
Linux não existe nada a instalar. Todo sink do PipeWire expõe automaticamente
um "monitor", que é uma fonte contendo exatamente o que está tocando nele.
Capturar o monitor do sink padrão é o equivalente ao VB-CABLE do Windows,
só que nativo.

Resolvemos o sink padrão dinamicamente a cada sessão, em vez de fixar um
índice num `.env`. Índice de device é frágil: muda quando um fone Bluetooth
conecta ou desconecta.
"""

from __future__ import annotations

import json
import logging
import subprocess

from domain.audio import AudioSource

logger = logging.getLogger(__name__)


class AudioSourceError(RuntimeError):
    """Não foi possível consultar o PipeWire."""


def _pw_dump() -> list[dict]:
    try:
        saida = subprocess.run(
            ["pw-dump"], capture_output=True, timeout=10, check=True
        ).stdout
    except FileNotFoundError as e:
        raise AudioSourceError(
            "pw-dump não encontrado. Instale com: sudo apt install pipewire-bin"
        ) from e
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise AudioSourceError(f"Falha ao consultar o PipeWire: {e}") from e

    try:
        return json.loads(saida)
    except json.JSONDecodeError as e:
        raise AudioSourceError(f"Saída do pw-dump não é JSON válido: {e}") from e


def _nodes(dump: list[dict]) -> list[dict]:
    return [o for o in dump if o.get("type") == "PipeWire:Interface:Node"]


def default_sink_name(dump: list[dict] | None = None) -> str | None:
    """Nome do sink de saída padrão (para onde o áudio está tocando agora)."""
    dump = dump if dump is not None else _pw_dump()

    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if obj.get("props", {}).get("metadata.name") != "default":
            continue
        for entrada in obj.get("metadata", []):
            if entrada.get("key") == "default.audio.sink":
                valor = entrada.get("value")
                if isinstance(valor, dict):
                    return valor.get("name")
                return valor
    return None


def list_sources() -> list[AudioSource]:
    """Lista fontes capturáveis: monitores de cada sink + microfones reais."""
    dump = _pw_dump()
    fontes: list[AudioSource] = []

    for node in _nodes(dump):
        props = node.get("info", {}).get("props", {}) or {}
        classe = props.get("media.class", "")
        nome = props.get("node.name")
        descricao = props.get("node.description") or nome
        if not nome:
            continue

        # Atenção: o monitor de um sink NÃO é um node separado chamado
        # "<sink>.monitor" — isso é convenção do PulseAudio. No PipeWire o
        # monitor são portas do próprio sink, e se alcança apontando para o
        # sink e pedindo captura de sink (ver capture/recorder.py).
        if classe == "Audio/Sink":
            fontes.append(
                AudioSource(node_name=nome, description=descricao, is_monitor=True)
            )
        elif classe == "Audio/Source":
            fontes.append(
                AudioSource(node_name=nome, description=descricao, is_monitor=False)
            )

    return fontes


def resolve_internal_audio() -> AudioSource:
    """A fonte que queremos por padrão: o monitor do sink padrão.

    É o que o usuário está de fato ouvindo — fone Bluetooth, alto-falante do
    notebook, o que estiver ativo no momento.
    """
    dump = _pw_dump()
    sink = default_sink_name(dump)

    if sink is None:
        raise AudioSourceError(
            "Nenhum sink de áudio padrão definido no PipeWire. "
            "Verifique se há saída de som ativa (wpctl status)."
        )

    descricao = sink
    for node in _nodes(dump):
        props = node.get("info", {}).get("props", {}) or {}
        if props.get("node.name") == sink:
            descricao = props.get("node.description") or sink
            break

    fonte = AudioSource(node_name=sink, description=descricao, is_monitor=True)
    logger.info("Fonte de áudio interno resolvida: %s", fonte.node_name)
    return fonte
