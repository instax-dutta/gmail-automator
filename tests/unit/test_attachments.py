import base64
from pathlib import Path

import pytest

from gmail_automator.attachments import resolve_attachments
from gmail_automator.config import Settings
from gmail_automator.errors import AttachmentPathNotAllowed, AttachmentTooLarge, InvalidRequest

KEY = base64.urlsafe_b64encode(b"a" * 32).decode()


def _settings(**overrides) -> Settings:
    return Settings(token_encryption_key=KEY, _env_file=None, **overrides)


@pytest.fixture
def allowed_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "allowed"
    directory.mkdir()
    (directory / "note.txt").write_text("hello from a file")
    (directory / "data.bin").write_bytes(bytes(range(256)))
    (directory / "big.bin").write_bytes(b"0" * 5000)
    return directory


def _spec(filename: str, **kwargs) -> tuple[str, str | None, str | None, str]:
    return (
        filename,
        kwargs.get("content"),
        kwargs.get("path"),
        kwargs.get("mime", "application/octet-stream"),
    )


# ------------------------------------------------------------------ switches


def test_no_attachments_needs_no_configuration() -> None:
    resolved = resolve_attachments([], settings=_settings())
    assert resolved.attachments == ()
    assert resolved.total_bytes == 0


def test_attachments_are_refused_when_disabled() -> None:
    with pytest.raises(InvalidRequest) as excinfo:
        resolve_attachments(
            [_spec("a.txt", content="aGk=")], settings=_settings(attachments_enabled=False)
        )
    assert "GMAIL_AUTOMATOR_ATTACHMENTS_ENABLED" in excinfo.value.message


# -------------------------------------------------------------------- inline


def test_inline_attachment_round_trips() -> None:
    resolved = resolve_attachments(
        [_spec("a.txt", content=base64.b64encode(b"hello").decode(), mime="text/plain")],
        settings=_settings(attachments_enabled=True),
    )
    assert len(resolved.attachments) == 1
    assert resolved.attachments[0].content == b"hello"
    assert resolved.attachments[0].mime_type == "text/plain"
    assert resolved.total_bytes == 5


def test_inline_attachments_need_no_allow_list() -> None:
    resolved = resolve_attachments(
        [_spec("a.txt", content="aGk=")], settings=_settings(attachments_enabled=True)
    )
    assert resolved.attachments[0].filename == "a.txt"


def test_invalid_inline_base64_is_a_clean_error() -> None:
    with pytest.raises(InvalidRequest) as excinfo:
        resolve_attachments(
            [_spec("a.txt", content="not base64!!")], settings=_settings(attachments_enabled=True)
        )
    assert excinfo.value.details["filename"] == "a.txt"


def test_inline_content_over_the_limit_is_rejected() -> None:
    payload = base64.b64encode(b"0" * 100).decode()
    with pytest.raises(AttachmentTooLarge):
        resolve_attachments(
            [_spec("a.bin", content=payload)],
            settings=_settings(attachments_enabled=True, attachment_max_bytes=10),
        )


# ---------------------------------------------------------------------- path


def test_path_attachment_is_read(allowed_dir: Path) -> None:
    resolved = resolve_attachments(
        [_spec("note.txt", path=str(allowed_dir / "note.txt"), mime="text/plain")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]),
    )
    assert resolved.attachments[0].content == b"hello from a file"


def test_path_mime_type_is_guessed_when_not_given(allowed_dir: Path) -> None:
    resolved = resolve_attachments(
        [_spec("note.txt", path=str(allowed_dir / "note.txt"), mime="")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]),
    )
    assert resolved.attachments[0].mime_type == "text/plain"


def test_path_outside_the_allow_list_is_refused(tmp_path: Path, allowed_dir: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    with pytest.raises(AttachmentPathNotAllowed) as excinfo:
        resolve_attachments(
            [_spec("secret.txt", path=str(secret))],
            settings=_settings(
                attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]
            ),
        )
    assert "GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS" in excinfo.value.message
    assert str(allowed_dir) in excinfo.value.details["allowed_dirs"]


def test_path_attachment_without_an_allow_list_is_refused(allowed_dir: Path) -> None:
    with pytest.raises(AttachmentPathNotAllowed) as excinfo:
        resolve_attachments(
            [_spec("note.txt", path=str(allowed_dir / "note.txt"))],
            settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[]),
        )
    assert "GMAIL_AUTOMATOR_ATTACHMENT_ALLOWED_DIRS" in excinfo.value.message


def test_a_symlink_escaping_the_allow_list_is_refused(tmp_path: Path, allowed_dir: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    link = allowed_dir / "innocent.txt"
    link.symlink_to(secret)
    with pytest.raises(AttachmentPathNotAllowed):
        resolve_attachments(
            [_spec("innocent.txt", path=str(link))],
            settings=_settings(
                attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]
            ),
        )


def test_a_symlink_inside_the_allow_list_is_allowed(allowed_dir: Path) -> None:
    link = allowed_dir / "alias.txt"
    link.symlink_to(allowed_dir / "note.txt")
    resolved = resolve_attachments(
        [_spec("alias.txt", path=str(link))],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]),
    )
    assert resolved.attachments[0].content == b"hello from a file"


