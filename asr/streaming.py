"""Transcrição contínua sobre o Whisper, via política LocalAgreement-2.

O Whisper é um modelo de lote: ele quer um trecho fechado e devolve o texto.
Usá-lo turno a turno coloca a transcrição no caminho crítico do Copilot — a
pessoa termina a pergunta e só então começamos a transcrever, somando ~2,2s
antes mesmo de acionar o LLM.

LocalAgreement-2 inverte isso. Transcrevemos um buffer que cresce, várias
vezes por segundo de fala, e comparamos cada hipótese nova com a anterior:
o prefixo em que as duas concordam é considerado estável e **confirmado**.
O que ainda diverge fica pendente, aguardando mais áudio.

    hipótese t1:  "qual é a sua experiência com sistemas"
    hipótese t2:  "qual é a sua experiência com sistemas distribuídos"
    confirmado:   "qual é a sua experiência com sistemas"      ← prefixo comum
    pendente:                                    "distribuídos"

Quando o VAD detecta o fim do turno, o texto já está praticamente pronto —
a transcrição sai do caminho crítico e o Copilot passa direto ao LLM.

--------------------------------------------------------------------------
ATENÇÃO — ESTA ABORDAGEM FOI MEDIDA E **PERDEU** NESTA MÁQUINA.

Latência do fim da fala até o texto pronto, mesmo áudio, CPU sem GPU:

    modelo   streaming (aqui)   lote (asr/whisper_local.py)
    tiny     0,7 - 1,4s         0,5s
    base     1,3 - 3,3s         0,9s
    small    2,8 - 4,7s         2,2s

O motivo é estrutural: o custo do Whisper é dominado pela passagem do
encoder sobre uma janela fixa de 30s, e ela custa quase o mesmo para 2s ou
20s de áudio. LocalAgreement roda essa passagem N vezes por turno em vez de
uma. Numa GPU, onde a passagem custa ~100ms, N vezes é barato e o streaming
ganha. Numa CPU, onde custa 1-2s, multiplicar a operação dominante só piora.
A qualidade também caiu: confirmar por prefixo comum trava erros cedo.

O módulo fica porque continua sendo a arquitetura certa em dois cenários —
se o projeto ganhar GPU, ou se trocarmos por um motor nativamente de
streaming — e porque os parciais durante a fala são úteis na interface,
mesmo que não estejam no caminho crítico. Só não o coloque entre o fim da
fala e o LLM: para isso, use `asr/whisper_local.py`.
--------------------------------------------------------------------------

Referência: Macháček, Dabre & Bojar, "Turning Whisper into Real-Time
Transcription System" (IJCNLP 2023), e a implementação de referência em
ufal/whisper_streaming (MIT). Aqui é uma reimplementação enxuta, sem as
camadas de tradução e servidor de socket do original.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class Word:
    text: str
    start: float  # segundos absolutos desde o início do stream
    end: float

    @property
    def key(self) -> str:
        """Forma normalizada para comparar hipóteses.

        Sem isto, uma vírgula a mais numa passagem quebraria o prefixo comum
        e a confirmação nunca avançaria.
        """
        return re.sub(r"[^\w]", "", self.text.lower())


@dataclass
class StreamingConfig:
    model_size: str = "small"
    language: str = "pt"
    compute_type: str = "int8"
    cpu_threads: int = 6

    # Áudio novo mínimo antes de rodar outra passagem. Menor = mais reativo e
    # mais caro; abaixo de ~0.7s a CPU não acompanha com o modelo `small`.
    min_chunk_s: float = 1.0

    # Teto absoluto do buffer (rede de segurança quando nada estabiliza).
    max_buffer_s: float = 20.0

    # Quanto de áudio já confirmado manter para trás, como contexto acústico.
    # Este é o parâmetro que decide a latência do fim de turno: o `finalize()`
    # só precisa transcrever o que sobrou no buffer, então quanto mais apertado
    # o corte, mais barata a passagem final. Abaixo de ~1s o modelo perde a
    # coarticulação e começa a errar a primeira palavra do trecho.
    context_s: float = 1.5

    # Contexto textual passado ao modelo (últimos N caracteres confirmados).
    prompt_chars: int = 200


@dataclass
class StreamingState:
    committed: list[Word] = field(default_factory=list)
    pending: list[Word] = field(default_factory=list)

    @property
    def committed_text(self) -> str:
        return " ".join(w.text for w in self.committed).strip()

    @property
    def pending_text(self) -> str:
        return " ".join(w.text for w in self.pending).strip()

    @property
    def full_text(self) -> str:
        return f"{self.committed_text} {self.pending_text}".strip()


class StreamingTranscriber:
    """Whisper em modo contínuo. Alimente com PCM; leia texto confirmado."""

    def __init__(self, config: StreamingConfig | None = None):
        self.config = config or StreamingConfig()
        self._model = None
        self._load_lock = threading.Lock()

        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_offset = 0.0  # tempo absoluto do início do buffer
        self._since_last_run = 0.0

        self._committed: list[Word] = []
        self._previous: list[Word] = []  # hipótese anterior, ainda não confirmada

    # ------------------------------------------------------------------
    # Modelo
    # ------------------------------------------------------------------

    def _load(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            from faster_whisper import WhisperModel

            logger.info("Carregando Whisper '%s' para streaming...", self.config.model_size)
            t0 = time.perf_counter()
            self._model = WhisperModel(
                self.config.model_size,
                device="cpu",
                compute_type=self.config.compute_type,
                cpu_threads=self.config.cpu_threads,
            )
            logger.info("Pronto em %.1fs", time.perf_counter() - t0)
            return self._model

    def warmup(self) -> None:
        self._load()

    # ------------------------------------------------------------------
    # Entrada de áudio
    # ------------------------------------------------------------------

    def insert_audio(self, pcm: bytes) -> None:
        """Acrescenta PCM 16 kHz mono int16 ao buffer."""
        amostras = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, amostras])
        self._since_last_run += amostras.size / SAMPLE_RATE

    @property
    def ready(self) -> bool:
        """Há áudio novo suficiente para valer outra passagem?"""
        return self._since_last_run >= self.config.min_chunk_s

    @property
    def buffer_seconds(self) -> float:
        return self._buffer.size / SAMPLE_RATE

    # ------------------------------------------------------------------
    # Passagem de transcrição
    # ------------------------------------------------------------------

    def process(self) -> list[Word]:
        """Roda uma passagem e devolve as palavras confirmadas agora.

        Devolve lista vazia quando nada novo estabilizou — o caso comum no
        meio de uma frase.
        """
        if self._buffer.size == 0:
            return []

        self._since_last_run = 0.0
        hipotese = self._transcrever_buffer()

        # Descarta o que já foi confirmado: comparamos apenas a cauda nova.
        limite = self._committed[-1].end if self._committed else 0.0
        hipotese = [w for w in hipotese if w.start >= limite - 0.1]

        novas = self._confirmar_prefixo_comum(hipotese)
        self._committed.extend(novas)

        # A hipótese atual, menos o que acabou de ser confirmado, vira a
        # referência da próxima comparação.
        self._previous = hipotese[len(novas):]

        # Cortar a cada passagem, não só quando o buffer estoura. É isso que
        # mantém o `finalize()` barato: quando a fala termina, o que resta no
        # buffer é apenas a cauda ainda não confirmada.
        self._trim()

        return novas

    def _transcrever_buffer(self) -> list[Word]:
        modelo = self._load()
        prompt = self.state.committed_text[-self.config.prompt_chars:] or None

        segmentos, _ = modelo.transcribe(
            self._buffer,
            language=self.config.language,
            beam_size=1,
            word_timestamps=True,
            # O VAD do Silero já governa quando alimentamos este buffer;
            # um segundo VAD aqui só apararia sílabas nas bordas.
            vad_filter=False,
            initial_prompt=prompt,
            # Sem isto, o modelo se apoia no próprio texto anterior e entra
            # em loops de repetição quando o áudio fica ambíguo.
            condition_on_previous_text=False,
        )

        palavras: list[Word] = []
        for seg in segmentos:
            for w in seg.words or []:
                texto = w.word.strip()
                if texto:
                    palavras.append(
                        Word(
                            text=texto,
                            start=self._buffer_offset + w.start,
                            end=self._buffer_offset + w.end,
                        )
                    )
        return palavras

    def _confirmar_prefixo_comum(self, hipotese: list[Word]) -> list[Word]:
        """LocalAgreement-2: confirma o prefixo em que duas passagens concordam."""
        n = 0
        for anterior, atual in zip(self._previous, hipotese):
            if anterior.key != atual.key or not atual.key:
                break
            n += 1
        return hipotese[:n]

    def _trim(self) -> None:
        """Descarta o áudio já confirmado, mantendo uma margem de contexto."""
        if not self._committed:
            # Nada estabilizou. Só cortamos se o buffer estourou o teto, senão
            # perderíamos áudio que ainda não virou texto.
            if self.buffer_seconds <= self.config.max_buffer_s:
                return
            manter = int(self.config.max_buffer_s * 0.5 * SAMPLE_RATE)
            descartadas = self._buffer.size - manter
            self._buffer = self._buffer[-manter:]
            self._buffer_offset += descartadas / SAMPLE_RATE
            logger.debug("Buffer cortado por tempo (nada confirmado)")
            return

        corte = self._committed[-1].end - self.config.context_s
        amostras = int((corte - self._buffer_offset) * SAMPLE_RATE)
        if amostras <= 0:
            return
        self._buffer = self._buffer[amostras:]
        self._buffer_offset += amostras / SAMPLE_RATE

    # ------------------------------------------------------------------
    # Fim de turno
    # ------------------------------------------------------------------

    def finalize(self) -> str:
        """Fecha o turno: confirma tudo que está pendente e devolve o texto.

        Chamado quando o VAD detecta fim de fala. Nesse ponto não há mais
        áudio para desambiguar, então a hipótese corrente é a resposta final.
        """
        if self._buffer.size > 0:
            hipotese = self._transcrever_buffer()
            limite = self._committed[-1].end if self._committed else 0.0
            self._committed.extend(w for w in hipotese if w.start >= limite - 0.1)

        texto = self.state.committed_text
        self.reset()
        return texto

    @property
    def state(self) -> StreamingState:
        return StreamingState(committed=list(self._committed), pending=list(self._previous))

    def reset(self) -> None:
        """Zera para o próximo turno, preservando a linha do tempo absoluta."""
        self._buffer_offset += self.buffer_seconds
        self._buffer = np.zeros(0, dtype=np.float32)
        self._since_last_run = 0.0
        self._committed = []
        self._previous = []
