"""Prompts e extração estruturada do Modo Aula.

Duas preocupações diferentes do Copilot:

1. **A transcrição não cabe num prompt só.** Uma aula de 1h em português gera
   algo como 40-60 mil caracteres de transcrição. Isso estoura o contexto de
   muitos modelos gratuitos (o OpenRouter tem modelo `:free` com 8k tokens de
   contexto) bem antes de sobrar espaço para a resposta. A solução é redução
   hierárquica: divide-se a transcrição em partes, resume-se cada parte
   (`map`), depois resume-se os resumos (`reduce`).

   O limite escolhido é `CHAR_LIMIT_PART = 12_000` caracteres por parte.
   Contas: em português, ~4 caracteres por token é uma aproximação razoável
   para transcrição falada, então 12.000 caracteres ≈ 3.000 tokens. Somando
   o prompt de sistema, as instruções e a resposta pedida (até ~1.000
   tokens), o total fica confortavelmente abaixo de 8k tokens — o teto do
   modelo mais restrito que este app precisa suportar (BYOK: o usuário pode
   trazer qualquer provedor). Um limite maior arriscaria estourar contexto
   exatamente no modelo gratuito mais comum; um limite menor só aumentaria
   o número de chamadas (e o custo) sem ganho de qualidade.

2. **A saída precisa ser estruturada, mas o modelo não é confiável.** Pedimos
   JSON e fazemos o parse tolerando o modelo embrulhar a resposta em
   ```json ... ```` (comportamento comum) ou devolver texto solto ao redor.
   Se nada disso for parseável, a extração degrada para texto puro (o
   resumo vira o texto bruto, listas ficam vazias) em vez de perder a
   chamada inteira — combina com a regra do projeto de nunca esconder erro
   do usuário sem alternativa.
"""

from __future__ import annotations

import json
import logging
import re

from domain.session import Segment

logger = logging.getLogger(__name__)

# Ver justificativa da conta no docstring do módulo.
CHAR_LIMIT_PART = 12_000

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


# ----------------------------------------------------------------------
# Divisão da transcrição em partes
# ----------------------------------------------------------------------


def split_into_parts(
    segments: list[Segment], char_limit: int = CHAR_LIMIT_PART
) -> list[list[Segment]]:
    """Agrupa segmentos em partes de até `char_limit` caracteres de texto.

    Nunca parte um segmento ao meio — se um segmento sozinho excede o limite,
    ele vira uma parte só, maior que a meta. É a mesma lógica de
    `SessionRecorder`: a fronteira de conteúdo real nunca é sacrificada por
    um limite arbitrário.
    """
    partes: list[list[Segment]] = []
    atual: list[Segment] = []
    tamanho_atual = 0

    for seg in segments:
        tamanho_seg = len(seg.text or "")
        if atual and tamanho_atual + tamanho_seg > char_limit:
            partes.append(atual)
            atual = []
            tamanho_atual = 0
        atual.append(seg)
        tamanho_atual += tamanho_seg

    if atual:
        partes.append(atual)

    return partes


def format_part(segments: list[Segment]) -> str:
    """Transcrição de uma parte, com o tempo de cada segmento marcado.

    O marcador `[123.4s]` é o que permite ao modelo apontar `source_time` nas
    tarefas extraídas — pedimos que ele copie o número, não que invente um.
    """
    linhas = [f"[{seg.started_at:.1f}s] {seg.text}" for seg in segments if seg.text.strip()]
    return "\n".join(linhas)


# ----------------------------------------------------------------------
# Prompt de mapeamento: cada parte vira resumo + insights + tópicos + tarefas
# ----------------------------------------------------------------------

MAP_SYSTEM = """Você analisa um trecho da transcrição automática de uma aula ou reunião \
gravada em português do Brasil. A transcrição pode ter erros de reconhecimento — \
interprete a intenção provável em vez de reclamar do texto.

Cada linha começa com um marcador de tempo entre colchetes, como "[123.4s]". Use esse \
número exatamente como aparece quando precisar referenciar o momento de algo.

Devolva SOMENTE um objeto JSON, sem texto antes ou depois, no formato:

{
  "summary": "resumo em prosa deste trecho, 2-4 frases",
  "insights": ["observação ou conclusão relevante", "..."],
  "topics": ["tópico ou assunto abordado", "..."],
  "tasks": [
    {
      "text": "o que precisa ser feito",
      "owner": "responsável, como foi dito, ou null se não foi dito",
      "due": "prazo, EXATAMENTE como foi dito (ex: 'até sexta'), sem calcular data, ou null",
      "source_time": 123.4
    }
  ]
}

Regras importantes:
- "due" nunca é uma data calculada por você. Copie a expressão usada por quem falou.
- "source_time" é o número do marcador de tempo da linha onde a tarefa foi mencionada.
- Se não houver tarefas, insights ou tópicos neste trecho, devolva listas vazias.
- Se o trecho não tiver conteúdo substancial, "summary" pode ser uma frase curta dizendo isso.
"""


