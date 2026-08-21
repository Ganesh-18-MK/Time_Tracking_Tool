"""Login lockout — a small in-memory guard against password brute-forcing.

Stdlib only, no new dependency (same reasoning as app/security.py: a new
package landing right before a production deploy is a needless risk at this
stage). Two independent layers live here:

1. Per-EMAIL failure lockout (original). Keyed by the *submitted* email
   (lowercased) regardless of whether that email actually exists, so a
   locked-out response looks identical whether the account is real or
   not — consistent with authenticate()'s enumeration-safe design in
   app/auth.py. Only failed attempts count.

2. Per-IP attempt throttle (added 2026-08-17, after a TPRM review flagged
   unusual traffic against the app). Keyed by client IP, counts every
   attempt — success or failure — not just wrong passwords. This catches
   what the email layer structurally can't: one IP hammering many
   *different* emails (credential stuffing / roster enumeration), or
   plain high-volume traffic that never trips any single email's counter.
   Deliberately more generous than the email layer (IP_MAX_ATTEMPTS >
   MAX_ATTEMPTS) so one shared office/NAT IP with several people signing
   in around the same time doesn't get everyone locked out.

   IP_MAX_ATTEMPTS raised 30 -> 200 (Ganesh, 2026-08-21, ahead of a planned
   headcount increase to ~150-250 employees). At 30, a single shared
   office/NAT IP logging in during a normal morning rush was already
   close to the ceiling at ~45 staff — at 150-250 it would trip on
   completely legitimate traffic with no attacker involved, exactly the
   false-positive load_test/ hit during ramp-up (see its README). 200
   comfortably covers the entire post-growth headcount logging in from
   one shared IP inside a single 10-minute window, while staying well
   below the volume a real credential-stuffing/enumeration run would
   produce — the TPRM-motivated protection this layer exists for is
   still intact, just resized for real usage instead of the old ~45-user
   baseline. Revisit this number again if headcount grows past what a
   comfortable buffer over 200 covers, or if IP_MAX_ATTEMPTS ever needs
   to move to a shared store (see the multi-instance/multi-worker note
   below) — the actual attempt volume from a real incident, not a guess,
   should drive the next change.

Both layers return the exact same lockout message (see _lockout_message in
app/routes/auth.py) so a caller can't tell which one tripped.

In-memory means the counters reset on process restart and aren't shared
across multiple instances. That's an acceptable trade-off at ~45 users, but
worth flagging: the app runs on Cloud Run (deploy_gcp.sh), which can
autoscale to more than one instance under load — neither counter here is
shared across instances if that happens, so a determined attacker spread
across enough concurrent requests could get more attempts through than the
numbers below suggest. If that becomes a real concern, move the counters
into Postgres (already the prod DB) or add a Cloud Armor rate-limit policy
in front of Cloud Run instead — this module is deliberately small so either
swap is easy.

RATE_LIMIT_DISABLED: an explicit, off-by-default escape hatch for local
load testing ONLY (e.g. load_test/, where 100 simulated users share the
one real IP of the machine running Locust and would otherwise all trip
the per-IP layer within seconds of ramp-up). Never set this in a real
deploy — deploy_gcp.sh does not set it, and it must stay that way.
"""
import os
import time
from collections import defaultdict
from threading import Lock

from fastapi import Request

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60

IP_MAX_ATTEMPTS = 200  # see module docstring, raised 30 -> 200 on 2026-08-21
IP_WINDOW_SECONDS = 10 * 60

RATE_LIMIT_DISABLED = os.environ.get("RATE_LIMIT_DISABLED", "0") == "1"

_lock = Lock()
_failures: dict = defaultdict(list)  # "scope:email" -> [failure timestamps]
_ip_hits: dict = defaultdict(list)   # "scope:ip" -> [attempt timestamps]


def _key(scope: str, email: str) -> str:
    return f"{scope}:{(email or '').strip().lower()}"


def _ip_key(scope: str, ip: str) -> str:
    return f"ip:{scope}:{ip}"


def client_ip(request: Request) -> str:
    """Best-effort real client IP. Cloud Run terminates TLS and proxies
    every request through its own front end, which appends the IP it saw
    on the socket as the LAST entry of X-Forwarded-For — any earlier
    entries in that header are attacker-controllable and not trustworthy.
    (There's no CDN/Firebase Hosting layer in front of Cloud Run here per
    deploy_gcp.sh, so "last entry" is the right hop to trust — that
    wouldn't hold if one were added later.) Falls back to the raw socket
    address for local/dev runs, where there's no proxy at all."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def seconds_until_unlock(scope: str, email: str) -> int:
    """0 if not currently locked out, else how many seconds remain.
    Also prunes attempts older than the lockout window as a side effect,
    so this module never needs a background cleanup task."""
    if RATE_LIMIT_DISABLED:
        return 0
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
    if RATE_LIMIT_DISABLED:
        return
    with _lock:
        _failures[_key(scope, email)].append(time.time())


def clear(scope: str, email: str) -> None:
    with _lock:
        _failures.pop(_key(scope, email), None)


def seconds_until_ip_unlock(scope: str, ip: str) -> int:
    """0 if this IP is under its attempt budget for `scope`, else how many
    seconds until it's allowed again. Identical shape to
    seconds_until_unlock above (prune-on-read, expiry anchored to the
    OLDEST attempt still in window via min(recent)) — that's what makes it
    a proper sliding window: the lockout decays as the oldest attempt in
    IP_WINDOW_SECONDS ages out, rather than resetting from whichever
    attempt happened last."""
    if RATE_LIMIT_DISABLED:
        return 0
    key = _ip_key(scope, ip)
    now = time.time()
    with _lock:
        recent = [t for t in _ip_hits[key] if now - t < IP_WINDOW_SECONDS]
        if recent:
            _ip_hits[key] = recent
        else:
            _ip_hits.pop(key, None)
        if len(recent) < IP_MAX_ATTEMPTS:
            return 0
        return max(0, int(IP_WINDOW_SECONDS - (now - min(recent))))


def record_ip_hit(scope: str, ip: str) -> None:
    if RATE_LIMIT_DISABLED:
        return
    with _lock:
        _ip_hits[_ip_key(scope, ip)].append(time.time())
