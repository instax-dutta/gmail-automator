import pytest

from fmaiily.crypto import CryptoError, TokenCipher

KEY = bytes(range(32))
KEY2 = bytes(reversed(range(32)))


def test_roundtrip_and_format() -> None:
    c = TokenCipher(KEY)
    token = c.encrypt("1//refresh-token", aad="me@example.com")
    assert token.startswith("v1.")
    assert c.decrypt(token, aad="me@example.com") == "1//refresh-token"


def test_nonce_is_unique_per_encryption() -> None:
    c = TokenCipher(KEY)
    assert c.encrypt("x") != c.encrypt("x")


def test_tamper_detected() -> None:
    c = TokenCipher(KEY)
    token = c.encrypt("secret")
    header, nonce, ct = token.split(".")
    flipped = "A" if ct[0] != "A" else "B"
    with pytest.raises(CryptoError):
        c.decrypt(f"{header}.{nonce}.{flipped}{ct[1:]}")


def test_wrong_key_detected() -> None:
    token = TokenCipher(KEY).encrypt("secret")
    with pytest.raises(CryptoError):
        TokenCipher(KEY2).decrypt(token)


def test_aad_binding() -> None:
    token = TokenCipher(KEY).encrypt("secret", aad="acct-a")
    with pytest.raises(CryptoError):
        TokenCipher(KEY).decrypt(token, aad="acct-b")


def test_old_key_decrypts_and_new_key_encrypts() -> None:
    old_token = TokenCipher(KEY).encrypt("secret")
    c = TokenCipher(KEY2, old_keys=(KEY,))
    # data written before rotation stays readable
    assert c.decrypt(old_token) == "secret"
    # new writes use the new key and are readable by the rotated cipher
    assert c.decrypt(c.encrypt("again")) == "again"
    # a cipher holding only the old key cannot read post-rotation writes
    with pytest.raises(CryptoError):
        TokenCipher(KEY).decrypt(c.encrypt("third"))


def test_invalid_wire_format() -> None:
    with pytest.raises(CryptoError):
        TokenCipher(KEY).decrypt("v2.bogus")
    with pytest.raises(CryptoError):
        TokenCipher(KEY).decrypt("not-a-token")


def test_key_length_enforced() -> None:
    with pytest.raises(ValueError):
        TokenCipher(b"short")