def build_map_messages(part_text: str) -> tuple[str, list[dict[str, str]]]:
    mensagens = [{"role": "user", "content": f"Trecho da transcrição:\n\n{part_text}"}]
    return MAP_SYSTEM, mensagens


# ----------------------------------------------------------------------
# Prompt de redução: uma lista de resumos parciais vira o resumo final
# ----------------------------------------------------------------------

REDUCE_SYSTEM = """Você recebe uma lista de resumos parciais de uma aula ou reunião longa, \
cada um cobrindo um trecho sucessivo da gravação (em ordem cronológica). Combine-os num \
resumo único, coerente e sem repetição, em português do Brasil.

Devolva SOMENTE um objeto JSON no formato:

{"summary": "resumo final em prosa, alguns parágrafos"}
"""


def build_reduce_messages(partial_summaries: list[str]) -> tuple[str, list[dict[str, str]]]:
    corpo = "\n\n".join(f"Trecho {i + 1}:\n{s}" for i, s in enumerate(partial_summaries) if s)
    mensagens = [{"role": "user", "content": corpo}]
    return REDUCE_SYSTEM, mensagens


# ----------------------------------------------------------------------
# Parse tolerante de JSON
# ----------------------------------------------------------------------


def parse_json_relaxed(texto: str) -> dict | None:
    """Extrai um objeto JSON de uma resposta de LLM, tolerando embrulhos.

    Tenta, em ordem: o texto puro; o conteúdo de um bloco ```` ```json ```` ou
    ```` ``` ````; e por fim o trecho entre a primeira `{` e a última `}` do
    texto (cobre o caso do modelo escrever uma frase de preâmbulo antes do
    JSON, apesar de instruído a não fazer isso). Devolve `None` se nada for
    parseável — quem chama decide como degradar.
    """
    if not texto or not texto.strip():
        return None

    candidatos = [texto.strip()]

    fence = _JSON_FENCE.search(texto)
    if fence:
        candidatos.append(fence.group(1).strip())

    inicio = texto.find("{")
    fim = texto.rfind("}")
    if inicio != -1 and fim != -1 and fim > inicio:
        candidatos.append(texto[inicio : fim + 1])

    for candidato in candidatos:
        try:
            dado = json.loads(candidato)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(dado, dict):
            return dado

    logger.warning("Resposta do LLM não era JSON parseável; degradando para texto puro")
    return None


def normalize_map_result(parsed: dict | None, raw_text: str) -> dict:
    """Preenche as chaves esperadas, tolerando um JSON parcial ou ausente."""
    if parsed is None:
        return {"summary": raw_text.strip(), "insights": [], "topics": [], "tasks": []}

    def _lista_de_str(chave: str) -> list[str]:
        valor = parsed.get(chave)
        if not isinstance(valor, list):
            return []
        return [str(v).strip() for v in valor if str(v).strip()]

    tarefas = []
    for item in parsed.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        texto = str(item.get("text") or "").strip()
        if not texto:
            continue
        source_time = item.get("source_time")
        try:
            source_time = float(source_time) if source_time is not None else None
        except (TypeError, ValueError):
            source_time = None
        tarefas.append(
            {
                "text": texto,
                "owner": (str(item["owner"]).strip() or None) if item.get("owner") else None,
                "due": (str(item["due"]).strip() or None) if item.get("due") else None,
                "source_time": source_time,
            }
        )

    resumo = parsed.get("summary")
    resumo = str(resumo).strip() if resumo else raw_text.strip()

    return {
        "summary": resumo,
        "insights": _lista_de_str("insights"),
        "topics": _lista_de_str("topics"),
        "tasks": tarefas,
    }
