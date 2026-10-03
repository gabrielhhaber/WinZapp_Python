"""
client/ui/dialogs/ai_result_dialog.py

Janela que mostra o resultado de uma transcrição de áudio, descrição de
imagem/vídeo ou conversão de PDF em texto acessível, de um jeito navegável:
o conteúdo é dividido em blocos (parágrafos/frases) e a pessoa pode andar
entre eles com as setas do teclado, para explorar com calma usando o leitor
de tela — em vez de precisar ouvir tudo de uma vez com um único "ler tudo".

Padrão de acessibilidade seguido: um wx.ListBox de itens navegáveis (o
NVDA/JAWS já leem cada item ao navegar com as setas, sem precisar de nenhum
código especial de leitura), mais uma caixa de texto multilinha somente
leitura contendo o texto completo, para quem preferir usar Ctrl+A/Ctrl+C ou
o modo de navegação do próprio leitor de tela.
"""

from __future__ import annotations

import re
import threading

import wx


def _split_into_blocks(text: str) -> list[str]:
    """
    Divide o texto em blocos navegáveis: primeiro por parágrafo (linhas em
    branco), e qualquer parágrafo que ainda fique muito longo é subdividido
    por frase, para que nenhum item da lista fique grande demais para ouvir
    de uma vez.
    """
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    blocks: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= 400:
            blocks.append(paragraph)
            continue
        # Parágrafo longo: subdivide por frase (mantendo a pontuação final).
        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        buffer = ""
        for sentence in sentences:
            if buffer and len(buffer) + len(sentence) > 400:
                blocks.append(buffer.strip())
                buffer = sentence
            else:
                buffer = f"{buffer} {sentence}".strip()
        if buffer:
            blocks.append(buffer.strip())
    return blocks or [text.strip()]


