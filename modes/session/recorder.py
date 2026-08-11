"""SessionRecorder — acumula turnos da gravação e os agrupa em blocos.

Existe por causa de um número medido em `asr/whisper_local.py`: com o perfil
"aula" (large-v3-turbo), cada chamada ao Whisper custa ~10s de piso
independente do tamanho do áudio. Transcrever `Utterance` a `Utterance`, como
o Copilot faz, pagaria esse piso a cada turno — inviável para uma aula de 1h
cheia de turnos curtos. Agrupando em blocos de ~30s antes de transcrever, o
custo fixo se dilui e o RTF medido cai para 0,44.

O agrupamento nunca corta um turno: um bloco fecha quando a soma das durações
dos turnos que ele contém alcança `block_seconds`, e o turno que cruzou a
marca fica inteiro dentro do bloco (o próximo bloco só começa no turno
seguinte). Um turno sozinho mais longo que `block_seconds` vira um bloco só,
maior que a meta — não há como evitar isso sem partir fala ao meio.

Sobre pausa/retomada: a captura (`CapturePipeline`) nunca para (regra do
projeto — ver AGENTS.md item 5), então o relógio embutido em cada
`Utterance.started_at` é o tempo de vida do pipeline, não o tempo de gravação
da sessão. Se o usuário pausa a aula por 10 minutos para o intervalo, os
turnos que chegarem depois de retomar viriam com um salto de 10 minutos no
relógio do pipeline. Por isso o recorder mantém seu próprio deslocamento
(`_raw_offset`) e o recalibra a cada `resume()`: o próximo turno depois da
pausa é ancorado exatamente onde o último turno antes dela parou, e o hiato
correspondente à pausa desaparece da linha do tempo da sessão. Enquanto
pausado, turnos recebidos são descartados — a UI não deveria mais estar
inscrita no pipeline nesse momento, mas o recorder não confia nisso.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from domain.audio import BYTES_PER_SECOND, Utterance
from domain.session import Session

logger = logging.getLogger(__name__)

BLOCO_SEGUNDOS_PADRAO = 30.0


@dataclass
class RecordedBlock:
    """Um bloco de ~30s de áudio pronto para transcrever de uma vez.

    `started_at`/`ended_at` já estão na linha do tempo da sessão (segundos
    desde o início, descontando pausas) — é o que vira `Segment.started_at`/
    `ended_at` depois de transcrito.
    """

    pcm: bytes
    started_at: float
    ended_at: float
    utterance_count: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.ended_at - self.started_at

    @property
    def audio_duration(self) -> float:
        """Duração real do PCM, útil para conferir contra `duration`."""
        return len(self.pcm) / BYTES_PER_SECOND


class SessionRecorder:
    """Recebe `Utterance` do `CapturePipeline` e entrega blocos ao processador.

    Não captura áudio, não transcreve, não fala com o LLM — só acumula e
    agrupa. `add()` é chamado da thread do pipeline; todo o estado é
    protegido por um lock simples, no mesmo espírito do `InMemoryRepository`.
    """

    def __init__(self, block_seconds: float = BLOCO_SEGUNDOS_PADRAO):
        if block_seconds <= 0:
            raise ValueError("block_seconds precisa ser positivo")
        self.block_seconds = block_seconds

        self._lock = threading.Lock()
        self._session: Session | None = None
        self._blocks: list[RecordedBlock] = []

        self._raw_offset: float | None = None
        self._recalibrar = True
        self._last_session_end = 0.0
        self._paused = False

        self._reset_bloco_atual()

    # ------------------------------------------------------------------

    def start(self, session: Session) -> None:
        """Começa (ou reinicia) a gravação para `session`."""
        with self._lock:
            self._session = session
            self._blocks = []
            self._raw_offset = None
            self._recalibrar = True
            self._last_session_end = 0.0
            self._paused = False
            self._reset_bloco_atual()
        logger.info("SessionRecorder iniciado para a sessão %s", session.id)

    def pause(self) -> None:
        """Suspende o cronômetro da sessão. Turnos recebidos são descartados."""
        with self._lock:
            self._paused = True
        logger.info("Gravação pausada")

    def resume(self) -> None:
        """Retoma o cronômetro. O hiato da pausa não entra na linha do tempo."""
        with self._lock:
            self._paused = False
            self._recalibrar = True
        logger.info("Gravação retomada")

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def pending_blocks(self) -> int:
        """Blocos já fechados, aguardando `stop()`. Só para inspeção/testes."""
        with self._lock:
            return len(self._blocks)

    def add(self, utterance: Utterance) -> None:
        """Recebe um turno transcrito ou cru; só o PCM e os tempos importam."""
        if not utterance.pcm:
            return

        with self._lock:
            if self._session is None:
                logger.warning("add() chamado antes de start(); turno ignorado")
                return
            if self._paused:
                return

            if self._recalibrar or self._raw_offset is None:
                self._raw_offset = utterance.started_at - self._last_session_end
                self._recalibrar = False

            inicio = utterance.started_at - self._raw_offset
            fim = utterance.ended_at - self._raw_offset
            # Trava de segurança: o relógio nunca deveria andar para trás, mas
            # um turno fora de ordem não pode corromper a linha do tempo.
            inicio = max(inicio, self._last_session_end)
            fim = max(fim, inicio)

            if self._bloco_inicio is None:
                self._bloco_inicio = inicio
            self._bloco_fim = fim
            self._bloco_pcm.append(utterance.pcm)
            self._bloco_contagem += 1
            self._bloco_duracao += fim - inicio
            self._last_session_end = fim

            if self._bloco_duracao >= self.block_seconds:
                self._fechar_bloco()

    def stop(self) -> list[RecordedBlock]:
        """Fecha o bloco em andamento e devolve todos os blocos acumulados."""
        with self._lock:
            self._fechar_bloco()
            blocos = self._blocks
            self._blocks = []
            return blocos

    # ------------------------------------------------------------------

    def _fechar_bloco(self) -> None:
        if not self._bloco_pcm:
            return
        bloco = RecordedBlock(
            pcm=b"".join(self._bloco_pcm),
            started_at=self._bloco_inicio,
            ended_at=self._bloco_fim,
            utterance_count=self._bloco_contagem,
        )
        self._blocks.append(bloco)
        self._reset_bloco_atual()

    def _reset_bloco_atual(self) -> None:
        self._bloco_pcm: list[bytes] = []
        self._bloco_inicio: float | None = None
        self._bloco_fim: float | None = None
        self._bloco_duracao = 0.0
        self._bloco_contagem = 0
