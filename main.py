# main.py
import sys
import logging

# ✅ Configurar logging PRIMEIRO, antes de qualquer outro import
from config.logging_config import log_file_path

from gui.app_window import MeetAssistantApp

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    try:
        logger.info("Iniciando Meet Assistant AI v3.2...")
        app = MeetAssistantApp()
        app.mainloop()
    except KeyboardInterrupt:
        logger.info("Aplicação interrompida pelo usuário")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Erro fatal na aplicação: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Meet Assistant encerrado")
