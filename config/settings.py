"""Configuração do usuário — modelo BYOK (bring your own key).

O app é feito para ser testado por terceiros, então nenhuma chave vai no
código nem no repositório: cada pessoa põe a sua na primeira execução, e ela
fica no diretório de config do usuário, não no projeto.

O padrão é OpenRouter porque uma única chave dá acesso a dezenas de modelos,
incluindo variantes `:free` — quem for testar não precisa gastar nada. Como
o OpenRouter fala o protocolo da OpenAI, o mesmo cliente atende qualquer
provedor compatível só trocando a `base_url`.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "meet-assistant"
CONFIG_PATH = CONFIG_DIR / "config.json"


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    default_model: str
    needs_key: bool = True
    signup_url: str = ""


# A ordem importa: é a ordem que aparece na tela de configuração.
PROVIDERS: dict[str, Provider] = {
    # Modelo padrão escolhido por medição, não por reputação. Critérios, nesta
    # ordem: primeiro token rápido (é o que dá sensação de tempo real),
    # resposta direta em pt-BR, e obediência ao marcador [IGNORAR].
    #
    # Os Nemotron da NVIDIA foram descartados apesar de gratuitos: são modelos
    # de raciocínio e vazam o "pensamento" na resposta, em inglês
    # ("We need to respond in Portuguese...") — inútil para ler de relance.
    "openrouter": Provider(
        key="openrouter",
        label="OpenRouter (recomendado)",
        base_url="https://openrouter.ai/api/v1",
        default_model="google/gemma-4-26b-a4b-it:free",
        signup_url="https://openrouter.ai/keys",
    ),
    "groq": Provider(
        key="groq",
        label="Groq (rápido, tem tier grátis)",
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        signup_url="https://console.groq.com/keys",
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        signup_url="https://platform.openai.com/api-keys",
    ),
    "ollama": Provider(
        key="ollama",
        label="Ollama (local, sem chave)",
        base_url="http://localhost:11434/v1",
        default_model="llama3.1:8b",
        needs_key=False,
    ),
    "custom": Provider(
        key="custom",
        label="Outro (compatível com OpenAI)",
        base_url="",
        default_model="",
    ),
}


@dataclass
class Settings:
    provider: str = "openrouter"
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    # "Contexto base" do Copilot: currículo, vaga, matéria da aula. Fica aqui
    # para sobreviver entre sessões — na v1 sumia a cada reinício.
    user_context: str = ""

    language: str = "pt"
    whisper_model: str = "small"

    def __post_init__(self) -> None:
        preset = PROVIDERS.get(self.provider)
        if preset:
            self.base_url = self.base_url or preset.base_url
            self.model = self.model or preset.default_model

    @property
    def preset(self) -> Provider | None:
        return PROVIDERS.get(self.provider)

    def validate(self) -> str | None:
        """Devolve a primeira pendência em texto legível, ou None se está ok."""
        if not self.base_url:
            return "Informe a URL da API do provedor."
        if not self.model:
            return "Informe o modelo."
        preset = self.preset
        precisa_chave = preset.needs_key if preset else True
        if precisa_chave and not self.api_key.strip():
            return "Informe sua chave de API."
        return None

    @property
    def is_ready(self) -> bool:
        return self.validate() is None


def load() -> Settings:
    """Lê a config do usuário. Devolve os padrões se ainda não existe."""
    if not CONFIG_PATH.exists():
        return Settings()

    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Config inválida em %s (%s). Usando padrões.", CONFIG_PATH, e)
        return Settings()

    conhecidos = {f for f in Settings.__dataclass_fields__}
    return Settings(**{k: v for k, v in dados.items() if k in conhecidos})


def save(settings: Settings) -> Path:
    """Grava a config com permissão 600 — o arquivo contém uma chave de API."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Cria já restrito, em vez de gravar e depois apertar: evita a janela em
    # que a chave fica legível para outros usuários da máquina.
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(asdict(settings), f, indent=2, ensure_ascii=False)

    os.chmod(CONFIG_PATH, 0o600)  # garante 600 mesmo se o arquivo já existia
    logger.info("Configuração salva em %s", CONFIG_PATH)
    return CONFIG_PATH


def mask_key(chave: str) -> str:
    """Forma segura de exibir uma chave em log ou na interface."""
    limpa = chave.strip()
    if len(limpa) <= 8:
        return "•" * len(limpa)
    return f"{limpa[:4]}{'•' * 8}{limpa[-4:]}"
