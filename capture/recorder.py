"""Captura de PCM cru via `pw-record`.

Decisão de projeto: em vez de PyAudio/PortAudio, falamos com o PipeWire pelo
utilitário `pw-record`, lendo PCM da stdout do processo.

O motivo é prático. PyAudio exige `portaudio19-dev`, compila C na instalação,
e enxerga os monitores do PipeWire de forma inconsistente através da camada de
compatibilidade do Pulse. O `pw-record` já vem com o PipeWire, aceita o nome
do node direto (incluindo monitores), e faz a conversão para 16 kHz mono int16
dentro do próprio servidor de áudio — que é o formato exigido pelo VAD.

Efeito colateral bem-vindo: a captura vira um `bytes` num pipe, o que torna
todo o resto do pipeline testável sem placa de som.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections.abc import Iterator

from domain.audio import CHANNELS, SAMPLE_RATE
from capture.sources import AudioSource

logger = logging.getLogger(__name__)

# 32 ms de áudio. Casa exatamente com a janela de 512 amostras do Silero,
# então o VAD recebe blocos inteiros sem precisar remontar nada.
READ_CHUNK_SAMPLES = 512
READ_CHUNK_BYTES = READ_CHUNK_SAMPLES * 2


class RecorderError(RuntimeError):
    pass


class PipeWireRecorder:
    """Lê PCM 16 kHz mono int16 de uma fonte do PipeWire."""

    def __init__(self, source: AudioSource, latency: str = "32ms"):
        self.source = source
        self.latency = latency
        self._proc: subprocess.Popen[bytes] | None = None
        self._stop = threading.Event()
        self._stderr: list[bytes] = []
        self._prefixo = b""

    def _comando(self) -> list[str]:
        cmd = [
            "pw-record",
            "--target", self.source.node_name,
        ]
        # Sem isto o pw-record ignora o --target apontado para um sink e cai
        # silenciosamente no microfone padrão — grava som, parece funcionar, e
        # captura a coisa errada. Custou uma sessão de depuração descobrir.
        if self.source.is_monitor:
            cmd += ["-P", "stream.capture.sink=true"]
        cmd += [
            "--rate", str(SAMPLE_RATE),
            "--channels", str(CHANNELS),
            "--format", "s16",
            "--latency", self.latency,
            "-",  # stdout
        ]
        return cmd

    def _consumir_cabecalho_au(self, stdout) -> None:
        """Descarta o cabeçalho AU que o pw-record escreve na stdout.

        Escrevendo para `-`, o pw-record não emite PCM cru: ele emite um
        container AU (Sun/NeXT), cujo cabeçalho começa com o magic ".snd" e
        declara onde os dados de fato começam. Ler esses bytes como áudio
        injeta um estalo no início de toda captura.
        """
        cabecalho = stdout.read(24)
        if len(cabecalho) < 24:
            raise RecorderError("pw-record encerrou antes de emitir o cabeçalho")

        magic = cabecalho[:4]
        if magic not in (b".snd", b"dns."):
            # Versão do pw-record que já entrega PCM cru: devolvemos os bytes
            # ao fluxo em vez de perdê-los.
            self._prefixo = cabecalho
            return

        ordem = "big" if magic == b".snd" else "little"
        offset = int.from_bytes(cabecalho[4:8], ordem)
        if offset > 24:
            stdout.read(offset - 24)  # campos extras/anotação
        self._prefixo = b""

    def _drenar_stderr(self) -> None:
        """Consome stderr numa thread para o processo não travar no buffer."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for linha in proc.stderr:
            self._stderr.append(linha)
            logger.debug("pw-record: %s", linha.decode(errors="replace").rstrip())

    def stream(self) -> Iterator[bytes]:
        """Gera blocos de PCM até `stop()` ser chamado.

        Cada bloco tem exatamente READ_CHUNK_BYTES, salvo o último.
        """
        if shutil.which("pw-record") is None:
            raise RecorderError(
                "pw-record não encontrado. Instale com: sudo apt install pipewire-bin"
            )

        self._stop.clear()
        self._stderr.clear()

        logger.info("Capturando de: %s", self.source)
        self._proc = subprocess.Popen(
            self._comando(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        threading.Thread(target=self._drenar_stderr, daemon=True).start()

        assert self._proc.stdout is not None
        try:
            self._consumir_cabecalho_au(self._proc.stdout)
            if self._prefixo:
                yield self._prefixo
                self._prefixo = b""

            while not self._stop.is_set():
                bloco = self._proc.stdout.read(READ_CHUNK_BYTES)
                if not bloco:
                    break
                yield bloco
        finally:
            self._encerrar()

        # Se morreu sozinho e não fomos nós que pedimos, o stderr explica.
        codigo = self._proc.returncode
        if codigo not in (0, None, -15) and not self._stop.is_set():
            detalhe = b"".join(self._stderr).decode(errors="replace").strip()
            raise RecorderError(
                f"pw-record encerrou com código {codigo}. {detalhe or 'Sem detalhes.'}"
            )

    def _encerrar(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.warning("pw-record não respondeu ao terminate; matando")
            proc.kill()
            proc.wait(timeout=2)
        logger.info("Captura encerrada")

    def stop(self) -> None:
        self._stop.set()
