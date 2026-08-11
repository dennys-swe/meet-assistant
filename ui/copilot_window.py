"""Janela flutuante do Copilot.

Regras que a v1 ensinou, por terem dado errado lá:

1. **Tudo que vem de outra thread entra por `after(0, ...)`.** Tkinter não é
   thread-safe; transcrição e LLM rodam fora da main thread.
2. **O switch "Ouvir" não para o motor de captura.** Ele só liga e desliga a
   inscrição no pipeline. Na v1, ligar/desligar mexia no motor de áudio e era
   a origem dos bugs de estado.
3. **Nada de `except: pass` silencioso.** Erro que o usuário precisa ver vai
   para a barra de status.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import customtkinter as ctk

from config import settings as cfg
from modes.copilot.detector import Detection
from modes.copilot.engine import Exchange

logger = logging.getLogger(__name__)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("dark-blue")

COR_PERGUNTA = "#FFA502"
COR_OK = "#2ED573"
COR_ERRO = "#FF4757"
COR_APAGADA = "#6B7280"
FONTE = "Roboto"


class CopilotWindow(ctk.CTk):
    def __init__(
        self,
        settings: cfg.Settings,
        on_settings_saved: Callable[[cfg.Settings], None] | None = None,
        on_listen_toggled: Callable[[bool], None] | None = None,
        on_auto_toggled: Callable[[bool], None] | None = None,
        on_answer_last: Callable[[], None] | None = None,
        on_context_changed: Callable[[str], None] | None = None,
        on_test_connection: Callable[[cfg.Settings], str | None] | None = None,
    ):
        super().__init__()
        self.settings = settings
        self.on_settings_saved = on_settings_saved
        self.on_listen_toggled = on_listen_toggled
        self.on_auto_toggled = on_auto_toggled
        self.on_answer_last = on_answer_last
        self.on_context_changed = on_context_changed
        self.on_test_connection = on_test_connection

        self.title("Meet Assistant — Copilot")
        self.geometry("440x660")
        self.minsize(380, 520)
        self.attributes("-topmost", True)

        self._respondendo = False
        self._container = None

        if self.settings.is_ready:
            self.mostrar_copilot()
        else:
            self.mostrar_configuracao(primeira_vez=True)

    # ==================================================================
    # Troca de tela
    # ==================================================================

    def _limpar(self) -> None:
        if self._container is not None:
            self._container.destroy()
        self._container = ctk.CTkFrame(self, fg_color="transparent")
        self._container.pack(fill="both", expand=True, padx=14, pady=14)

    # ==================================================================
    # Tela de configuração (BYOK)
    # ==================================================================

    def mostrar_configuracao(self, primeira_vez: bool = False) -> None:
        self._limpar()
        c = self._container

        titulo = "Bem-vindo 👋" if primeira_vez else "Configurações"
        ctk.CTkLabel(c, text=titulo, font=(FONTE, 20, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            c,
            text="Use sua própria chave de API. Ela fica só na sua máquina —\n"
                 "nada é enviado para nenhum servidor nosso.",
            font=(FONTE, 11),
            text_color=COR_APAGADA,
            justify="left",
        ).pack(anchor="w", pady=(2, 14))

        ctk.CTkLabel(c, text="Provedor", font=(FONTE, 12, "bold")).pack(anchor="w")
        self._rotulos = {p.label: p.key for p in cfg.PROVIDERS.values()}
        atual = cfg.PROVIDERS.get(self.settings.provider)
        self._combo_provedor = ctk.CTkOptionMenu(
            c, values=list(self._rotulos), command=self._ao_trocar_provedor
        )
        self._combo_provedor.set(atual.label if atual else next(iter(self._rotulos)))
        self._combo_provedor.pack(fill="x", pady=(2, 10))

        self._link = ctk.CTkLabel(c, text="", font=(FONTE, 10), text_color="#5B9BD5")
        self._link.pack(anchor="w", pady=(0, 6))

        ctk.CTkLabel(c, text="Chave de API", font=(FONTE, 12, "bold")).pack(anchor="w")
        self._entry_chave = ctk.CTkEntry(c, show="•", placeholder_text="sk-...")
        self._entry_chave.pack(fill="x", pady=(2, 10))
        if self.settings.api_key:
            self._entry_chave.insert(0, self.settings.api_key)

        ctk.CTkLabel(c, text="Modelo", font=(FONTE, 12, "bold")).pack(anchor="w")
        self._entry_modelo = ctk.CTkEntry(c)
        self._entry_modelo.pack(fill="x", pady=(2, 10))
        self._entry_modelo.insert(0, self.settings.model)

        self._frame_url = ctk.CTkFrame(c, fg_color="transparent")
        ctk.CTkLabel(self._frame_url, text="URL da API", font=(FONTE, 12, "bold")).pack(anchor="w")
        self._entry_url = ctk.CTkEntry(self._frame_url, placeholder_text="https://.../v1")
        self._entry_url.pack(fill="x", pady=(2, 0))
        self._entry_url.insert(0, self.settings.base_url)

        self._status_config = ctk.CTkLabel(c, text="", font=(FONTE, 11), wraplength=380, justify="left")
        self._status_config.pack(anchor="w", pady=(8, 6))

        botoes = ctk.CTkFrame(c, fg_color="transparent")
        botoes.pack(fill="x", side="bottom")
        self._btn_testar = ctk.CTkButton(
            botoes, text="Testar conexão", fg_color="#374151", hover_color="#4B5563",
            command=self._testar_conexao,
        )
        self._btn_testar.pack(side="left", expand=True, fill="x", padx=(0, 5))
        ctk.CTkButton(botoes, text="Salvar", command=self._salvar).pack(
            side="left", expand=True, fill="x", padx=(5, 0)
        )

        if not primeira_vez:
            ctk.CTkButton(
                c, text="← Voltar", fg_color="transparent", hover_color="#374151",
                command=self.mostrar_copilot,
            ).pack(side="bottom", fill="x", pady=(0, 6))

        self._ao_trocar_provedor(self._combo_provedor.get())

    def _ao_trocar_provedor(self, rotulo: str) -> None:
        preset = cfg.PROVIDERS[self._rotulos[rotulo]]

        if preset.key == "custom":
            self._frame_url.pack(fill="x", pady=(0, 10))
        else:
            self._frame_url.pack_forget()
            self._entry_url.delete(0, "end")
            self._entry_url.insert(0, preset.base_url)

        if preset.default_model and not self._entry_modelo.get().strip():
            self._entry_modelo.delete(0, "end")
            self._entry_modelo.insert(0, preset.default_model)

        if preset.needs_key:
            self._entry_chave.configure(state="normal", placeholder_text="sk-...")
            self._link.configure(
                text=f"Pegue sua chave em {preset.signup_url}" if preset.signup_url else ""
            )
        else:
            self._entry_chave.configure(state="normal", placeholder_text="(não precisa)")
            self._link.configure(text="O Ollama roda local; nenhuma chave é necessária.")

    def _coletar(self) -> cfg.Settings:
        preset = cfg.PROVIDERS[self._rotulos[self._combo_provedor.get()]]
        return cfg.Settings(
            provider=preset.key,
            base_url=self._entry_url.get().strip() or preset.base_url,
            api_key=self._entry_chave.get().strip(),
            model=self._entry_modelo.get().strip(),
            user_context=self.settings.user_context,
        )

    def _testar_conexao(self) -> None:
        s = self._coletar()
        if pendencia := s.validate():
            self._status_config.configure(text=f"⚠️ {pendencia}", text_color=COR_PERGUNTA)
            return
        if self.on_test_connection is None:
            return

        self._btn_testar.configure(state="disabled", text="Testando...")
        self._status_config.configure(text="Falando com o provedor...", text_color=COR_APAGADA)

        def worker() -> None:
            erro = self.on_test_connection(s)
            self.after(0, self._resultado_teste, erro)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _resultado_teste(self, erro: str | None) -> None:
        self._btn_testar.configure(state="normal", text="Testar conexão")
        if erro:
            self._status_config.configure(text=f"❌ {erro}", text_color=COR_ERRO)
        else:
            self._status_config.configure(text="✅ Conexão funcionando.", text_color=COR_OK)

    def _salvar(self) -> None:
        s = self._coletar()
        if pendencia := s.validate():
            self._status_config.configure(text=f"⚠️ {pendencia}", text_color=COR_PERGUNTA)
            return

        self.settings = s
        cfg.save(s)
        if self.on_settings_saved:
            self.on_settings_saved(s)
        self.mostrar_copilot()

    # ==================================================================
    # Tela principal
    # ==================================================================

    def mostrar_copilot(self) -> None:
        self._limpar()
        c = self._container

        topo = ctk.CTkFrame(c, fg_color="transparent")
        topo.pack(fill="x")
        self._switch_ouvir = ctk.CTkSwitch(topo, text="Ouvir", command=self._alternar_escuta)
        self._switch_ouvir.pack(side="left")
        self._switch_auto = ctk.CTkSwitch(topo, text="Auto", command=self._alternar_auto)
        self._switch_auto.select()
        self._switch_auto.pack(side="left", padx=(14, 0))
        ctk.CTkButton(
            topo, text="⚙", width=32, fg_color="transparent", hover_color="#374151",
            command=lambda: self.mostrar_configuracao(),
        ).pack(side="right")

        self._label_fonte = ctk.CTkLabel(c, text="", font=(FONTE, 10), text_color=COR_APAGADA)
        self._label_fonte.pack(anchor="w", pady=(2, 8))

        ctk.CTkLabel(c, text="Transcrição", font=(FONTE, 11, "bold")).pack(anchor="w")
        # Transcrição e resposta dividem o espaço livre. Fixar a transcrição
        # deixava a resposta ocupando meia janela vazia enquanto a conversa
        # rolava espremida em cinco linhas.
        self._box_transcricao = ctk.CTkTextbox(c, height=170, fg_color="#111827", wrap="word")
        self._box_transcricao.pack(fill="both", expand=True, pady=(2, 10))
        self._box_transcricao.configure(state="disabled")
        self._box_transcricao.tag_config("pergunta", foreground=COR_PERGUNTA)
        self._box_transcricao.tag_config("normal", foreground="#D1D5DB")

        cabecalho = ctk.CTkFrame(c, fg_color="transparent")
        cabecalho.pack(fill="x")
        ctk.CTkLabel(cabecalho, text="Resposta", font=(FONTE, 11, "bold")).pack(side="left")
        self._label_gatilho = ctk.CTkLabel(cabecalho, text="", font=(FONTE, 10), text_color=COR_APAGADA)
        self._label_gatilho.pack(side="right")

        self._box_resposta = ctk.CTkTextbox(
            c, height=170, fg_color="#0F172A", text_color=COR_OK, font=(FONTE, 14), wrap="word"
        )
        self._box_resposta.pack(fill="both", expand=True, pady=(2, 8))
        self._placeholder_resposta = (
            "Ligue o switch “Ouvir”. A resposta aparece aqui assim que uma "
            "pergunta for detectada."
        )
        self._box_resposta.insert("0.0", self._placeholder_resposta)
        self._box_resposta.configure(state="disabled")

        ctk.CTkButton(
            c, text="Responder último turno", height=34, fg_color="#374151",
            hover_color="#4B5563", command=self._responder_ultimo,
        ).pack(fill="x")

        self._contexto_aberto = False
        self._btn_contexto = ctk.CTkButton(
            c, text="▸ Contexto (currículo, vaga, matéria)", height=28,
            fg_color="transparent", hover_color="#374151", anchor="w",
            command=self._alternar_contexto,
        )
        self._btn_contexto.pack(fill="x", pady=(6, 0))

        self._box_contexto = ctk.CTkTextbox(c, height=80, wrap="word")
        if self.settings.user_context:
            self._box_contexto.insert("0.0", self.settings.user_context)
        self._box_contexto.bind("<FocusOut>", lambda _e: self._salvar_contexto())

        self._status = ctk.CTkLabel(c, text="", font=(FONTE, 10), text_color=COR_APAGADA, wraplength=400)
        self._status.pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------------
    # Interações
    # ------------------------------------------------------------------

    def _alternar_escuta(self) -> None:
        ligado = bool(self._switch_ouvir.get())
        if self.on_listen_toggled:
            self.on_listen_toggled(ligado)
        self.set_status("Ouvindo o áudio do sistema." if ligado else "Escuta pausada.")

    def _alternar_auto(self) -> None:
        ligado = bool(self._switch_auto.get())
        if self.on_auto_toggled:
            self.on_auto_toggled(ligado)
        self.set_status(
            "Respondendo automaticamente às perguntas detectadas." if ligado
            else "Modo manual: use “Responder último turno”. Economiza cota."
        )

    def _alternar_contexto(self) -> None:
        self._contexto_aberto = not self._contexto_aberto
        if self._contexto_aberto:
            self._box_contexto.pack(fill="x", pady=(4, 0))
            self._btn_contexto.configure(text="▾ Contexto (currículo, vaga, matéria)")
        else:
            self._salvar_contexto()
            self._box_contexto.pack_forget()
            self._btn_contexto.configure(text="▸ Contexto (currículo, vaga, matéria)")

    def _salvar_contexto(self) -> None:
        texto = self._box_contexto.get("0.0", "end").strip()
        if texto == self.settings.user_context:
            return
        self.settings.user_context = texto
        if self.on_context_changed:
            self.on_context_changed(texto)
        cfg.save(self.settings)

    def _responder_ultimo(self) -> None:
        if self.on_answer_last:
            self.on_answer_last()

    # ==================================================================
    # API chamada de outras threads — sempre via after(0, ...)
    # ==================================================================

    def set_source(self, descricao: str) -> None:
        self.after(0, lambda: self._label_fonte.configure(text=f"🎧 {descricao}"))

    def set_status(self, texto: str, cor: str = COR_APAGADA) -> None:
        self.after(0, lambda: self._status.configure(text=texto, text_color=cor))

    def append_turn(self, texto: str, deteccao: Detection) -> None:
        self.after(0, self._append_turn, texto, deteccao)

    def _append_turn(self, texto: str, deteccao: Detection) -> None:
        box = self._box_transcricao
        box.configure(state="normal")
        marca = "❓ " if deteccao.is_question else "· "
        # Linha em branco entre turnos: sem ela, blocos longos de fala
        # contínua viram um paredão ilegível.
        box.insert("end", f"{marca}{texto}\n\n", "pergunta" if deteccao.is_question else "normal")
        box.see("end")
        box.configure(state="disabled")

    def start_answer(self, troca: Exchange) -> None:
        self.after(0, self._start_answer, troca)

    def _start_answer(self, troca: Exchange) -> None:
        self._respondendo = True
        self._label_gatilho.configure(text=troca.detection.reason, text_color=COR_APAGADA)
        self._status.configure(text="")  # limpa erro da resposta anterior
        box = self._box_resposta
        box.configure(state="normal")
        box.delete("0.0", "end")
        box.insert("0.0", "Consultando…")
        box.configure(state="disabled")

    def append_answer_chunk(self, troca: Exchange, _pedaco: str) -> None:
        # Reescrevemos o texto acumulado em vez de anexar o pedaço: assim uma
        # resposta cancelada no meio não deixa restos na tela.
        self.after(0, self._set_answer, troca.answer)

    def _set_answer(self, texto: str) -> None:
        box = self._box_resposta
        box.configure(state="normal")
        box.delete("0.0", "end")
        box.insert("0.0", texto)
        box.see("end")
        box.configure(state="disabled")

    def finish_answer(self, troca: Exchange) -> None:
        self.after(0, self._finish_answer, troca)

    def _finish_answer(self, troca: Exchange) -> None:
        self._respondendo = False
        if troca.error:
            # A caixa não pode ficar em "Consultando…" para sempre: o erro
            # aparece no rodapé, mas quem olha a resposta precisa entender
            # que ela não vem.
            self._set_answer("—")
            self._label_gatilho.configure(text="falhou", text_color=COR_ERRO)
        elif troca.ignored:
            self._set_answer(self._placeholder_resposta)
            self._label_gatilho.configure(text="não exigia resposta")

    def show_error(self, mensagem: str) -> None:
        self.set_status(f"❌ {mensagem}", COR_ERRO)
