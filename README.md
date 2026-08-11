# Meet Assistant v2

Reconstrução do Meet-Assistant para Linux. O que já funciona ponta a ponta:
**captura de áudio interno → VAD → turnos de fala → transcrição**.

Ainda não há resumo, detecção de pergunta, persistência nem interface.

## Por que isto existe

Na v1 o áudio era fatiado a cada 3,5 s pelo relógio. O corte caía no meio de
palavras, a transcrição vinha com confiança baixa e era descartada pelo
limiar — e não havia frase inteira em que ancorar a detecção de pergunta.

Aqui quem decide onde cortar é o VAD. Cada `Utterance` começa e termina em
fronteira real de fala.

## Estado atual

```
capture/
  sources.py       descoberta de fontes do PipeWire (monitor do sink padrão)
  recorder.py      pw-record → PCM 16 kHz mono int16
  vad.py           Silero VAD via ONNX  [derivado de BasedHardware/omi, MIT]
  segmenter.py     máquina de estados SILENCE/SPEECH/HANGOVER → Utterance
  pipeline.py      junta tudo; publica turnos para os consumidores
asr/
  base.py          interface Transcriber (troca de motor sem tocar nos modos)
  whisper_local.py faster-whisper em CPU, int8, com perfis por modo
  worker.py        fila + thread; transcrição nunca bloqueia a captura
  streaming.py     LocalAgreement-2 — medido e fora do caminho crítico (ver abaixo)
  live.py          VAD + streaming numa thread (parciais para a interface)
modes/copilot/
  detector.py      detecção de pergunta em pt-BR (heurística, 0ms, sem rede)
  engine.py        CopilotEngine: turno → detecção → LLM em streaming
llm/
  base.py          interface LLMClient
  openai_compat.py um cliente para OpenRouter/Groq/OpenAI/Ollama
config/
  settings.py      config do usuário (BYOK) em ~/.config/meet-assistant/
ui/
  copilot_window.py  janela flutuante (CustomTkinter)
domain/
  audio.py         Utterance, AudioSource, formato canônico
vendor/silero/     modelo silero_vad.onnx (MIT)
```

Diretórios vazios (`storage/`, `exporters/`) são os próximos passos.

## Copilot: traga sua própria chave (BYOK)

O app é feito para ser testado por outras pessoas, então **nenhuma chave vai
no código**. Cada usuário configura a sua na primeira execução, e ela fica em
`~/.config/meet-assistant/config.json` com permissão 600.

A transcrição roda local — o áudio nunca sai da máquina. Só a geração da
resposta usa a chave do usuário.

Como OpenRouter, Groq, OpenAI e Ollama falam o mesmo protocolo, existe um
único cliente (`llm/openai_compat.py`) e trocar de provedor é trocar a
`base_url`:

| provedor | por que |
|---|---|
| OpenRouter (padrão) | uma chave, dezenas de modelos, vários `:free` |
| Groq | rápido, tier gratuito |
| OpenAI | |
| Ollama | local, sem chave, sem internet |

### Cota: o `:free` tem teto diário

Modelos `:free` do OpenRouter são limitados a ~50 requisições/dia sem crédito
na conta. Isso **não** passa com espera — só renova no dia seguinte, e o app
distingue esse caso do limite por minuto (que passa, e ele repete sozinho).

Um modelo pago resolve por quase nada. Custo real de uma resposta nossa
(~700 tokens de entrada, 150 de saída):

| modelo | 1º token | custo/resposta | cabe em US$ 3 |
|---|---|---|---|
| `google/gemma-3-12b-it` | 0,80s | US$ 0,000057 | ~52.000 respostas |
| `qwen/qwen3-30b-a3b-instruct-2507` | 1,23s | US$ 0,000063 | ~47.000 |
| `google/gemini-2.5-flash-lite` | 1,58s | US$ 0,000065 | ~46.000 |

O padrão de primeira execução continua num modelo `:free`, para o app
funcionar sem exigir cartão de quem for testar.

### Escolha do modelo padrão

Medido, não escolhido por reputação. O critério que manda é **tempo até o
primeiro token** — é ele que dá sensação de tempo real, não a resposta
completa.

| modelo (gratuito) | 1º token | resultado |
|---|---|---|
| `google/gemma-4-26b-a4b-it:free` | **1,8s** | ✅ padrão: direto, pt-BR, obedece `[IGNORAR]` |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 1,0–5,2s | ❌ vaza raciocínio em inglês |
| `nvidia/nemotron-3.5-lightning:free` | 2,2s | ❌ idem, responde "Here's a thinking process:" |
| `nvidia/nemotron-3-super-120b-a12b:free` | 22,6s | ❌ lento demais |
| `openai/gpt-oss-20b:free` | — | ❌ não devolve `content` |

