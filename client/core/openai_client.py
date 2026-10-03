"""
core/openai_client.py

Integração com a API da OpenAI para acessibilidade no WinZapp — mesmo papel do
core/gemini_client.py, usado como provedor de IA alternativo (fallback) quando o
Gemini falha ou não está configurado, e ordenado por core/ai_providers.py:
    - Transcrição de mensagens de áudio/voz (Whisper / gpt-4o-*-transcribe)
    - Descrição de imagens (modelos com visão, ex.: gpt-4o-mini)
    - Conversão de PDFs em texto acessível, com descrição das imagens internas

Limitação real da OpenAI (não é bug): a API de chat/visão da OpenAI não aceita
arquivo de vídeo bruto (.mp4 etc.) como entrada — só imagens. Por isso este
módulo não oferece descrição de vídeo; core/ai_providers.py já sabe disso e
nunca chama describe_visual_media()/ask_about_visual_media() daqui com
is_video=True (as duas funções abaixo levantam erro se isso acontecer, como
proteção extra caso um chamador futuro esqueça dessa regra).

Todas as funções recebem a chave de API como parâmetro (mesmo padrão do
gemini_client.py) — nunca leem variável de ambiente diretamente.

Requer a biblioteca oficial:
    pip install openai
"""

from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path
from typing import Optional

from openai import OpenAI
from openai import APIError, APIStatusError, AuthenticationError, RateLimitError


# Modelo padrão para descrição de imagem e conversão de PDF (visão + texto).
# "gpt-4o-mini" é o modelo "pequeno" com visão mais barato/rápido da família
# atual — troque aqui (não em settings_dialog.py) quando a OpenAI lançar ou
# aposentar um modelo, e quem estiver no automático (campo em branco) já
# atualiza sozinho, sem precisar mexer em Configurações.
DEFAULT_MODEL = "gpt-4o-mini"

RECOMMENDED_MODELS = [
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4.1-mini",
    "gpt-4.1",
]

# Modelo usado para transcrição de áudio — é um endpoint separado
# (audio.transcriptions), não o mesmo modelo de chat/visão configurado acima,
# então não é exposto como campo próprio em Configurações para não confundir
# quem só quer digitar uma chave e usar o automático em tudo.
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"


def resolve_model(configured_model: Optional[str]) -> str:
    """Mesma regra do gemini_client.resolve_model(): configurado, ou DEFAULT_MODEL
    se o campo (opção "Automático") estiver em branco."""
    configured_model = (configured_model or "").strip()
    return configured_model or DEFAULT_MODEL


_INLINE_SIZE_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB — ver nota em _read_as_data_url()


class OpenAIClientError(Exception):
    """Erro amigável para exibir na interface do WinZapp — mesmo papel de
    GeminiClientError em core/gemini_client.py."""


def _build_client(api_key: str) -> OpenAI:
    if not api_key or not api_key.strip():
        raise OpenAIClientError(
            "Nenhuma chave de API da OpenAI foi configurada. "
            "Abra Configurações > Transcrições e Descrições e informe sua chave."
        )
    return OpenAI(api_key=api_key.strip())


def _read_as_data_url(file_path: str) -> str:
    """
    Lê o arquivo e devolve como data: URL em base64, formato aceito pela API
    de chat/visão da OpenAI para imagem e PDF.

    Diferente do Gemini (que tem uma File API própria para upload prévio de
    arquivos grandes), aqui o arquivo sempre vai embutido no pedido — por
    isso o limite de tamanho é mais conservador. Um arquivo maior que isso
    ainda pode funcionar nos outros provedores configurados (é exatamente
    para isso que existe o fallback em core/ai_providers.py).
    """
    path = Path(file_path)
    if not path.exists():
        raise OpenAIClientError(f"Arquivo não encontrado: {file_path}")

    size = path.stat().st_size
    if size > _INLINE_SIZE_LIMIT_BYTES:
        raise OpenAIClientError(
            "Este arquivo é grande demais para ser enviado à OpenAI "
            f"(limite: {_INLINE_SIZE_LIMIT_BYTES // (1024 * 1024)} MB)."
        )

    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}", mime_type


# Retry: mesmo raciocínio do gemini_client.py — erros de sobrecarga/limite de
# requisições costumam se resolver sozinhos em segundos.
_MAX_RETRIES = 3
_RETRY_BASE_DELAY_SECONDS = 2.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

_BILLING_URL = "https://platform.openai.com/account/billing"


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status in _RETRYABLE_STATUS_CODES:
        return True
    return isinstance(exc, RateLimitError)


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return (
            "A OpenAI não aceitou sua chave de API. Confira se ela foi "
            "copiada corretamente em Configurações > Transcrições e Descrições. "
            f"Detalhe técnico: {exc}"
        )
    if isinstance(exc, RateLimitError):
        text = str(exc).lower()
        if "quota" in text or "billing" in text or "insufficient_quota" in text:
            return (
                "O crédito da sua conta da OpenAI acabou. Acesse "
                f"{_BILLING_URL} para adicionar mais crédito. "
                f"Detalhe técnico: {exc}"
            )
        return (
            "Você atingiu o limite de uso da sua chave da OpenAI por enquanto "
            f"(cota esgotada). Aguarde alguns instantes e tente de novo. "
            f"Detalhe técnico: {exc}"
        )
    return (
        "A OpenAI recusou o pedido. Verifique se a chave de API está "
        f"correta e se ainda há cota disponível. Detalhe técnico: {exc}"
    )


