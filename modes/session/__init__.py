"""Modo Aula: grava a sessão inteira e só ao final resume, extrai insights e tarefas."""

from __future__ import annotations

from modes.session.processor import Cancelado, ProcessorCallbacks, SessionProcessor
from modes.session.recorder import RecordedBlock, SessionRecorder

__all__ = [
    "Cancelado",
    "ProcessorCallbacks",
    "SessionProcessor",
    "RecordedBlock",
    "SessionRecorder",
]
