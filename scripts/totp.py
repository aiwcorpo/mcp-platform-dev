"""TOTP (RFC 6238, HMAC-SHA-1, 6 digits, 30 s) for the dev test users and the Authenticator scenario.

Seeds of the realm users come from .secrets/dev.env (KC_USER_<NAME>_OTP_SEED); a seed returned by the broker at
registration is passed in by the caller. Nothing here prints or logs a seed or a code.
"""
import base64
import hashlib
import hmac
import struct
import time

STEP = 30


def code(seed_base32, at=None, offset=0):
    """Code of the 30 s step containing `at` (default now), `offset` steps away."""
    seed = seed_base32.replace(" ", "").upper()
    key = base64.b32decode(seed + "=" * (-len(seed) % 8))
    counter = int((time.time() if at is None else at) // STEP) + offset
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    cut = digest[-1] & 0x0F
    return f"{(struct.unpack('>I', digest[cut:cut + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def seconds_left(at=None):
    return STEP - (time.time() if at is None else at) % STEP


_last_step = {}


def fresh_code(seed_base32, minimum_left=4):
    """A code that stays valid for at least `minimum_left` seconds and was not handed out before in this process
    (Keycloak and the broker accept every step once, so a second login in the same step would be refused; the call
    waits for the next step instead)."""
    if seconds_left() < minimum_left:
        time.sleep(seconds_left() + 0.2)
    step = int(time.time() // STEP)
    if _last_step.get(seed_base32) == step:
        time.sleep(seconds_left() + 0.2)
        step += 1
    _last_step[seed_base32] = step
    return code(seed_base32)