def _chat_with_retry(client: OpenAI, **kwargs) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 2):
        try:
            response = client.chat.completions.create(**kwargs)
        except (APIStatusError, APIError, RateLimitError) as exc:
            if _is_retryable(exc) and attempt <= _MAX_RETRIES:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                continue
            raise OpenAIClientError(_classify_error(exc)) from exc
        else:
            text = (response.choices[0].message.content or "").strip()
            if not text:
                raise OpenAIClientError("A OpenAI não retornou nenhum conteúdo para este arquivo.")
            return text

    raise OpenAIClientError(
        f"O serviço da OpenAI teve um problema temporário. Tente novamente em instantes. "
        f"Detalhe técnico: {last_exc}"
    )


def transcribe_audio(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,  # ignorado — ver DEFAULT_TRANSCRIBE_MODEL acima
    language_hint: Optional[str] = "português do Brasil",
) -> str:
    """Transcreve uma mensagem de áudio/voz para texto via Whisper/gpt-4o-transcribe."""
    client = _build_client(api_key)
    path = Path(file_path)
    if not path.exists():
        raise OpenAIClientError(f"Arquivo não encontrado: {file_path}")

    last_exc: Optional[Exception] = None
    for attempt in range(1, _MAX_RETRIES + 2):
        try:
            with open(path, "rb") as fh:
                result = client.audio.transcriptions.create(
                    model=DEFAULT_TRANSCRIBE_MODEL,
                    file=fh,
                    language="pt",
                    prompt="Transcrição de mensagem de voz do WhatsApp, em português do Brasil.",
                )
        except (APIStatusError, APIError, RateLimitError) as exc:
            if _is_retryable(exc) and attempt <= _MAX_RETRIES:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                continue
            raise OpenAIClientError(_classify_error(exc)) from exc
        else:
            text = (getattr(result, "text", "") or "").strip()
            if not text:
                raise OpenAIClientError("A OpenAI não retornou nenhuma transcrição para este áudio.")
            return text

    raise OpenAIClientError(
        f"O serviço da OpenAI teve um problema temporário. Tente novamente em instantes. "
        f"Detalhe técnico: {last_exc}"
    )


def describe_visual_media(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    is_video: bool = False,
) -> str:
    """Descreve o conteúdo de uma imagem para uma pessoa cega ou com baixa visão.

    Vídeo não é suportado pela API de visão da OpenAI (só imagem) — ver nota
    no topo do arquivo; core/ai_providers.py nunca deveria chegar aqui com
    is_video=True, mas a checagem fica como proteção extra.
    """
    if is_video:
        raise OpenAIClientError(
            "A OpenAI não aceita vídeo como entrada — só o Gemini processa vídeo hoje."
        )

    client = _build_client(api_key)
    data_url, _ = _read_as_data_url(file_path)

    prompt = (
        "Descreva esta imagem para uma pessoa cega, em português do Brasil. "
        "Descreva de forma clara e objetiva o que aparece: pessoas, objetos, "
        "ambiente, cores relevantes e texto visível na imagem. Evite frases "
        "como 'a imagem mostra' — descreva diretamente o conteúdo. Responda "
        "apenas com a descrição, sem comentários adicionais."
    )
    return _chat_with_retry(
        client,
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    )


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
        raise OpenAIClientError(
            "A OpenAI não aceita vídeo como entrada — só o Gemini processa vídeo hoje."
        )

    client = _build_client(api_key)
    data_url, _ = _read_as_data_url(file_path)

    prompt = (
        "Com base nesta imagem, responda em português do Brasil à seguinte "
        f"pergunta de forma direta e específica: {question.strip()}\n"
        "Se a informação pedida não estiver visível ou não puder ser "
        "determinada com confiança, diga isso claramente em vez de "
        "adivinhar. Responda apenas com a resposta, sem comentários "
        "adicionais."
    )
    return _chat_with_retry(
        client,
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    )


def pdf_to_accessible_text(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Converte o conteúdo de um PDF em texto acessível, descrevendo também as
    imagens encontradas dentro do documento.

    Usa a entrada de arquivo (content type "file") da API de chat da OpenAI,
    suportada pelos modelos da família gpt-4o/gpt-4.1 para leitura direta de
    PDF (texto e imagens internas), sem precisar converter página por página
    para imagem antes.
    """
    client = _build_client(api_key)
    data_url, _ = _read_as_data_url(file_path)
    filename = Path(file_path).name

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
    return _chat_with_retry(
        client,
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "file", "file": {"filename": filename, "file_data": data_url}},
            ],
        }],
    )
