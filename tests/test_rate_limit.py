"""app/rate_limit.py — the per-EMAIL failure lockout (pre-existing, had no
dedicated test coverage before this file) and the per-IP attempt throttle
added 2026-08-17 after a TPRM review flagged unusual traffic against the
app. Pure-function tests, no FastAPI TestClient (see test_tickets.py's
module docstring for why this project doesn't have one anywhere) —
client_ip() is the one function that touches a Request-shaped object,
faked here with a tiny stand-in rather than a real Starlette Request.
"""
from types import SimpleNamespace

import pytest

from app import rate_limit


@pytest.fixture(autouse=True)
def _isolate_state():
    """The module's counters are process-global dicts, so leftover entries
    from one test would silently affect the next — clear both before and
    after every test in this file."""
    rate_limit._failures.clear()
    rate_limit._ip_hits.clear()
    yield
    rate_limit._failures.clear()
    rate_limit._ip_hits.clear()


def _fake_clock(monkeypatch, start=1_000_000.0):
    """Patches rate_limit.time.time to a controllable fake clock so
    lockout-expiry math can be tested without real sleeps. Returns a
    callable that advances the fake clock by N seconds."""
    state = {"now": start}
    monkeypatch.setattr(rate_limit.time, "time", lambda: state["now"])

    def advance(seconds):
        state["now"] += seconds

    return advance


class TestEmailLockout:
    def test_unlocked_before_max_attempts(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.MAX_ATTEMPTS - 1):
            rate_limit.record_failure("login", "a@x.com")
        assert rate_limit.seconds_until_unlock("login", "a@x.com") == 0

    def test_locks_out_at_max_attempts(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure("login", "a@x.com")
        assert rate_limit.seconds_until_unlock("login", "a@x.com") > 0

    def test_lockout_expires_after_window(self, monkeypatch):
        advance = _fake_clock(monkeypatch)
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure("login", "a@x.com")
        advance(rate_limit.LOCKOUT_SECONDS + 1)
        assert rate_limit.seconds_until_unlock("login", "a@x.com") == 0

    def test_clear_removes_lockout(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure("login", "a@x.com")
        rate_limit.clear("login", "a@x.com")
        assert rate_limit.seconds_until_unlock("login", "a@x.com") == 0

    def test_email_is_case_and_whitespace_insensitive(self, monkeypatch):
        # a@x.com and "  A@X.com " must share one counter, or the lockout
        # is trivially dodged by re-casing the same address
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure("login", "  A@X.com ")
        assert rate_limit.seconds_until_unlock("login", "a@x.com") > 0

    def test_scopes_are_independent(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.MAX_ATTEMPTS):
            rate_limit.record_failure("login", "a@x.com")
        assert rate_limit.seconds_until_unlock("signup", "a@x.com") == 0


class TestIpThrottle:
    def test_unlocked_before_max_attempts(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.IP_MAX_ATTEMPTS - 1):
            rate_limit.record_ip_hit("login", "1.2.3.4")
        assert rate_limit.seconds_until_ip_unlock("login", "1.2.3.4") == 0

    def test_throttles_at_max_attempts(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.IP_MAX_ATTEMPTS):
            rate_limit.record_ip_hit("login", "1.2.3.4")
        assert rate_limit.seconds_until_ip_unlock("login", "1.2.3.4") > 0

    def test_throttle_decays_as_oldest_attempt_ages_out(self, monkeypatch):
        # sliding window, not a fixed lockout from the last hit — this is
        # what distinguishes seconds_until_ip_unlock from a naive "block
        # for N minutes after tripping" implementation
        advance = _fake_clock(monkeypatch)
        for _ in range(rate_limit.IP_MAX_ATTEMPTS):
            rate_limit.record_ip_hit("login", "1.2.3.4")
        advance(rate_limit.IP_WINDOW_SECONDS + 1)
        assert rate_limit.seconds_until_ip_unlock("login", "1.2.3.4") == 0

    def test_different_ips_are_independent(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.IP_MAX_ATTEMPTS):
            rate_limit.record_ip_hit("login", "1.2.3.4")
        assert rate_limit.seconds_until_ip_unlock("login", "9.9.9.9") == 0

    def test_different_scopes_are_independent(self, monkeypatch):
        _fake_clock(monkeypatch)
        for _ in range(rate_limit.IP_MAX_ATTEMPTS):
            rate_limit.record_ip_hit("login", "1.2.3.4")
        assert rate_limit.seconds_until_ip_unlock("signup", "1.2.3.4") == 0

    def test_ip_budget_is_more_generous_than_email_budget(self):
        # documents the deliberate choice in the module docstring: a
        # shared office/NAT IP with several people signing in shouldn't
        # get everyone locked out as fast as one misbehaving email would
        assert rate_limit.IP_MAX_ATTEMPTS > rate_limit.MAX_ATTEMPTS


class TestClientIp:
    def _request(self, headers=None, client_host="10.0.0.5"):
        return SimpleNamespace(
            headers=headers or {},
            client=SimpleNamespace(host=client_host) if client_host else None,
        )

    def test_trusts_last_entry_of_x_forwarded_for(self):
        # Cloud Run's own front end appends the IP it saw on the socket as
        # the LAST hop of X-Forwarded-For; earlier entries are supplied by
        # the client (or any proxy upstream of Cloud Run) and can't be
        # trusted — trusting the first entry instead would let an attacker
        # simply set their own X-Forwarded-For to bypass the throttle.
        req = self._request(headers={"x-forwarded-for": "9.9.9.9, 34.1.2.3"})
        assert rate_limit.client_ip(req) == "34.1.2.3"

    def test_single_entry_x_forwarded_for(self):
        req = self._request(headers={"x-forwarded-for": "34.1.2.3"})
        assert rate_limit.client_ip(req) == "34.1.2.3"

    def test_falls_back_to_socket_address_when_no_header(self):
        # local/dev runs (uvicorn directly, no Cloud Run proxy in front)
        req = self._request(headers={}, client_host="127.0.0.1")
        assert rate_limit.client_ip(req) == "127.0.0.1"

    def test_falls_back_to_unknown_when_nothing_available(self):
        req = self._request(headers={}, client_host=None)
        assert rate_limit.client_ip(req) == "unknown"
