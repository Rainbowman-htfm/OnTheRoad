"""The Mastodon bot, from the site's /events.json to its posts; Mastodon is faked with httpx.MockTransport."""

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from integrations import mastodon_bot

NOW = datetime.fromisoformat("2026-11-01T10:00:00+01:00")


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch, tmp_path):
    """The bot reads the .env next to manage.py: not the real one in tests."""
    monkeypatch.setattr(mastodon_bot, "ROOT", tmp_path)


def concert(number: int, band: str = "MADAM", start: str = "2026-11-14T20:30:00+01:00", **event: Any):
    """One entry of /events.json, as the site writes it, with what the bot reads."""
    return {
        "band": {"name": band, "page": f"https://dates.example/{band.lower()}/", "claimed": band != "MADAM"},
        "event": {
            "identifier": f"concert-{number}@dates.example",
            "startDate": start,
            "eventStatus": "https://schema.org/EventScheduled",
            "location": {"name": "Le Bouillon", "address": {"addressLocality": "Orléans"}},
        }
        | event,
    }


class Fake:
    """Records the posts instead of sending them, and the state saved after each one."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str | None]] = []
        self.state: dict[str, Any] | None = None

    def post(self, text: str, reply_to: str | None) -> str:
        self.posts.append((text, reply_to))
        return f"post-{len(self.posts)}"

    def save(self, state: dict[str, Any]) -> None:
        self.state = json.loads(json.dumps(state))  # a copy, as written to the file

    def run(self, events: list[dict[str, Any]], now: datetime = NOW) -> list[tuple[str, str | None]]:
        before = len(self.posts)
        mastodon_bot.run(events, self.state, now, self.post, self.save)
        return self.posts[before:]


def test_first_run_announces_nothing_already_on_the_site():
    bot = Fake()
    assert bot.run([concert(1), concert(2)]) == []
    [(text, reply_to)] = bot.run([concert(1), concert(2), concert(3, start="2026-11-15")])
    assert text == (
        "MADAM: 1 new date\n\nSun 15 Nov – Le Bouillon, Orléans\n\nhttps://dates.example/madam/\n"
        "Listed from the band's public announcements, not by the band."
    )
    assert reply_to is None


def test_new_dates_one_post_per_band():
    bot = Fake()
    bot.run([])
    [(madam, _), (splenius, _)] = bot.run(
        [concert(1), concert(2, band="Splenius", superEvent={"name": "Hellfest"}), concert(3, start="2026-12-05")]
    )
    assert madam.startswith("MADAM: 2 new dates\n\nSat 14 Nov, 20:30 – Le Bouillon, Orléans\nSat 5 Dec – ")
    assert "Hellfest, Orléans" in splenius  # the event's name, as on the site
    assert "not by the band" not in splenius  # Splenius manages its page
    assert bot.run([concert(1), concert(2, band="Splenius"), concert(3, start="2026-12-05")]) == []  # once


def test_many_dates_fit_in_one_post():
    bot = Fake()
    bot.run([])
    [(text, _)] = bot.run([concert(number, start=f"2027-01-{number:02}") for number in range(1, 31)])
    assert len(text) <= mastodon_bot.MAX_LENGTH
    assert "…and " in text and text.count("Le Bouillon") < 30
    assert "https://dates.example/madam/" in text  # where to see them all


def test_at_most_five_posts_an_hour_the_rest_waits():
    bot = Fake()
    bot.run([])
    events = [concert(number, band=f"Band {number}") for number in range(7)]
    assert len(bot.run(events)) == 5
    assert bot.run(events, NOW + timedelta(minutes=15)) == []
    assert len(bot.run(events, NOW + timedelta(minutes=61))) == 2  # the two left, announced once each
    assert bot.run(events, NOW + timedelta(hours=2)) == []


def test_cancelled_or_moved_replies_to_the_announcement():
    bot = Fake()
    bot.run([])
    bot.run([concert(1), concert(2)])
    cancelled = concert(1, eventStatus="https://schema.org/EventCancelled")
    moved = concert(2, start="2026-11-21T20:30:00+01:00", eventStatus="https://schema.org/EventRescheduled")
    assert bot.run([cancelled, moved]) == [
        ("Cancelled: Sat 14 Nov, 20:30 – Le Bouillon, Orléans", "post-1"),
        ("New date: Sat 21 Nov, 20:30 – Le Bouillon, Orléans", "post-1"),
    ]
    assert bot.run([cancelled, moved]) == []  # said once


def test_time_announced_later_is_not_a_new_date():
    bot = Fake()
    bot.run([])
    bot.run([concert(1, start="2026-11-14")])  # time not announced yet
    assert bot.run([concert(1, start="2026-11-14T20:30:00+01:00")]) == []
    assert bot.run([concert(1, start="2026-11-14T21:00:00+01:00")]) == []  # corrected, same day


def test_dates_known_before_the_bot_or_cancelled_from_the_start_are_not_news():
    bot = Fake()
    bot.run([concert(1)])  # already on the site: no post to reply to
    assert bot.run([concert(1, eventStatus="https://schema.org/EventCancelled")]) == []
    assert bot.run([concert(1), concert(2, eventStatus="https://schema.org/EventCancelled")]) == []


def test_concerts_gone_from_the_file_are_forgotten():
    bot = Fake()
    bot.run([concert(1)])
    bot.run([])
    assert bot.state == {"concerts": {}, "posted": []}


def test_main_reads_the_site_and_posts_to_mastodon(monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"concerts": {}, "posted": []}))
    for name, value in {
        "BOT_SITE_URL": "https://dates.example/",
        "MASTODON_URL": "https://mastodon.example",
        "MASTODON_TOKEN": "secret",
        "BOT_STATE_FILE": str(state_file),
    }.items():
        monkeypatch.setenv(name, value)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "dates.example":
            return httpx.Response(200, json=[concert(1)])
        return httpx.Response(200, json={"id": "110"})

    mastodon_bot.main(httpx.MockTransport(handler))
    site, status = requests
    assert str(site.url) == "https://dates.example/events.json"
    assert "Authorization" not in site.headers  # the token only goes to Mastodon
    assert str(status.url) == "https://mastodon.example/api/v1/statuses"
    assert status.headers["Authorization"] == "Bearer secret"
    assert status.headers["Idempotency-Key"]
    form = parse_qs(status.content.decode())
    assert form["visibility"] == ["unlisted"] and form["status"][0].startswith("MADAM: 1 new date")
    assert json.loads(state_file.read_text())["concerts"]["concert-1@dates.example"]["post"] == "110"


def test_without_a_token_it_only_shows_the_posts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BOT_SITE_URL", "https://dates.example")
    monkeypatch.setenv("BOT_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("MASTODON_TOKEN", raising=False)
    mastodon_bot.main(httpx.MockTransport(lambda request: httpx.Response(200, json=[concert(1)])))
    assert "MADAM: 1 new date" in capsys.readouterr().out
    assert not (tmp_path / "state.json").exists()
