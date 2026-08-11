# Onde paramos

Sessão pausada por limite de uso. Este arquivo é o suficiente para retomar sem
reconstruir contexto.

## Estado do git

```
aaad1a3  copilot: captura do microfone como segunda fonte     ← agente D ✅
cffe354  exporters: markdown, Obsidian e JSON                 ← agente B ✅
15a7f1e  storage: repositório SQLite e em memória             ← agente A ✅
6fe314c  contratos de domínio + AGENTS.md
19ef2b2  base: captura, VAD, transcrição e Copilot
```

Tudo commitado. Os quatro agentes concluíram.

## Testes (últimos verificados, rodados por mim, não pelos agentes)

```
tests/test_segmenter.py     8/8
tests/test_detector.py     16/16
tests/test_engine.py       12/12
tests/test_storage.py      18/18
tests/test_exporters.py    12/12
tests/test_multi_source.py  9/9
tests/test_session_mode.py 13/13
                           ─────
                           88/88
```

## Próximo passo imediato

1. **Rodar o revisor no Opus** sobre os quatro diffs. Ele precisa **executar**
   a suíte, não só ler código. Dois pontos para ele examinar especificamente:

   - `storage/sqlite_repo.py`, `add_segments`: recupera os IDs relendo os
     últimos N por `session_id` depois do commit, e no modo arquivo não há
     lock. Corrida teórica entre threads. Tentei reproduzir com 12 threads,
     largada por barreira e 540 inserções — **não reproduzi**. Sugestão de
     correção: capturar `max(id)` antes do insert e selecionar `id > esse`,
     ou envolver insert+select numa transação `BEGIN IMMEDIATE`.

   - `copilot_app.py`, `self._speaker_atual`: o próprio agente D sinalizou como
     a parte mais frágil. O `on_turn` do motor não carrega `speaker`, então o
     app guarda o falante numa variável de instância lida pelo callback. É
     seguro hoje porque o worker de transcrição é uma thread só e `ingest` é
     síncrono — quebra em silêncio no dia que houver duas threads. **Correção
     certa:** passar `speaker` pela assinatura de `on_turn`.

2. **Só então o teste real** com áudio de aula/reunião.

## Depois disso, na fila

- Integrar o Modo Aula na interface (hoje só existe o Copilot na janela).
- Modo Stealth: esconder a janela da captura de tela. No Wayland é mais
  complicado que no Windows — precisa de investigação.
- Aviso de cota `:free` na tela de configuração (atrito de onboarding).

## Lembretes

- **Revogar a chave do OpenRouter** que apareceu em texto puro no histórico da
  conversa e gerar outra.
- O modelo configurado hoje é `google/gemma-3-12b-it` (pago, ~US$0,000057 por
  resposta). O padrão de primeira execução continua num `:free`, de propósito,
  para o app funcionar sem exigir cartão de quem for testar.
- Antes de mexer em qualquer coisa: leia `AGENTS.md`. As sete travas ali
  custaram medição.
