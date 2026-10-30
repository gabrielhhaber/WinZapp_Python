"""Capability-gated call PCM configuration; no client, device or environment I/O."""


def configure_call_audio(layer, pcm48=False):
    capability = getattr(layer, "pcm48_available", None)
    if callable(capability) and capability():
        layer.configure(True, pcm48=pcm48)
    else:
        layer.configure(True)
