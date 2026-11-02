"""Receiver side of the internal JWT (C2): ES256 signature against the gateway JWKS and the rules of
mcp-contracts/schemas/internal-jwt.schema.json that a schema cannot express. Standard library only (the mocks run on a plain
python image), so the P-256 signature check is done here with integers; it is slow (milliseconds) and meant for stand-ins.

verify() raises Rejected(reason); the reasons are those of mcp-contracts/test-vectors/jwt.
"""
import base64
import hashlib
import json

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

SERVICE_SUBJECT = "system:mcp-gateway"
SERVICE_METHODS = {"initialize", "server/discover", "tools/list", "ping"}
BROKER_TOOL_PATHS = {"/v1/credentials/resolve", "/v1/authenticator", "/v1/authenticator/verify", "/v1/authenticator/invalidate-codes"}
COMMON_CLAIMS = {"iss", "aud", "sub", "grp", "rid", "prv", "iat", "exp", "jti"}
USER_CLAIMS = COMMON_CLAIMS | {"usr", "azp", "tool", "mfa"}


class Rejected(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _double_j(p):
    x, y, z = p
    if y == 0:
        return (0, 1, 0)
    yy = y * y % P
    s = 4 * x * yy % P
    m = 3 * (x - z * z) * (x + z * z) % P
    x3 = (m * m - 2 * s) % P
    return x3, (m * (s - x3) - 8 * yy * yy) % P, 2 * y * z % P


def _add_j(p, q):
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    z1z1, z2z2 = p[2] * p[2] % P, q[2] * q[2] % P
    u1, u2 = p[0] * z2z2 % P, q[0] * z1z1 % P
    s1, s2 = p[1] * q[2] * z2z2 % P, q[1] * p[2] * z1z1 % P
    if u1 == u2:
        return _double_j(p) if s1 == s2 else (0, 1, 0)
    h, r = (u2 - u1) % P, (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    return x3, (r * (v - x3) - s1 * hhh) % P, p[2] * q[2] * h % P


def _affine_x(p):
    return None if p[2] == 0 else p[0] * pow(p[2] * p[2], -1, P) % P


def _shamir(k1, p1, k2, p2):
    """k1*p1 + k2*p2 with one shared doubling chain (Jacobian coordinates, a single inversion at the end)."""
    both = _add_j(p1, p2)
    result = (0, 1, 0)
    for bit in range(max(k1.bit_length(), k2.bit_length()) - 1, -1, -1):
        result = _double_j(result)
        pair = ((k1 >> bit) & 1, (k2 >> bit) & 1)
        if pair == (1, 0):
            result = _add_j(result, p1)
        elif pair == (0, 1):
            result = _add_j(result, p2)
        elif pair == (1, 1):
            result = _add_j(result, both)
    return result


def _add(a, b):
    """Affine addition, kept for the signer of the tests."""
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0]:
        if (a[1] + b[1]) % P == 0:
            return None
        slope = (3 * a[0] * a[0] - 3) * pow(2 * a[1], -1, P) % P
    else:
        slope = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (slope * slope - a[0] - b[0]) % P
    return x, (slope * (a[0] - x) - a[1]) % P


def _multiply(k, point):
    result = None
    while k:
        if k & 1:
            result = _add(result, point)
        point = _add(point, point)
        k >>= 1
    return result


def on_curve(point):
    x, y = point
    return 0 < x < P and 0 < y < P and (y * y - (x * x * x - 3 * x + B)) % P == 0


def public_point(jwk):
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("not a P-256 key")
    point = (int.from_bytes(b64url_decode(jwk["x"]), "big"), int.from_bytes(b64url_decode(jwk["y"]), "big"))
    if not on_curve(point):
        raise ValueError("point not on the curve")
    return point


def verify_es256(signing_input, signature, point):
    if len(signature) != 64:
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (0 < r < N and 0 < s < N):
        return False
    e = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
    w = pow(s, -1, N)
    x = _affine_x(_shamir(e * w % N, (G[0], G[1], 1), r * w % N, (point[0], point[1], 1)))
    return x is not None and x % N == r


def sign_es256(signing_input, private_scalar, nonce):
    """Test helper: signs with the given nonce. A fixed nonce is fine for unit tests of a verifier, never for real keys."""
    e = int.from_bytes(hashlib.sha256(signing_input).digest(), "big")
    r = _multiply(nonce, G)[0] % N
    s = pow(nonce, -1, N) * (e + r * private_scalar) % N
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def _segments(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise Rejected("invalid_claims")
    try:
        header = json.loads(b64url_decode(parts[0]))
        payload = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except (ValueError, UnicodeDecodeError):
        raise Rejected("invalid_claims") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise Rejected("invalid_claims")
    return header, payload, signature, (parts[0] + "." + parts[1]).encode()


def _key_for(header, jwks, allow_test_keys, devstack):
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise Rejected("unknown_kid")
    if kid.startswith("test-") and not allow_test_keys:
        raise Rejected("test_key_not_allowed")
    if kid.startswith("dev-") and not (allow_test_keys and devstack):
        raise Rejected("test_key_not_allowed")
    for jwk in jwks.get("keys", []):
        if jwk.get("kid") == kid:
            try:
                return public_point(jwk)
            except (KeyError, ValueError):
                raise Rejected("unknown_kid") from None
    raise Rejected("unknown_kid")


def _text(claims, name, limit=255):
    value = claims.get(name)
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise Rejected("invalid_claims")
    return value


def _checked_claims(claims, issuer, audience, now, skew, max_lifetime):
    if not isinstance(claims.get("aud"), str):
        raise Rejected("invalid_claims")
    for name in ("iss", "sub", "rid", "prv", "jti"):
        _text(claims, name, 255 if name != "jti" else 128)
    for name in ("iat", "exp"):
        if not isinstance(claims.get(name), int) or isinstance(claims[name], bool) or claims[name] < 0:
            raise Rejected("invalid_claims")
    if not isinstance(claims.get("grp"), list) or not all(isinstance(g, str) for g in claims["grp"]) \
            or len(set(claims["grp"])) != len(claims["grp"]):
        raise Rejected("invalid_claims")
    if not set(claims) <= USER_CLAIMS:
        raise Rejected("invalid_claims")
    if claims["iss"] != issuer:
        raise Rejected("wrong_issuer")
    if claims["aud"] != audience:
        raise Rejected("wrong_audience")
    if claims["exp"] < now - skew:
        raise Rejected("expired")
    if claims["iat"] > now + skew:
        raise Rejected("not_yet_valid")
    if claims["exp"] - claims["iat"] > max_lifetime:
        raise Rejected("lifetime_too_long")


def _variant(claims, request):
    kind, method = request.get("kind"), request.get("method")
    path = request.get("path", "")
    service = claims["sub"] == SERVICE_SUBJECT
    if service:
        if claims["grp"] or "tool" in claims or "usr" in claims or "azp" in claims or "mfa" in claims:
            raise Rejected("invalid_claims")
        allowed = (kind == "jsonrpc" and method in SERVICE_METHODS) \
            or (kind == "http" and ((method, path) in {("GET", "/mcp-manifest"), ("GET", "/v1/systems")}))
        if not allowed:
            raise Rejected("service_token_not_allowed")
        return
    if claims["sub"].startswith("system:"):
        raise Rejected("invalid_claims")
    for name in ("usr", "azp"):
        _text(claims, name)
    needs_tool = (kind == "jsonrpc" and method == "tools/call") or (kind == "http" and path in BROKER_TOOL_PATHS)
    if needs_tool:
        if "tool" not in claims:
            raise Rejected("tool_missing")
        _text(claims, "tool", 64)
        if kind == "jsonrpc" and claims["tool"] != request.get("toolName"):
            raise Rejected("tool_mismatch")
    elif "tool" in claims:
        raise Rejected("tool_not_allowed")


class VerifiedTokens:
    """Tokens whose signature was checked, by the full token text, until their own exp (plus the skew). Only the signature step is
    skipped for a token seen again; claims, replay, variant and request id are checked on every call. Bounded."""

    def __init__(self, limit=4096):
        self.limit, self.entries = limit, {}

    def valid(self, token, point, now):
        entry = self.entries.get(token)
        return entry is not None and entry[0] >= now and entry[1] == point

    def add(self, token, point, expires, now):
        if len(self.entries) >= self.limit:
            self.entries = {t: e for t, e in self.entries.items() if e[0] >= now}
        if len(self.entries) < self.limit:
            self.entries[token] = (expires, point)


def verify(token, *, jwks, issuer, audience, now, request, seen_jtis=(), skew=30, max_lifetime=60,
           allow_test_keys=False, devstack=False, verified=None):
    """Returns the claims of an acceptable token. `request`: {kind: jsonrpc|http, method, path, toolName, xRequestId}.
    `seen_jtis` is a container of identifiers already used; the caller records the returned jti."""
    if not token:
        raise Rejected("missing_token")
    header, claims, signature, signing_input = _segments(token)
    if header.get("alg") != "ES256":
        raise Rejected("alg_not_allowed")
    point = _key_for(header, jwks, allow_test_keys, devstack)
    if verified is not None and verified.valid(token, point, now):
        pass
    elif not verify_es256(signing_input, signature, point):
        raise Rejected("bad_signature")
    elif verified is not None and isinstance(claims.get("exp"), int):
        verified.add(token, point, claims["exp"] + skew, now)
    _checked_claims(claims, issuer, audience, now, skew, max_lifetime)
    if claims["jti"] in seen_jtis:
        raise Rejected("replayed_jti")
    _variant(claims, request)
    if request.get("xRequestId") != claims["rid"]:
        raise Rejected("request_id_mismatch")
    return claims
