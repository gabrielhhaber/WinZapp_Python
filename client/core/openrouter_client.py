"""
core/openrouter_client.py

Integração com a OpenRouter (API compatível com a da OpenAI, ver
core/openai_compat.py) como provedor de IA alternativo na ordem de
core/ai_providers.py. Uma única chave dá acesso a centenas de modelos, inclusive
gratuitos — por isso o modelo é configurável e o campo aceita qualquer id.
    - Descrição de imagens
    - Conversão de PDFs em texto acessível (a OpenRouter extrai o PDF por conta
      própria quando o modelo não lê PDF nativamente)

Limitações reais (não é bug): o WinZapp não usa a OpenRouter para transcrever
áudio nem descrever vídeo — nem todo modelo aceita essas entradas, e o Gemini
já cobre ambos. core/ai_providers.py não a chama nesses casos.

Requer a biblioteca oficial: pip install openai
"""

from __future__ import annotations

from core.openai_compat import CompatProvider, OpenAICompatClient, OpenAICompatError as OpenRouterClientError

# Modelo barato, com visão e leitura de PDF. Troque aqui quando for aposentado.
DEFAULT_MODEL = "google/gemini-3.5-flash-lite"

RECOMMENDED_MODELS = [
    "google/gemini-3.5-flash-lite",
    "google/gemini-3.5-flash",
    "qwen/qwen3.8-27b:free",
    "anthropic/claude-sonnet-5",
]

_client = OpenAICompatClient(CompatProvider(
    label="OpenRouter",
    base_url="https://openrouter.ai/api/v1",
    default_model=DEFAULT_MODEL,
    billing_url="https://openrouter.ai/settings/credits",
    transcribe_model="",
    supports_pdf=True,
))

resolve_model = _client.resolve_model
transcribe_audio = _client.transcribe_audio
describe_visual_media = _client.describe_visual_media
ask_about_visual_media = _client.ask_about_visual_media
pdf_to_accessible_text = _client.pdf_to_accessible_text