def test_a_sibling_directory_with_a_shared_prefix_is_refused(
    tmp_path: Path, allowed_dir: Path
) -> None:
    """/data/allowed-evil must not pass a check written as `startswith('/data/allowed')`."""
    sibling = tmp_path / "allowed-evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("do not read me")
    with pytest.raises(AttachmentPathNotAllowed):
        resolve_attachments(
            [_spec("secret.txt", path=str(sibling / "secret.txt"))],
            settings=_settings(
                attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]
            ),
        )


def test_a_directory_is_not_a_valid_attachment(tmp_path: Path) -> None:
    directory = tmp_path / "allowed"
    (directory / "sub").mkdir(parents=True)
    with pytest.raises(AttachmentPathNotAllowed) as excinfo:
        resolve_attachments(
            [_spec("sub", path=str(directory / "sub"))],
            settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(directory)]),
        )
    assert "not a regular file" in excinfo.value.message


def test_a_missing_path_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(AttachmentPathNotAllowed):
        resolve_attachments(
            [_spec("nope.txt", path=str(tmp_path / "nope.txt"))],
            settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(tmp_path)]),
        )


def test_an_oversized_file_is_rejected_before_being_read(allowed_dir: Path) -> None:
    with pytest.raises(AttachmentTooLarge) as excinfo:
        resolve_attachments(
            [_spec("big.bin", path=str(allowed_dir / "big.bin"))],
            settings=_settings(
                attachments_enabled=True,
                attachment_allowed_dirs=[str(allowed_dir)],
                attachment_max_bytes=1000,
            ),
        )
    assert excinfo.value.details["max_bytes"] == 1000


def test_binary_content_is_preserved_byte_for_byte(allowed_dir: Path) -> None:
    resolved = resolve_attachments(
        [_spec("data.bin", path=str(allowed_dir / "data.bin"), mime="")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]),
    )
    assert resolved.attachments[0].content == bytes(range(256))


def test_several_attachments_are_all_resolved(allowed_dir: Path) -> None:
    resolved = resolve_attachments(
        [
            _spec("note.txt", path=str(allowed_dir / "note.txt"), mime="text/plain"),
            _spec("inline.txt", content="aGk=", mime="text/plain"),
        ],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]),
    )
    assert [a.filename for a in resolved.attachments] == ["note.txt", "inline.txt"]
    assert resolved.total_bytes == 19


def test_multiple_allow_list_directories_are_honoured(tmp_path: Path, allowed_dir: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("x")
    resolved = resolve_attachments(
        [
            _spec("note.txt", path=str(allowed_dir / "note.txt"), mime="text/plain"),
            _spec("x.txt", path=str(other / "x.txt"), mime="text/plain"),
        ],
        settings=_settings(
            attachments_enabled=True,
            attachment_allowed_dirs=[str(allowed_dir), str(other)],
        ),
    )
    assert len(resolved.attachments) == 2


def test_a_relative_path_is_resolved_against_the_working_directory(
    allowed_dir: Path, monkeypatch
) -> None:
    monkeypatch.chdir(allowed_dir)
    resolved = resolve_attachments(
        [_spec("note.txt", path="note.txt", mime="text/plain")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]),
    )
    assert resolved.attachments[0].content == b"hello from a file"


def test_traversal_out_of_the_allow_list_is_refused(tmp_path: Path, allowed_dir: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read me")
    with pytest.raises(AttachmentPathNotAllowed):
        resolve_attachments(
            [_spec("secret.txt", path=str(allowed_dir / ".." / "secret.txt"))],
            settings=_settings(
                attachments_enabled=True, attachment_allowed_dirs=[str(allowed_dir)]
            ),
        )


def test_pdf_is_guessed_from_content_when_the_name_says_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "allowed"
    directory.mkdir()
    (directory / "document").write_bytes(b"%PDF-1.7\n...")
    resolved = resolve_attachments(
        [_spec("document", path=str(directory / "document"), mime="")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(directory)]),
    )
    assert resolved.attachments[0].mime_type == "application/pdf"


def test_an_explicit_mime_type_is_never_overridden(tmp_path: Path) -> None:
    directory = tmp_path / "allowed"
    directory.mkdir()
    (directory / "note.txt").write_text("hello")
    resolved = resolve_attachments(
        [_spec("note.txt", path=str(directory / "note.txt"), mime="application/x-custom")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(directory)]),
    )
    assert resolved.attachments[0].mime_type == "application/x-custom"


def test_unknown_type_falls_back_to_octet_stream(tmp_path: Path) -> None:
    directory = tmp_path / "allowed"
    directory.mkdir()
    (directory / "blob.unknownext").write_bytes(b"\x00\x01")
    resolved = resolve_attachments(
        [_spec("blob.unknownext", path=str(directory / "blob.unknownext"), mime="")],
        settings=_settings(attachments_enabled=True, attachment_allowed_dirs=[str(directory)]),
    )
    assert resolved.attachments[0].mime_type == "application/octet-stream"
