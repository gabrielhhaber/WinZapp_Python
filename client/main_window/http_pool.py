"""Process-wide pooled HTTP session (Keep-Alive) behind requests.get/post.

main.py installs _patched_get/_patched_post over requests.get/post at
import time; the objects live here so the mixins can reach the session.
"""

import requests


# Enable global HTTP connection pooling (Keep-Alive) to optimize remote API request latency
_http_session = requests.Session()
_orig_get = requests.get
_orig_post = requests.post



def _patched_get(*args, **kwargs):
    return _http_session.get(*args, **kwargs)

def _patched_post(*args, **kwargs):
    return _http_session.post(*args, **kwargs)
