"""
core/claude_client.py

Integração com a API da Claude (Anthropic) para acessibilidade no WinZapp —
mesmo papel do core/gemini_client.py, usado como último provedor de IA na
ordem de fallback definida em core/ai_providers.py (Gemini -> OpenAI -> Claude):
    - Descrição de imagens (Claude tem suporte nativo a visão)
    - Conversão de PDFs em texto acessível, com descrição das imagens internas
      (Claude lê o PDF diretamente, página por página, sem precisar converter
      para imagem antes)

Limitações reais da API da Claude (não é bug):
    - Não existe nenhum modo de entrada de ÁUDIO na API da Claude — por isso
      este módulo não oferece transcrição de áudio.
    - Assim como a OpenAI, a Claude não aceita arquivo de VÍDEO bruto como
      entrada — só imagem e PDF.
core/ai_providers.py já sabe disso e nunca chama transcribe_audio() nem
describe_visual_media()/ask_about_visual_media() daqui com is_video=True; as
funções abaixo levantam erro nesses casos só como proteção extra.

Todas as funções recebem a chave de API como parâmetro (mesmo padrão do
gemini_client.py) — nunca leem variável de ambiente diretamente.

Requer a biblioteca oficial:
    pip install anthropic
"""

from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path
from typing import Optional

import anthropic
from anthropic import APIError, APIStatusError, AuthenticationError, RateLimitError


# Modelos atuais da Claude (geração 5), usados como padrão e como opções
# recomendadas no seletor de modelo em Configurações. "claude-sonnet-5" é o
# modelo intermediário — bom equilíbrio entre qualidade e custo/velocidade
# para descrição de imagem e leitura de PDF, mesmo papel do "flash" no
# Gemini e do "mini" na OpenAI. Troque aqui (não em settings_dialog.py)
# quando a Anthropic lançar ou aposentar um modelo.
DEFAULT_MODEL = "claude-sonnet-5"

RECOMMENDED_MODELS = [
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
    "claude-opus-5",
]

_MAX_OUTPUT_TOKENS = 8192


def resolve_model(configured_model: Optional[str]) -> str:
    """Mesma regra do gemini_client.resolve_model(): configurado, ou DEFAULT_MODEL
    se o campo (opção "Automático") estiver em branco."""
    configured_model = (configured_model or "").strip()
    return configured_model or DEFAULT_MODEL


# A API da Claude aceita o documento embutido em base64 no próprio pedido
# (sem upload prévio, ao contrário do Gemini). O limite documentado é de
# 32MB por requisição — usamos uma margem de segurança abaixo disso.
_INLINE_SIZE_LIMIT_BYTES = 28 * 1024 * 1024  # 28 MB


class ClaudeClientError(Exception):
    """Erro amigável para exibir na interface do WinZapp — mesmo papel de
    GeminiClientError em core/gemini_client.py."""


def _build_client(api_key: str) -> anthropic.Anthropic:
    if not api_key or not api_key.strip():
        raise ClaudeClientError(
            "Nenhuma chave de API da Claude foi configurada. "
            "Abra Configurações > Transcrições e Descrições e informe sua chave."
        )
    return anthropic.Anthropic(api_key=api_key.strip())


def _read_as_base64(file_path: str) -> tuple[str, str]:
    path = Path(file_path)
    if not path.exists():
        raise ClaudeClientError(f"Arquivo não encontrado: {file_path}")

    size = path.stat().st_size
    if size > _INLINE_SIZE_LIMIT_BYTES:
        raise ClaudeClientError(
            "Este arquivo é grande demais para ser enviado à Claude "
            f"(limite: {_INLINE_SIZE_LIMIT_BYTES // (1024 * 1024)} MB)."
        )

    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return data, mime_type


_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SECONDS = 2.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}  # 529 = "overloaded_error" da Claude

_BILLING_URL = "https://console.anthropic.com/settings/billing"


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in _RETRYABLE_STATUS_CODES:
        return True
    return isinstance(exc, RateLimitError)


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return (
            "A Claude não aceitou sua chave de API. Confira se ela foi "
            "copiada corretamente em Configurações > Transcrições e Descrições. "
            f"Detalhe técnico: {exc}"
        )
    if isinstance(exc, RateLimitError):
        return (
            "Você atingiu o limite de uso da sua chave da Claude por enquanto "
            "(cota esgotada). Aguarde alguns instantes e tente de novo, ou "
            f"confira seu plano e limites em {_BILLING_URL}. "
            f"Detalhe técnico: {exc}"
        )
    text = str(exc).lower()
    if "credit" in text or "billing" in text:
        return (
            "O crédito da sua conta da Claude acabou. Acesse "
            f"{_BILLING_URL} para adicionar mais crédito. "
            f"Detalhe técnico: {exc}"
        )
    return (
        "A Claude recusou o pedido. Verifique se a chave de API está "
        f"correta e se ainda há cota disponível. Detalhe técnico: {exc}"
    )


