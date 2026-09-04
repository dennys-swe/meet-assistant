# Instruções para agentes

Leia antes de escrever qualquer linha. As regras abaixo não são preferência de
estilo: cada uma custou depuração ou medição, e o código já está do jeito certo.
Reverter qualquer uma delas quebra o app de formas que os testes podem não pegar.

## Contexto do projeto

App de desktop para Linux que escuta o áudio do sistema (aulas, reuniões,
entrevistas), transcreve localmente e ajuda o usuário. Dois modos:

- **Copilot** (existe): detecta perguntas ao vivo e responde via LLM.
- **Modo Aula** (não existe ainda): grava a sessão inteira e gera resumo,
  insights e tarefas ao final.

É pensado como **SaaS testável por terceiros**: o usuário traz a própria chave
de API (BYOK). Nunca embuta chave, nunca assuma que quem roda é o dono do repo.

## Travas — não mexa nisto

1. **Captura é `pw-record`, não PyAudio.** E exige `-P stream.capture.sink=true`
   para gravar o monitor de um sink. Sem essa propriedade o `--target` é
   ignorado em silêncio e você grava o microfone achando que gravou o sistema.
   Ver `capture/recorder.py`.

2. **O monitor de um sink não é um node `<sink>.monitor`.** Isso é convenção do
   PulseAudio. No PipeWire são portas do próprio sink.

3. **`asr/streaming.py` fica FORA do caminho crítico.** LocalAgreement-2 foi
   implementado, medido e perdeu para o modo em lote em CPU (2,8–4,7s contra
   2,2s), com qualidade pior. O motivo é estrutural, está documentado no topo
   do arquivo. Não o coloque entre o fim da fala e o LLM.

4. **Tkinter só na main thread.** Toda atualização vinda de outra thread passa
   por `self.after(0, ...)`. Ver `ui/copilot_window.py`.

5. **Ligar/desligar a escuta não para o motor de captura.** Só conecta e
   desconecta o consumidor via `CapturePipeline.subscribe`. Mexer no motor era
   a origem dos bugs de estado da v1.

6. **Nada de `except: pass`.** Erro que o usuário precisa ver vai para a barra
   de status, em português e sem jargão de HTTP.

7. **O LLM é acessado só pela interface `LLMClient`.** Um cliente único
   OpenAI-compat atende OpenRouter, Groq, OpenAI e Ollama. Não adicione SDK de
   provedor.

## Padrões do código

- Python 3.14, `from __future__ import annotations`, type hints.
- **Comentários e docstrings em português.** Identificadores em inglês nas
  interfaces públicas; nomes internos em português são aceitos (o código
  existente mistura, siga o arquivo em que estiver mexendo).
- Comente o **porquê**, não o quê. Os melhores comentários deste repo explicam
  uma decisão contra-intuitiva ou um bug que já aconteceu.
- Sem dependência nova sem necessidade real. As atuais: `numpy`,
  `onnxruntime`, `faster-whisper`, `httpx`, `customtkinter`.

## Testes — obrigatórios

Todo trabalho entrega testes que rodam **sem rede, sem áudio e sem GUI**.
Siga o formato dos existentes (`tests/test_*.py`): funções `test_*`, runner
`__main__` no fim do arquivo, executável por
`.venv/bin/python tests/test_x.py`.

Use `FakeLLM` de `tests/test_engine.py` como referência para dublês.

Rode antes de entregar:

```bash
.venv/bin/python tests/test_segmenter.py
.venv/bin/python tests/test_detector.py
.venv/bin/python tests/test_engine.py
```

Os 36 existentes precisam continuar passando.

## Escopo

Mexa **apenas** nos arquivos do seu escopo. Se precisar de algo fora dele,
pare e relate em vez de editar — outro agente pode estar no mesmo arquivo.
