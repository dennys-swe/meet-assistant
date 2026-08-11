import speech_recognition as sr
import google.generativeai as genai
import threading
import logging
import time
from config import settings

logger = logging.getLogger(__name__)

# ✅ Função para retry com backoff exponencial
def retry_with_backoff(func, max_retries=3, initial_delay=1, backoff_factor=2):
    """Wrapper para retry automático com exponential backoff"""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = initial_delay * (backoff_factor ** attempt)
            logger.warning(f"Tentativa {attempt + 1}/{max_retries} falhou: {e}. Aguardando {delay}s antes de retentar...")
            time.sleep(delay)

class AIService:
    def __init__(self):
        # 1. Validar e configurar a API do Gemini
        if not settings.GEMINI_API_KEY:
            logger.error("GEMINI_API_KEY não configurada! Adicione em .env para usar Copilot/Resumo")
            raise ValueError("GEMINI_API_KEY não configurada em .env")
        
        try:
            genai.configure(api_key=settings.GEMINI_API_KEY)
            # Note: Validação real da API key acontece na primeira chamada generate_content()
            self.modelo_gemini = genai.GenerativeModel('gemini-3-flash-preview')
            self.trava_gemini = threading.Lock()  # ✅ Protege Gemini API calls
            logger.info("Gemini API configurado com sucesso")
        except Exception as e:
            logger.error(f"Erro ao configurar Gemini: {e}")
            raise

        # 2. Carrega o modelo Whisper (Local) para a Aba 1 - AGORA LAZY LOADED
        self.modelo_whisper = None
        self.trava_whisper = threading.Lock()
        logger.info("Whisper pronto para lazy loading (carregará na primeira uso)")
        
        # 3. Inicializa o motor rápido do Google para a Aba 2
        self.recognizer_google = sr.Recognizer()
        logger.info("Google Speech Recognition inicializado")

    def _load_whisper_lazy(self):
        """Carrega Whisper sob demanda (primeira uso)"""
        if self.modelo_whisper is not None:
            return
        
        with self.trava_whisper:
            if self.modelo_whisper is not None:  # Double-check após adquirir lock
                return
            
            try:
                import whisper  # Import aqui para evitar module-level
                logger.info("Carregando modelo Whisper na RAM (pode levar 2-3 segundos)...")
                self.modelo_whisper = whisper.load_model("tiny")
                logger.info("Whisper carregado com sucesso")
            except Exception as e:
                logger.error(f"Erro ao carregar Whisper: {e}")
                raise

    # --- FUNÇÕES DO GEMINI (TEXTO) ---
    def resumir_aula_gemini(self, materia, texto_transcrito):
        """Gera o resumo estruturado da aula usando o Gemini com proteção de thread e retry"""
        if not texto_transcrito or len(texto_transcrito.strip()) < 50:
            logger.warning("Texto muito curto para resumir. Mínimo 50 caracteres.")
            return "Texto insuficiente para gerar resumo."
        
        prompt = f"Matéria: {materia}. Resuma EXCLUSIVAMENTE baseado neste texto:\n{texto_transcrito}"
        
        def _call_gemini():
            with self.trava_gemini:  # ✅ Lock protege Gemini
                logger.debug(f"Chamando Gemini.generate_content com prompt de {len(prompt)} chars")
                response = self.modelo_gemini.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(max_output_tokens=500, temperature=0.7)
                )
                return response.text if response else "Nenhuma resposta do Gemini"
        
        try:
            # ✅ NOVO: Retry automático
            result = retry_with_backoff(_call_gemini, max_retries=2, initial_delay=2)
            logger.info(f"Resumo Gemini gerado com sucesso ({len(result)} chars)")
            return result
        except Exception as e:
            logger.error(f"Erro ao gerar resumo Gemini após retries: {e}", exc_info=True)
            return f"Erro ao gerar resumo: {str(e)[:100]}"

    def responder_pergunta_copilot(self, contexto, pergunta):
        """Gera a resposta rápida para a Aba Copilot com proteção de thread e retry"""
        if not pergunta or len(pergunta.strip()) < 3:
            logger.warning("Pergunta muito curta. Mínimo 3 caracteres.")
            return "Pergunta inválida ou muito curta."
        
        prompt = f"Contexto: {contexto}. Responda a pergunta em máximo 100 palavras: {pergunta}"
        
        def _call_gemini():
            with self.trava_gemini:  # ✅ Lock protege Gemini
                logger.debug("Chamando Gemini.generate_content para Copilot")
                response = self.modelo_gemini.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(max_output_tokens=200, temperature=0.5)
                )
                return response.text if response else "Nenhuma resposta"
        
        try:
            # ✅ NOVO: Retry automático
            result = retry_with_backoff(_call_gemini, max_retries=2, initial_delay=1)
            logger.info(f"Resposta Copilot gerada com sucesso ({len(result)} chars)")
            return result
        except Exception as e:
            logger.error(f"Erro ao gerar resposta Copilot após retries: {e}", exc_info=True)
            return f"Desculpe, não consegui processar sua pergunta: {str(e)[:80]}"

    # --- FUNÇÕES DE TRANSCRIÇÃO (ÁUDIO PARA TEXTO) ---
    def transcrever_arquivo_whisper(self, caminho_wav):
        """Usa o Whisper Local para transcrever o arquivo longo da aula (Aba 1)"""
        self._load_whisper_lazy()  # ✅ Carrega se não tiver carregado
        
        with self.trava_whisper:
            try:
                # Mantemos travado em PT para evitar alucinações na aula longa
                logger.info(f"Transcrevendo arquivo Whisper: {caminho_wav}")
                resultado = self.modelo_whisper.transcribe(caminho_wav, language="pt", fp16=False)
                texto = resultado['text'].strip()
                logger.info(f"Transcrição Whisper concluída ({len(texto)} chars)")
                return texto
            except Exception as e:
                logger.error(f"Erro ao transcrever com Whisper: {e}", exc_info=True)
                raise

    def transcrever_memoria_google(self, dados_brutos, idioma="pt-BR"):
        """Transcreve áudio em chuncos com confidence threshold"""
        try:
            # ✅ LOG: Verificar dados brutos
            if not dados_brutos or len(dados_brutos) < 100:
                logger.warning(f"⚠️ Dados de áudio inválidos: {len(dados_brutos) if dados_brutos else 0} bytes")
                return ""
            
            logger.info(f"🔊 Transcrevendo {len(dados_brutos)} bytes de áudio ({len(dados_brutos)/1024:.1f}KB)")
            audio_virtual = sr.AudioData(dados_brutos, settings.SAMPLE_RATE, settings.get_sample_width())
            
            # show_all=True permite ver o nível de certeza da IA (0.0 a 1.0)
            logger.debug(f"  └─ Chamando Google Speech Recognition (idioma: {idioma})...")
            predicao = self.recognizer_google.recognize_google(audio_virtual, language=idioma, show_all=True)
            
            if not predicao or 'alternative' not in predicao:
                logger.warning(f"⚠️ Google Speech: sem alternativas retornadas")
                return ""

            melhor_hipotese = predicao['alternative'][0]
            confianca = melhor_hipotese.get('confidence', 0)
            transcript = melhor_hipotese.get('transcript', '').strip()

            # ✅ NOVO: Log mais detalhado
            logger.info(f"📝 Google Speech Result:")
            logger.info(f"   Texto: '{transcript}'")
            logger.info(f"   Confiança: {confianca:.2%} (Threshold: {settings.CONFIDENCE_THRESHOLD:.2%})")
            
            # SÓ ESCREVE NA TELA SE TIVER MAIS DE THRESHOLD DE CERTEZA
            # Isso impede que o chiado vire "palavras fantasma"
            if confianca > settings.CONFIDENCE_THRESHOLD:
                logger.info(f"✅ Legenda aceita: '{transcript}'")
                return transcript
            else:
                logger.debug(f"❌ Confiança insuficiente ({confianca:.2%} <= {settings.CONFIDENCE_THRESHOLD:.2%}) - ignorado")
                return ""  # Se for incerto, ignora (fica limpo)
            
        except sr.UnknownValueError:
            logger.warning(f"⚠️ Google Speech: não conseguiu compreender o áudio (ruído?)")
            return ""
        except sr.RequestError as e:
            logger.error(f"❌ Google Speech erro de requisição: {e}")
            return ""
        except Exception as e:
            logger.error(f"❌ Erro inesperado em Google Speech: {e}", exc_info=True)
            return ""