def _generate_text(client: anthropic.Anthropic, model: str, prompt: str, content_block: dict) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=_MAX_OUTPUT_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [content_block, {"type": "text", "text": prompt}],
                }],
            )
        except (APIStatusError, APIError, RateLimitError) as exc:
            if _is_retryable(exc) and attempt <= _MAX_RETRIES:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                continue
            raise ClaudeClientError(_classify_error(exc)) from exc
        else:
            parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            text = "".join(parts).strip()
            if not text:
                raise ClaudeClientError("A Claude não retornou nenhum conteúdo para este arquivo.")
            return text

    raise ClaudeClientError(
        f"O serviço da Claude teve um problema temporário. Tente novamente em instantes. "
        f"Detalhe técnico: {last_exc}"
    )


def transcribe_audio(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    language_hint: Optional[str] = "português do Brasil",
) -> str:
    raise ClaudeClientError(
        "A API da Claude não tem suporte a áudio — use o Gemini ou a OpenAI "
        "para transcrição de mensagens de voz."
    )


def describe_visual_media(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    is_video: bool = False,
) -> str:
    """Descreve o conteúdo de uma imagem para uma pessoa cega ou com baixa visão.

    Vídeo não é suportado pela API da Claude — ver nota no topo do arquivo;
    core/ai_providers.py nunca deveria chegar aqui com is_video=True, mas a
    checagem fica como proteção extra.
    """
    if is_video:
        raise ClaudeClientError(
            "A Claude não aceita vídeo como entrada — só o Gemini processa vídeo hoje."
        )

    client = _build_client(api_key)
    data, mime_type = _read_as_base64(file_path)

    prompt = (
        "Descreva esta imagem para uma pessoa cega, em português do Brasil. "
        "Descreva de forma clara e objetiva o que aparece: pessoas, objetos, "
        "ambiente, cores relevantes e texto visível na imagem. Evite frases "
        "como 'a imagem mostra' — descreva diretamente o conteúdo. Responda "
        "apenas com a descrição, sem comentários adicionais."
    )
    content_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": data},
    }
    return _generate_text(client, model, prompt, content_block)


def ask_about_visual_media(
    file_path: str,
    api_key: str,
    question: str,
    *,
    model: str = DEFAULT_MODEL,
    is_video: bool = False,
) -> str:
    """Responde a uma pergunta específica sobre uma imagem já descrita antes."""
    if is_video:
        raise ClaudeClientError(
            "A Claude não aceita vídeo como entrada — só o Gemini processa vídeo hoje."
        )

    client = _build_client(api_key)
    data, mime_type = _read_as_base64(file_path)

    prompt = (
        "Com base nesta imagem, responda em português do Brasil à seguinte "
        f"pergunta de forma direta e específica: {question.strip()}\n"
        "Se a informação pedida não estiver visível ou não puder ser "
        "determinada com confiança, diga isso claramente em vez de "
        "adivinhar. Responda apenas com a resposta, sem comentários "
        "adicionais."
    )
    content_block = {
        "type": "image",
        "source": {"type": "base64", "media_type": mime_type, "data": data},
    }
    return _generate_text(client, model, prompt, content_block)


def pdf_to_accessible_text(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Converte o conteúdo de um PDF em texto acessível, descrevendo também as
    imagens encontradas dentro do documento.

    Usa o bloco de conteúdo "document" da API da Claude (PDF em base64),
    suportado nativamente pelos modelos com visão da família Claude para
    leitura direta de PDF (texto e imagens internas por página).
    """
    client = _build_client(api_key)
    data, _mime_type = _read_as_base64(file_path)

    prompt = (
        "Extraia todo o texto deste PDF, em português do Brasil, mantendo a "
        "ordem de leitura natural do documento (título, parágrafos, "
        "listas, tabelas). Sempre que houver uma imagem, gráfico ou "
        "diagrama no documento, insira no lugar correspondente uma "
        "descrição entre colchetes, no formato: "
        "'[Imagem: descrição do que aparece]'. Não pule nenhuma página. "
        "Responda apenas com o texto acessível resultante, sem comentários "
        "adicionais."
    )
    content_block = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
    }
    return _generate_text(client, model, prompt, content_block)
