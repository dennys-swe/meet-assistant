"""Transcrição local com faster-whisper (CTranslate2, int8 em CPU).

Local por padrão, por três motivos: o áudio de aulas e reuniões não sai da
máquina, não há custo por minuto, e funciona offline. A troca por uma API
continua possível — basta outra implementação de `Transcriber`.

Números medidos nesta máquina (12 threads, sem GPU, int8, áudio real de uma
palestra em português capturada pelo pipeline):

    modelo             piso     RTF em 25s     qualidade
    tiny               ~0.5s    0.03           ruim, erra nomes e termos
    base               ~0.9s    0.06           medíocre
    small              ~2.2s    0.14           boa
    large-v3-turbo     ~10s     0.44           melhor

O Whisper processa em janelas fixas de 30s, então cada chamada tem um custo
mínimo independente do tamanho do áudio. É por isso que a tabela tem duas
colunas: turnos curtos pagam o piso, turnos longos diluem.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from asr.base import Transcriber, TranscriptionResult

logger = logging.getLogger(__name__)


# Perfis por modo. O Copilot precisa responder enquanto a conversa acontece;
# o Modo Aula processa depois e pode pagar mais caro por qualidade.
#
# ATENÇÃO ao usar o perfil "aula": o custo fixo de ~10s por chamada do
# large-v3-turbo só se justifica em blocos grandes. Transcrever turno a turno
# com ele custa 10s por frase — o Modo Aula precisa agrupar os turnos em
# blocos de ~30s antes de chamar. Enquanto esse agrupamento não existir,
# prefira o perfil "copilot" também para gravações de aula.
PROFILES = {
    "copilot": {"model_size": "small", "beam_size": 1},
    "aula": {"model_size": "large-v3-turbo", "beam_size": 5},
}


class WhisperLocal(Transcriber):
    def __init__(
        self,
        model_size: str = "small",
        language: str | None = "pt",
        beam_size: int = 1,
        compute_type: str = "int8",
        cpu_threads: int = 6,
    ):
        self.model_size = model_size
        self.language = language
        self.beam_size = beam_size
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads

        self._model = None
        self._lock = threading.Lock()

    @classmethod
    def for_profile(cls, profile: str, **kwargs) -> "WhisperLocal":
        if profile not in PROFILES:
            raise ValueError(f"Perfil desconhecido: {profile}. Use {list(PROFILES)}")
        return cls(**{**PROFILES[profile], **kwargs})

    def _load(self):
        """Carrega sob demanda: o app abre rápido e só paga o custo de RAM
        do modelo se a transcrição for de fato usada."""
        if self._model is not None:
            return self._model

        with self._lock:
            if self._model is not None:
                return self._model

            from faster_whisper import WhisperModel

            logger.info("Carregando Whisper '%s' (%s)...", self.model_size, self.compute_type)
            t0 = time.perf_counter()
            self._model = WhisperModel(
                self.model_size,
                device="cpu",
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
            )
            logger.info("Whisper pronto em %.1fs", time.perf_counter() - t0)
            return self._model

    def warmup(self) -> None:
        self._load()

    def transcribe_pcm(self, pcm: bytes, language: str | None = None) -> TranscriptionResult:
        if not pcm:
            return TranscriptionResult(text="")

        modelo = self._load()
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0

        t0 = time.perf_counter()
        # O VAD interno do faster-whisper fica desligado de propósito: os
        # limites de fala já foram decididos pelo Silero no segmentador, e
        # deixar dois VADs em série só apara sílabas nas pontas.
        segmentos, info = modelo.transcribe(
            audio,
            language=language or self.language,
            beam_size=self.beam_size,
            vad_filter=False,
        )
        segmentos = list(segmentos)
        decorrido = int((time.perf_counter() - t0) * 1000)

        texto = " ".join(s.text.strip() for s in segmentos).strip()

        # avg_logprob é log-probabilidade média por token; exponenciar dá algo
        # entre 0 e 1 que serve para ordenar confiança, não como probabilidade.
        confianca = None
        if segmentos:
            media = sum(s.avg_logprob for s in segmentos) / len(segmentos)
            confianca = float(np.exp(media))

        return TranscriptionResult(
            text=texto,
            language=getattr(info, "language", None),
            confidence=confianca,
            duration_ms=decorrido,
        )
