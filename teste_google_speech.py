import sys
sys.path.insert(0, '.')
from config import settings
import speech_recognition as sr

print('Teste Google Speech Recognition')
print('Fale algo nos próximos 3 segundos...')
print()

try:
    with sr.Microphone(device_index=settings.DEVICE_INDEX_VALIDATED) as source:
        recognizer = sr.Recognizer()
        print('🎤 Calibrando ruído ambiente...')
        recognizer.adjust_for_ambient_noise(source, duration=1)
        print('REC: Capturando...')
        audio = recognizer.listen(source, timeout=3, phrase_time_limit=3)
        
        print('Enviando para Google...')
        resultado = recognizer.recognize_google(audio, language='pt-BR', show_all=True)
        
        if resultado and 'alternative' in resultado:
            melhor = resultado['alternative'][0]
            confianca = melhor.get('confidence', 0)
            texto = melhor.get('transcript', '')
            
            print()
            print(f'✅ TRANSCRIÇÃO BEM-SUCEDIDA!')
            print(f'   Texto: "{texto}"')
            print(f'   Confiança: {confianca:.2%}')
            
            if confianca > settings.CONFIDENCE_THRESHOLD:
                print(f'   Status: ✅ ACEITO (acima de {settings.CONFIDENCE_THRESHOLD:.2%})')
            else:
                print(f'   Status: ❌ REJEITADO (abaixo de {settings.CONFIDENCE_THRESHOLD:.2%})')
except sr.UnknownValueError:
    print('❌ Google não compreendeu o áudio (muito ruído?)')
except sr.RequestError as e:
    print(f'❌ Erro na requisição: {e}')
except Exception as e:
    print(f'❌ Erro: {e}')
