# Onde paramos

## Estado do git

```
ad8b069  e2e: harness de teste com audio real e estrategia de testes
b2b3001  fecha a corrida em add_segments e leva o falante em on_turn
160a60c  modo Aula (agente C)
cb85a4b  modes/session
7daaf9c  nota de retomada
aaad1a3  copilot: microfone como segunda fonte (agente D)
cffe354  exporters (agente B)
15a7f1e  storage (agente A)
```

Tudo commitado. **As duas pendências da revisão foram corrigidas** — não
precisa mais rodar revisor em cima delas.

## Testes

`92/92` nas unidades. Rode assim (não há pytest no venv; cada arquivo é um
runner):

```bash
for f in tests/test_*.py; do .venv/bin/python "$f"; done
```

Quatro testes novos. O que prova a correção da corrida no SQLite é
determinístico — para a thread A dentro da fresta entre o insert e a releitura
e verifica se a B consegue escrever. **Confirmado que ele falha no código
antigo**; martelar com 12 threads não falhava, que era o problema do
diagnóstico original.

Camada nova de teste end-to-end: `e2e_copilot.py` e `TESTES.md`.

## O que o teste com áudio real mostrou

10 minutos de Roda Viva tocados no sink e capturados pelo monitor
(`runs/2026-08-11T17-58-33/`):

| métrica | valor |
|---|---|
| turnos | 88 |
| fala capturada | 570 s de 600 s |
| RTF mediano | **0,22** — folga de 4,5× |
| ASR mediano | 1,6 s |

O caminho `pw-record` → VAD → Whisper → detector aguenta fala contínua com
sobra. O caminho de rede também foi exercitado (4 chamadas reais ao
OpenRouter, streaming e teto funcionando, ~US$ 0,0002).

## Próximo passo: o detector

É o único ponto fraco que apareceu, e ele erra **dos dois lados**. Os três
casos reproduzem em uma linha, sem áudio:

```python
from modes.copilot.detector import QuestionDetector
d = QuestionDetector()
d.detect_in_turn("O que diferencia?")                      # ❌ deveria ser pergunta
d.detect_in_turn("Então, queria entender se a senhora acha…")  # ❌ deveria ser pergunta
d.detect_in_turn("Eu acho que pode ser conversado, discutido…")  # ❌ NÃO é pergunta
```

1. **Pergunta curta morre no filtro de tamanho.** `detector.py:127` corta em
   `MIN_PALAVRAS = 4` antes de chegar ao `endswith("?")` da linha 140. "Por
   quê?", "E agora?", "O que diferencia?" nunca passam. A correção provável é
   só ordem: `?` é o sinal mais forte que existe, deveria ser avaliado antes
   do filtro de tamanho — mas depois da regra de muleta, senão "isso faz
   sentido, né?" vira pergunta.

2. **Pergunta indireta escapa.** O padrão da linha 42 exige
   `(gostaria|queria) de (saber|entender)`. Em entrevista formal sai "queria
   entender da senhora se…", sem o "de".

3. **"pode ser" dispara falso positivo.** O padrão de pedido da linha 40 casa
   com `pode\s+\w+`, e "pode ser" é das construções mais comuns do português
   falado. Metade das detecções dos 10 minutos foi isso: "pode ser
   questionado", "pode ser modificado". O guard de hedge (`_RE_HEDGE.match`)
   não salva porque é ancorado no início: "**Eu** acho que pode ser…" não
   casa.

O item 3 é o que custa dinheiro: com `auto_answer` ligado, cada falso positivo
é uma chamada paga, e o LLM responde a fragmento sem sentido ("Para o que?
Para ter sua fé explorada…").

**Não mexi no detector de propósito.** O comentário na linha 151 diz que a
regra ingênua deu 100% de falso positivo, então ele está calibrado contra
transcrição real, e mudar precisão/recall muda o custo por hora que você
mediu. É decisão de produto, não de refactor.

## Na fila, depois disso

- Integrar o Modo Aula na interface (hoje só existe o Copilot na janela).
- Modo Stealth: esconder a janela da captura de tela. No Wayland é mais
  complicado que no Windows — precisa de investigação.
- Aviso de cota `:free` na tela de configuração (atrito de onboarding).

## Lembretes

- **Revogar a chave do OpenRouter** que apareceu em texto puro no histórico da
  conversa e gerar outra. Continua pendente.
- O modelo configurado hoje é `google/gemma-3-12b-it` (pago, ~US$0,000057 por
  resposta). O padrão de primeira execução continua num `:free`, de propósito.
- Antes de mexer em qualquer coisa: leia `AGENTS.md`. As sete travas ali
  custaram medição.
- `runs/` está no `.gitignore` — os artefatos de teste não vão para o repo.