A família Nemotron é de **modelos de raciocínio**: eles emitem o
"pensamento" junto da resposta. Para um texto que será lido de relance no
meio de uma reunião, é o pior perfil possível — mesmo sendo gratuitos e
tendo 1M de contexto.

Qualquer modelo pode ser trocado na tela de Configurações.

```bash
sudo apt install python3-tk        # o Tk não vem com o Python no Ubuntu
.venv/bin/pip install httpx customtkinter
.venv/bin/python copilot_app.py
```

## Detecção de pergunta

Portão heurístico local (0ms, sem rede) + `[IGNORAR]` no prompt do LLM para
o filtro fino. A heurística corta ~90% do volume; o modelo resolve retórica e
intenção. Uma chamada de API por pergunta real, nenhuma nos demais turnos.

A regra ingênua — "contém palavra interrogativa" — **falhou em 100% dos casos
reais**: em fala corrida, `como`, `quando` e `para que` são conjunções
("quando eu falo de liderança...", "como as coisas acontecem..."). As regras
que sobraram, validadas contra transcrição real:

- termina em `?` → pergunta
- pedido explícito sem `?` ("me conta", "você pode explicar") → pergunta
- interrogativo **abrindo** o turno e frase curta (≤15 palavras) → pergunta
- `porque` junto é conjunção causal; só `por que` separado é interrogativo
- muletas (`né?`, `tá?`, `certo?`) nunca disparam

Validado ao vivo numa aula real: 23 turnos, 2 perguntas detectadas (ambas
corretas), zero falso positivo.

A busca é **por frase, não pelo turno inteiro**: quem fala sem pausar gera
blocos com várias frases dentro, e uma pergunta enterrada no meio some se
olharmos só o todo. Mas ao LLM vai o **turno completo** — a frase sozinha
costuma ser fragmento sem referente ("mas por que eles têm a necessidade?"
rendeu uma resposta genérica sobre escassez de recursos quando ia sem
contexto).

### Auto vs. manual

O switch **Auto** decide se o Copilot chama o LLM sozinho:

- **ligado** — responde a toda pergunta detectada. É o ponto do produto numa
  entrevista em que *você* é o candidato.
- **desligado** — transcreve e destaca as perguntas, mas só chama o LLM no
  botão "Responder último turno". Certo para assistir aula ou palestra, onde
  quase toda pergunta é do professor para a turma.

### Sessão real medida (simulação de entrevista, 4min19s)

```
18 turnos transcritos   mediana 4,2s por turno (teto de 8s)
 4 chamadas ao LLM      100% OK, 0 erros, 0 abandonadas
                        custo total ≈ US$ 0,0002
```

## Rodar

Requisitos do sistema: PipeWire com `pw-record` e `pw-dump`
(`sudo apt install pipewire-bin`). Não precisa de PyAudio, PortAudio,
cabo de áudio virtual nem qualquer configuração manual de device.

```bash
python3 -m venv .venv
.venv/bin/pip install numpy onnxruntime faster-whisper

.venv/bin/python demo_capture.py --list      # fontes disponíveis
.venv/bin/python demo_capture.py             # só captura + VAD
.venv/bin/python demo_live.py                # captura + transcrição ao vivo
.venv/bin/python demo_live.py --profile aula # qualidade alta
.venv/bin/python tests/test_segmenter.py     # 8 testes, sem placa de som
```

## Transcrição: números medidos

Whisper local (faster-whisper, int8, CPU), nesta máquina — 12 threads, sem
GPU — sobre áudio real de uma palestra em português capturada pelo pipeline.
RTF = tempo de processamento ÷ duração do áudio.

| modelo | piso de latência | RTF em turno de 25s | qualidade |
|---|---|---|---|
| tiny | ~0.5s | 0.03 | ruim: erra nomes próprios e termos técnicos |
| base | ~0.9s | 0.06 | medíocre |
| small | ~2.2s | 0.14 | boa — legível e fiel ao conteúdo |
| large-v3-turbo | ~10s | 0.44 | melhor: acerta nomes e pontuação |

O Whisper processa em janelas fixas de 30s, então **existe um piso de latência
por chamada, independente do tamanho do turno**. Com `small`, um turno de 2,8s
levou 2,2s. Com `large-v3-turbo`, um turno de 1,1s levou 10s — RTF 8.89.

Duas consequências de projeto:

1. **Copilot fica com `small`.** É o melhor equilíbrio: ~2,2s de piso, com
   qualidade utilizável. Nenhum modelo local vai abaixo disso — o piso é
   arquitetural do Whisper, não falta de CPU.

2. **Modo Aula não pode transcrever turno a turno.** Pagar 10s de custo fixo
   por frase torna o `large-v3-turbo` inviável assim. A aula deve juntar os
   turnos em blocos de ~30s (o tamanho da janela do modelo) e transcrever em
   lote — aí o custo fixo se dilui e o RTF real fica em 0.44, ou seja, uma
   aula de 1h transcrita em ~26 min de processamento.

A qualidade justifica: onde o `small` entendeu *"Conte ele era rápido"*, o
turbo entendeu *"Mediram Tom Brady de fora para dentro"*.

## Transcrição contínua: tentada, medida, descartada do caminho crítico

A hipótese era tirar o ASR do caminho crítico do Copilot: transcrever
enquanto a pessoa fala (LocalAgreement-2, `asr/streaming.py`), de modo que ao
fim da frase o texto já estivesse pronto. Implementado e medido no mesmo
áudio, a hipótese **não se confirmou**:

| modelo | streaming | lote |
|---|---|---|
| tiny | 0,7 – 1,4s | 0,5s |
| base | 1,3 – 3,3s | 0,9s |
| small | 2,8 – 4,7s | 2,2s |

O lote ganha em todos os tamanhos, e a qualidade do streaming também é pior.

A razão é estrutural. O custo do Whisper é dominado pela passagem do encoder
sobre uma janela fixa de 30s, que custa quase o mesmo para 2s ou 20s de
áudio. LocalAgreement roda essa passagem várias vezes por turno em vez de uma
só. Em GPU, onde a passagem custa ~100ms, isso é barato e o streaming ganha.
Em CPU, onde custa 1–2s, multiplicar a operação dominante só piora.

O módulo permanece no projeto: é a arquitetura correta se houver GPU ou se
trocarmos por um motor nativamente de streaming, e os parciais durante a fala
servem à interface. Só não entra entre o fim da fala e o LLM.

Reproduzível sem placa de som:

```bash
.venv/bin/python demo_stream.py --model small --replay turno_0002.wav
```

## Duas armadilhas do PipeWire

Ambas custaram depuração e estão documentadas no código para não voltarem:

1. **O monitor de um sink não é um node `<sink>.monitor`.** Isso é convenção
   do PulseAudio. No PipeWire são portas do próprio sink.

2. **`pw-record --target <sink>` sozinho não captura o monitor** — ele ignora
   o alvo em silêncio e cai no microfone padrão. Grava som, parece funcionar,
   e captura a coisa errada. É preciso `-P stream.capture.sink=true`.

Detalhe menor, mesmo efeito: escrevendo para stdout, o `pw-record` emite um
container AU (magic `.snd`), não PCM cru. O cabeçalho precisa ser descartado.

## Validação

Testado com uma palestra do YouTube tocando num fone Bluetooth (o sink padrão
é resolvido em tempo de execução, então headset ou alto-falante tanto faz):

```
[   0.0s →  25.5s]  25.5s de fala
[  26.0s →  31.4s]   5.3s de fala
[  31.9s →  37.6s]   5.7s de fala
[  38.1s →  39.2s]   1.1s de fala
```

Rodando o VAD de novo sobre os WAVs salvos: 85–95% das janelas de cada turno
são fala, com 64–75% da energia na banda de voz (300–3400 Hz).

E com transcrição ao vivo (`demo_live.py`, perfil copilot):

```
[   0.0s] ( 9.4s · ASR 2.6s · RTF 0.28)
   ...Essa foto do dia 21 de agosto de 2016. O dia da final Olímpica
   aqui no Rio de Janeiro contra Itália.

[  44.1s] ( 8.0s · ASR 2.5s · RTF 0.32)
   Eu tenho um livro de cabeceira que diz Only the Paranoia Survival,
   ou seja, aqueles caras que têm quase uma paranoia em fazerem,
   realizarem, não desistirem
```

## Créditos

`capture/vad.py` e as constantes de segmentação derivam de
[BasedHardware/omi](https://github.com/BasedHardware/omi)
(`backend/utils/stt/vad.py` e `vad_gate.py`), MIT — cópia em
`vendor/OMI_LICENSE`. O modelo Silero VAD também é MIT.

Descartamos do Omi a camada de VAD hospedado, Redis, telemetria e o
acoplamento com Deepgram, que não fazem sentido num app local.
