"""Silero VAD via ONNX — detecção de fala quadro a quadro.

--------------------------------------------------------------------------
Derivado de BasedHardware/omi, `backend/utils/stt/vad.py` (MIT License,
Copyright (c) 2024 Based Hardware Contributors). Cópia da licença em
vendor/OMI_LICENSE. O modelo silero_vad.onnx é do projeto Silero VAD,
também MIT.

Aproveitamos daqui a mecânica de inferência — que tem uma sutileza fácil de
errar, documentada abaixo — e descartamos toda a camada de VAD hospedado,
Redis e telemetria do Omi, que não fazem sentido num app local.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).resolve().parent.parent / "vendor" / "silero" / "silero_vad.onnx"

# Constantes do modelo — Silero VAD v6 (modelo completo, opset 16).
# A 16 kHz o modelo exige janelas de 512 amostras (32 ms), mais uma cauda de
# 64 amostras da janela anterior prefixada em cada chamada.
#
# O contexto é crítico: sem ele o estado recorrente não consegue rastrear a
# fala entre janelas e as probabilidades ficam presas perto de zero. Este é o
# detalhe que faz implementações ingênuas de Silero "não funcionarem".
SAMPLE_RATE = 16_000
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64
_STATE_SHAPE = (2, 1, 128)

_session: ort.InferenceSession | None = None
_init_lock = threading.Lock()


def _get_session() -> ort.InferenceSession:
    """Sessão ONNX compartilhada (singleton, thread-safe).

    `InferenceSession.run()` é thread-safe para dados de entrada diferentes —
    o estado recorrente é passado por chamada, não guardado na sessão.
    """
    global _session
    if _session is not None:
        return _session

    with _init_lock:
        if _session is not None:
            return _session
        if not MODEL_PATH.exists():
            raise FileNotFoundError(f"Modelo Silero VAD não encontrado em {MODEL_PATH}")

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        opts.log_severity_level = 3  # silencia avisos do ORT
        _session = ort.InferenceSession(str(MODEL_PATH), sess_options=opts)
        logger.info("Silero VAD carregado (%s)", MODEL_PATH.name)
        return _session


def make_fresh_state() -> tuple[np.ndarray, np.ndarray]:
    """Estado zerado para um novo stream.

    Retorna (state, context):
      state:   float32 (2, 1, 128) — estado recorrente do ONNX
      context: float32 (1, 64)     — cauda da janela anterior
    """
    return (
        np.zeros(_STATE_SHAPE, dtype=np.float32),
        np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32),
    )


def run_window(
    audio_window: np.ndarray,
    state: np.ndarray,
    context: np.ndarray,
) -> tuple[float, np.ndarray, np.ndarray]:
    """Roda o VAD numa única janela de 512 amostras.

    Args:
        audio_window: float32 (512,) — 16 kHz mono, normalizado em [-1, 1]
        state:        float32 (2, 1, 128)
        context:      float32 (1, 64)

    Returns:
        (probabilidade_de_fala, novo_state, novo_context)
    """
    sess = _get_session()
    audio_2d = audio_window.reshape(1, -1).astype(np.float32)

    # Prefixa o contexto da janela anterior — exigido pelo wrapper ONNX do Silero
    x = np.concatenate([context, audio_2d], axis=1)  # (1, 576)

    saida, novo_state = sess.run(
        None,
        {"input": x, "state": state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)},
    )
    novo_context = audio_2d[:, -CONTEXT_SAMPLES:]
    return float(saida[0][0]), novo_state, novo_context


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    """Converte PCM int16 little-endian para float32 normalizado em [-1, 1]."""
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
