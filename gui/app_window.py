import customtkinter as ctk
import threading
import time
import re
import os
import ctypes
import speech_recognition as sr
import logging
from datetime import datetime
from collections import deque

# Importamos os nossos módulos limpos!
from core.ai_services import AIService
from core.audio_engine import AudioEngine
from config import settings

logger = logging.getLogger(__name__)

ctk.set_appearance_mode(settings.APPEARANCE_MODE)
ctk.set_default_color_theme(settings.COLOR_THEME)

class MeetAssistantApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # 1. ESSENCIAL: Criar o serviço primeiro
        try:
            self.ai_service = AIService()
        except ValueError as e:
            logger.error(f"Erro crítico na inicialização: {e}")
            self._show_error_dialog(f"Erro na inicialização:\n{str(e)}")
            return
        except Exception as e:
            logger.error(f"Erro inesperado ao inicializar AIService: {e}")
            self._show_error_dialog(f"Erro inesperado:\n{str(e)}")
            return

        # 2. AGORA SIM: Calibrar o ruído usando o serviço que acabamos de criar
        try:
            device_idx = settings.DEVICE_INDEX_VALIDATED
            logger.info(f"Calibrando ruído com device {device_idx}...")
            with sr.Microphone(device_index=device_idx) as source:
                # O recognizer vive dentro do ai_service, por isso a ordem acima é vital
                self.ai_service.recognizer_google.adjust_for_ambient_noise(source, duration=1)
            logger.info("Calibração de ruído concluída")
        except Exception as e:
            logger.warning(f"Aviso: Não foi possível calibrar o ruído: {e}")

        # 3. Criar o motor de áudio e configurar a janela
        self.audio_engine = AudioEngine(callback_processar_chunk=self._atualizar_legendas_copilot_safe)
        logger.info("MeetAssistantApp inicializado com sucesso")


        self.title("Meet Assistant AI - v3.2 (Modular)")
        self.geometry("400x550")
        self.resizable(False, False)
        self.attributes("-topmost", True)    
        
        # Variáveis de Estado da UI
        self.historico_texto_copilot = deque(maxlen=4)
        self.inicio_gravacao = None
        self.tempo_pausado = 0
        self.inicio_pausa_atual = None
        self.idioma_copilot = "pt-BR"

        self.setup_ui()
        
        # Liga a escuta do microfone
        self.audio_engine.iniciar()
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def setup_ui(self):
        # Cabeçalho
        self.frame_top = ctk.CTkFrame(self, fg_color="transparent")
        self.frame_top.pack(fill="x", padx=10, pady=(10, 0))
        
        self.seletor_idioma = ctk.CTkSegmentedButton(self.frame_top, values=["PT", "ES", "EN"], command=self.alterar_idioma)
        self.seletor_idioma.set("PT")
        self.seletor_idioma.pack(side="left")

        self.switch_stealth = ctk.CTkSwitch(self.frame_top, text="Modo Stealth", command=self.toggle_stealth)
        self.switch_stealth.pack(side="right")

        # Abas
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=10)
        
        self.tab_aula = self.tabview.add("📚 Modo Aula")
        self.tab_copilot = self.tabview.add("🚀 Copilot (Ao Vivo)")

        self.setup_aba_aula()
        self.setup_aba_copilot()

    def alterar_idioma(self, valor):
        mapa = {"PT": "pt-BR", "ES": "es-ES", "EN": "en-US"}
        self.idioma_copilot = mapa.get(valor, "pt-BR")

    def toggle_stealth(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            if self.switch_stealth.get():
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
            else:
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0)
        except Exception:
            pass

    def toggle_copilot_listening(self):
        try:
            if self.switch_copilot_listening.get():
                self.audio_engine.ativar_copilot()
                self.label_live.configure(text="🔴 Ouvindo... (Últimas falas):", text_color="#FFA502")
            else:
                self.audio_engine.desativar_copilot()
                self.label_live.configure(text="⚪ Copilot pausado", text_color="gray")
        except Exception:
            pass

    # ================= ABA AULA =================
    def setup_aba_aula(self):
        self.entry_materia = ctk.CTkEntry(self.tab_aula, placeholder_text="Matéria")
        self.entry_materia.pack(fill="x", pady=(10, 5))
        self.entry_tema = ctk.CTkEntry(self.tab_aula, placeholder_text="Tema")
        self.entry_tema.pack(fill="x", pady=(0, 15))

        self.label_timer = ctk.CTkLabel(self.tab_aula, text="00:00", font=("Roboto", 35, "bold"))
        self.label_timer.pack(pady=(0, 5))
        self.label_substatus = ctk.CTkLabel(self.tab_aula, text="", font=("Roboto", 12, "bold"))
        self.label_substatus.pack(pady=(0, 10))

        self.btn_acao_aula = ctk.CTkButton(self.tab_aula, text="INICIAR GRAVAÇÃO", fg_color="#6C63FF", command=self.toggle_aula)
        self.btn_acao_aula.pack(fill="x")
        self.btn_pausa = ctk.CTkButton(self.tab_aula, text="⏸ PAUSAR", fg_color="#FFA502", state="disabled", command=self.toggle_pausa)
        self.btn_pausa.pack(fill="x", pady=(10, 0))

    def toggle_aula(self):
        if not self.audio_engine.gravando_aula:
            self.audio_engine.iniciar_gravacao_aula()
            self.inicio_gravacao = datetime.now()
            self.tempo_pausado = 0
            self.btn_acao_aula.configure(text="⏹ PARAR E SALVAR", fg_color="#FF4757")
            self.btn_pausa.configure(state="normal", text="⏸ PAUSAR", fg_color="#FFA502")
            self.atualizar_cronometro()
        else:
            self.btn_acao_aula.configure(state="disabled", text="PROCESSANDO IA...")
            self.btn_pausa.configure(state="disabled")
            threading.Thread(target=self.processar_aula_backend).start()

    def toggle_pausa(self):
        pausado = self.audio_engine.alternar_pausa_aula()
        if pausado:
            self.btn_pausa.configure(text="▶ RETOMAR", fg_color="#2ED573")
            self.label_substatus.configure(text="⏸ EM PAUSA", text_color="yellow")
            self.inicio_pausa_atual = datetime.now()
        else:
            self.btn_pausa.configure(text="⏸ PAUSAR", fg_color="#FFA502")
            self.label_substatus.configure(text="")
            self.tempo_pausado += (datetime.now() - self.inicio_pausa_atual).total_seconds()

    def atualizar_cronometro(self):
        if self.audio_engine.gravando_aula:
            if not self.audio_engine.pausado_aula:
                delta = (datetime.now() - self.inicio_gravacao).total_seconds() - self.tempo_pausado
                mins, segs = divmod(int(delta), 60)
                self.label_timer.configure(text=f"{mins:02}:{segs:02}")
            self.after(1000, self.atualizar_cronometro)

    def processar_aula_backend(self):
        nome_wav = self.audio_engine.parar_gravacao_aula("temp_aula.wav")
        materia = self.entry_materia.get().strip()
        tema = self.entry_tema.get().strip()
        
        try:
            # Update UI via after() para thread-safety
            self.after(0, self._atualizar_ui_aula_safe, "Transcrevendo...", "white")
            logger.info(f"Transcrevendo aula: {nome_wav}")
            texto_transcrito = self.ai_service.transcrever_arquivo_whisper(nome_wav)
            
            if len(texto_transcrito) < 50:
                self.after(0, self._atualizar_ui_aula_safe, "⚠️ Áudio Curto (< 50 chars)", "orange")
                logger.warning(f"Áudio transcrito muito curto: {len(texto_transcrito)} chars")
            else:
                self.after(0, self._atualizar_ui_aula_safe, "Resumindo com Gemini...", "white")
                logger.info(f"Gerando resumo Gemini para matéria: {materia}")
                resumo_ia = self.ai_service.resumir_aula_gemini(materia, texto_transcrito)
                
                materia_limpa = re.sub(r'[\\/*?:"<>|]', "", materia) if materia else "Geral"
                nome_arquivo = f"{re.sub(r'[\\/*?:\"<>|]', '', tema)}.md" if tema else f"Aula_{int(time.time())}.md"
                path = os.path.join(settings.OBSIDIAN_PATH, materia_limpa)
                os.makedirs(path, exist_ok=True)
                
                caminho_completo = os.path.join(path, nome_arquivo)
                with open(caminho_completo, "w", encoding="utf-8") as f:
                    f.write(resumo_ia)
                logger.info(f"Resumo salvo em: {caminho_completo}")
                self.after(0, self._atualizar_ui_aula_safe, "✅ SALVO NO OBSIDIAN!", "#2ED573")
                
        except Exception as e:
            logger.error(f"Erro ao processar aula: {e}", exc_info=True)
            self.after(0, self._atualizar_ui_aula_safe, f"❌ ERRO: {str(e)[:30]}...", "red")
            
        self.after(0, lambda: self.btn_acao_aula.configure(state="normal", text="INICIAR GRAVAÇÃO", fg_color="#6C63FF"))

    # ================= ABA COPILOT =================
    def setup_aba_copilot(self):
        self.label_copilot = ctk.CTkLabel(self.tab_copilot, text="Contexto Base (Seu trabalho/currículo):", font=("Roboto", 12))
        self.label_copilot.pack(anchor="w", pady=(5,0))

        self.textbox_contexto = ctk.CTkTextbox(self.tab_copilot, height=70)
        self.textbox_contexto.pack(fill="x", pady=(0, 10))

        self.switch_copilot_listening = ctk.CTkSwitch(self.tab_copilot, text="Copilot ON/OFF", command=self.toggle_copilot_listening)
        self.switch_copilot_listening.pack(anchor="w", pady=(0, 10))
        self.switch_copilot_listening.deselect()

        self.label_live = ctk.CTkLabel(self.tab_copilot, text="⚪ Copilot pausado", font=("Roboto", 11, "bold"), text_color="gray")
        self.label_live.pack(anchor="w")
        
        self.textbox_live = ctk.CTkTextbox(self.tab_copilot, height=60, fg_color="#1E1E1E", text_color="white")
        self.textbox_live.pack(fill="x", pady=(0, 10))
        self.textbox_live.insert("0.0", "Aguardando áudio...")
        self.textbox_live.configure(state="disabled")

        self.btn_sos = ctk.CTkButton(
            self.tab_copilot, text="🚀 RESPONDER À PERGUNTA ACIMA", 
            font=("Roboto", 14, "bold"), height=45, fg_color="#FF4757", hover_color="#e04050",
            command=self.acionar_copilot
        )
        self.btn_sos.pack(fill="x", pady=5)

        self.textbox_resposta = ctk.CTkTextbox(self.tab_copilot, height=120, font=("Roboto", 16, "bold"), text_color="#2ED573")
        self.textbox_resposta.pack(fill="both", expand=True)
        self.textbox_resposta.insert("0.0", "A sugestão da IA aparecerá aqui...")
        self.textbox_resposta.configure(state="disabled")

    def _atualizar_legendas_copilot_safe(self, dados_brutos):
        """Callback chamado pelo AudioEngine a cada 3.5 segundos (thread-safe via after)"""
        try:
            if not dados_brutos or len(dados_brutos) < 100:
                logger.debug(f"⚠️ Áudio inválido recebido: {len(dados_brutos) if dados_brutos else 0} bytes")
                return
            
            logger.info(f"📥 Callback recebido: {len(dados_brutos)} bytes de áudio")
            
            # Transcreve em thread background, depois atualiza UI no main thread
            def _transcrever_e_atualizar():
                try:
                    logger.debug(f"🔄 Iniciando transcrição Google Speech (idioma: {self.idioma_copilot})...")
                    texto = self.ai_service.transcrever_memoria_google(dados_brutos, self.idioma_copilot)
                    
                    if texto and len(texto.strip()) > 0:
                        logger.info(f"✅ Legenda obtida: '{texto}'")
                        self.historico_texto_copilot.append(texto)
                        texto_completo = " ... ".join(self.historico_texto_copilot)
                        
                        # Atualiza UI via after() para garantir thread-safety
                        self.after(0, self._atualizar_textbox_live, texto_completo)
                    else:
                        logger.debug("❌ Transcrição vazia ou confiança baixa - nada a exibir")
                except Exception as e:
                    logger.error(f"❌ Erro ao transcrever audio copilot: {e}", exc_info=True)
            
            # Executa transcrição em thread separada para não bloquear UI
            threading.Thread(target=_transcrever_e_atualizar, daemon=True).start()
            
        except Exception as e:
            logger.error(f"❌ Erro em _atualizar_legendas_copilot_safe: {e}", exc_info=True)
    
    def _atualizar_textbox_live(self, texto):
        """Atualiza textbox_live de forma thread-safe no main thread"""
        self.textbox_live.configure(state="normal")
        self.textbox_live.delete("0.0", "end")
        self.textbox_live.insert("end", texto)
        self.textbox_live.configure(state="disabled")

    def atualizar_legendas_copilot(self, dados_brutos):
        """Callback chamado pelo AudioEngine a cada 3.5 segundos"""
        texto = self.ai_service.transcrever_memoria_google(dados_brutos, self.idioma_copilot)
        if texto:
            self.historico_texto_copilot.append(texto)
            texto_completo = " ... ".join(self.historico_texto_copilot)
            
            self.textbox_live.configure(state="normal")
            self.textbox_live.delete("0.0", "end")
            self.textbox_live.insert("end", texto_completo)
            self.textbox_live.configure(state="disabled")

    def acionar_copilot(self):
        texto_pergunta = self.textbox_live.get("0.0", "end").strip()
        contexto_usuario = self.textbox_contexto.get("0.0", "end").strip()
        
        if len(texto_pergunta) < 5 or "Aguardando áudio" in texto_pergunta:
            self.textbox_resposta.configure(state="normal")
            self.textbox_resposta.delete("0.0", "end")
            self.textbox_resposta.insert("0.0", "⚠️ Nenhuma pergunta captada ainda.")
            self.textbox_resposta.configure(state="disabled")
            return
            
        self.btn_sos.configure(state="disabled", text="⏳ GERANDO RESPOSTA...")
        self.textbox_resposta.configure(state="normal")
        self.textbox_resposta.delete("0.0", "end")
        self.textbox_resposta.insert("0.0", "Consultando o Gemini...")
        self.textbox_resposta.configure(state="disabled")
        
        threading.Thread(target=self.pedir_resposta_backend, args=(contexto_usuario, texto_pergunta)).start()

    def pedir_resposta_backend(self, contexto, pergunta):
        try:
            logger.info("Gerando resposta Copilot...")
            resposta_ia = self.ai_service.responder_pergunta_copilot(contexto, pergunta)
            self.after(0, self._atualizar_textbox_resposta_safe, resposta_ia)
            logger.info(f"Resposta Copilot gerada: {len(resposta_ia)} chars")
        except Exception as e:
            logger.error(f"Erro ao gerar resposta Copilot: {e}", exc_info=True)
            msg_erro = f"❌ Erro: {str(e)[:60]}..."
            self.after(0, self._atualizar_textbox_resposta_safe, msg_erro)
        finally:
            self.after(0, self._restaurar_botao_sos)

    def _atualizar_ui_aula_safe(self, status_text, status_color):
        """Atualiza UI da aula de forma thread-safe"""
        self.label_substatus.configure(text=status_text, text_color=status_color)

    def _atualizar_textbox_resposta_safe(self, texto):
        """Atualiza resposta de forma thread-safe"""
        self.textbox_resposta.configure(state="normal")
        self.textbox_resposta.delete("0.0", "end")
        self.textbox_resposta.insert("0.0", texto)
        self.textbox_resposta.configure(state="disabled")
    
    def _restaurar_botao_sos(self):
        """Restaura botão SOS após resposta"""
        self.btn_sos.configure(state="normal", text="🚀 RESPONDER À PERGUNTA ACIMA")

    def _show_error_dialog(self, mensagem):
        """Exibe diálogo de erro"""
        logger.error(f"Erro mostrado ao usuário: {mensagem}")
        # Cria label de erro na janela principal
        ctk.CTkLabel(self, text=mensagem, text_color="red", font=("Roboto", 12)).pack(pady=20)

    def on_closing(self):
        logger.info("Encerrando MeetAssistantApp...")
        self.audio_engine.parar()
        self.destroy()