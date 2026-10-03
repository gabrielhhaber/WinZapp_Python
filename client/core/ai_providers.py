"""
core/ai_providers.py

Camada de orquestração multi-provedor para os recursos de IA e acessibilidade
do WinZapp (transcrição de áudio, descrição de imagem/vídeo/figurinha,
conversão de PDF em texto acessível).

Por quê: cada provedor de IA (Gemini, OpenAI, Claude, Groq, OpenRouter) tem instabilidade e
limites de cota independentes um do outro. Em vez de depender de um único
provedor, o WinZapp tenta cada chave configurada, na ordem escolhida pelo
usuário em Configurações > Transcrições e Descrições (por padrão: Gemini,
OpenAI, Claude, Groq e OpenRouter, nessa ordem) — e só mostra erro para o
usuário quando TODOS os provedores habilitados, configurados e compatíveis
com aquele tipo de mídia falharem. O usuário controla, por provedor, se ele
entra ou não na tentativa automática (checkbox "Ativado" na lista de
provedores) e em que ordem cada um é tentado (mover para cima/para baixo na
mesma lista) — ver provider_order() e is_provider_enabled() abaixo. Não há
"escolher um único provedor fixo": mesmo reordenados e com alguns
desativados, os demais habilitados e configurados continuam servindo de rede
de segurança uns para os outros.

Limitações reais de cada provedor (não é bug, é o que cada API aceita hoje —
ver o topo de cada core/<provedor>_client.py para o detalhe técnico):
    - Vídeo bruto (.mp4 etc.): só o Gemini processa nativamente. OpenAI e
      Claude não entram no fallback de vídeo.
    - Áudio: Gemini, OpenAI (gpt-4o-transcribe) e Groq (Whisper) transcrevem.
      Claude e OpenRouter não entram no fallback de áudio.
    - Imagem e figurinha (mesmo pipeline de imagem): todos participam.
    - PDF: todos, exceto a Groq (a API dela só aceita imagem e áudio).

Cada provedor tem seu próprio módulo (core/gemini_client.py,
core/openai_client.py, core/claude_client.py, core/groq_client.py,
core/openrouter_client.py — estes dois sobre core/openai_compat.py) com a mesma "forma" de função
(mesmos parâmetros/retorno: transcribe_audio, describe_visual_media,
ask_about_visual_media, pdf_to_accessible_text, resolve_model) — este
arquivo só decide QUAL chamar e em que ordem, nunca COMO cada um fala com
sua API.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core import gemini_client
from core import openai_client
from core import claude_client
from core import groq_client
from core import openrouter_client


class AIProviderError(Exception):
    """
    Erro amigável, já pronto para mostrar na interface (ou falar pelo leitor
    de tela), reunindo o que cada provedor tentado respondeu — para que o
    usuário veja exatamente por que a mídia não pôde ser processada por
    nenhum deles, em vez de só a mensagem da última tentativa.
    """


@dataclass(frozen=True)
class _ProviderSpec:
    id: str
    label: str
    key_setting: str
    model_setting: str
    module: object
    supports_audio: bool
    supports_video: bool
    supports_pdf: bool = True


# Ordem de fallback PADRÃO: Gemini primeiro (provedor original, já testado),
# depois OpenAI, Claude, Groq e OpenRouter. O usuário pode reordenar e
# ativar/desativar cada um em Configurações — ver provider_order() e
# is_provider_enabled() logo abaixo — então esta lista só vale para quem
# nunca mexeu nessas configurações (ou para um provedor novo que ainda não
# apareça na ordem salva do usuário).
PROVIDERS: list[_ProviderSpec] = [
    _ProviderSpec(
        id="gemini", label="Gemini",
        key_setting="gemini_api_key", model_setting="gemini_model",
        module=gemini_client, supports_audio=True, supports_video=True,
    ),
    _ProviderSpec(
        id="openai", label="OpenAI",
        key_setting="openai_api_key", model_setting="openai_model",
        module=openai_client, supports_audio=True, supports_video=False,
    ),
    _ProviderSpec(
        id="claude", label="Claude",
        key_setting="claude_api_key", model_setting="claude_model",
        module=claude_client, supports_audio=False, supports_video=False,
    ),
    _ProviderSpec(
        id="groq", label="Groq",
        key_setting="groq_api_key", model_setting="groq_model",
        module=groq_client, supports_audio=True, supports_video=False,
        supports_pdf=False,
    ),
    _ProviderSpec(
        id="openrouter", label="OpenRouter",
        key_setting="openrouter_api_key", model_setting="openrouter_model",
        module=openrouter_client, supports_audio=False, supports_video=False,
    ),
]


def _providers_by_id() -> dict[str, _ProviderSpec]:
    """Recalculado a cada chamada, nunca cacheado no import: os testes
    (tests/test_ai_providers.py) trocam o módulo PROVIDERS inteiro via
    monkeypatch.setattr() para injetar provedores falsos, e um dict
    calculado uma vez no carregamento do módulo continuaria apontando para
    os provedores REAIS mesmo depois da troca."""
    return {p.id: p for p in PROVIDERS}


def provider_order(ai_settings: dict) -> list[str]:
    """Ordem de tentativa configurada pelo usuário (lista de ids em
    ai_settings["provider_order"], preenchida pela lista reordenável em
    Configurações > Transcrições e Descrições). Ids desconhecidos na lista
    salva são ignorados (provedor removido de uma versão antiga do app); um
    provedor que ainda não apareça nela (provedor novo, ou configuração de
    antes desta lista existir) entra no fim, na ordem padrão de PROVIDERS."""
    known_ids = {p.id for p in PROVIDERS}
    saved = ai_settings.get("provider_order") or []
    ordered = [pid for pid in saved if pid in known_ids]
    ordered += [p.id for p in PROVIDERS if p.id not in ordered]
    return ordered


def is_provider_enabled(ai_settings: dict, provider_id: str) -> bool:
    """Checkbox "Ativado" da lista de provedores. Ligado por padrão — um
    provedor sem essa chave ainda na configuração (versão antiga do app)
    continua participando do fallback exatamente como antes desta opção
    existir."""
    return bool(ai_settings.get(f"{provider_id}_enabled", True))


def configured_provider_ids(ai_settings: dict) -> list[str]:
    """Provedores habilitados e com chave de API preenchida em
    Configurações, na ordem escolhida pelo usuário (provider_order). Lista
    vazia = nenhum provedor disponível (recursos de IA indisponíveis, mesmo
    que o interruptor "Ativar" esteja ligado)."""
    by_id = _providers_by_id()
    return [
        pid for pid in provider_order(ai_settings)
        if is_provider_enabled(ai_settings, pid)
        and (ai_settings.get(by_id[pid].key_setting) or "").strip()
    ]


# Tipo de mensagem -> (chave de i18n do item de menu, opção de configuração
# que liga aquele tipo, em ai_accessibility). Fica aqui, e não no painel de
# conversas, para poder ser testado sem wx: o painel só pergunta.
_ACTION_BY_MESSAGE_TYPE = {
    "audioMessage": ("ai_transcribe_audio_menu", "transcribe_audio"),
    "imageMessage": ("ai_describe_image_menu", "describe_images"),
    "videoMessage": ("ai_describe_video_menu", "describe_videos"),
    "stickerMessage": ("ai_transcribe_sticker_menu", "transcribe_stickers"),
    "documentMessage": ("ai_pdf_accessible_menu", "pdf_to_accessible_text"),
}


def usable_settings(ai_settings) -> Optional[dict]:
    """ai_settings se o recurso pode ser usado agora (chave geral ligada e ao
    menos um provedor com chave salva); senão None — quem chama simplesmente
    não oferece o item de menu, em vez de oferecer e falhar ao clicar."""
    if not isinstance(ai_settings, dict) or not ai_settings.get("enabled"):
        return None
    if not configured_provider_ids(ai_settings):
        return None
    return ai_settings


def action_label_key(msg_type: str, ai_settings) -> str:
    """Chave de i18n do item de menu para este tipo de mensagem, ou "" quando
    o recurso está inutilizável ou desligado para este tipo."""
    if usable_settings(ai_settings) is None:
        return ""
    entry = _ACTION_BY_MESSAGE_TYPE.get(msg_type)
    if entry is None:
        return ""
    label_key, toggle = entry
    return label_key if ai_settings.get(toggle, True) else ""


def is_pdf_message(msg: dict) -> bool:
    """Se uma documentMessage é um PDF. O prompt de PDF é escrito para a
    estrutura de PDF; qualquer outro documento (docx, apk, xlsx...) é recusado
    em vez de enviado e mal interpretado."""
    inner = ((msg.get("message") or {}).get("documentMessage")) or {}
    mimetype = (inner.get("mimetype") or "").split(";")[0].strip().lower()
    return mimetype == "application/pdf"


def _chain_for(
    ai_settings: dict,
    *,
    needs_audio: bool = False,
    needs_video: bool = False,
    needs_pdf: bool = False,
    prefer: Optional[str] = None,
) -> list[_ProviderSpec]:
    by_id = _providers_by_id()
    chain = [
        by_id[pid] for pid in provider_order(ai_settings)
        if is_provider_enabled(ai_settings, pid)
        and (ai_settings.get(by_id[pid].key_setting) or "").strip()
        and (not needs_audio or by_id[pid].supports_audio)
        and (not needs_video or by_id[pid].supports_video)
        and (not needs_pdf or by_id[pid].supports_pdf)
    ]
    if prefer:
        # Tenta primeiro o provedor preferido (ex.: o que respondeu a
        # descrição inicial de uma imagem, ao perguntar algo específico
        # sobre ela depois) — mas mantém os demais na cadeia, como rede de
        # segurança caso ele agora falhe.
        chain = sorted(chain, key=lambda p: p.id != prefer)
    return chain


def _run_chain(
    chain: list[_ProviderSpec],
    ai_settings: dict,
    call: Callable[[object, str, str], str],
) -> tuple[str, str]:
    """
    call(module, api_key, model) -> texto do resultado.

    Tenta cada provedor da cadeia em ordem; devolve (texto, provider_id) do
    primeiro que responder com sucesso. Se todos falharem (ou a cadeia
    estiver vazia — nenhuma chave compatível configurada), levanta
    AIProviderError com o motivo de cada um.
    """
    if not chain:
        raise AIProviderError(
            "Nenhum provedor de IA compatível com este tipo de mídia está "
            "configurado. Abra Configurações > Transcrições e Descrições e "
            "informe pelo menos uma chave de API (Gemini, OpenAI, Claude, Groq ou OpenRouter)."
        )

    failures: list[str] = []
    for spec in chain:
        api_key = (ai_settings.get(spec.key_setting) or "").strip()
        model = spec.module.resolve_model((ai_settings.get(spec.model_setting) or "").strip())
        try:
            text = call(spec.module, api_key, model)
            return text, spec.id
        except Exception as exc:  # noqa: BLE001 — cada módulo já traduz a mensagem pro usuário
            failures.append(f"{spec.label}: {exc}")
            continue

    raise AIProviderError(
        "Nenhum dos provedores de IA configurados conseguiu processar esta mídia:\n\n"
        + "\n\n".join(failures)
    )


def transcribe_audio(file_path: str, ai_settings: dict) -> tuple[str, str]:
    """Transcreve áudio/voz, tentando Gemini e depois OpenAI (Claude não
    entra — ver limitação no topo do arquivo). Devolve (texto, provider_id)."""
    chain = _chain_for(ai_settings, needs_audio=True)
    return _run_chain(
        chain, ai_settings,
        lambda mod, key, model: mod.transcribe_audio(file_path, key, model=model),
    )


def describe_visual_media(
    file_path: str, ai_settings: dict, *, is_video: bool = False,
) -> tuple[str, str]:
    """Descreve imagem/figurinha (todos os provedores) ou vídeo (só Gemini).
    Devolve (texto, provider_id)."""
    chain = _chain_for(ai_settings, needs_video=is_video)
    return _run_chain(
        chain, ai_settings,
        lambda mod, key, model: mod.describe_visual_media(
            file_path, key, model=model, is_video=is_video
        ),
    )


def ask_about_visual_media(
    file_path: str,
    ai_settings: dict,
    question: str,
    *,
    is_video: bool = False,
    prefer: Optional[str] = None,
) -> tuple[str, str]:
    """Responde a uma pergunta específica sobre uma imagem/vídeo já descrito.

    prefer: id do provedor que respondeu a descrição inicial (ver
    ui/conversations.py) — tentado primeiro, para manter a mesma "voz" entre
    a descrição e as perguntas de acompanhamento, mas com os demais
    provedores compatíveis ainda disponíveis como reserva. Devolve
    (texto, provider_id).
    """
    chain = _chain_for(ai_settings, needs_video=is_video, prefer=prefer)
    return _run_chain(
        chain, ai_settings,
        lambda mod, key, model: mod.ask_about_visual_media(
            file_path, key, question, model=model, is_video=is_video
        ),
    )


def pdf_to_accessible_text(file_path: str, ai_settings: dict) -> tuple[str, str]:
    """Converte PDF em texto acessível (todos, exceto a Groq). Devolve
    (texto, provider_id)."""
    chain = _chain_for(ai_settings, needs_pdf=True)
    return _run_chain(
        chain, ai_settings,
        lambda mod, key, model: mod.pdf_to_accessible_text(file_path, key, model=model),
    )
