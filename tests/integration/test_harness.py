from datetime import UTC, datetime, timedelta

from gmail_automator.container import build_container
from tests.support.fakes import FakeClock


def test_container_builds_with_test_settings(
    settings, seeded_engine, fake_transport, fake_clock
) -> None:
    c = build_container(settings, engine=seeded_engine, transport=fake_transport, clock=fake_clock)
    assert c.settings is settings
    assert c.cipher.decrypt(c.cipher.encrypt("x")) == "x"
    assert c.clock.now() == datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def test_fake_clock_advances() -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(timedelta(hours=25))
    assert clock.now() == datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
