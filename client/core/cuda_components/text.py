"""What the noise reducer's settings say about devices and components (no wx)."""

from core.cuda_components.catalog import COMPONENTS
from core.cuda_components.model import DEVICE_CPU, DEVICE_COREML, SHARED_CUBLAS_CU12

DEVICE_LABEL_KEYS = {
    "cpu": "noise_reducer_device_cpu",
    "cuda12": "noise_reducer_device_cuda12",
    "cuda13": "noise_reducer_device_cuda13",
    "coreml": "noise_reducer_device_coreml",
}


def size_text(i18n, size_bytes) -> str:
    """MB under 1 GB, GB from there, in the language's decimal separator."""
    if size_bytes >= 1024 ** 3:
        text = f"{size_bytes / 1024 ** 3:.1f} GB"
    else:
        text = f"{size_bytes / 1024 ** 2:.0f} MB"
    return text.replace(".", i18n.t("decimal_separator"))


def title_of(i18n, component_id) -> str:
    if component_id == SHARED_CUBLAS_CU12:
        return i18n.t("noise_reducer_shared_cublas")
    return COMPONENTS[component_id].title


def components_text(i18n, device, missing_ids, download_bytes, current_device) -> str:
    """The components field: what `device` still needs, or that it needs
    nothing; and that a change applies from the next start."""
    if device in (DEVICE_CPU, DEVICE_COREML):
        lines = [i18n.t("noise_reducer_components_none_needed")]
    elif not missing_ids:
        lines = [i18n.t("noise_reducer_components_ready")]
    else:
        lines = [i18n.t("noise_reducer_components_missing").format(
            count=len(missing_ids), size=size_text(i18n, download_bytes)),
            ", ".join(title_of(i18n, cid) for cid in missing_ids) + "."]
    if device != current_device:
        lines.append(i18n.t("noise_reducer_device_next_start"))
    return " ".join(lines)
