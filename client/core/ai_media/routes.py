"""Optional provider order per kind of task (Settings > Online AI > Provider routes).

Without a route a kind uses the install-wide order, exactly as before. A route
is a selection AND an order: only its providers are tried, in its order. Every
other rule stays where it was: ``config.chain`` still drops a provider that is
switched off, has no key (or, for a local server, no model or address) or does
not take the kind, and consent is still asked for every provider of the chain
that is really used, so a route can never send to a provider without the
consent it already needs.

A route whose providers are all unusable leaves the kind with no provider (it
is not offered), it never falls back to the general order: the person left the
other providers out on purpose, perhaps to keep a kind on a local model.
"""
from .config import KINDS, PROVIDERS, supports

#: The tasks that may have a route, in the order the window lists them: the
#: media kinds, the chat summary ("text") and writing help ("writing").
TASKS = (*KINDS, "text", "writing")
#: i18n key naming each task in the routes window.
TASK_KEY = {"audio": "ai_route_task_audio", "image": "ai_route_task_image",
            "sticker": "ai_route_task_sticker", "video": "ai_route_task_video",
            "pdf": "ai_route_task_pdf", "text": "ai_result_summary_title",
            "writing": "ai_route_task_writing"}
assert set(TASK_KEY) == set(TASKS)


def validated(raw):
    """``{task: [provider, ...]}`` from the saved value, whatever it holds.

    Unknown tasks and providers, providers that do not take the task, repeats
    and non-lists are dropped; a task left with no provider has no route."""
    if not isinstance(raw, dict):
        return {}
    routes = {}
    for task in TASKS:
        saved = raw.get(task)
        if not isinstance(saved, list):
            continue
        order = [p for p in dict.fromkeys(p for p in saved if isinstance(p, str))
                 if p in PROVIDERS and supports(p, task)]
        if order:
            routes[task] = order
    return routes


def route_for(config, task):
    """The route of ``task`` in ``config`` (validated preferences, or a dict
    that has no routes at all), or None to use the general order."""
    routes = config.get("routes") if isinstance(config, dict) else None
    route = routes.get(task) if isinstance(routes, dict) else None
    return list(route) if isinstance(route, list) and route else None


def candidates(config, task, route=None):
    """What the routes window lists for ``task``: ``route`` (the draft; the
    saved route when None) first, then the other providers that take the task
    in the general order. Returns ``(providers, selected)``; ``selected`` is the
    route's own providers, the ones the window checks."""
    own = route if route is not None else (route_for(config, task) or [])
    own = [p for p in dict.fromkeys(own) if supports(p, task)]
    rest = [p for p in config.get("order", PROVIDERS) if p not in own and supports(p, task)]
    return own + rest, own
