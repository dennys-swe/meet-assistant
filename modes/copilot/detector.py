"""Detecção de pergunta em português.

Este é o portão que decide quando o Copilot fala. Ele é deliberadamente
**barato e permissivo**: roda em microssegundos, sem rede, e prefere deixar
passar uma frase duvidosa a perder uma pergunta real.

O filtro fino fica com o LLM, que recebe a instrução de devolver `[IGNORAR]`
quando o turno não pedia resposta. A divisão é econômica: julgar retórica e
intenção exige um modelo, mas mandar todos os turnos para a API custaria uma
chamada a cada frase dita numa reunião. A heurística corta ~90% do volume,
e o modelo resolve o resto.

Trabalhamos sobre o texto do Whisper, que já vem pontuado — então o simples
"termina em ?" resolve a maioria dos casos.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Pronomes e advérbios interrogativos. Precisam de fronteira de palavra: sem
# isso, "como" casaria dentro de "comportamento".
#
# Note que `porque` (uma palavra) NÃO está aqui, só `por que` (duas). Em
# português a grafia junta é conjunção causal — "Porque o líder não quer" é
# afirmação. O Whisper respeita essa distinção, então ela é utilizável.
INTERROGATIVOS = [
    r"qual", r"quais", r"quando", r"onde", r"quem", r"como",
    r"quanto", r"quantos", r"quanta", r"quantas",
    r"por\s+que", r"por\s+quê", r"pra\s+que", r"para\s+que",
    r"o\s+que", r"que\s+que", r"sera\s+que", r"cade",
]

# Pedidos que são perguntas sem ponto de interrogação — muito comuns em fala,
# e justamente os que o "termina em ?" perde.
PEDIDOS = [
    r"me\s+(explica|fala|conta|diz|da\s+um\s+exemplo)",
    r"(pode|poderia|consegue|conseguiria|sabe|saberia)\s+(me\s+)?\w+",
    r"(voce|vc)\s+(sabe|saberia|acha|acredita|conhece|ja\s+\w+)",
    r"(gostaria|queria)\s+de\s+(saber|entender)",
    r"(explica|fala|conta|comenta)\s+(um\s+pouco\s+)?(sobre|de|do|da)",
    r"(fala|conte|conta)\s+(um\s+pouco\s+)?(sobre|de|da|do)",
    r"(qual|como)\s+(e|foi|seria)\s+a\s+sua",
]

# Frases curtas de confirmação. São perguntas gramaticalmente, mas ninguém
# quer o Copilot respondendo "né?" no meio de uma reunião.
MULETAS = {
    "ne", "ta", "certo", "entendeu", "sabe", "viu", "beleza", "ok",
    "tudo bem", "nao e", "e isso", "faz sentido", "concorda",
}

# Hedges: abrem uma frase de incerteza, não um pedido. "Não sei se poderia
# acessar o banco" casa com o padrão de pedido ("poderia acessar") mas é uma
# afirmação. Apareceu em aula real e virou falso positivo.
_RE_HEDGE = re.compile(
    r"\b(nao\s+sei|nao\s+tenho\s+certeza|nao\s+faco\s+ideia|talvez|acho\s+que)\b"
)

MIN_PALAVRAS = 4

# Teto para aceitar uma pergunta sem "?", apoiada só no interrogativo inicial.
# Pergunta falada é curta; passando disso, quase sempre é oração subordinada.
MAX_PALAVRAS_SEM_INTERROGACAO = 15

_RE_INTERROGATIVOS = re.compile(r"\b(" + "|".join(INTERROGATIVOS) + r")\b")
_RE_PEDIDOS = re.compile(r"\b(" + "|".join(PEDIDOS) + r")\b")


@dataclass(frozen=True)
class Detection:
    is_question: bool
    reason: str = ""  # o que disparou — a interface mostra isso ao usuário

    def __bool__(self) -> bool:
        return self.is_question


_RE_FRASE = re.compile(r"[^.!?…]+[.!?…]+|[^.!?…]+$")


def split_sentences(texto: str) -> list[str]:
    """Quebra um turno em frases.

    Existe porque turno de áudio e turno linguístico não são a mesma coisa.
    Quem fala sem pausar gera um bloco de 30 segundos com dez frases dentro —
    e uma pergunta enterrada no meio some, porque o bloco inteiro é longo
    demais para as regras de detecção.

    O Whisper pontua, então a quebra por `.?!` é confiável o bastante.
    """
    frases = [f.strip() for f in _RE_FRASE.findall(texto or "")]
    return [f for f in frases if f]


def _normalizar(texto: str) -> str:
    """Minúsculas, sem acento, espaços colapsados.

    Sem remover acento, "por quê" e "por que" viram padrões diferentes — e o
    Whisper alterna entre as duas grafias na mesma sessão.
    """
    sem_acento = unicodedata.normalize("NFKD", texto.lower())
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento).strip()


class QuestionDetector:
    """Decide se um turno de fala contém uma pergunta dirigida ao usuário."""

    def __init__(self, min_palavras: int = MIN_PALAVRAS):
        self.min_palavras = min_palavras

    def detect(self, texto: str) -> Detection:
        bruto = (texto or "").strip()
        if not bruto:
            return Detection(False)

        normalizado = _normalizar(bruto)
        sem_pontuacao = normalizado.rstrip("?!.,; ")
        palavras = sem_pontuacao.split()

        if sem_pontuacao in MULETAS:
            return Detection(False, "muleta de conversa")

        if len(palavras) < self.min_palavras:
            return Detection(False, f"turno curto ({len(palavras)} palavras)")

        # As últimas palavras carregam a muleta: "isso faz sentido, né?"
        if " ".join(palavras[-2:]) in MULETAS or palavras[-1] in MULETAS:
            # Só salvamos o turno se o interrogativo estiver logo no começo.
            # No meio da frase ele quase sempre é conjunção, não pergunta:
            # "exatamente como você falou, né?" é concordância, enquanto
            # "qual foi o prazo, né?" é pergunta de verdade.
            abertura = " ".join(palavras[:3])
            if not _RE_INTERROGATIVOS.match(abertura):
                return Detection(False, "termina em muleta de confirmação")

        if bruto.endswith("?"):
            return Detection(True, "termina em ?")

        # Sem "?", um pedido explícito ainda é pergunta: "me conta como foi",
        # "você pode explicar". Estes padrões são inequívocos — exceto quando
        # a frase abre com um hedge, que os transforma em incerteza declarada.
        if m := _RE_PEDIDOS.search(sem_pontuacao):
            if _RE_HEDGE.match(sem_pontuacao):
                return Detection(False, "abre com incerteza, não é pedido")
            return Detection(True, f"pedido: '{m.group(0)}'")

        # Interrogativo solto é evidência fraca. Em fala corrida "como",
        # "quando" e "para que" são conjunções na esmagadora maioria das
        # vezes: "quando eu falo de liderança...", "como as coisas
        # acontecem...". Testado contra transcrição real, a regra ingênua
        # deu 100% de falso positivo.
        #
        # Só aceitamos quando ele ABRE o turno e a frase é curta — que é a
        # forma de uma pergunta falada de verdade.
        if _RE_INTERROGATIVOS.match(sem_pontuacao):
            if len(palavras) <= MAX_PALAVRAS_SEM_INTERROGACAO:
                m = _RE_INTERROGATIVOS.match(sem_pontuacao)
                return Detection(True, f"abre com '{m.group(0)}'")
            return Detection(False, "interrogativo no início, mas frase longa demais")

        return Detection(False, "sem marca de pergunta")

    def detect_in_turn(self, texto: str) -> tuple[Detection, str]:
        """Procura uma pergunta dentro de um turno com várias frases.

        Devolve `(detecção, frase)`. Quando mais de uma frase é pergunta,
        vence a última: numa conversa, é a mais recente que está no ar.
        """
        frases = split_sentences(texto)
        if len(frases) <= 1:
            return self.detect(texto), (texto or "").strip()

        for frase in reversed(frases):
            deteccao = self.detect(frase)
            if deteccao.is_question:
                return deteccao, frase

        return Detection(False, "nenhuma frase é pergunta"), (texto or "").strip()
