from typing import NoReturn

import httpx
import pytest

from concerts import signals


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call outside services: use httpx.MockTransport and the recorded responses in tests/fixtures/."""

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("Network access in tests: use httpx.MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture(autouse=True)
def no_bands_waiting() -> None:
    """Each test starts with no band waiting to be rebuilt. Most tests never run the jobs that follow a save,
    so the bands they leave would otherwise be built in a later test, where their ids may be other bands."""
    signals.pending.ids = set()
