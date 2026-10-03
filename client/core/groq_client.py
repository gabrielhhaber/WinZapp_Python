"""
core/groq_client.py

Integração com a Groq (API compatível com a da OpenAI, ver core/openai_compat.py)
como provedor de IA alternativo na ordem de core/ai_providers.py:
    - Transcrição de áudio/voz (Whisper large-v3-turbo) — rápida e com plano gratuito
    - Descrição de imagens (modelo multimodal)

Limitações reais da Groq (não é bug): a API não aceita arquivo de vídeo nem PDF
como entrada — só imagem e áudio. core/ai_providers.py já sabe disso e não a
chama para vídeo/PDF.

Requer a biblioteca oficial: pip install openai
"""

from __future__ import annotations

from core.openai_compat import CompatProvider, OpenAICompatClient, OpenAICompatError as GroqClientError

# Modelo multimodal atual da Groq. Troque aqui quando ele for aposentado: quem
# está no automático (campo em branco) atualiza sozinho.
DEFAULT_MODEL = "qwen/qwen3.8-27b"

RECOMMENDED_MODELS = [
    "qwen/qwen3.8-27b",
]

_client = OpenAICompatClient(CompatProvider(
    label="Groq",
    base_url="https://api.groq.com/openai/v1",
    default_model=DEFAULT_MODEL,
    billing_url="https://console.groq.com/settings/billing",
    transcribe_model="whisper-large-v3-turbo",
    supports_pdf=False,
))

resolve_model = _client.resolve_model
transcribe_audio = _client.transcribe_audio
describe_visual_media = _client.describe_visual_media
ask_about_visual_media = _client.ask_about_visual_media
pdf_to_accessible_text = _client.pdf_to_accessible_text
