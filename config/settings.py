import os
import logging
from dotenv import load_dotenv

# Carrega as variáveis de ambiente (o seu .env)
load_dotenv()

# Configure logging
logger = logging.getLogger(__name__)

# --- CHAVES DE API ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY não configurada. Aba Copilot/Resumo não funcionará. Adicione a chave em .env")

# --- CONFIGURAÇÕES DE ÁUDIO GLOBAIS ---
DEVICE_INDEX = int(os.getenv("DEVICE_INDEX", "-1"))  # -1 = default do sistema
SAMPLE_RATE = 16000  # ✅ OTIMIZADO: Google Speech reconhece melhor em 16kHz
CHUNK = 1024
CHANNELS = 1  # ✅ MUDADO: Mono para melhor compatibilidade com Google Speech
_SAMPLE_WIDTH = None  # Lazy-loaded

# --- CONFIGURAÇÕES DE OBSIDIAN ---
OBSIDIAN_PATH = os.getenv("OBSIDIAN_PATH", os.path.join(os.path.expanduser("~"), "OneDrive", "Documentos", "Obsidian"))
if not os.path.exists(OBSIDIAN_PATH):
    logger.warning(f"⚠️ OBSIDIAN_PATH '{OBSIDIAN_PATH}' não existe. Verifique o caminho no .env")

# --- CONFIGURAÇÕES DE INTERFACE (TEMA) ---
APPEARANCE_MODE = "Dark"
COLOR_THEME = "dark-blue"
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))

# --- FUNÇÕES DE INICIALIZAÇÃO ---
def get_sample_width():
    """Lazy load sample width (evita inicializar PyAudio no module level)"""
    global _SAMPLE_WIDTH
    if _SAMPLE_WIDTH is None:
        import pyaudio  # Import aqui para evitar module-level
        try:
            _SAMPLE_WIDTH = pyaudio.get_sample_size(pyaudio.paInt16)
        except Exception as e:
            logger.error(f"Erro ao obter sample width: {e}")
            _SAMPLE_WIDTH = 2  # Fallback para 16-bit
    return _SAMPLE_WIDTH

def validate_device_index():
    """Valida e lista dispositivos disponíveis; retorna DEVICE_INDEX se válido, senão default"""
    try:
        import pyaudio  # Import aqui para evitar module-level
        p = pyaudio.PyAudio()
        device_count = p.get_device_count()
        
        logger.info(f"\n{'='*80}")
        logger.info(f"DISPOSITIVOS DE ÁUDIO DISPONÍVEIS ({device_count} total):")
        logger.info(f"{'='*80}")
        for i in range(device_count):
            try:
                info = p.get_device_info_by_index(i)
                maxInputChannels = info['maxInputChannels']
                maxOutputChannels = info['maxOutputChannels']
                sampleRate = int(info['defaultSampleRate'])
                nome = info['name']
                
                marker = ""
                # Marca dispositivos potencialmente úteis
                if 'stereo mix' in nome.lower() or 'what u hear' in nome.lower():
                    marker = " 🎙️ (STEREO MIX - potencial captura do Meet)"
                elif 'mic' in nome.lower():
                    marker = " 🎤 (MICROFONE)"
                elif 'speaker' in nome.lower() or 'output' in nome.lower():
                    marker = " 🔊 (SPEAKER/OUTPUT)"
                
                logger.info(
                    f"  [{i}] {nome}{marker}\n"
                    f"       └─ In: {maxInputChannels}ch, Out: {maxOutputChannels}ch, Taxa: {sampleRate}Hz"
                )
            except Exception as e:
                logger.debug(f"Erro ao listar device {i}: {e}")
        
        logger.info(f"{'='*80}\n")
        p.terminate()
        
        # Se DEVICE_INDEX = -1 (default), usa o padrão do sistema
        if DEVICE_INDEX == -1:
            logger.info("✅ Usando dispositivo DEFAULT do sistema (-1)")
            return -1
        
        if DEVICE_INDEX < 0 or DEVICE_INDEX >= device_count:
            logger.warning(f"⚠️ Device {DEVICE_INDEX} não encontrado. Usando default (0).")
            return 0
        
        logger.info(f"✅ Device validado: {DEVICE_INDEX}")
        return DEVICE_INDEX
        
    except Exception as e:
        logger.error(f"Erro ao validar device: {e}. Usando default (0).")
        return 0

# Inicializar device no módulo (chamado uma vez)
DEVICE_INDEX_VALIDATED = validate_device_index()