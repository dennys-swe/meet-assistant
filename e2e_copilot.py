#!/usr/bin/env python3
"""Teste end-to-end do Copilot com áudio real do sistema.

O que os testes de `tests/` não podem cobrir: eles rodam sem áudio, sem rede e
sem GUI de propósito. Este arquivo é o outro lado — liga o pipeline inteiro
(PipeWire → VAD → Whisper → detector → LLM) num áudio de verdade e mede.

    .venv/bin/python e2e_copilot.py --minutos 5
    .venv/bin/python e2e_copilot.py --minutos 3 --llm real --max-chamadas 5
    .venv/bin/python e2e_copilot.py --replay runs/2026-08-11T20-00/sessao.wav

Toque uma entrevista ou aula no navegador e rode. Cada turno vira um .wav em
`runs/<carimbo>/turnos/`, e a sessão inteira vira `sessao.wav` — a partir daí o
mesmo material roda quantas vezes for preciso com `--replay`, sem depender de
alguém falar na hora. É assim que um caso ruim vira teste permanente.

O LLM é FALSO por padrão. Nenhuma chamada de rede acontece sem `--llm real`, e
mesmo aí `--max-chamadas` corta antes de a cota virar problema.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import statistics
import sys
import threading
import time
import wave
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from asr.whisper_local import WhisperLocal
from asr.worker import TranscriptionWorker
from capture.pipeline import CapturePipeline
from capture.segmenter import Segmenter, SegmenterConfig
from capture.sources import AudioSourceError, list_sources, resolve_internal_audio
from domain.audio import BYTES_PER_SECOND, SAMPLE_RATE, Utterance
from llm.base import LLMClient
from modes.copilot.engine import CopilotEngine, EngineCallbacks, EngineConfig

logger = logging.getLogger("e2e")

VERDE, AMARELO, CINZA, RESET = "\033[32m", "\033[33m", "\033[90m", "\033[0m"


class LLMDeMentira(LLMClient):
    """Substitui o provedor sem tocar na rede.

    Devolve uma resposta fixa com um atraso parecido com o de um modelo real,
    para que o teste exercite o mesmo caminho de streaming e cancelamento que
    a interface usa em produção.
    """

    def __init__(self, atraso_por_palavra: float = 0.02):
        self.atraso = atraso_por_palavra
        self.chamadas: list[str] = []

    def stream_reply(self, system, messages, max_tokens=300, temperature=0.3) -> Iterator[str]:
        self.chamadas.append(messages[-1]["content"] if messages else "")
        for palavra in "Resposta simulada para efeito de medicao .".split():
            time.sleep(self.atraso)
            yield palavra + " "

    def check(self) -> str | None:
        return None


class LLMComTeto(LLMClient):
    """Envelope que corta o provedor real depois de N chamadas.

    Um teste autônomo não pode gastar a cota de quem estiver rodando: uma
    entrevista de TV rende ~140 perguntas por hora, e isso com o auto-answer
    ligado vira 140 chamadas.
    """

    def __init__(self, interno: LLMClient, teto: int):
        self.interno = interno
        self.teto = teto
        self.chamadas = 0
        self.cortadas = 0

    def stream_reply(self, system, messages, max_tokens=300, temperature=0.3) -> Iterator[str]:
        if self.chamadas >= self.teto:
            self.cortadas += 1
            yield f"[teto de {self.teto} chamadas atingido — resposta não pedida]"
            return
        self.chamadas += 1
        yield from self.interno.stream_reply(system, messages, max_tokens, temperature)

    def check(self) -> str | None:
        return self.interno.check()


# -- gravação ------------------------------------------------------------


def escrever_wav(caminho: Path, pcm: bytes) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(caminho), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def ler_wav(caminho: Path) -> bytes:
    with wave.open(str(caminho), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(
                f"{caminho}: esperado PCM 16 kHz mono 16 bits, "
                f"veio {w.getframerate()} Hz / {w.getnchannels()} canal(is)"
            )
        return w.readframes(w.getnframes())


# -- coleta --------------------------------------------------------------


class Relatorio:
    def __init__(self, destino: Path):
        self.destino = destino
        self.turnos: list[dict] = []
        self.perguntas: list[dict] = []
        self.respostas: list[dict] = []
        self.erros: list[str] = []
        self.t0 = time.perf_counter()
        self._lock = threading.Lock()

    def turno(self, fala: Utterance, e_pergunta: bool) -> None:
        asr = fala.meta.get("asr", {})
        ms = asr.get("duration_ms", 0)
        with self._lock:
            self.turnos.append(
                {
                    "inicio_s": round(fala.started_at, 2),
                    "duracao_s": round(fala.duration, 2),
                    "asr_ms": ms,
                    "rtf": round((ms / 1000) / fala.duration, 3) if fala.duration else None,
                    "speaker": fala.speaker,
                    "pergunta": e_pergunta,
                    "texto": fala.text,
                }
            )

    def resumo(self) -> dict:
        rtfs = [t["rtf"] for t in self.turnos if t["rtf"]]
        latencias = [t["asr_ms"] for t in self.turnos if t["asr_ms"]]
        fala_s = sum(t["duracao_s"] for t in self.turnos)
        return {
            "duracao_do_teste_s": round(time.perf_counter() - self.t0, 1),
            "turnos": len(self.turnos),
            "segundos_de_fala": round(fala_s, 1),
            "perguntas_detectadas": len(self.perguntas),
            "taxa_de_perguntas_pct": round(
                100 * len(self.perguntas) / len(self.turnos), 1
            )
            if self.turnos
            else 0.0,
            "respostas_geradas": len(self.respostas),
            "rtf_mediano": round(statistics.median(rtfs), 3) if rtfs else None,
            "rtf_p95": round(sorted(rtfs)[int(len(rtfs) * 0.95)], 3) if len(rtfs) > 2 else None,
            "asr_ms_mediano": round(statistics.median(latencias)) if latencias else None,
            "asr_ms_p95": round(sorted(latencias)[int(len(latencias) * 0.95)])
            if len(latencias) > 2
            else None,
            "erros": self.erros,
        }

    def salvar(self) -> None:
        self.destino.mkdir(parents=True, exist_ok=True)
        (self.destino / "relatorio.json").write_text(
            json.dumps(
                {
                    "resumo": self.resumo(),
                    "turnos": self.turnos,
                    "perguntas": self.perguntas,
                    "respostas": self.respostas,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        linhas = ["# Transcrição do teste\n"]
        for t in self.turnos:
            marca = "❓" if t["pergunta"] else "·"
            quem = f" _{t['speaker']}_" if t["speaker"] else ""
            linhas.append(f"{marca} **[{t['inicio_s']:.0f}s]**{quem} {t['texto']}\n")
        (self.destino / "transcricao.md").write_text("\n".join(linhas), encoding="utf-8")


# -- execução ------------------------------------------------------------


def montar_llm(args) -> LLMClient:
    if args.llm == "fake":
        return LLMDeMentira()

    import config.settings as cfg
    from llm import from_settings

    s = cfg.load()
    if not s.is_ready:
        raise SystemExit(
            "--llm real precisa de chave configurada em ~/.config/meet-assistant/.\n"
            "Rode o app uma vez e salve a chave, ou use o padrão --llm fake."
        )
    print(f"   LLM real: {s.model} via {s.base_url} (teto de {args.max_chamadas} chamadas)")
    return LLMComTeto(from_settings(s), args.max_chamadas)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutos", type=float, default=5.0, help="para sozinho depois disso")
    p.add_argument("--modelo", default="small")
    p.add_argument("--idioma", default="pt")
    p.add_argument("--fonte", help="node.name do PipeWire; padrão é o áudio interno")
    p.add_argument("--replay", type=Path, help="roda sobre um .wav em vez de capturar")
    p.add_argument("--llm", choices=["fake", "real"], default="fake")
    p.add_argument("--max-chamadas", type=int, default=5, help="teto de chamadas com --llm real")
    p.add_argument("--auto", action="store_true", help="responde a toda pergunta (padrão: manual)")
    p.add_argument("--saida", type=Path, default=Path("runs"))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )

    carimbo = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    destino = args.saida / carimbo
    rel = Relatorio(destino)
    llm = montar_llm(args)

    # Mesma configuração do app (ver copilot_app.py): fechar o turno cedo vale
    # mais do que a frase perfeita, e o corte forçado em 8s é o que dá a
    # sensação de "ao vivo" para quem fala sem pausar.
    seg_config = SegmenterConfig(end_silence_ms=450, max_utterance_s=8.0)

    engine = CopilotEngine(
        llm=llm,
        config=EngineConfig(auto_answer=args.auto, cooldown_s=8.0),
    )

    respondendo: dict[int, list[str]] = {}

    def ao_turno(texto: str, deteccao, speaker) -> None:
        # `speaker` chega pela assinatura — o app não guarda mais o falante
        # numa variável de instância.
        if deteccao.is_question:
            rel.perguntas.append({"texto": texto, "score": getattr(deteccao, "score", None)})
            print(f"  {AMARELO}❓ {texto}{RESET}", flush=True)
        else:
            print(f"  {CINZA}·{RESET} {texto}", flush=True)

    def ao_chunk(troca, pedaco: str) -> None:
        respondendo.setdefault(troca.id, []).append(pedaco)

    def ao_pronto(troca) -> None:
        rel.respostas.append({"pergunta": troca.question, "resposta": troca.answer})
        print(f"  {VERDE}🤖 {troca.answer.strip()[:300]}{RESET}\n", flush=True)

    def ao_erro(msg: str) -> None:
        rel.erros.append(msg)
        print(f"  ⚠️  {msg}", flush=True)

    engine.callbacks = EngineCallbacks(
        on_turn=ao_turno, on_answer_chunk=ao_chunk, on_answer_done=ao_pronto, on_error=ao_erro
    )

    transcriber = WhisperLocal(model_size=args.modelo, language=args.idioma, beam_size=1)
    print(f"\n🧠 Carregando Whisper '{args.modelo}'...", flush=True)
    t0 = time.perf_counter()
    transcriber.warmup()
    print(f"   pronto em {time.perf_counter() - t0:.1f}s")

    pcm_da_sessao: list[bytes] = []
    n = {"i": 0}

    def ao_transcrever(fala: Utterance) -> None:
        if not fala.text.strip():
            return
        n["i"] += 1
        escrever_wav(destino / "turnos" / f"{n['i']:03d}.wav", fala.pcm)
        deteccao = engine.ingest(fala.text, speaker=fala.speaker)
        rel.turno(fala, deteccao.is_question)

    worker = TranscriptionWorker(transcriber, ao_transcrever, language=args.idioma)
    worker.start()

    try:
        if args.replay:
            print(f"\n🎞️  Replay de {args.replay}\n")
            pcm = ler_wav(args.replay)
            segmenter = Segmenter(seg_config)
            bloco = BYTES_PER_SECOND // 10  # 100 ms, como o recorder entrega
            for i in range(0, len(pcm), bloco):
                for fala in segmenter.feed(pcm[i : i + bloco]):
                    worker.submit(fala)
            for fala in segmenter.flush():
                worker.submit(fala)
        else:
            try:
                fontes = {f.node_name: f for f in list_sources()}
                fonte = fontes[args.fonte] if args.fonte else resolve_internal_audio()
            except (AudioSourceError, KeyError) as e:
                print(f"Erro de áudio: {e}", file=sys.stderr)
                return 1

            print(f"\n🎧 Fonte: {fonte}")
            print(f"⏱️  Gravando por até {args.minutos:.0f} min. Ctrl+C para parar antes.\n", flush=True)

            pipeline = CapturePipeline(source=fonte, config=seg_config)
            pipeline.subscribe(worker.submit)
            pipeline.subscribe(lambda f: pcm_da_sessao.append(f.pcm))

            limite = time.perf_counter() + args.minutos * 60
            signal.signal(signal.SIGINT, lambda *_: pipeline.recorder.stop())
            parar = threading.Timer(args.minutos * 60, pipeline.recorder.stop)
            parar.daemon = True
            parar.start()
            try:
                for _ in pipeline.utterances():
                    if time.perf_counter() > limite:
                        pipeline.recorder.stop()
            finally:
                parar.cancel()
    finally:
        print("\n   Drenando transcrições pendentes...", flush=True)
        worker.stop(drain=True)
        time.sleep(1.0)  # deixa a última resposta do LLM terminar

    if pcm_da_sessao:
        escrever_wav(destino / "sessao.wav", b"".join(pcm_da_sessao))

    if worker.dropped:
        rel.erros.append(f"{worker.dropped} turno(s) descartado(s) por fila cheia")
    if isinstance(llm, LLMComTeto) and llm.cortadas:
        rel.erros.append(f"{llm.cortadas} resposta(s) suprimida(s) pelo teto de chamadas")

    rel.salvar()
    r = rel.resumo()
    print("\n" + "─" * 62)
    for chave, valor in r.items():
        if chave != "erros":
            print(f"  {chave:.<34} {valor}")
    for erro in r["erros"]:
        print(f"  ⚠️  {erro}")
    print(f"\n  Artefatos em {destino}/")
    print("─" * 62 + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
