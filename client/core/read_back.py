"""Believe an action when a fresh reading shows its effect.

For provider actions whose own answer says too little: a route that passes on
a library's raw result (an object, a flag, or nothing), or a request made of
two steps of which the second can fail after the first took effect.
"""

import time


def run_action(action, read, achieved, attempts=3, pause=1.5, sleep=time.sleep):
    """Run *action* once, never again, then *read* until *achieved(reading)*.

    Returns (state, reading). "done": a reading shows the effect. "failed":
    the action was refused and no reading shows it. "unconfirmed": it was
    accepted, and no reading shows it yet. *reading* is the last one that
    worked, None when none did.

    A refused action is read back as patiently as an accepted one. Seen on a
    real account: creating a community with no group answers an error (the
    library then links an empty list, which WhatsApp refuses) after the
    community exists, and the page shows it about half a second later.
    """
    refused = False
    try:
        action()
    except Exception:
        refused = True
    reading = None
    for attempt in range(attempts):
        if attempt:
            sleep(pause)
        try:
            reading = read()
        except Exception:
            continue
        if achieved(reading):
            return "done", reading
    return ("failed" if refused else "unconfirmed"), reading
