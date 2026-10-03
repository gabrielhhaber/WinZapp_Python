"""
core/openai_compat.py

Núcleo compartilhado dos provedores de IA que falam o mesmo protocolo da API da
OpenAI (chat/completions e audio/transcriptions) em outro endereço: Groq e
OpenRouter hoje (core/groq_client.py, core/openrouter_client.py).

Por quê um núcleo à parte, em vez de reaproveitar core/openai_client.py: aquele
módulo é a integração da própria OpenAI, com mensagens de erro, link de
cobrança e modelo de transcrição fixos dela. Aqui tudo isso é parâmetro
(nome do provedor, base_url, link de cobrança), e cada provedor declara só o
que a sua API realmente aceita — imagem, PDF e/ou áudio — em vez de fingir que
todos aceitam tudo. A função que um provedor não suporta levanta erro amigável
(proteção extra: core/ai_providers.py já não a chama nesses casos).

Interface idêntica à dos outros módulos de provedor (transcribe_audio,
describe_visual_media, ask_about_visual_media, pdf_to_accessible_text,
resolve_model), então core/ai_providers.py trata todos do mesmo jeito. A chave
de API sempre chega como parâmetro, nunca de variável de ambiente.
"""

from __future__ import annotations

import base64
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openai import OpenAI
from openai import APIError, APIStatusError, AuthenticationError, RateLimitError


_INLINE_SIZE_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB — o arquivo vai embutido no pedido
_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SECONDS = 2.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class OpenAICompatError(Exception):
    """Erro amigável para exibir na interface — mesmo papel de
    OpenAIClientError/ClaudeClientError."""


@dataclass(frozen=True)
class CompatProvider:
    label: str                    # "Groq", "OpenRouter" — aparece nas mensagens
    base_url: str
    default_model: str            # imagem + PDF (chat)
    billing_url: str
    transcribe_model: str = ""    # vazio = provedor sem transcrição de áudio
    supports_pdf: bool = False


