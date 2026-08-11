# Estratégia de testes

Três camadas. Cada uma existe porque a de baixo não consegue provar o que a de
cima prova, e nenhuma delas substitui as outras.

```
   1. unidade      tests/*.py            sem rede, sem áudio, sem GUI     rápido, roda sempre
   2. end-to-end   e2e_copilot.py        áudio real, pipeline inteiro     minutos, roda por release
   3. manual       copilot_app.py        janela, mouse, ouvido            só o humano vê
```

## 1. Unidade — `tests/`

Obrigatórias e sem desculpa: rodam sem rede, sem áudio e sem GUI (ver
`AGENTS.md`). São elas que travam regressão de lógica.

```bash
for f in tests/test_*.py; do .venv/bin/python "$f"; done
```

Dois testes merecem nota porque são de um tipo diferente dos outros:

- `test_add_segments_segura_a_trava_ate_reler_os_ids` — **corrida
  determinística**. A tentação, ao testar concorrência, é martelar com N
  threads e torcer. Isso não funcionou: 12 threads e 540 inserções não
  reproduziram o bug. O teste que presta para a thread no meio da fresta (via
  `set_trace_callback`) e verifica se outro gravador consegue entrar. Ele
  falha no código antigo e passa no novo — que é o mínimo que se exige de um
  teste de regressão.

- `test_falante_nao_se_mistura_entre_threads` — guarda a invariante que o bug
  do `_speaker_atual` quebrava: o falante tem que chegar pela chamada, nunca
  por estado compartilhado.

**Regra:** todo bug de concorrência encontrado vira teste determinístico, não
teste por martelo. Se você não consegue fazer o teste falhar no código antigo,
você não testou a correção — testou o agendador.

## 2. End-to-end — `e2e_copilot.py`

A camada 1 é cega para tudo que importa em produção: se o `pw-record` está com
a flag certa, se o VAD corta onde deveria, se o Whisper aguenta o ritmo, se o
detector acha pergunta em fala espontânea (que é bem diferente de fala de
teste). Este harness liga o caminho inteiro num áudio de verdade e mede.

```bash
# captura o que estiver tocando no sistema por 5 minutos, LLM falso
.venv/bin/python e2e_copilot.py --minutos 5

# repete sobre um áudio já gravado — determinístico, sem depender do relógio
.venv/bin/python e2e_copilot.py --replay runs/<carimbo>/sessao.wav

# com o provedor real, com teto de chamadas para não torrar cota
.venv/bin/python e2e_copilot.py --minutos 3 --llm real --auto --max-chamadas 5
```

**O LLM é falso por padrão.** Nenhuma chamada de rede sai sem `--llm real`, e
mesmo aí o envelope `LLMComTeto` corta depois de N. Isso não é zelo abstrato:
uma entrevista de TV rende ~140 perguntas por hora, e com auto-answer ligado
são 140 chamadas.

### Corpus

Áudio de entrevista jornalística (Roda Viva e afins) é o material certo para o
Copilot: PT-BR espontâneo, muitos falantes, e — o que interessa — muita
pergunta de verdade, feita do jeito torto que gente faz pergunta. Fala de
teste ("Qual é a capital da França?") não exercita nada.

Como montar um corpus a partir de um vídeo, sem `ffmpeg` (o PyAV já vem junto
com o `faster-whisper`):

```bash
pip install yt-dlp                       # fora do venv do projeto
yt-dlp -f worstaudio -o entrevista.m4a "<url>"
.venv/bin/python - <<'EOF'               # m4a → wav 16 kHz mono
import av, wave
cont = av.open("entrevista.m4a"); s = cont.streams.audio[0]
r = av.AudioResampler(format="s16", layout="mono", rate=16000)
pcm = b"".join(q.to_ndarray().tobytes()
               for p in cont.demux(s) for f in p.decode() for q in r.resample(f))
w = wave.open("entrevista.wav", "wb"); w.setnchannels(1); w.setsampwidth(2)
w.setframerate(16000); w.writeframes(pcm); w.close()
EOF
pw-play entrevista.wav &                 # o monitor do sink captura isto
.venv/bin/python e2e_copilot.py --minutos 10
```

Tocar o arquivo no sink e capturar pelo monitor **não é trapaça**: é
exatamente o caminho que o áudio de uma reunião percorre. Testa `pw-record`,
a flag `stream.capture.sink=true`, o resample e a latência real.

### Artefatos

Cada execução deixa `runs/<carimbo>/`:

| arquivo | serve para |
|---|---|
| `relatorio.json` | números: RTF, latência de ASR, turnos, perguntas |
| `transcricao.md` | leitura humana, com ❓ nas perguntas detectadas |
| `sessao.wav` | a sessão inteira, para `--replay` |
| `turnos/NNN.wav` | um turno por arquivo — matéria-prima de teste de unidade |

O ciclo é este: rodou ao vivo → achou um caso ruim → o `.wav` daquele turno
vira fixture em `tests/`, e o caso nunca mais volta sozinho.

### Números de referência

Medidos nesta máquina (CPU, Whisper `small`, `int8`, áudio de Roda Viva):

| métrica | valor | por quê importa |
|---|---|---|
| RTF mediano | ~0,22 | folga de 4,5× sobre o tempo real; abaixo de 1,0 o app acompanha |
| ASR mediano | ~1,6 s | é o tempo entre a pessoa calar e o texto aparecer |
| turnos de 8 s | maioria | o corte forçado domina em fala contínua, e é de propósito |

Regressão de desempenho é RTF mediano subindo de 0,22 para perto de 1,0 —
aí o app deixa de acompanhar fala contínua e a fila do worker começa a
descartar.

## 3. Manual

O que nenhum script vê: a janela redesenhando durante o streaming da resposta,
o "Ouvir" ligando e desligando sem travar, a mensagem de erro quando a chave
está errada, o texto legível a três metros de distância.

Roteiro mínimo antes de mostrar para alguém:

1. Abrir sem chave nenhuma → tem que explicar o que fazer, não quebrar.
2. Salvar chave errada → "Testar conexão" tem que dizer o que houve em
   português, sem código HTTP na cara do usuário.
3. Ligar "Ouvir" com áudio tocando → transcrição andando na tela.
4. Desligar e religar três vezes → nada de estado preso (foi o bug da v1).
5. Fechar a janela no meio de uma resposta → sem traceback no terminal.
