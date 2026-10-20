from typing import NoReturn

import httpx
import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never call outside services: use httpx.MockTransport and the recorded responses in tests/fixtures/."""

    def refuse(*args: object, **kwargs: object) -> NoReturn:
        raise RuntimeError("Network access in tests: use httpx.MockTransport")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