class AIResultDialog(wx.Dialog):
    """
    Diálogo simples e totalmente acessível por teclado:
        - Tab / Shift+Tab: alterna entre a lista navegável e os botões
        - Setas cima/baixo, dentro da lista: andam bloco a bloco
        - Ctrl+C: copia o bloco atual (quando a lista está focada)
        - Botão "Copiar tudo": copia o texto inteiro para a área de transferência
        - Esc / botão Fechar: fecha a janela
    """

    def __init__(self, parent, title: str, full_text: str, ask_fn=None):
        """
        ask_fn: callable(question: str) -> str, or None.

        When provided (used for image/video results, not audio or PDF),
        the dialog shows an extra field letting the person ask something
        specific that the general description might have missed — e.g. the
        price of one product in a promotional flyer, or a particular value
        on an invoice. ask_fn is called on a background thread so the
        window (and screen reader) never freezes while waiting for the
        answer; it should raise on failure with a message safe to show the
        user directly.
        """
        i18n = parent.i18n if hasattr(parent, "i18n") else None
        super().__init__(
            parent,
            title=title,
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._full_text = full_text
        self._blocks = _split_into_blocks(full_text)
        self._i18n = i18n
        self._ask_fn = ask_fn
        self._build()
        self.SetSize((640, 560 if ask_fn else 480))
        self.Centre()

    def _t(self, key: str, default: str) -> str:
        if self._i18n is not None:
            translated = self._i18n.t(key)
            # i18n.t() returns the key itself when missing — fall back nicely.
            return translated if translated != key else default
        return default

    def _build(self):
        sizer = wx.BoxSizer(wx.VERTICAL)

        list_label = wx.StaticText(
            self, label=self._t("ai_result_blocks_label", "Conteúdo (navegue com as setas):")
        )
        sizer.Add(list_label, 0, wx.LEFT | wx.TOP | wx.RIGHT, 10)

        self._listbox = wx.ListBox(self, choices=self._blocks, style=wx.LB_SINGLE)
        if self._blocks:
            self._listbox.SetSelection(0)
        self._listbox.Bind(wx.EVT_KEY_DOWN, self._on_list_key_down)
        sizer.Add(self._listbox, 1, wx.EXPAND | wx.ALL, 10)

        if self._ask_fn is not None:
            ask_label = wx.StaticText(
                self,
                label=self._t(
                    "ai_result_ask_label",
                    "Pergunte algo específico sobre esta mídia:",
                ),
            )
            sizer.Add(ask_label, 0, wx.LEFT | wx.RIGHT, 10)

            ask_row = wx.BoxSizer(wx.HORIZONTAL)
            self._question_field = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
            self._question_field.Bind(wx.EVT_TEXT_ENTER, self._on_ask)
            ask_row.Add(self._question_field, 1, wx.RIGHT, 6)

            self._ask_btn = wx.Button(
                self, label=self._t("ai_result_ask_btn", "&Perguntar")
            )
            self._ask_btn.Bind(wx.EVT_BUTTON, self._on_ask)
            ask_row.Add(self._ask_btn, 0)

            sizer.Add(ask_row, 0, wx.EXPAND | wx.ALL, 10)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        copy_all_btn = wx.Button(
            self, label=self._t("ai_result_copy_all_btn", "&Copiar tudo")
        )
        copy_all_btn.Bind(wx.EVT_BUTTON, self._on_copy_all)
        btn_sizer.Add(copy_all_btn, 0, wx.RIGHT, 6)

        close_btn = wx.Button(self, wx.ID_CLOSE, label=self._t("close", "&Fechar"))
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(close_btn, 0)

        sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)

        self.SetSizer(sizer)
        self.SetEscapeId(wx.ID_CLOSE)
        self._listbox.SetFocus()

    def _on_list_key_down(self, event):
        keycode = event.GetKeyCode()
        if keycode == ord("C") and event.ControlDown():
            self._copy_current_block()
            return
        event.Skip()

    def _copy_current_block(self):
        idx = self._listbox.GetSelection()
        if idx == wx.NOT_FOUND:
            return
        self._copy_to_clipboard(self._blocks[idx])

    def _on_copy_all(self, event):
        self._copy_to_clipboard(self._full_text)

    def _copy_to_clipboard(self, text: str):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(text))
            wx.TheClipboard.Close()

    def _on_ask(self, event):
        question = self._question_field.GetValue().strip()
        if not question or self._ask_fn is None:
            return

        self._ask_btn.Enable(False)
        self._question_field.Enable(False)
        waiting_label = self._t("ai_result_asking_msg", "Perguntando à IA...")
        # Gives immediate feedback in the list itself — a screen reader
        # arriving at this item hears that a question is in flight, instead
        # of silence while the request is out.
        waiting_index = self._listbox.Append(f"{waiting_label} ({question})")
        self._listbox.SetSelection(waiting_index)

        def _run():
            stop_watchdog = threading.Event()

            def _watchdog():
                while not stop_watchdog.wait(8):
                    parent = self.GetParent()
                    if parent is not None and hasattr(parent, "output"):
                        wx.CallAfter(
                            parent.output,
                            self._t(
                                "ai_still_processing_msg",
                                "Ainda processando com a IA...",
                            ),
                        )

            watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
            watchdog_thread.start()
            try:
                answer = self._ask_fn(question)
                error_text = None
            except Exception as exc:  # noqa: BLE001 — surfaced to the user as-is
                answer = None
                error_text = str(exc)
            finally:
                stop_watchdog.set()

            def _finish():
                self._listbox.Delete(waiting_index)
                if error_text is not None:
                    wx.MessageBox(
                        error_text,
                        self._t("ai_result_ask_error_title", "Não foi possível responder"),
                        wx.OK | wx.ICON_ERROR,
                        self,
                    )
                else:
                    entry = f"P: {question}\nR: {answer}"
                    self._blocks.append(entry)
                    self._full_text = f"{self._full_text}\n\n{entry}"
                    new_index = self._listbox.Append(entry)
                    self._listbox.SetSelection(new_index)
                    self._listbox.SetFocus()
                self._ask_btn.Enable(True)
                self._question_field.Enable(True)
                self._question_field.SetValue("")
                self._question_field.SetFocus()

            wx.CallAfter(_finish)

        threading.Thread(target=_run, daemon=True).start()
