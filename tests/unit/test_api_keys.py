import pytest

from gmail_automator.api_keys import (
    ALL_SCOPES,
    ApiKeyContext,
    generate_key,
    hash_key,
    split_key,
)
from gmail_automator.errors import Forbidden, InvalidRequest, Unauthorized


def test_generated_key_matches_the_documented_shape() -> None:
    full, prefix, key_hash = generate_key()
    assert full.startswith("fmg_")
    # urlsafe secrets may themselves contain "_", so only the first two separators are structural
    assert full.count("_") >= 2
    assert len(prefix) == len("fmg_") + 8
    assert len(key_hash) == 64
    scheme, prefix_part, secret = full.split("_", 2)
    assert scheme == "fmg"
    assert len(prefix_part) == 8
    assert len(secret) == 43


def test_split_key_round_trips() -> None:
    full, prefix, _ = generate_key()
    assert split_key(full) == (prefix, full.split("_", 2)[2])


def test_split_key_rejects_malformed_input() -> None:
    for bad in ("", "nope", "fmg_only", "fmg_abc_", "xxx_abc_def", "fmg__secret"):
        assert split_key(bad) is None


def test_generated_keys_are_unique() -> None:
    keys = {generate_key()[0] for _ in range(50)}
    assert len(keys) == 50


def test_hash_is_stable_and_not_the_secret() -> None:
    full, _, key_hash = generate_key()
    secret = full.split("_", 2)[2]
    assert hash_key(secret) == key_hash
    assert secret not in key_hash


# --------------------------------------------------------------- permissions


def test_local_context_is_fully_permitted() -> None:
    key = ApiKeyContext.local()
    for scope in ALL_SCOPES:
        key.require_scope(scope)
    assert key.allows_account("anything@example.com")
    assert key.key_id is None


def test_scope_check_is_exact_membership() -> None:
    key = ApiKeyContext(key_id=1, name="k", scopes=("send",))
    assert key.has_scope("send")
    assert not key.has_scope("read")
    with pytest.raises(Forbidden) as excinfo:
        key.require_scope("admin")
    assert excinfo.value.details["required_scope"] == "admin"


def test_account_allowlist_is_case_insensitive_and_optional() -> None:
    unrestricted = ApiKeyContext(key_id=1, name="k")
    assert unrestricted.allows_account("anyone@example.com")
    restricted = ApiKeyContext(key_id=2, name="k", allowed_accounts=("Me@Example.com",))
    assert restricted.allows_account("me@example.com")
    assert not restricted.allows_account("other@example.com")
    with pytest.raises(Forbidden):
        restricted.require_account("other@example.com")


def test_bearer_header_parsing() -> None:
    from gmail_automator.api_keys import bearer_token_from_header

    assert bearer_token_from_header("Bearer fmg_a_b") == "fmg_a_b"
    assert bearer_token_from_header("bearer   fmg_a_b  ") == "fmg_a_b"
    for bad in (None, "", "Basic abc", "Bearer", "Bearer   "):
        with pytest.raises(Unauthorized):
            bearer_token_from_header(bad)


# ------------------------------------------------------------------ issuance


def test_issue_and_verify_a_key(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    service = ApiKeyService(container=container)
    context, full_key = service.create(name="agent", scopes=("send", "read"))
    verified = service.verify(full_key)
    assert verified.key_id == context.key_id
    assert verified.name == "agent"
    assert set(verified.scopes) == {"send", "read"}


def test_the_secret_is_never_stored(container) -> None:
    from gmail_automator.api_keys import ApiKeyService
    from gmail_automator.models import ApiKeyRow

    _, full_key = ApiKeyService(container=container).create(name="agent")
    secret = full_key.split("_", 2)[2]
    with container.session_factory() as session:
        rows = list(session.query(ApiKeyRow).all())
    assert len(rows) == 1
    assert secret not in rows[0].key_hash
    assert full_key not in rows[0].key_hash
    assert rows[0].key_prefix in full_key


def test_verification_updates_last_used(container) -> None:
    from gmail_automator.api_keys import ApiKeyService
    from gmail_automator.models import ApiKeyRow

    service = ApiKeyService(container=container)
    _, full_key = service.create(name="agent")
    service.verify(full_key)
    with container.session_factory() as session:
        row = session.query(ApiKeyRow).one()
    assert row.last_used_at is not None


def test_a_wrong_secret_is_rejected(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    service = ApiKeyService(container=container)
    _, full_key = service.create(name="agent")
    prefix, _ = split_key(full_key)
    with pytest.raises(Unauthorized):
        service.verify(f"{prefix}_not-the-real-secret")


def test_an_unknown_prefix_is_rejected(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    with pytest.raises(Unauthorized):
        ApiKeyService(container=container).verify("fmg_REDACTED_IN_HISTORY")


def test_a_malformed_key_is_rejected(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    with pytest.raises(Unauthorized):
        ApiKeyService(container=container).verify("garbage")


def test_a_revoked_key_stops_working(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    service = ApiKeyService(container=container)
    _, full_key = service.create(name="agent")
    prefix, _ = split_key(full_key)
    service.verify(full_key)
    service.revoke(prefix)
    with pytest.raises(Unauthorized):
        service.verify(full_key)


def test_revoking_an_unknown_prefix_is_an_invalid_request(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    with pytest.raises(InvalidRequest):
        ApiKeyService(container=container).revoke("fmg_00000000")


def test_listing_never_exposes_the_hash(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    service = ApiKeyService(container=container)
    service.create(name="one")
    service.create(name="two", allowed_accounts=("a@example.com",))
    listed = service.list_keys()
    assert [row["name"] for row in listed] == ["one", "two"]
    assert listed[1]["allowed_accounts"] == ["a@example.com"]
    for row in listed:
        assert "key_hash" not in row
        assert row["revoked_at"] is None


def test_creating_a_key_requires_a_name(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    with pytest.raises(InvalidRequest):
        ApiKeyService(container=container).create(name="   ")


def test_creating_a_key_rejects_an_unknown_scope(container) -> None:
    from gmail_automator.api_keys import ApiKeyService

    with pytest.raises(InvalidRequest) as excinfo:
        ApiKeyService(container=container).create(name="k", scopes=("send", "sudo"))
    assert excinfo.value.details["allowed"] == list(ALL_SCOPES)
