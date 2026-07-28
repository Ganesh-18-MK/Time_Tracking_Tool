"""Password hashing for employee self-signup.

Stdlib only (hashlib.pbkdf2_hmac) — deliberately no bcrypt/passlib. This
project's requirements.txt pins no versions, and a brand-new dependency
landing right before a production deploy is exactly how the Starlette
TemplateResponse incident happened (see app/templating.py). PBKDF2-SHA256
is a solid, dependency-free choice for this scale.

Stored format: "pbkdf2_sha256$<iterations>$<salt-hex>$<hash-hex>" so the
iteration count can be raised later without invalidating already-hashed
passwords (verify_password reads whatever count is stored).
"""
import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 260_000  # OWASP 2023 minimum recommendation for PBKDF2-SHA256
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """False (never raises) for malformed/empty stored hashes — covers
    accounts that haven't run /signup yet (password_hash is NULL)."""
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False
