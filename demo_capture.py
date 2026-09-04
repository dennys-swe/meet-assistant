#!/usr/bin/env python3
"""Demo do pipeline de captura: mostra os turnos de fala saindo em tempo real.

    python demo_capture.py                 # captura o áudio interno (sink padrão)
    python demo_capture.py --list          # lista as fontes disponíveis
    python demo_capture.py --source NOME   # captura uma fonte específica
    python demo_capture.py --save-wav DIR  # salva o WAV de cada turno

Ainda não há transcrição — o objetivo aqui é ver o VAD acertando as fronteiras
de fala antes de acoplar qualquer ASR.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import wave
from pathlib import Path

from capture.pipeline import CapturePipeline
from capture.sources import AudioSourceError, list_sources, resolve_internal_audio
from domain.audio import CHANNELS, SAMPLE_RATE, SAMPLE_WIDTH, Utterance


def configurar_log(verboso: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verboso else logging.INFO,
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def salvar_wav(fala: Utterance, destino: Path, indice: int) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / f"turno_{indice:04d}.wav"
    with wave.open(str(caminho), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(fala.pcm)
    return caminho


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="lista fontes e sai")
    parser.add_argument("--source", help="node.name da fonte do PipeWire")
    parser.add_argument("--save-wav", type=Path, help="salva cada turno em WAV")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    configurar_log(args.verbose)

    try:
        if args.list:
            print("\nFontes de áudio disponíveis:\n")
            for fonte in list_sources():
                marca = "🔊" if fonte.is_monitor else "🎤"
                print(f"  {marca}  {fonte.description}")
                print(f"      {fonte.node_name}\n")
            print("Padrão (áudio interno):", resolve_internal_audio().node_name, "\n")
            return 0

        if args.source:
            fontes = {f.node_name: f for f in list_sources()}
            fonte = fontes.get(args.source)
            if fonte is None:
                print(f"Fonte '{args.source}' não encontrada. Use --list.", file=sys.stderr)
                return 1
        else:
            fonte = resolve_internal_audio()

    except AudioSourceError as e:
        print(f"Erro de áudio: {e}", file=sys.stderr)
        return 1

    pipeline = CapturePipeline(source=fonte)
    signal.signal(signal.SIGINT, lambda *_: pipeline.recorder.stop())

    print(f"\n🎧 Capturando: {fonte}")
    print("   Coloque um vídeo ou reunião para tocar. Ctrl+C para parar.\n")

    total = 0
    try:
        for indice, fala in enumerate(pipeline.utterances(), start=1):
            total += 1
            extra = ""
            if args.save_wav:
                extra = f"  → {salvar_wav(fala, args.save_wav, indice).name}"
            print(
                f"  [{fala.started_at:6.1f}s → {fala.ended_at:6.1f}s] "
                f"{fala.duration:4.1f}s de fala{extra}"
            )
    except Exception as e:
        print(f"\nFalha na captura: {e}", file=sys.stderr)
        return 1

    print(f"\n✅ {total} turno(s) de fala detectado(s).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
