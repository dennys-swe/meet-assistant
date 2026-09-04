"""Segmentação de fala: transforma PCM contínuo em turnos completos.

Esta é a peça que a v1 não tinha. Lá o áudio era fatiado a cada 3,5 s por
relógio, o que cortava palavras ao meio — a transcrição vinha com confiança
baixa e era descartada, e não havia frase inteira em que ancorar a detecção
de pergunta. Aqui quem decide onde cortar é o VAD, então cada `Utterance`
começa e termina em fronteira real de fala.

Máquina de estados (herdada de omi/backend/utils/stt/vad_gate.py, MIT):

    SILENCE  --fala detectada-->  SPEECH
    SPEECH   --silêncio-------->  HANGOVER
    HANGOVER --fala de novo---->  SPEECH      (era só uma pausa curta)
    HANGOVER --silêncio persiste-> SILENCE    (fim do turno: emite Utterance)

O pré-roll é o que impede o clique de perder a primeira sílaba: mantemos os
últimos 300 ms num buffer circular e, quando a fala é detectada, ela já entra
com esse trecho anterior colado na frente.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

import numpy as np

from capture.vad import (
    WINDOW_SAMPLES,
    make_fresh_state,
    pcm_to_float32,
    run_window,
)
from domain.audio import BYTES_PER_SECOND, SAMPLE_RATE, Utterance

logger = logging.getLogger(__name__)

WINDOW_BYTES = WINDOW_SAMPLES * 2
WINDOW_MS = WINDOW_SAMPLES / SAMPLE_RATE * 1000  # 32 ms


class State(str, Enum):
    SILENCE = "silence"
    SPEECH = "speech"
    HANGOVER = "hangover"


@dataclass
class SegmenterConfig:
    """Parâmetros de segmentação.

    `speech_threshold` e `pre_roll_ms` vêm calibrados de produção do Omi.

    `end_silence_ms` é nosso, e propositalmente diferente: o Omi usa 4000 ms de
    hangover porque o objetivo lá é manter o socket do provedor de STT aberto.
    Nosso objetivo é outro — fechar um turno de fala — e 4 s de espera deixaria
    o Copilot inutilizável ao vivo. 700 ms cobre a pausa natural entre frases
    sem partir uma frase no meio.
    """

    speech_threshold: float = 0.65   # Omi: VAD_GATE_SPEECH_THRESHOLD
    pre_roll_ms: int = 300           # Omi: VAD_GATE_PRE_ROLL_MS
    end_silence_ms: int = 700
    min_speech_ms: int = 250         # descarta estalos, tosse, clique de mouse
    max_utterance_s: float = 30.0    # corte forçado para quem fala sem pausa


class Segmenter:
    """Consome PCM e produz `Utterance` fechadas.

    Uso:
        seg = Segmenter()
        for bloco in recorder.stream():
            for fala in seg.feed(bloco):
                print(fala)
        for fala in seg.flush():   # fecha o turno pendente no fim da sessão
            print(fala)
    """

    def __init__(self, config: SegmenterConfig | None = None):
        self.config = config or SegmenterConfig()
        self._state = State.SILENCE
        self._vad_state, self._vad_context = make_fresh_state()

        # Sobra de bytes que não completou uma janela de 512 amostras
        self._residuo = b""

        # Buffer circular de pré-roll, em janelas
        n_pre_roll = max(1, round(self.config.pre_roll_ms / WINDOW_MS))
        self._pre_roll: deque[bytes] = deque(maxlen=n_pre_roll)

        self._buffer_fala: list[bytes] = []
        self._silencio_ms = 0.0
        self._fala_ms = 0.0
        self._inicio_turno = 0.0
        self._decorrido_ms = 0.0

    @property
    def elapsed_seconds(self) -> float:
        return self._decorrido_ms / 1000

    @property
    def state(self) -> State:
        return self._state

    def feed(self, pcm: bytes) -> Iterator[Utterance]:
        """Alimenta o segmentador com PCM 16 kHz mono int16."""
        dados = self._residuo + pcm
        n_janelas, sobra = divmod(len(dados), WINDOW_BYTES)
        self._residuo = dados[len(dados) - sobra :] if sobra else b""

        for i in range(n_janelas):
            janela = dados[i * WINDOW_BYTES : (i + 1) * WINDOW_BYTES]
            fala = self._processar_janela(janela)
            if fala is not None:
                yield fala

    def flush(self) -> Iterator[Utterance]:
        """Fecha um turno em aberto (fim de sessão, botão de parar)."""
        if self._state in (State.SPEECH, State.HANGOVER):
            fala = self._fechar_turno()
            if fala is not None:
                yield fala
        self._state = State.SILENCE

    # ------------------------------------------------------------------
    # Máquina de estados
    # ------------------------------------------------------------------

    def _processar_janela(self, janela: bytes) -> Utterance | None:
        amostras = pcm_to_float32(janela)
        prob, self._vad_state, self._vad_context = run_window(
            amostras, self._vad_state, self._vad_context
        )
        e_fala = prob >= self.config.speech_threshold
        self._decorrido_ms += WINDOW_MS

        if self._state is State.SILENCE:
            self._pre_roll.append(janela)
            if e_fala:
                self._abrir_turno()
            return None

        # Em SPEECH ou HANGOVER o áudio sempre entra no turno: o trecho de
        # silêncio do hangover faz parte da frase se a fala voltar, e é barato
        # descartá-lo no fim se não voltar.
        self._buffer_fala.append(janela)

        if e_fala:
            self._fala_ms += WINDOW_MS
            self._silencio_ms = 0.0
            self._state = State.SPEECH
        else:
            self._silencio_ms += WINDOW_MS
            self._state = State.HANGOVER
            if self._silencio_ms >= self.config.end_silence_ms:
                return self._fechar_turno()

        duracao = len(b"".join(self._buffer_fala)) / BYTES_PER_SECOND
        if duracao >= self.config.max_utterance_s:
            logger.debug("Corte forçado em %.1fs (fala contínua)", duracao)
            return self._fechar_turno()

        return None

    def _abrir_turno(self) -> None:
        pre_roll = list(self._pre_roll)
        self._pre_roll.clear()
        self._buffer_fala = pre_roll
        self._inicio_turno = max(
            0.0, self._decorrido_ms / 1000 - len(pre_roll) * WINDOW_MS / 1000
        )
        self._fala_ms = WINDOW_MS
        self._silencio_ms = 0.0
        self._state = State.SPEECH

    def _fechar_turno(self) -> Utterance | None:
        pcm = b"".join(self._buffer_fala)
        fala_ms = self._fala_ms

        self._buffer_fala = []
        self._silencio_ms = 0.0
        self._fala_ms = 0.0
        self._state = State.SILENCE
        self._vad_state, self._vad_context = make_fresh_state()

        if fala_ms < self.config.min_speech_ms:
            logger.debug("Turno descartado: só %.0fms de fala", fala_ms)
            return None

        # Apara o silêncio final do hangover, preservando uma margem curta
        # para não cortar a consoante final da última palavra.
        margem = int(0.2 * BYTES_PER_SECOND)
        excedente = int(self.config.end_silence_ms / 1000 * BYTES_PER_SECOND) - margem
        if excedente > 0 and len(pcm) > excedente:
            pcm = pcm[:-excedente]

        return Utterance(
            pcm=pcm,
            started_at=self._inicio_turno,
            ended_at=self._inicio_turno + len(pcm) / BYTES_PER_SECOND,
            meta={"speech_ms": round(fala_ms)},
        )
