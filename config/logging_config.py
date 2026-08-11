"""
Configuração centralizada de logging para o Meet Assistant
"""
import logging
import logging.handlers
import os
from datetime import datetime

def setup_logging(log_level=logging.INFO):
    """Configura logging estruturado com arquivo + console"""
    
    # Verificar se já foi configurado para evitar duplicação
    root_logger = logging.getLogger()
    if root_logger.hasHandlers():
        # Já configurado, apenas ajustar nível se necessário
        root_logger.setLevel(log_level)
        for handler in root_logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(log_level)
        # Retornar o arquivo de log atual (se existir)
        for handler in root_logger.handlers:
            if hasattr(handler, 'baseFilename'):
                return handler.baseFilename
        return None  # Não encontrou arquivo
    
    # Criar pasta de logs se não existir
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    
    # Nome do arquivo de log com timestamp
    log_file = os.path.join(log_dir, f"meet_assistant_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    
    # Formato detalhado para arquivo (com timestamps e source)
    file_formatter = logging.Formatter(
        '%(asctime)s | %(name)-30s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Formato mais conciso para console
    console_formatter = logging.Formatter(
        '%(levelname)-8s | %(name)s | %(message)s'
    )
    
    # Configurar root logger
    root_logger.setLevel(log_level)
    
    # Handler para arquivo (com rotação automática)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5*1024*1024,  # 5MB antes de rotacionar
        backupCount=5  # Manter 5 arquivos rotacionados
    )
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)  # Arquivo captura tudo
    
    # Handler para console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(log_level)
    
    # Adicionar handlers ao root logger
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    
    # Log inicial
    root_logger.info("="*80)
    root_logger.info(f"MEET ASSISTANT INICIADO - Log file: {log_file}")
    root_logger.info("="*80)
    root_logger.info(f"Log level: {logging.getLevelName(log_level)}")
    
    return log_file

# Configure ao importar
log_file_path = setup_logging()
