"""Interface de LLM.

Mesmo papel que `asr/base.py` cumpre para transcrição: o motor do Copilot
fala com esta interface e nunca com um provedor concreto. Assim trocar
OpenRouter por Groq, OpenAI ou Ollama não toca em nenhuma linha do motor —
e os testes rodam contra um LLM falso, sem rede.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator


class LLMError(RuntimeError):
    """Falha ao falar com o provedor, já traduzida para o usuário final.

    A mensagem desta exceção vai direto para a tela — quem for testar o app
    não deve ver um traceback de HTTP nem um código 402 solto.
    """


class LLMClient(ABC):
    """Contrato mínimo de um provedor de LLM."""

    @abstractmethod
    def stream_reply(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = 300,
        temperature: float = 0.3,
    ) -> Iterator[str]:
        """Gera a resposta em pedaços, conforme o modelo produz.

        Streaming não é luxo aqui: com ~2,2s já gastos na transcrição, o que
        salva a sensação de tempo real é a resposta começar a aparecer, não
        ficar pronta. O consumidor pode parar de iterar a qualquer momento
        para cancelar a geração.
        """

    @abstractmethod
    def check(self) -> str | None:
        """Valida chave e modelo com uma chamada mínima.

        Devolve None se está tudo certo, ou uma mensagem de erro pronta para
        exibir. É o que sustenta o botão "Testar conexão" da interface, para
        o usuário descobrir que a chave está errada agora, e não no meio de
        uma reunião.
        """
