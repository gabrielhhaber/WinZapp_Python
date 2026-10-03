"""
core/gemini_client.py

Integração com a API do Gemini (Google) para acessibilidade no WinZapp:
    - Transcrição de mensagens de áudio/voz
    - Descrição de imagens e vídeos
    - Conversão de PDFs em texto acessível, com descrição das imagens internas

Todas as funções recebem a chave de API como parâmetro (não leem variáveis de
ambiente diretamente), para que o chamador decida se a chave vem do arquivo
.env (uso de desenvolvimento/teste) ou das configurações salvas pelo usuário
dentro do próprio WinZapp (uso normal, inclusive por outras pessoas que
usarem esta versão modificada).

Requer a biblioteca oficial:
    pip install google-genai
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types
from google.genai.errors import APIError, ClientError


# Python's mimetypes module doesn't know several audio extensions commonly
# seen in WhatsApp voice/media messages (WPPConnect/WhatsApp itself uses
# .ogg/.opus for voice notes, which Python DOES recognise correctly — this
# covers formats that show up in forwarded/shared audio instead). Without
# this, a file like a forwarded .m4a gets guessed as
# "application/octet-stream", which the Gemini API rejects outright with
# "Unsupported MIME type" instead of processing it as audio.
_EXTRA_MIME_TYPES = {
    ".m4a": "audio/mp4",
    ".3gp": "audio/3gpp",
    ".3gpp": "audio/3gpp",
    ".amr": "audio/amr",
    ".aac": "audio/aac",
    ".opus": "audio/ogg",
}
for _ext, _mime in _EXTRA_MIME_TYPES.items():
    mimetypes.add_type(_mime, _ext)


# Modelo padrão. "gemini-2.5-flash" foi descontinuado pela Google para
# chaves de API novas e será desligado completamente (inclusive chaves
# antigas) em 16/10/2026 — troquei para "gemini-3.5-flash", a opção
# "flash" estável atual (rápida, de baixo custo, e confirmada com suporte
# a áudio, imagem, vídeo e PDF — os mesmos tipos de mídia usados aqui).
#
# Usado como fallback sempre que o usuário deixa o campo "Modelo do
# Gemini" em Configurações > IA e Acessibilidade em branco (opção
# "Automático") — ver resolve_model() abaixo. Trocar este valor no
# futuro (quando a Google aposentar mais um modelo) já atualiza sozinho
# todo mundo que estiver no automático, sem precisar mexer na tela de
# Configurações nem pedir pro usuário trocar nada.
DEFAULT_MODEL = "gemini-3.5-flash"

# Opções oferecidas no seletor de modelo em Configurações, para quem
# preferir fixar um modelo específico em vez de usar o "Automático"
# (DEFAULT_MODEL acima). Mantida aqui — não em settings_dialog.py — para
# ficar num único lugar fácil de achar na próxima vez que a Google lançar
# ou aposentar um modelo "flash".
RECOMMENDED_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
]


def resolve_model(configured_model: Optional[str]) -> str:
    """
    Decide qual modelo usar numa chamada: o que o usuário escolheu em
    Configurações, ou DEFAULT_MODEL se ele deixou em branco ("Automático").

    Centralizado aqui (em vez de repetir "or DEFAULT_MODEL" em cada
    chamador) para que mudar a regra do automático no futuro não exija
    caçar todo lugar que lê a configuração.
    """
    configured_model = (configured_model or "").strip()
    return configured_model or DEFAULT_MODEL

# Tamanho a partir do qual preferimos subir o arquivo pela File API do Gemini
# em vez de enviar os bytes embutidos direto no pedido. Arquivos de áudio e
# vídeo do WhatsApp costumam passar disso facilmente.
_INLINE_SIZE_LIMIT_BYTES = 15 * 1024 * 1024  # 15 MB


class GeminiClientError(Exception):
    """
    Erro amigável para exibir na interface do WinZapp.

    A mensagem já vem em português e pronta para ser falada pelo leitor de
    tela ou exibida numa caixa de mensagem — sem stacktraces nem termos
    técnicos, para não confundir quem estiver ouvindo com o leitor de tela.
    """


def _build_client(api_key: str) -> genai.Client:
    if not api_key or not api_key.strip():
        raise GeminiClientError(
            "Nenhuma chave de API do Gemini foi configurada. "
            "Abra Configurações > IA e Acessibilidade e informe sua chave."
        )
    return genai.Client(api_key=api_key.strip())


def _upload_or_inline(client: genai.Client, file_path: str):
    """
    Decide entre enviar o arquivo embutido no pedido (mais rápido para
    arquivos pequenos) ou fazer upload prévio pela File API do Gemini (mais
    confiável para arquivos maiores, como vídeos e PDFs longos).
    """
    path = Path(file_path)
    if not path.exists():
        raise GeminiClientError(f"Arquivo não encontrado: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"
    size = path.stat().st_size

    if size <= _INLINE_SIZE_LIMIT_BYTES:
        data = path.read_bytes()
        return types.Part.from_bytes(data=data, mime_type=mime_type)

    uploaded = client.files.upload(file=str(path))
    return _wait_until_active(client, uploaded)


def _wait_until_active(client: genai.Client, uploaded_file, timeout_seconds: int = 90):
    """
    Files enviados pela File API do Gemini começam no estado PROCESSING —
    especialmente vídeos, que o Gemini precisa processar antes de ficarem
    utilizáveis — e só podem ser referenciados numa chamada depois de
    chegarem a ACTIVE. Sem essa espera, uma chamada feita cedo demais falha
    com "File ... is not in an ACTIVE state" mesmo com upload bem-sucedido.
    """
    deadline = time.monotonic() + timeout_seconds
    current = uploaded_file
    while getattr(current.state, "name", current.state) == "PROCESSING":
        if time.monotonic() >= deadline:
            raise GeminiClientError(
                "O Gemini demorou demais para processar este arquivo "
                "(comum com vídeos maiores). Tente novamente em instantes."
            )
        time.sleep(2)
        current = client.files.get(name=uploaded_file.name)

    state_name = getattr(current.state, "name", current.state)
    if state_name != "ACTIVE":
        raise GeminiClientError(
            "O Gemini não conseguiu processar este arquivo "
            f"(estado: {state_name}). Tente novamente ou com outro arquivo."
        )
    return current


# Quantas tentativas extras (além da primeira) para erros temporários do
# lado do Gemini (503 "model overloaded", 500 interno, 504 timeout, 429
# limite de requisições por minuto na camada gratuita). Esse tipo de erro
# costuma se resolver sozinho em segundos — apps prontos (como o app
# oficial do Gemini) já fazem esse retry por baixo dos panos, por isso a
# mesma chave "quase nunca falha" em alguns lugares e falha visivelmente
# aqui sem essa lógica.
_MAX_RETRIES = 4

# Espera entre tentativas, em segundos, crescendo a cada nova tentativa
# (backoff exponencial: 2s, 4s, 8s, 16s). Evita martelar a API logo depois
# de uma resposta de sobrecarga.
_RETRY_BASE_DELAY_SECONDS = 2.0

# Códigos de status HTTP que valem a pena tentar de novo. 503/500/504 são
# problemas do lado do Gemini (sobrecarga, erro interno, timeout); 429 é
# limite de requisições por minuto, que também costuma liberar sozinho
# após uma pequena espera. Outros códigos 4xx (400, 401, 403...) indicam
# um problema que uma nova tentativa não resolve — chave inválida, cota
# diária esgotada, arquivo rejeitado — e falham na hora, como antes.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_retryable(exc: APIError) -> bool:
    code = getattr(exc, "code", None)
    if code in _RETRYABLE_STATUS_CODES:
        return True
    # Alguns erros de sobrecarga do Gemini chegam como ClientError/APIError
    # sem o código HTTP populado no objeto, mas com "UNAVAILABLE" ou
    # "overloaded" no texto — cobre esse caso também.
    text = str(exc).upper()
    return "UNAVAILABLE" in text or "OVERLOADED" in text


# Link oficial do Google para gerenciar cota/crédito/faturamento de uma chave
# de API do Gemini. Citado nas mensagens amigáveis abaixo — centralizado aqui
# para não ficar espalhado em cada string.
_BILLING_URL = "https://ai.studio/projects"


def _classify_client_error(exc: ClientError) -> str:
    """
    Traduz um ClientError do Gemini numa mensagem amigável e específica.

    O SDK devolve, para causas bem diferentes (chave inválida, cota por
    minuto esgotada, crédito pré-pago zerado...), o mesmo tipo de exceção
    com um "status" textual (`exc.status`, ex.: "RESOURCE_EXHAUSTED",
    "PERMISSION_DENIED") e uma mensagem legível em `exc.message`. Sem essa
    distinção, todas essas situações caíam na mesma frase genérica
    ("verifique a chave e a cota"), que não ajuda quem está ouvindo pelo
    leitor de tela a entender o que fazer a seguir.
    """
    status = (getattr(exc, "status", None) or "").upper()
    message = str(getattr(exc, "message", None) or exc)
    message_lower = message.lower()

    if status == "RESOURCE_EXHAUSTED":
        # A Google usa o mesmo status "RESOURCE_EXHAUSTED" tanto para cota
        # temporária (por minuto/por dia) quanto para crédito pré-pago
        # zerado — só o texto da mensagem diferencia os dois casos.
        if any(
            term in message_lower
            for term in ("prepayment", "prepago", "pré-pago", "credit", "crédito", "billing", "faturamento")
        ):
            return (
                "O crédito da sua conta do Gemini acabou. Acesse "
                f"{_BILLING_URL} para adicionar mais crédito e voltar a "
                "usar a transcrição e a descrição por IA. "
                f"Detalhe técnico: {exc}"
            )
        return (
            "Você atingiu o limite de uso da sua chave do Gemini por "
            "enquanto (cota esgotada). Aguarde alguns minutos e tente de "
            f"novo, ou confira seu plano e limites em {_BILLING_URL}. "
            f"Detalhe técnico: {exc}"
        )

    if status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return (
            "O Gemini não aceitou sua chave de API. Confira se ela foi "
            "copiada corretamente em Configurações > IA e Acessibilidade. "
            f"Detalhe técnico: {exc}"
        )

    return (
        "O Gemini recusou o pedido. Verifique se a chave de API está "
        f"correta e se ainda há cota disponível. Detalhe técnico: {exc}"
    )


def _generate_text(
    client: genai.Client, model: str, prompt: str, media_part,
    *, temperature: Optional[float] = None,
) -> str:
    last_exc: Optional[Exception] = None
    config = types.GenerateContentConfig(temperature=temperature) if temperature is not None else None

    for attempt in range(1, _MAX_RETRIES + 2):  # 1 tentativa inicial + retries
        try:
            response = client.models.generate_content(
                model=model,
                contents=[media_part, prompt],
                config=config,
            )
        except ClientError as exc:
            # Erros comuns: chave inválida, cota excedida, arquivo rejeitado.
            if "Unsupported MIME type" in str(exc):
                raise GeminiClientError(
                    "O Gemini não reconheceu o formato deste arquivo. Isso pode "
                    "acontecer com formatos de áudio/vídeo menos comuns — "
                    "avise o desenvolvedor para que esse formato seja tratado "
                    f"corretamente. Detalhe técnico: {exc}"
                ) from exc
            if _is_retryable(exc) and attempt <= _MAX_RETRIES:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                continue
            raise GeminiClientError(_classify_client_error(exc)) from exc
        except APIError as exc:
            if _is_retryable(exc) and attempt <= _MAX_RETRIES:
                last_exc = exc
                time.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
                continue
            raise GeminiClientError(
                f"O serviço do Gemini teve um problema temporário e continuou "
                f"indisponível após {attempt} tentativas. Tente novamente em "
                f"instantes. Detalhe técnico: {exc}"
            ) from exc
        else:
            text = (response.text or "").strip()
            if not text:
                raise GeminiClientError(
                    "O Gemini não retornou nenhum conteúdo para este arquivo."
                )
            return text

    # Não deveria chegar aqui (o loop sempre retorna ou levanta antes), mas
    # cobre o caso defensivamente.
    raise GeminiClientError(
        f"O serviço do Gemini teve um problema temporário. Tente novamente em instantes. Detalhe técnico: {last_exc}"
    )


def transcribe_audio(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    language_hint: Optional[str] = "português do Brasil",
) -> str:
    """
    Transcreve uma mensagem de áudio/voz para texto.

    Retorna apenas o texto transcrito, pronto para ser mostrado na janela
    navegável de resultado.
    """
    client = _build_client(api_key)
    media_part = _upload_or_inline(client, file_path)

    # Ao contrário da OpenAI/Groq (Whisper, um modelo de transcrição
    # dedicado e determinístico), o Gemini transcreve áudio como qualquer
    # outro pedido a um modelo generativo — e um modelo generativo, com
    # temperatura default, pode "completar" trechos incertos com palavras
    # plausíveis em vez de admitir que não entendeu, em vez de reportar
    # fielmente o que está no áudio (relatado por um usuário: a transcrição
    # devolvida continha frases que não estavam no áudio original).
    # temperature=0.0 pede a saída mais determinística/literal possível da
    # API, e o prompt abaixo proíbe explicitamente completar ou adivinhar —
    # nenhuma das duas sozinha garante zero invenção (o modelo ainda pode
    # alucinar), mas juntas reduzem bastante a chance.
    prompt = (
        "Transcreva integralmente o áudio a seguir para texto corrido, em "
        f"{language_hint}. Transcreva apenas o que for realmente dito no "
        "áudio, palavra por palavra — nunca complete, resuma, corrija, "
        "parafraseie ou adivinhe uma palavra ou frase que você não consiga "
        "entender com certeza, e nunca invente conteúdo que não esteja "
        "no áudio. Use pontuação adequada para facilitar a leitura por um "
        "leitor de tela. Para qualquer trecho inaudível, incerto ou sem "
        "fala (silêncio, apenas ruído), indique com '[inaudível]' em vez "
        "de tentar preenchê-lo. Responda apenas com a transcrição, sem "
        "comentários adicionais."
    )
    return _generate_text(client, model, prompt, media_part, temperature=0.0)


def describe_visual_media(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    is_video: bool = False,
) -> str:
    """
    Descreve o conteúdo de uma imagem ou vídeo para uma pessoa cega ou com
    baixa visão.
    """
    client = _build_client(api_key)
    media_part = _upload_or_inline(client, file_path)

    tipo = "vídeo" if is_video else "imagem"
    prompt = (
        f"Descreva este {tipo} para uma pessoa cega, em português do Brasil. "
        "Descreva de forma clara e objetiva o que aparece: pessoas, objetos, "
        "ambiente, cores relevantes, texto visível na imagem, e, se for "
        "vídeo, a sequência de ações e qualquer fala ou som importante. "
        "Evite frases como 'a imagem mostra' — descreva diretamente o "
        "conteúdo. Responda apenas com a descrição, sem comentários "
        "adicionais."
    )
    return _generate_text(client, model, prompt, media_part)


def ask_about_visual_media(
    file_path: str,
    api_key: str,
    question: str,
    *,
    model: str = DEFAULT_MODEL,
    is_video: bool = False,
) -> str:
    """
    Responde a uma pergunta específica sobre uma imagem ou vídeo já
    descrito antes — por exemplo, o preço de um produto num encarte
    promocional, ou um valor específico numa fatura, que a descrição geral
    não tenha mencionado.
    """
    client = _build_client(api_key)
    media_part = _upload_or_inline(client, file_path)

    tipo = "vídeo" if is_video else "imagem"
    prompt = (
        f"Com base neste {tipo}, responda em português do Brasil à "
        f"seguinte pergunta de forma direta e específica: {question.strip()}\n"
        "Se a informação pedida não estiver visível ou não puder ser "
        "determinada com confiança, diga isso claramente em vez de "
        "adivinhar. Responda apenas com a resposta, sem comentários "
        "adicionais."
    )
    return _generate_text(client, model, prompt, media_part)


def pdf_to_accessible_text(
    file_path: str,
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
) -> str:
    """
    Converte o conteúdo de um PDF em texto acessível, descrevendo também as
    imagens encontradas dentro do documento, no ponto em que elas aparecem.
    """
    client = _build_client(api_key)
    media_part = _upload_or_inline(client, file_path)

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
    return _generate_text(client, model, prompt, media_part)
