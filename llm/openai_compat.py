"""Cliente para qualquer API compatível com o formato OpenAI.

Um cliente só atende OpenRouter, Groq, OpenAI e Ollama, porque todos expõem
`POST /chat/completions` com streaming por SSE. Trocar de provedor é trocar
`base_url` — não há SDK por provedor, nem uma classe por serviço.

Falamos HTTP direto com `httpx` em vez de usar o SDK da OpenAI: são ~60
linhas, evita uma dependência pesada, e nos deixa traduzir cada erro para
uma frase que o usuário final entenda.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator

import httpx

from llm.base import LLMClient, LLMError

logger = logging.getLogger(__name__)

TIMEOUT_CONEXAO = 10.0
TIMEOUT_LEITURA = 60.0

# Modelos gratuitos limitam por minuto; 429 é rotina. Poucas tentativas e
# curtas: numa conversa ao vivo, uma resposta que chega 30s depois já não
# serve para nada — melhor desistir e avisar.
MAX_TENTATIVAS_429 = 3
ESPERA_MAXIMA_429 = 6.0


class OpenAICompatClient(LLMClient):
    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        app_name: str = "meet-assistant",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.model = model
        self.app_name = app_name

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # O OpenRouter usa estes cabeçalhos para atribuir uso ao app; os
        # demais provedores simplesmente ignoram.
        if "openrouter" in self.base_url:
            headers["X-Title"] = self.app_name
            headers["HTTP-Referer"] = "https://github.com/dennys-swe/meet-assistant"
        return headers

    def _payload(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }

    # ------------------------------------------------------------------
    # Tradução de erros
    # ------------------------------------------------------------------

    def _erro_http(self, status: int, corpo: str) -> LLMError:
        detalhe = ""
        try:
            dados = json.loads(corpo)
            detalhe = (dados.get("error") or {}).get("message", "") if isinstance(dados.get("error"), dict) else ""
        except (json.JSONDecodeError, AttributeError):
            detalhe = corpo[:200]

        mensagens = {
            401: "Chave de API inválida ou expirada. Confira em Configurações.",
            403: "Chave sem permissão para este modelo.",
            402: "Sem créditos no provedor. Use um modelo :free ou adicione saldo.",
            429: (
                "Cota diária de modelos gratuitos esgotada — esperar não resolve, "
                "ela só renova amanhã. Troque para um modelo pago em Configurações "
                "(custa frações de centavo por resposta) ou use outro provedor."
            )
            if _e_limite_diario(corpo)
            else "Muitas requisições seguidas. Aguarde alguns segundos.",
            404: f"Modelo '{self.model}' não encontrado neste provedor.",
        }
        base = mensagens.get(status)
        if base is None:
            base = f"O provedor respondeu com erro {status}."
        return LLMError(f"{base} {detalhe}".strip())

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def stream_reply(
        self,
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int = 300,
        temperature: float = 0.3,
    ) -> Iterator[str]:
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(system, messages, max_tokens, temperature, stream=True)
        timeout = httpx.Timeout(TIMEOUT_LEITURA, connect=TIMEOUT_CONEXAO)

        try:
            with httpx.Client(timeout=timeout) as client:
                for tentativa in range(MAX_TENTATIVAS_429):
                    with client.stream("POST", url, headers=self._headers(), json=payload) as r:
                        # 429 em modelo gratuito é rotina, não exceção: os
                        # provedores limitam por minuto. Esperar um pouco
                        # resolve, e é melhor do que estampar um erro
                        # vermelho na cara de quem está numa reunião.
                        if r.status_code == 429 and tentativa < MAX_TENTATIVAS_429 - 1:
                            r.read()
                            # Cota diária esgotada não passa com espera: só
                            # vira à meia-noite. Repetir aqui só empurra o
                            # usuário para uma sala de espera sem saída.
                            if _e_limite_diario(r.text):
                                raise self._erro_http(429, r.text)
                            espera = self._espera_do_429(r, tentativa)
                            logger.info("429 do provedor; nova tentativa em %.1fs", espera)
                            time.sleep(espera)
                            continue

                        if r.status_code >= 400:
                            r.read()
                            raise self._erro_http(r.status_code, r.text)

                        for linha in r.iter_lines():
                            pedaco = self._parse_sse(linha)
                            if pedaco is _FIM:
                                return
                            if pedaco:
                                yield pedaco
                        return

        except httpx.TimeoutException as e:
            raise LLMError("O provedor não respondeu a tempo.") from e
        except httpx.ConnectError as e:
            alvo = "o Ollama está rodando?" if "localhost" in self.base_url else "verifique sua internet."
            raise LLMError(f"Não foi possível conectar ao provedor — {alvo}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Falha de rede ao falar com o provedor: {e}") from e

    @staticmethod
    def _espera_do_429(resposta: httpx.Response, tentativa: int) -> float:
        """Quanto esperar antes de repetir, respeitando o provedor.

        Se ele mandou `Retry-After`, obedecemos — ele sabe melhor que nós.
        Senão, backoff exponencial com teto, porque numa conversa ao vivo não
        adianta esperar meio minuto.
        """
        cabecalho = resposta.headers.get("retry-after")
        if cabecalho:
            try:
                return min(float(cabecalho), ESPERA_MAXIMA_429)
            except ValueError:
                pass
        return min(1.5 * (2**tentativa), ESPERA_MAXIMA_429)

    @staticmethod
    def _parse_sse(linha: str) -> str | None | object:
        """Extrai o texto de uma linha SSE. Devolve _FIM ao ver [DONE]."""
        if not linha or not linha.startswith("data:"):
            return None

        dados = linha[5:].strip()
        if dados == "[DONE]":
            return _FIM

        try:
            evento = json.loads(dados)
        except json.JSONDecodeError:
            return None

        escolhas = evento.get("choices") or []
        if not escolhas:
            return None
        return (escolhas[0].get("delta") or {}).get("content") or None

    def check(self) -> str | None:
        """Uma chamada mínima, sem streaming, só para validar chave e modelo."""
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(
            "Responda apenas: ok",
            [{"role": "user", "content": "ok"}],
            max_tokens=5,
            temperature=0.0,
            stream=False,
        )

        try:
            t0 = time.perf_counter()
            r = httpx.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=httpx.Timeout(20.0, connect=TIMEOUT_CONEXAO),
            )
            if r.status_code >= 400:
                return str(self._erro_http(r.status_code, r.text))
            logger.info("Conexão validada em %.1fs", time.perf_counter() - t0)
            return None

        except httpx.TimeoutException:
            return "O provedor não respondeu a tempo."
        except httpx.ConnectError:
            alvo = "o Ollama está rodando?" if "localhost" in self.base_url else "verifique sua internet."
            return f"Não foi possível conectar — {alvo}"
        except httpx.HTTPError as e:
            return f"Falha de rede: {e}"


_FIM = object()


def _e_limite_diario(corpo: str) -> bool:
    """Distingue teto diário de limite por minuto na resposta 429.

    Os dois chegam como 429, mas exigem reações opostas: o de minuto passa
    com alguns segundos de espera, o diário só renova no dia seguinte. Tratar
    os dois igual fazia o app repetir em vão e pedir ao usuário que
    "aguardasse um minuto" por algo que levaria horas.
    """
    texto = (corpo or "").lower()
    return any(m in texto for m in ("per-day", "per day", "daily", "free-models-per"))


def from_settings(settings) -> OpenAICompatClient:
    """Constrói o cliente a partir de `config.settings.Settings`."""
    return OpenAICompatClient(
        base_url=settings.base_url,
        api_key=settings.api_key,
        model=settings.model,
    )
