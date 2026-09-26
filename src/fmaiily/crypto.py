from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from fmaiily.errors import GatewayError

VERSION = "v1"
NONCE_BYTES = 12


class CryptoError(GatewayError):
    """Raised when ciphertext cannot be decrypted or has an invalid format."""

    code, http_status = "crypto_error", 500

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message, details=details)


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def encode_key(key: bytes) -> str:
    """Render a raw key the way `FMAIILY_TOKEN_ENCRYPTION_KEY` expects it."""
    return _b64e(key)


class TokenCipher:
    def __init__(self, key: bytes, *, old_keys: tuple[bytes, ...] = ()) -> None:
        if len(key) != 32:
            raise ValueError("encryption key must be exactly 32 bytes")
        for old in old_keys:
            if len(old) != 32:
                raise ValueError("old encryption keys must be exactly 32 bytes")
        self._key = key
        self._old_keys = old_keys
        self._aesgcm = AESGCM(key)
        self._old = tuple(AESGCM(k) for k in old_keys)

    @property
    def known_keys(self) -> tuple[bytes, ...]:
        """Every key this cipher can read, newest first.

        A staged key rotation keeps the previous key here until every stored value has been
        re-encrypted, so reads keep working throughout the window.
        """
        return (self._key, *self._old_keys)

    def encrypt(self, plaintext: str, *, aad: str = "") -> str:
        nonce = os.urandom(NONCE_BYTES)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode(), aad.encode())
        return f"{VERSION}.{_b64e(nonce)}.{_b64e(ct)}"

    def decrypt(self, token: str, *, aad: str = "") -> str:
        try:
            version, nonce_b64, ct_b64 = token.split(".")
        except ValueError as exc:
            raise CryptoError("invalid token format") from exc
        if version != VERSION:
            raise CryptoError(f"unsupported token version: {version!r}")
        try:
            nonce, ct = _b64d(nonce_b64), _b64d(ct_b64)
        except Exception as exc:
            raise CryptoError("invalid base64 in token") from exc
        for cipher in (self._aesgcm, *self._old):
            try:
                return cipher.decrypt(nonce, ct, aad.encode()).decode()
            except InvalidTag:
                continue
        raise CryptoError("decryption failed: wrong key, tampered data, or mismatched AAD")
