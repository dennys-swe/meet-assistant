"""Testes do segmentador com áudio sintético.

O ponto de ter isto: o pipeline inteiro, do VAD para cima, é verificável sem
placa de som, sem microfone e sem falar nada. Só a camada `recorder` toca no
PipeWire — todo o resto recebe `bytes`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capture.segmenter import Segmenter, SegmenterConfig  # noqa: E402
from domain.audio import BYTES_PER_SECOND, SAMPLE_RATE  # noqa: E402


def silencio(segundos: float) -> bytes:
    return b"\x00\x00" * int(SAMPLE_RATE * segundos)


def _ressonador(sinal: np.ndarray, freq: float, banda: float) -> np.ndarray:
    """Filtro de formante: ressonador IIR de dois polos."""
    r = np.exp(-np.pi * banda / SAMPLE_RATE)
    a1 = 2 * r * np.cos(2 * np.pi * freq / SAMPLE_RATE)
    a2 = -r * r
    saida = np.zeros_like(sinal)
    y1 = y2 = 0.0
    for i in range(sinal.size):
        y = sinal[i] + a1 * y1 + a2 * y2
        saida[i] = y
        y2, y1 = y1, y
    return saida / (np.abs(saida).max() + 1e-9)


def fala_sintetica(segundos: float, seed: int = 1) -> bytes:
    """Voz sintética por modelo fonte-filtro.

    Uma senoide com harmônicos não serve: o Silero é treinado em fala real e
    trata zumbido estacionário como ruído — as probabilidades decaem para
    perto de zero em poucos segundos. Foi assim que a primeira versão deste
    fixture deu falso negativo.

    O que engana o VAD de forma honesta é reproduzir a *estrutura* da voz:
    trem de pulsos glotais com contorno de f0 (declinação + vibrato), três
    formantes, e envelope silábico com pausas reais entre sílabas.
    Rende ~75-95% das janelas acima do limiar, como fala de verdade.
    """
    rng = np.random.default_rng(seed)
    n = int(SAMPLE_RATE * segundos)
    t = np.arange(n) / SAMPLE_RATE

    # Contorno de f0: declina ao longo da frase, com micro-variação
    f0 = (
        120 * (1 - 0.25 * t / max(segundos, 1e-9))
        + 6 * np.sin(2 * np.pi * 3.1 * t)
        + 4 * np.sin(2 * np.pi * 0.7 * t)
    )
    fase = 2 * np.pi * np.cumsum(f0) / SAMPLE_RATE

    fonte = np.zeros(n)
    for k in range(1, 41):
        fonte += np.sin(k * fase) / k
    fonte += 0.03 * rng.standard_normal(n)

    trato = sum(
        ganho * _ressonador(fonte, freq, banda)
        for freq, banda, ganho in [(700, 90, 1.0), (1220, 110, 0.6), (2600, 160, 0.3)]
    )

    # Envelope silábico: sílabas de 120-260ms separadas por pausas de 20-70ms
    envelope = np.zeros(n)
    pos = 0
    while pos < n:
        dur = int(SAMPLE_RATE * rng.uniform(0.12, 0.26))
        fim = min(pos + dur, n)
        envelope[pos:fim] = np.hanning(max(fim - pos, 2))[: fim - pos]
        pos = fim + int(SAMPLE_RATE * rng.uniform(0.02, 0.07))

    sinal = trato * envelope
    sinal = sinal / (np.abs(sinal).max() + 1e-9) * 0.7
    return (sinal * 32767).astype("<i2").tobytes()


def alimentar(seg: Segmenter, pcm: bytes, bloco: int = 1024):
    saidas = []
    for i in range(0, len(pcm), bloco):
        saidas.extend(seg.feed(pcm[i : i + bloco]))
    return saidas


def test_silencio_nao_gera_turno():
    seg = Segmenter()
    assert alimentar(seg, silencio(3.0)) == []
    assert list(seg.flush()) == []


def test_uma_fala_gera_um_turno():
    seg = Segmenter()
    audio = silencio(0.5) + fala_sintetica(2.0) + silencio(1.5)
    turnos = alimentar(seg, audio) + list(seg.flush())

    assert len(turnos) == 1, f"esperava 1 turno, veio {len(turnos)}"
    t = turnos[0]
    assert 1.5 < t.duration < 3.0, f"duração inesperada: {t.duration:.2f}s"


def test_duas_falas_separadas_geram_dois_turnos():
    seg = Segmenter()
    audio = (
        silencio(0.4)
        + fala_sintetica(1.5)
        + silencio(1.5)          # pausa longa: fecha o turno
        + fala_sintetica(1.5)
        + silencio(1.5)
    )
    turnos = alimentar(seg, audio) + list(seg.flush())
    assert len(turnos) == 2, f"esperava 2 turnos, veio {len(turnos)}"


def test_pausa_curta_nao_parte_a_frase():
    """Uma respirada de 300 ms no meio da frase não pode virar dois turnos —
    é exatamente o que quebrava a detecção de pergunta na v1."""
    seg = Segmenter()
    audio = (
        silencio(0.4)
        + fala_sintetica(1.2)
        + silencio(0.3)          # menor que end_silence_ms (700ms)
        + fala_sintetica(1.2)
        + silencio(1.5)
    )
    turnos = alimentar(seg, audio) + list(seg.flush())
    assert len(turnos) == 1, f"a frase foi partida em {len(turnos)} turnos"


def test_pre_roll_preserva_o_inicio_da_fala():
    """O turno deve conter mais áudio do que só o trecho após a detecção."""
    seg = Segmenter()
    audio = silencio(1.0) + fala_sintetica(2.0) + silencio(1.5)
    turnos = alimentar(seg, audio) + list(seg.flush())

    assert len(turnos) == 1
    duracao_pcm = len(turnos[0].pcm) / BYTES_PER_SECOND
    assert duracao_pcm > 1.9, f"pré-roll não preservou o ataque: {duracao_pcm:.2f}s"


def test_estalo_curto_e_descartado():
    seg = Segmenter()
    audio = silencio(0.5) + fala_sintetica(0.1) + silencio(1.5)
    turnos = alimentar(seg, audio) + list(seg.flush())
    assert turnos == [], "um estalo de 100ms não deveria virar turno"


def test_corte_forcado_em_fala_continua():
    seg = Segmenter(SegmenterConfig(max_utterance_s=3.0))
    audio = silencio(0.3) + fala_sintetica(8.0) + silencio(1.5)
    turnos = alimentar(seg, audio) + list(seg.flush())

    assert len(turnos) >= 2, "fala longa deveria ser cortada em vários turnos"
    for t in turnos:
        assert t.duration <= 3.5, f"turno passou do limite: {t.duration:.2f}s"


def test_flush_fecha_turno_em_aberto():
    seg = Segmenter()
    audio = silencio(0.3) + fala_sintetica(2.0)  # acaba falando, sem silêncio final
    turnos = alimentar(seg, audio)
    assert turnos == [], "não deveria fechar sem silêncio"

    pendentes = list(seg.flush())
    assert len(pendentes) == 1, "flush deveria fechar o turno pendente"


if __name__ == "__main__":
    import traceback

    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    falhas = 0
    for teste in testes:
        try:
            teste()
            print(f"  ✅ {teste.__name__}")
        except Exception as e:
            falhas += 1
            print(f"  ❌ {teste.__name__}: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()

    print(f"\n{len(testes) - falhas}/{len(testes)} passaram")
    sys.exit(1 if falhas else 0)
