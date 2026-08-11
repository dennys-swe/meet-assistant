#!/usr/bin/env python3
"""Captura + transcrição ao vivo: o pipeline completo até o texto.

    python demo_live.py                      # perfil copilot (small, rápido)
    python demo_live.py --profile aula       # qualidade alta
    python demo_live.py --model tiny         # sobrepõe o modelo do perfil
    python demo_live.py --source NOME

Mostra, para cada turno, a latência da transcrição e quanto ela representa
da duração do áudio (RTF). É esse número que decide se o Copilot é viável.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from asr.whisper_local import PROFILES, WhisperLocal
from asr.worker import TranscriptionWorker
from capture.pipeline import CapturePipeline
from capture.segmenter import SegmenterConfig
from capture.sources import AudioSourceError, list_sources, resolve_internal_audio
from domain.audio import Utterance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=list(PROFILES), default="copilot")
    parser.add_argument("--model", help="sobrepõe o modelo do perfil")
    parser.add_argument("--source", help="node.name da fonte do PipeWire")
    parser.add_argument("--language", default="pt")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s | %(name)-22s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        if args.source:
            fontes = {f.node_name: f for f in list_sources()}
            fonte = fontes.get(args.source)
            if fonte is None:
                print(f"Fonte '{args.source}' não encontrada. Use demo_capture.py --list", file=sys.stderr)
                return 1
        else:
            fonte = resolve_internal_audio()
    except AudioSourceError as e:
        print(f"Erro de áudio: {e}", file=sys.stderr)
        return 1

    kwargs = {"model_size": args.model} if args.model else {}
    transcriber = WhisperLocal.for_profile(args.profile, language=args.language, **kwargs)

    print(f"\n🎧 Fonte:  {fonte}")
    print(f"🧠 Perfil: {args.profile} (modelo {transcriber.model_size}, idioma {args.language})")
    print("   Carregando modelo...", flush=True)

    t0 = time.perf_counter()
    transcriber.warmup()
    print(f"   Pronto em {time.perf_counter() - t0:.1f}s. Ctrl+C para parar.\n")

    contador = {"n": 0}

    def ao_transcrever(fala: Utterance) -> None:
        if not fala.text:
            return
        contador["n"] += 1
        asr = fala.meta.get("asr", {})
        ms = asr.get("duration_ms", 0)
        rtf = (ms / 1000) / fala.duration if fala.duration else 0
        print(f"  [{fala.started_at:6.1f}s] ({fala.duration:4.1f}s · ASR {ms / 1000:.1f}s · RTF {rtf:.2f})")
        print(f"     {fala.text}\n", flush=True)

    worker = TranscriptionWorker(transcriber, ao_transcrever, language=args.language)
    worker.start()

    # O Copilot fecha turnos mais cedo: melhor uma frase um pouco picada e
    # respondida a tempo do que a frase perfeita chegando tarde demais.
    config = SegmenterConfig(end_silence_ms=500) if args.profile == "copilot" else SegmenterConfig()
    pipeline = CapturePipeline(source=fonte, config=config)
    pipeline.subscribe(worker.submit)

    signal.signal(signal.SIGINT, lambda *_: pipeline.recorder.stop())

    try:
        for _ in pipeline.utterances():
            pass
    except Exception as e:
        print(f"\nFalha na captura: {e}", file=sys.stderr)
        return 1
    finally:
        print("\n   Finalizando transcrições pendentes...", flush=True)
        worker.stop(drain=True)

    print(f"\n✅ {contador['n']} turno(s) transcrito(s).")
    if worker.dropped:
        print(f"⚠️  {worker.dropped} descartado(s) por fila cheia.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
