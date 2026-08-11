#!/usr/bin/env python3
"""Transcrição contínua ao vivo — o texto aparece enquanto a pessoa fala.

    python demo_stream.py
    python demo_stream.py --model tiny        # mais reativo, menos preciso
    python demo_stream.py --min-chunk 0.7

A linha que se atualiza mostra o estado corrente:
  · em branco  = confirmado pelo LocalAgreement (não muda mais)
  · em cinza   = pendente (ainda pode ser reescrito)

Quando o VAD fecha o turno, a linha final sai com a latência medida entre o
fim da fala e o texto pronto. É esse número que define se o Copilot é viável.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
import wave
from pathlib import Path

from asr.live import LiveTranscriber, Partial, Turn
from asr.streaming import StreamingConfig
from capture.recorder import PipeWireRecorder
from capture.sources import AudioSourceError, list_sources, resolve_internal_audio

CINZA = "\033[90m"
RESET = "\033[0m"
LIMPA = "\033[2K\r"

BLOCO_BYTES = 512 * 2  # 32 ms, igual ao que o recorder entrega


def reproduzir(caminhos, live) -> None:
    """Alimenta WAVs no ritmo do relógio, como se viessem do microfone.

    Reproduzir em tempo real é essencial: se despejarmos o arquivo de uma vez,
    a transcrição tem todo o áudio disponível e a latência medida não significa
    nada. Entre arquivos entra silêncio, para o VAD fechar o turno.
    """
    silencio = b"\x00\x00" * 16000  # 1s
    for caminho in caminhos:
        with wave.open(str(caminho), "rb") as wf:
            if wf.getframerate() != 16000 or wf.getnchannels() != 1:
                raise ValueError(f"{caminho}: esperado 16 kHz mono")
            pcm = wf.readframes(wf.getnframes())

        for trecho in (pcm, silencio):
            for i in range(0, len(trecho), BLOCO_BYTES):
                inicio = time.perf_counter()
                live.feed(trecho[i : i + BLOCO_BYTES])
                atraso = 0.032 - (time.perf_counter() - inicio)
                if atraso > 0:
                    time.sleep(atraso)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="pt")
    parser.add_argument("--min-chunk", type=float, default=1.0)
    parser.add_argument("--source")
    parser.add_argument(
        "--replay",
        nargs="+",
        help="reproduz WAVs (16 kHz mono) em tempo real em vez de capturar; "
             "torna a medição reproduzível e dispensa placa de som",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s | %(name)-22s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    fonte = None
    if not args.replay:
        try:
            if args.source:
                fontes = {f.node_name: f for f in list_sources()}
                fonte = fontes.get(args.source)
                if fonte is None:
                    print(f"Fonte '{args.source}' não encontrada.", file=sys.stderr)
                    return 1
            else:
                fonte = resolve_internal_audio()
        except AudioSourceError as e:
            print(f"Erro de áudio: {e}", file=sys.stderr)
            return 1

    turnos: list[Turn] = []

    def ao_parcial(p: Partial) -> None:
        linha = f"  {p.committed} {CINZA}{p.pending}{RESET}"
        print(f"{LIMPA}{linha[:160]}", end="", flush=True)

    def ao_turno(t: Turn) -> None:
        turnos.append(t)
        print(f"{LIMPA}  ✓ [{t.started_at:6.1f}s → {t.ended_at:6.1f}s] latência {t.latency_ms:5d}ms")
        print(f"    {t.text}\n", flush=True)

    live = LiveTranscriber(
        on_partial=ao_parcial,
        on_turn=ao_turno,
        streaming=StreamingConfig(
            model_size=args.model, language=args.language, min_chunk_s=args.min_chunk
        ),
    )

    origem = "replay: " + ", ".join(Path(p).name for p in args.replay) if args.replay else str(fonte)
    print(f"\n🎧 Fonte:  {origem}")
    print(f"🧠 Modelo: {args.model} (chunk mínimo {args.min_chunk}s)")
    print("   Carregando...", flush=True)
    t0 = time.perf_counter()
    live.warmup()
    print(f"   Pronto em {time.perf_counter() - t0:.1f}s.\n")

    live.start()

    try:
        if args.replay:
            reproduzir(args.replay, live)
        else:
            recorder = PipeWireRecorder(fonte)
            signal.signal(signal.SIGINT, lambda *_: recorder.stop())
            for bloco in recorder.stream():
                live.feed(bloco)
    except Exception as e:
        print(f"\nFalha na captura: {e}", file=sys.stderr)
        return 1
    finally:
        live.stop()

    if turnos:
        latencias = sorted(t.latency_ms for t in turnos)
        mediana = latencias[len(latencias) // 2]
        print(f"\n✅ {len(turnos)} turno(s). Latência: mediana {mediana}ms, "
              f"mín {latencias[0]}ms, máx {latencias[-1]}ms\n")
    else:
        print("\n✅ Nenhum turno detectado.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
