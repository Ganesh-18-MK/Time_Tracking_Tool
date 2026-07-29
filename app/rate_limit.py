"""Login lockout — a small in-memory guard against password brute-forcing.

Stdlib only, no new dependency (same reasoning as app/security.py: a new
package landing right before a production deploy is a needless risk at this
stage). Keyed by the *submitted* email (lowercased) regardless of whether
that email actually exists, so a locked-out response looks identical
whether the account is real or not — consistent with authenticate()'s
enumeration-safe design in app/auth.py.

In-memory means the counters reset on process restart and aren't shared
across multiple worker processes. That's an acceptable trade-off at ~45
users on a single Render web service (one process). If this ever runs with
more than one worker/dyno, move the counters into the database or a shared
cache instead — this module is deliberately small so that swap is easy.
"""
import time
from collections import defaultdict
from threading import Lock

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60

_lock = Lock()
_failures: dict = defaultdict(list)  # "scope:email" -> [failure timestamps]


def _key(scope: str, email: str) -> str:
    return f"{scope}:{(email or '').strip().lower()}"


def seconds_until_unlock(scope: str, email: str) -> int:
    """0 if not currently locked out, else how many seconds remain.
    Also prunes attempts older than the lockout window as a side effect,
    so this module never needs a background cleanup task."""
    key = _key(scope, email)
    now = time.time()
    with _lock:
        recent = [t for t in _failures[key] if now - t < LOCKOUT_SECONDS]
        if recent:
            _failures[key] = recent
        else:
            _failures.pop(key, None)
        if len(recent) < MAX_ATTEMPTS:
            return 0
        return max(0, int(LOCKOUT_SECONDS - (now - min(recent))))


def record_failure(scope: str, email: str) -> None:
    with _lock:
        _failures[_key(scope, email)].append(time.time())


def clear(scope: str, email: str) -> None:
    with _lock:
        _failures.pop(_key(scope, email), None)
