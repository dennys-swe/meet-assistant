import pyaudio
import wave
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from config import settings

logger = logging.getLogger(__name__)

class AudioEngine:
    def __init__(self, callback_processar_chunk):
        self.motor_ligado = False
        
        # ✅ NOVO: Controles independentes para Copilot e Aula
        self.callback_copilot_ativo = False  # ON/OFF do Copilot sem parar motor
        self.gravacao_aula_ativa = False     # Controlado por botão de ativar/desativar
        self.pausado_aula = False             # Pausa/Retoma durante gravação
        
        self.frames_aula = []
        self.lock_frame_aula = threading.Lock()  # ✅ NOVO: Protege frames_aula de race conditions
        
        self.chunk_copilot = []
        self.lock_copilot = threading.Lock()  # Protege chunk_copilot de race conditions
        # 'callback' é a função da interface que será chamada a cada 3.5 segundos
        self.callback_processar_chunk = callback_processar_chunk 
        
        self.limite_captura = int((settings.SAMPLE_RATE / settings.CHUNK) * 3.5)
        self.thread_motor = None
        
        # ThreadPoolExecutor com limite de workers (evita threads explosão)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="AudioEngine-")
        logger.info("🎙️ AudioEngine criado (callbacks: OFF, gravação: OFF)")

    @property
    def gravando_aula(self):
        """Alias para compatibilidade com GUI - retorna gravacao_aula_ativa"""
        return self.gravacao_aula_ativa

    def iniciar(self):
        """Liga a 'orelha' em background"""
        self.motor_ligado = True
        self.thread_motor = threading.Thread(target=self._loop_gravacao, daemon=True)
        self.thread_motor.start()
        logger.info("AudioEngine iniciado")

    def parar(self):
        self.motor_ligado = False
        # ✅ NOVO: Shutdown do executor de forma segura
        try:
            self.executor.shutdown(wait=True)
            logger.info("ThreadPoolExecutor finalizado")
        except Exception as e:
            logger.error(f"Erro ao finalizar executor: {e}")
        logger.info("🛑 AudioEngine parado")

    # ═══════════════════════════════════════════════════════════════
    # ✅ NOVO: CONTROLES ON/OFF (ligar/desligar SEM parar motor)
    # ═══════════════════════════════════════════════════════════════
    
    def ativar_copilot(self):
        """Liga o Copilot (ativa callbacks de transcrição)"""
        if self.callback_copilot_ativo:
            logger.warning("⚠️ Copilot já está ativado")
            return
        
        self.callback_copilot_ativo = True
        logger.info("🚀 COPILOT ATIVADO - Legendas em tempo real")
    
    def desativar_copilot(self):
        """Desliga o Copilot (desativa callbacks, motor continua capturando)"""
        if not self.callback_copilot_ativo:
            logger.warning("⚠️ Copilot já está desativado")
            return
        
        self.callback_copilot_ativo = False
        with self.lock_copilot:
            self.chunk_copilot.clear()  # Limpa buffer pendente
        logger.info("🔇 COPILOT DESATIVADO - Escuta parada")
    
    def ativar_gravacao_aula(self):
        """Liga a gravação da aula (ativa acúmulo de frames_aula)"""
        if self.gravacao_aula_ativa:
            logger.warning("⚠️ Gravação de aula já está ativada")
            return
        
        with self.lock_frame_aula:
            self.frames_aula.clear()
        self.gravacao_aula_ativa = True
        self.pausado_aula = False
        logger.info("📚 GRAVAÇÃO DE AULA ATIVADA")
    
    def desativar_gravacao_aula(self):
        """Desliga a gravação da aula (para de acumular frames_aula)"""
        if not self.gravacao_aula_ativa:
            logger.warning("⚠️ Gravação de aula já está desativada")
            return
        
        self.gravacao_aula_ativa = False
        self.pausado_aula = False
        logger.info("⏹️ GRAVAÇÃO DE AULA DESATIVADA")

    # ═══════════════════════════════════════════════════════════════

    # --- CONTROLES DA ABA AULA ---
    def iniciar_gravacao_aula(self):
        """[RETROCOMPAT] Inicia gravação de aula (chama ativar_gravacao_aula)"""
        self.ativar_gravacao_aula()

    def alternar_pausa_aula(self):
        """Alterna pausa da gravação de aula"""
        self.pausado_aula = not self.pausado_aula
        status = "⏸ PAUSA" if self.pausado_aula else "▶ RETOMADA"
        logger.info(f"Aula {status}")
        return self.pausado_aula

    def parar_gravacao_aula(self, nome_arquivo="temp_aula.wav"):
        """Para gravação e salva arquivo WAV"""
        self.desativar_gravacao_aula()  # Usa nova flag
        try:
            sample_width = settings.get_sample_width()  # ✅ Lazy-load
            
            # ✅ NOVO: Lock protege frames_aula durante leitura
            with self.lock_frame_aula:
                frames_para_salvar = self.frames_aula.copy()
                self.frames_aula.clear()
            
            with wave.open(nome_arquivo, 'wb') as wf:
                wf.setnchannels(settings.CHANNELS)
                wf.setsampwidth(sample_width)
                wf.setframerate(settings.SAMPLE_RATE)
                wf.writeframes(b''.join(frames_para_salvar))
            logger.info(f"💾 Gravação salva em: {nome_arquivo}")
        except Exception as e:
            logger.error(f"Erro ao salvar gravação: {e}")
            raise
        return nome_arquivo

    # --- O LOOP INVISÍVEL ---
    def _loop_gravacao(self):
        """Loop invisível de captura contínua"""
        p = pyaudio.PyAudio()
        stream = None
        contador_frames = 0
        ultimo_log_tempo = 0  # ✅ NOVO: Para log a cada 10 segundos
        
        try:
            device_index = settings.DEVICE_INDEX_VALIDATED
            sample_width = settings.get_sample_width()  # ✅ Lazy-load
            
            logger.info(f"🎙️ Abrindo stream PyAudio: device={device_index}, rate={settings.SAMPLE_RATE}Hz, channels={settings.CHANNELS}, chunk={settings.CHUNK}")
            stream = p.open(
                format=pyaudio.paInt16, 
                channels=settings.CHANNELS, 
                rate=settings.SAMPLE_RATE,
                input=True, 
                input_device_index=device_index, 
                frames_per_buffer=settings.CHUNK
            )
            logger.info("✅ Stream PyAudio aberto com sucesso")
            
            while self.motor_ligado:
                try:
                    data = stream.read(settings.CHUNK, exception_on_overflow=False)
                    contador_frames += 1
                    tempo_atual = contador_frames * settings.CHUNK / settings.SAMPLE_RATE
                    
                    # ✅ LOG A CADA 10 SEGUNDOS (debug de captura)
                    if tempo_atual - ultimo_log_tempo >= 10:
                        logger.debug(f"📊 Captura contínua: {contador_frames} frames ({tempo_atual:.1f}s capturados)")
                        ultimo_log_tempo = tempo_atual
                    
                    if self.gravacao_aula_ativa and not self.pausado_aula:
                        self.frames_aula.append(data)
                    
                    # ✅ NOVO: Só acumula chunk_copilot se Copilot estiver ativado
                    if self.callback_copilot_ativo:
                        # ✅ NOVO: Lock protege chunk_copilot
                        with self.lock_copilot:
                            self.chunk_copilot.append(data)
                            chunk_size = len(self.chunk_copilot)
                            bytes_acumulados = sum(len(d) for d in self.chunk_copilot)
                        
                        # Se encheu o balde de 3.5s, manda para a interface via callback
                        if chunk_size >= self.limite_captura:
                            with self.lock_copilot:
                                dados_brutos = b''.join(self.chunk_copilot)
                                self.chunk_copilot.clear()
                            
                            # ✅ NOVO: Log detalhado do callback
                            tamanho_kb = len(dados_brutos) / 1024
                            logger.info(f"🎯 CALLBACK ACIONADO: {len(dados_brutos)} bytes (~{tamanho_kb:.1f}KB) | Callback: {'PRESENTE' if self.callback_processar_chunk else 'AUSENTE'}")
                            
                            # ✅ CRÍTICO: Verificar se callback existe
                            if self.callback_processar_chunk:
                                # ✅ NOVO: Usa ThreadPoolExecutor em vez de criar thread nova
                                self.executor.submit(self.callback_processar_chunk, dados_brutos)
                            else:
                                logger.warning("⚠️ callback_processar_chunk é None!")
                    else:
                        # Copilot desativado - limpa chunk pendente se existir
                        if self.chunk_copilot:
                            with self.lock_copilot:
                                self.chunk_copilot.clear()
                        
                except IOError as e:
                    logger.error(f"❌ Erro ao ler do stream (IOError): {e}", exc_info=True)
                    import time
                    time.sleep(0.1)
                    continue
                except Exception as e:
                    logger.error(f"❌ Erro inesperado ao ler chunk de áudio: {e}", exc_info=True)
                    
            if stream:
                stream.stop_stream()
                stream.close()
            logger.info("✅ Stream PyAudio fechado com sucesso")
            
        except Exception as e:
            logger.error(f"❌ Erro crítico no PyAudio: {e}", exc_info=True)
        finally:
            p.terminate()
            logger.info("PyAudio terminado")