class OpenAICompatClient:
    def __init__(self, provider: CompatProvider):
        self.provider = provider

    # ── configuração ────────────────────────────────────────────────────
    def resolve_model(self, configured_model: Optional[str]) -> str:
        return (configured_model or "").strip() or self.provider.default_model

    def _build_client(self, api_key: str) -> OpenAI:
        if not api_key or not api_key.strip():
            raise OpenAICompatError(
                f"Nenhuma chave de API da {self.provider.label} foi configurada. "
                "Abra Configurações > Transcrições e Descrições e informe sua chave."
            )
        return OpenAI(api_key=api_key.strip(), base_url=self.provider.base_url)

    def _read_as_data_url(self, file_path: str) -> str:
        path = Path(file_path)
        if not path.exists():
            raise OpenAICompatError(f"Arquivo não encontrado: {file_path}")
        if path.stat().st_size > _INLINE_SIZE_LIMIT_BYTES:
            raise OpenAICompatError(
                f"Este arquivo é grande demais para ser enviado à {self.provider.label} "
                f"(limite: {_INLINE_SIZE_LIMIT_BYTES // (1024 * 1024)} MB)."
            )
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{data}"

    # ── erros ───────────────────────────────────────────────────────────
    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        return (
            getattr(exc, "status_code", None) in _RETRYABLE_STATUS_CODES
            or isinstance(exc, RateLimitError)
        )

    def _classify_error(self, exc: Exception) -> str:
        label = self.provider.label
        if isinstance(exc, AuthenticationError):
            return (
                f"A {label} não aceitou sua chave de API. Confira se ela foi "
                "copiada corretamente em Configurações > Transcrições e "
                f"Descrições. Detalhe técnico: {exc}"
            )
        if getattr(exc, "status_code", None) == 402:
            return (
                f"O crédito da sua conta da {label} acabou. Acesse "
                f"{self.provider.billing_url} para adicionar mais crédito. "
                f"Detalhe técnico: {exc}"
            )
        if isinstance(exc, RateLimitError):
            return (
                f"Você atingiu o limite de uso da sua chave da {label} por "
                "enquanto (cota esgotada). Aguarde alguns instantes e tente "
                f"de novo. Detalhe técnico: {exc}"
            )
        return (
            f"A {label} recusou o pedido. Verifique se a chave de API está "
            f"correta, se o modelo escolhido aceita este tipo de arquivo e se "
            f"ainda há cota disponível. Detalhe técnico: {exc}"
        )

    def _with_retry(self, call):
        """Executa call() com as mesmas regras de repetição dos outros
        provedores; devolve o retorno de call() ou levanta OpenAICompatError."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, _MAX_RETRIES + 2):
            try:
                return call()
            except (APIStatusError, APIError, RateLimitError) as exc:
                if self._is_retryable(exc) and attempt <= _MAX_RETRIES:
                    last_exc = exc
                    time.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                    continue
                raise OpenAICompatError(self._classify_error(exc)) from exc
        raise OpenAICompatError(
            f"O serviço da {self.provider.label} teve um problema temporário. "
            f"Tente novamente em instantes. Detalhe técnico: {last_exc}"
        )

    def _chat(self, client: OpenAI, **kwargs) -> str:
        response = self._with_retry(lambda: client.chat.completions.create(**kwargs))
        choices = getattr(response, "choices", None) or []
        text = ((choices[0].message.content if choices else "") or "").strip()
        if not text:
            raise OpenAICompatError(
                f"A {self.provider.label} não retornou nenhum conteúdo para este arquivo."
            )
        return text

    # ── recursos ────────────────────────────────────────────────────────
    def transcribe_audio(
        self, file_path: str, api_key: str, *, model: str = "", language_hint=None,
    ) -> str:
        if not self.provider.transcribe_model:
            raise OpenAICompatError(
                f"A {self.provider.label} não transcreve áudio pela WinZapp."
            )
        client = self._build_client(api_key)
        path = Path(file_path)
        if not path.exists():
            raise OpenAICompatError(f"Arquivo não encontrado: {file_path}")

        def _call():
            with open(path, "rb") as fh:
                return client.audio.transcriptions.create(
                    model=self.provider.transcribe_model,
                    file=fh,
                    language="pt",
                    prompt="Transcrição de mensagem de voz do WhatsApp, em português do Brasil.",
                )

        result = self._with_retry(_call)
        text = (getattr(result, "text", "") or "").strip()
        if not text:
            raise OpenAICompatError(
                f"A {self.provider.label} não retornou nenhuma transcrição para este áudio."
            )
        return text

    def _reject_video(self, is_video: bool) -> None:
        if is_video:
            raise OpenAICompatError(
                f"A {self.provider.label} não aceita vídeo como entrada — só o Gemini processa vídeo hoje."
            )

    def _image_request(self, file_path: str, api_key: str, model: str, prompt: str) -> str:
        client = self._build_client(api_key)
        data_url = self._read_as_data_url(file_path)
        return self._chat(
            client,
            model=self.resolve_model(model),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }],
        )

    def describe_visual_media(
        self, file_path: str, api_key: str, *, model: str = "", is_video: bool = False,
    ) -> str:
        self._reject_video(is_video)
        return self._image_request(
            file_path, api_key, model,
            "Descreva esta imagem para uma pessoa cega, em português do Brasil. "
            "Descreva de forma clara e objetiva o que aparece: pessoas, objetos, "
            "ambiente, cores relevantes e texto visível na imagem. Evite frases "
            "como 'a imagem mostra' — descreva diretamente o conteúdo. Responda "
            "apenas com a descrição, sem comentários adicionais.",
        )

    def ask_about_visual_media(
        self, file_path: str, api_key: str, question: str, *,
        model: str = "", is_video: bool = False,
    ) -> str:
        self._reject_video(is_video)
        return self._image_request(
            file_path, api_key, model,
            "Com base nesta imagem, responda em português do Brasil à seguinte "
            f"pergunta de forma direta e específica: {question.strip()}\n"
            "Se a informação pedida não estiver visível ou não puder ser "
            "determinada com confiança, diga isso claramente em vez de "
            "adivinhar. Responda apenas com a resposta, sem comentários "
            "adicionais.",
        )

    def pdf_to_accessible_text(self, file_path: str, api_key: str, *, model: str = "") -> str:
        if not self.provider.supports_pdf:
            raise OpenAICompatError(
                f"A {self.provider.label} não aceita PDF como entrada."
            )
        client = self._build_client(api_key)
        data_url = self._read_as_data_url(file_path)
        return self._chat(
            client,
            model=self.resolve_model(model),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "Extraia todo o texto deste PDF, em português do Brasil, mantendo a "
                        "ordem de leitura natural do documento (título, parágrafos, "
                        "listas, tabelas). Sempre que houver uma imagem, gráfico ou "
                        "diagrama no documento, insira no lugar correspondente uma "
                        "descrição entre colchetes, no formato: "
                        "'[Imagem: descrição do que aparece]'. Não pule nenhuma página. "
                        "Responda apenas com o texto acessível resultante, sem comentários "
                        "adicionais."
                    )},
                    {"type": "file", "file": {
                        "filename": Path(file_path).name, "file_data": data_url,
                    }},
                ],
            }],
        )
