#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
🔍 Script de Diagnóstico de Áudio para Meet Assistant
Este script ajuda a identificar problemas de captura de áudio em tempo real
"""

import pyaudio
import speech_recognition as sr
import logging
import sys
import time
from config import settings

# Configurar logging para console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

print("\n" + "="*80)
print("🔍 DIAGNÓSTICO DE ÁUDIO - Meet Assistant")
print("="*80 + "\n")

# ============================================================================
# 1. LISTAR TODOS OS DISPOSITIVOS DE ÁUDIO
# ============================================================================
print("📋 [1/5] Listando dispositivos de áudio...\n")

p = pyaudio.PyAudio()
device_count = p.get_device_count()

logger.info(f"Encontrados {device_count} dispositivos:\n")

for i in range(device_count):
    try:
        info = p.get_device_info_by_index(i)
        maxInputChannels = info['maxInputChannels']
        maxOutputChannels = info['maxOutputChannels']
        sampleRate = int(info['defaultSampleRate'])
        nome = info['name']
        
        marker = ""
        if maxInputChannels > 0 and 'stereo mix' in nome.lower():
            marker = " ← 🎙️ (POSSÍVEL CAPTURA DO MEET!)"
        elif maxInputChannels > 0:
            marker = " ← 🎤 (Entrada de Áudio)"
        
        logger.info(
            f"  [{i}] {nome}{marker}\n"
            f"       └─ Input: {maxInputChannels}ch, Output: {maxOutputChannels}ch, Taxa: {sampleRate}Hz"
        )
    except Exception as e:
        logger.error(f"Erro ao ler device {i}: {e}")

p.terminate()

# ============================================================================
# 2. TESTAR CONFIGURAÇÃO ATUAL
# ============================================================================
print("\n" + "="*80)
print("⚙️  [2/5] Testando configuração atual...\n")

logger.info(f"Device Index: {settings.DEVICE_INDEX_VALIDATED}")
logger.info(f"Sample Rate: {settings.SAMPLE_RATE} Hz")
logger.info(f"Channels: {settings.CHANNELS}")
logger.info(f"Chunk Size: {settings.CHUNK}")
logger.info(f"Confidence Threshold: {settings.CONFIDENCE_THRESHOLD:.2%}")

# ============================================================================
# 3. TESTAR CAPTURA DE ÁUDIO (3 SEGUNDOS)
# ============================================================================
print("\n" + "="*80)
print("🎙️  [3/5] Testando captura de áudio (3 segundos)...\n")

logger.info("Certifique-se que há BARULHO ao redor para testar a captura!")
logger.info("Aguarde 3 segundos...\n")

try:
    p = pyaudio.PyAudio()
    
    stream = p.open(
        format=pyaudio.paInt16,
        channels=settings.CHANNELS,
        rate=settings.SAMPLE_RATE,
        input=True,
        input_device_index=settings.DEVICE_INDEX_VALIDATED,
        frames_per_buffer=settings.CHUNK
    )
    
    logger.info("✅ Stream aberto com sucesso!")
    logger.info("REC: Capturando áudio...\n")
    
    frames = []
    for i in range(int((settings.SAMPLE_RATE / settings.CHUNK) * 3)):  # 3 segundos
        try:
            data = stream.read(settings.CHUNK, exception_on_overflow=False)
            frames.append(data)
            print(".", end="", flush=True)
        except Exception as e:
            logger.error(f"Erro ao capturar frame: {e}")
    
    print("\n")
    
    stream.stop_stream()
    stream.close()
    p.terminate()
    
    # Calcular volume aproximado
    import array
    dados_completos = b''.join(frames)
    audio_array = array.array('h', dados_completos)
    rms = (sum(x**2 for x in audio_array) / len(audio_array)) ** 0.5
    
    logger.info(f"✅ Captura concluída: {len(dados_completos)} bytes capturados ({len(dados_completos)/1024:.1f}KB)")
    logger.info(f"📊 Nível de áudio (RMS): {rms:.0f} (Esperado: >500 para voz clara)")
    
    if rms < 200:
        logger.warning("⚠️  Nível muito baixo! Possível problema:")
        logger.warning("    - Device errado selecionado")
        logger.warning("    - Silêncio ao redor")
        logger.warning("    - Volume do microfone baixo")
except Exception as e:
    logger.error(f"❌ Erro ao capturar áudio: {e}", exc_info=True)

# ============================================================================
# 4. TESTAR GOOGLE SPEECH RECOGNITION
# ============================================================================
print("\n" + "="*80)
print("🔊 [4/5] Testando Google Speech Recognition...\n")

try:
    logger.info("Capturando áudio novamente para transcrição (3 segundos)...\n")
    
    with sr.Microphone(device_index=settings.DEVICE_INDEX_VALIDATED) as source:
        recognizer = sr.Recognizer()
        logger.info("REC: Calibrando ruído ambiente...\n")
        recognizer.adjust_for_ambient_noise(source, duration=1)
        
        logger.info("REC: Capturando áudio (fale algo!)...\n")
        print(".", end="", flush=True)
        audio = recognizer.listen(source, timeout=3, phrase_time_limit=3)
        print("\n")
        
        logger.info("Enviando para Google Speech Recognition...\n")
        try:
            resultado = recognizer.recognize_google(audio, language="pt-BR", show_all=True)
            
            if resultado and 'alternative' in resultado:
                melhor = resultado['alternative'][0]
                confianca = melhor.get('confidence', 0)
                texto = melhor.get('transcript', '')
                
                logger.info(f"✅ Transcrição bem-sucedida!")
                logger.info(f"   Texto: '{texto}'")
                logger.info(f"   Confiança: {confianca:.2%}")
                
                if confianca < settings.CONFIDENCE_THRESHOLD:
                    logger.warning(f"⚠️  Confiança baixa! Threshold é {settings.CONFIDENCE_THRESHOLD:.2%}")
            else:
                logger.warning("⚠️  Google não retornou alternativas")
                
        except sr.UnknownValueError:
            logger.warning("⚠️  Google não conseguiu compreender o áudio")
        except sr.RequestError as e:
            logger.error(f"❌ Erro na requisição Google: {e}")
            
except Exception as e:
    logger.error(f"❌ Erro ao testar Google Speech: {e}", exc_info=True)

# ============================================================================
# 5. RECOMENDAÇÕES
# ============================================================================
print("\n" + "="*80)
print("✅ [5/5] Recomendações\n")

logger.info("Com base nos testes acima:")
logger.info("")
logger.info("❓ As legendas não aparecem? Verifique:")
logger.info("")
logger.info("1️⃣  Device correto selecionado?")
logger.info("   - Procure por '[XY] ... STEREO MIX' ou similar na lista acima")
logger.info("   - Se encontrou, adicione ao arquivo .env: DEVICE_INDEX=[XY]")
logger.info("")
logger.info("2️⃣  Nível de áudio baixo?")
logger.info("   - Aumente volume do microfone no Windows")
logger.info("   - Abra: Som > Entrada > Dispositivo atual > Volume")
logger.info("")
logger.info("3️⃣  Google Speech não transcreve?")
logger.info("   - Verificar conexão com internet (Google Speech precisa)")
logger.info("   - Tentar com volume mais alto")
logger.info("")
logger.info("4️⃣  Confiança muito baixa (abaixo de 85%)?")
logger.info("   - Aumentar volume do microfone")
logger.info("   - Reduzir ruído de fundo")
logger.info("   - Ou reduzir CONFIDENCE_THRESHOLD no .env")
logger.info("")

print("="*80 + "\n")
logger.info("🎉 Diagnóstico concluído! Relance a aplicação para testar as mudanças.")
