# 🔧 Guia de Troubleshooting - Legendas Não Aparecem em Tempo Real

## ⚡ Resumo das Mudanças Realizadas

Foram aplicadas as seguintes correções:

### 1. **Configuração de Áudio Otimizada** ✅
- ✅ `SAMPLE_RATE`: Mudado de 48000 Hz → **16000 Hz** (melhor para Google Speech Recognition)
- ✅ `CHANNELS`: Mudado de 2 (estéreo) → **1 (mono)** (melhor compatibilidade)
- ✅ `DEVICE_INDEX`: Mudado de 1 → **-1 (default do sistema)**
- ✅ Device agora é listado automaticamente com nomes e capacidades

### 2. **Logs Detalhados Adicionados** 📊
- 🎙️ Device de áudio agora lista **TODOS os dispositivos** disponíveis
- 🎯 AudioEngine agora mostra quando **callback é acionado**
- 📝 Google Speech mostra **confiança** e **texto** de cada transcrição
- 📥 GUI mostra o **fluxo de áudio** a cada etapa

### 3. **Script de Diagnóstico Criado** 🔍
Arquivo: `diagnostico_audio.py`

## 🚀 Como Usar o Script de Diagnóstico

```bash
# 1. Abra terminal na raiz do projeto
cd c:\Users\Dennys Alves\Projetos\Meet-Assistant\Meet-Assistant-Project

# 2. Execute o diagnóstico
python diagnostico_audio.py
```

Este script vai:
1. ✅ Listar **TODOS** os dispositivos de áudio
2. ✅ Testar captura de 3 segundos
3. ✅ Testar Google Speech Recognition
4. ✅ Dar recomendações baseadas nos resultados

## 📋 Checklist de Verificação

- [ ] Rodei `diagnostico_audio.py`?
- [ ] Identifiquei qual device tem "STEREO MIX" ou "Google Meet"?
- [ ] Adicionei ao `.env`? Exemplo: `DEVICE_INDEX=5`
- [ ] Verifiquei volume do microfone no Windows?
- [ ] Testei com voz clara próximo ao microfone?
- [ ] Verifiquei se Google Speech conseguiu transcrever?

## ❌ Possíveis Problemas e Soluções

### Problema 1: "Device não encontrado"
**Solução:**
```bash
# Rode o diagnóstico e procure por
# [XY] ... STEREO MIX - potencial captura do Meet
```
Depois adicione ao seu `.env`:
```
DEVICE_INDEX=XY
```

### Problema 2: "Nível de áudio baixo (RMS < 200)"
**Solução:**
1. Abra: **Windows Settings** > **Sound** > **Input**
2. Aumente o volume do seu microfone para **80-100%**
3. Verifique se o microfone está bem conectado

### Problema 3: "Google Speech não transcreve nada"
**Solução:**
1. ✅ Verifique conexão com internet (essencial!)
2. ✅ Aumente volume do microfone
3. ✅ Fale mais próximo do microfone
4. ✅ Reduza ruído de fundo

### Problema 4: "Confiança muito baixa (< 85%)"
**Solução A** (Recomendado):
- Melhorar qualidade do áudio (voz clara, sem ruído)

**Solução B** (Alternativa):
Adicione ao `.env`:
```
CONFIDENCE_THRESHOLD=0.5
```
(Reduz de 85% para 50%, mas pode incluir transcrições erradas)

## 🎯 Fluxo Esperado (Com Logs Detalhados)

Quando tudo funciona, você verá:

```
INFO | 📋 [1/5] Listando dispositivos de áudio...
INFO | [5] Stereo Mix (What U Hear) - 🎙️ (STEREO MIX - potencial captura do Meet)
INFO | ⚙️  [2/5] Testando configuração atual...
INFO | 🎙️  [3/5] Testando captura de áudio (3 segundos)...
INFO | ✅ Stream aberto com sucesso!
INFO | 📊 Nível de áudio (RMS): 2500 (Esperado: >500 para voz clara)
INFO | ✅ Captura concluída: 96000 bytes capturados (93.8KB)
INFO | ✅ Transcrição bem-sucedida!
INFO | Texto: 'olá mundo teste'
INFO | Confiança: 95.23%
```

## 🎙️ Teste Final - No Modo Copilot

Após resolver a configuração:

1. Abra `main.py` (ou execute `iniciar.bat`)
2. Vá para aba **🚀 Copilot (Ao Vivo)**
3. **Fale algo próximo ao microfone**
4. Aguarde 3.5 segundos
5. A legenda deve aparecer em tempo real + histórico

Se ver vários emojis 🎙️ 🎯 📝 ✅ nos logs = **Está funcionando!**

## 📞 Próximas Etapas se Nada Funcionar

Se após tudo isso ainda não funcionar:

1. **Verifique o Windows**: Settings > Sound > Input volume
2. **Teste o microfone**: Abra Gravador de Voz e teste
3. **Teste conexão**: `ping google.com` (Google Speech precisa de internet)
4. **Cole os logs completos** (do console) para análise detalhada

---

**Boa sorte! 🚀**
