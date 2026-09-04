"""Testes da detecção de pergunta. Sem rede, sem áudio, sem modelo."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modes.copilot.detector import QuestionDetector, split_sentences  # noqa: E402

det = QuestionDetector()

PERGUNTAS = [
    "Qual é a sua experiência com sistemas distribuídos?",
    "Como você lidaria com um conflito dentro do time?",
    "Por que você quer trabalhar aqui?",
    "Você pode me explicar como funciona o deploy?",
    "Me conta um pouco sobre o seu último projeto",       # sem "?"
    "Gostaria de saber qual foi o maior desafio técnico",  # sem "?"
    "O que te motivou a mudar de área?",
    "Quanto tempo você levou para entregar isso?",
    "Você já trabalhou com Kubernetes em produção?",
    "Fala um pouco sobre a sua stack preferida",
    "Onde você se vê daqui a cinco anos?",
    "Será que dá para escalar isso horizontalmente?",
]

NAO_PERGUNTAS = [
    "Então a gente começou o projeto em janeiro do ano passado.",
    "Isso faz muito sentido, obrigado pela explicação.",
    "né?",
    "certo?",
    "tá?",
    "Beleza, entendi perfeitamente o ponto.",
    "A arquitetura usa filas para desacoplar os serviços.",
    "Eu trabalhei cinco anos com backend em Python.",
    "Legal, isso é bem interessante mesmo.",
    "Bom dia a todos, vamos começar a reunião.",
    "Isso aí, exatamente como você falou, né?",
    "",
]


def test_perguntas_sao_detectadas():
    falhas = [t for t in PERGUNTAS if not det.detect(t).is_question]
    assert not falhas, f"não detectou como pergunta: {falhas}"


def test_nao_perguntas_sao_ignoradas():
    falhas = [(t, det.detect(t).reason) for t in NAO_PERGUNTAS if det.detect(t).is_question]
    assert not falhas, f"falso positivo: {falhas}"


def test_muletas_curtas_nao_disparam():
    for t in ["né?", "tá?", "certo?", "entendeu?", "faz sentido?"]:
        assert not det.detect(t).is_question, t


def test_muleta_no_fim_nao_dispara():
    t = "A gente entregou isso na sprint passada, não é?"
    assert not det.detect(t).is_question


def test_pergunta_real_com_muleta_no_fim_ainda_dispara():
    """'Qual foi o prazo, né?' continua sendo uma pergunta de verdade."""
    assert det.detect("Qual foi o prazo combinado com o cliente, né?").is_question


def test_acento_nao_importa():
    assert det.detect("Por que voce escolheu essa abordagem").is_question
    assert det.detect("Por quê você escolheu essa abordagem?").is_question


def test_interrogativo_dentro_de_palavra_nao_conta():
    """'como' em 'comportamento' não pode disparar."""
    d = det.detect("O comportamento do sistema mudou bastante depois disso.")
    assert not d.is_question, d.reason


# Turnos reais transcritos pelo pipeline a partir de uma palestra. A regra
# ingênua (qualquer interrogativo em qualquer posição) marcava os quatro como
# pergunta — 100% de falso positivo em fala corrida. Ficam aqui como
# regressão: são exatamente o tipo de frase que o Copilot vai ouvir o dia todo.
FALA_CORRIDA_REAL = [
    "para que nunca jogou com ele, mas sempre contra ele. Mediram Tom Brady "
    "de fora para dentro. Conte ele era rápido, ele não era rápido.",
    "Agora, quando eu falo de pessoas, de liderança, eu falo a base disso "
    "tudo, é a Primeira, a Integridade, Transparência.",
    "Você é muito talentosa, mas se você desconfiar um pouco do processo, "
    "como as coisas acontecem, atenção.",
    "Porque o líder não quer fazer...",
]


def test_fala_corrida_real_nao_dispara():
    falhas = [(t[:50], det.detect(t).reason) for t in FALA_CORRIDA_REAL if det.detect(t).is_question]
    assert not falhas, f"falso positivo em fala real: {falhas}"


def test_porque_junto_e_conjuncao_por_que_separado_e_pergunta():
    assert not det.detect("Porque o time não conseguiu entregar no prazo").is_question
    assert det.detect("Por que o time não conseguiu entregar?").is_question


def test_hedge_nao_e_pedido():
    """'Não sei se poderia acessar' casa com o padrão de pedido, mas é
    incerteza declarada. Falso positivo real, capturado em aula."""
    assert not det.detect(
        "Não sei se poderia acessar, não sei se o SAP tem suporte para "
        "acessar diretamente o banco e fazer um insert lá."
    ).is_question
    assert not det.detect("Acho que você pode usar uma fila aqui").is_question
    # mas o pedido de verdade continua passando
    assert det.detect("Você poderia explicar como funciona esse insert").is_question


def test_interrogativo_no_meio_da_frase_nao_dispara():
    assert not det.detect("Eu expliquei como a arquitetura foi montada naquele projeto").is_question


def test_split_sentences():
    f = split_sentences("Isso é uma frase. E outra! Uma pergunta? Sem ponto final")
    assert len(f) == 4, f
    assert f[2] == "Uma pergunta?"
    assert split_sentences("") == []


def test_pergunta_enterrada_em_turno_longo():
    """Quem fala sem pausar gera blocos com várias frases. Uma pergunta no
    meio some se olharmos só o bloco inteiro — motivo da detecção por frase."""
    bloco = (
        "Então a gente vinha discutindo isso há um tempo. E aí eu fiquei pensando. "
        "Qual foi a maior dificuldade que você encontrou nesse processo? Porque não é simples."
    )
    assert not det.detect(bloco).is_question, "o bloco inteiro não deveria casar"

    d, frase = det.detect_in_turn(bloco)
    assert d.is_question
    assert frase == "Qual foi a maior dificuldade que você encontrou nesse processo?"


def test_turno_longo_sem_pergunta_continua_negativo():
    bloco = (
        "uma história, mas entendeu fenômeno social. Eu tô preocupado com como é que a "
        "desigualdade social brasileira se dá no dia a dia e como é que os ricos fazem. "
        "Porque tem de mais legal na pesquisa antropológica que a capacidade que ela tem."
    )
    d, _ = det.detect_in_turn(bloco)
    assert not d.is_question, d.reason


def test_ultima_pergunta_do_turno_vence():
    bloco = "Qual é o seu nome? E de onde você veio?"
    d, frase = det.detect_in_turn(bloco)
    assert d.is_question
    assert frase == "E de onde você veio?"


def test_reason_e_preenchida():
    assert det.detect("Qual é o seu maior desafio?").reason
    assert det.detect("Isso foi entregue ontem.").reason


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
