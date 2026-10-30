"""The bot, from the site's /events.json to its posts; Mastodon and Bluesky are faked with httpx.MockTransport."""

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from integrations import bot as site_bot

NOW = datetime.fromisoformat("2026-11-01T10:00:00+01:00")


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch, tmp_path):
    """The bot reads the .env next to manage.py: not the real one in tests."""
    monkeypatch.setattr(site_bot, "ROOT", tmp_path)


def concert(
    number: int,
    band: str = "MADAM",
    start: str = "2026-11-14T20:30:00+01:00",
    announced_at: str = "2026-10-01T10:00:00+02:00",
    **event: Any,
):
    """One entry of /events.json, as the site writes it, with what the bot reads."""
    return {
        "band": {"name": band, "page": f"https://dates.example/{band.lower()}/", "claimed": band != "MADAM"},
        "announced_at": announced_at,
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

    def run(
        self, events: list[dict[str, Any]], now: datetime = NOW, modified: str = ""
    ) -> list[tuple[str, str | None]]:
        before = len(self.posts)
        site_bot.run(events, self.state, now, self.post, self.save, modified)
        return self.posts[before:]


def test_first_run_announces_nothing_already_on_the_site():
    bot = Fake()
    assert bot.run([concert(1), concert(2)]) == []
    [(text, reply_to)] = bot.run([concert(1), concert(2), concert(3, start="2026-11-15")])
    assert text == (
        "MADAM: 1 new date\n\nSun 15 Nov – Le Bouillon, Orléans\n\nhttps://dates.example/madam/\n"
        "Listed from the band's public announcements, not by the band.\n\n#concert #MADAM #Orleans"
    )
    assert reply_to is None


def test_new_dates_wait_an_hour_to_be_corrected():
    bot = Fake()
    bot.run([])
    entered = concert(1, announced_at="2026-11-01T09:30:00+01:00")  # half an hour before NOW
    assert bot.run([entered]) == []
    fixed = concert(1, start="2026-11-15T20:30:00+01:00", announced_at="2026-11-01T09:30:00+01:00")
    [(text, _)] = bot.run([fixed], NOW + timedelta(minutes=30))
    assert "Sun 15 Nov, 20:30" in text  # the corrected date, posted once


def test_hashtags_one_per_city_without_accents_or_dashes():
    assert site_bot.hashtag("Saint-Pierre-lès-Elbeuf") == "#SaintPierreLesElbeuf"
    assert site_bot.hashtag("L'Isle-d'Abeau") == "#LIsleDAbeau"
    assert site_bot.hashtag("Grandma’s Ashes") == site_bot.hashtag("Grandma's Ashes") == "#GrandmasAshes"
    assert site_bot.hashtag("1349") == ""  # digits alone are not a hashtag
    bot = Fake()
    bot.run([])
    paris = concert(2, location={"name": "Glazart", "address": {"addressLocality": "Paris"}})
    [(text, _)] = bot.run([concert(1), paris, concert(3, start="2026-12-01")])
    assert text.endswith("\n\n#concert #MADAM #Orleans #Paris")


def test_genres_as_hashtags_after_the_band():
    bot = Fake()
    bot.run([])
    entry = concert(1)
    entry["band"]["genres"] = ["Metal", "Post-rock"]
    [(text, _)] = bot.run([entry])
    assert text.endswith("\n\n#concert #MADAM #Metal #PostRock #Orleans")


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
    assert len(text) <= site_bot.MAX_LENGTH["mastodon"]
    assert "…and " in text and text.count("Le Bouillon") < 30
    assert "https://dates.example/madam/" in text  # where to see them all


def test_at_most_five_posts_an_hour_the_rest_waits():
    bot = Fake()
    bot.run([], modified="Sat, 31 Oct 2026 08:00:00 GMT")
    events = [concert(number, band=f"Band {number}") for number in range(7)]
    assert len(bot.run(events, modified="Sun, 01 Nov 2026 08:00:00 GMT")) == 5
    assert bot.state and bot.state["modified"] == "Sat, 31 Oct 2026 08:00:00 GMT"  # the file is needed again
    assert bot.run(events, NOW + timedelta(minutes=15)) == []
    assert len(bot.run(events, NOW + timedelta(minutes=61), "Sun, 01 Nov 2026 08:00:00 GMT")) == 2  # announced once
    assert bot.state["modified"] == "Sun, 01 Nov 2026 08:00:00 GMT"  # all posted
    assert bot.run(events, NOW + timedelta(hours=2)) == []


def test_cancelled_or_moved_replies_to_the_announcement():
    bot = Fake()
    bot.run([])
    bot.run([concert(1), concert(2)])
    cancelled = concert(1, eventStatus="https://schema.org/EventCancelled")
    moved = concert(2, start="2026-11-21T20:30:00+01:00", eventStatus="https://schema.org/EventRescheduled")
    assert bot.run([cancelled, moved]) == [
        ("Cancelled: MADAM · Sat 14 Nov, 20:30 – Le Bouillon, Orléans", "post-1"),
        ("New date: MADAM · Sat 21 Nov, 20:30 – Le Bouillon, Orléans", "post-1"),
    ]
    assert bot.run([cancelled, moved]) == []  # said once
    assert bot.run([concert(1), moved]) == [("Back on: MADAM · Sat 14 Nov, 20:30 – Le Bouillon, Orléans", "post-1")]
    assert bot.run([cancelled, moved]) == [("Cancelled: MADAM · Sat 14 Nov, 20:30 – Le Bouillon, Orléans", "post-1")]


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
    assert bot.state == {"concerts": {}, "posted": [], "modified": ""}


def test_main_reads_the_site_and_posts_to_mastodon(monkeypatch, tmp_path):
    state_file = tmp_path / "mastodon_bot.json"
    state_file.write_text(json.dumps({"concerts": {}, "posted": []}))
    mastodon_settings(monkeypatch)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "dates.example":
            return httpx.Response(200, json=[concert(1)], headers={"Last-Modified": "Sun, 01 Nov 2026 08:00:00 GMT"})
        return httpx.Response(200, json={"id": "110"})

    site_bot.main("mastodon", httpx.MockTransport(handler))
    site, status = requests
    assert str(site.url) == "https://dates.example/events.json"
    assert "Authorization" not in site.headers  # the token only goes to Mastodon
    assert str(status.url) == "https://mastodon.example/api/v1/statuses"
    assert status.headers["Authorization"] == "Bearer secret"
    assert status.headers["Idempotency-Key"]
    form = parse_qs(status.content.decode())
    assert form["visibility"] == ["unlisted"] and form["status"][0].startswith("MADAM: 1 new date")
    assert "If-Modified-Since" not in site.headers  # no date saved yet
    state = json.loads(state_file.read_text())
    assert state["concerts"]["concert-1@dates.example"]["post"] == "110"
    assert state["modified"] == "Sun, 01 Nov 2026 08:00:00 GMT"


def mastodon_settings(monkeypatch):
    for name, value in {
        "BOT_SITE_URL": "https://dates.example/",
        "MASTODON_URL": "https://mastodon.example",
        "MASTODON_TOKEN": "secret",
    }.items():
        monkeypatch.setenv(name, value)


def test_file_not_downloaded_again_until_it_changes(monkeypatch, tmp_path):
    state_file = tmp_path / "mastodon_bot.json"
    state_file.write_text(json.dumps({"concerts": {}, "posted": [], "modified": "Sun, 01 Nov 2026 08:00:00 GMT"}))
    mastodon_settings(monkeypatch)
    asked: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.headers.get("If-Modified-Since"))
        return httpx.Response(304)

    site_bot.main("mastodon", httpx.MockTransport(handler))
    assert asked == ["Sun, 01 Nov 2026 08:00:00 GMT"]  # and nothing posted


def test_announcement_deleted_by_hand_the_reply_is_posted_alone(monkeypatch, tmp_path):
    state_file = tmp_path / "mastodon_bot.json"
    known = {"concert-1@dates.example": {"start": "2026-11-14T20:30:00+01:00", "cancelled": False, "post": "109"}}
    state_file.write_text(json.dumps({"concerts": known, "posted": []}))
    mastodon_settings(monkeypatch)
    replies: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "dates.example":
            return httpx.Response(200, json=[concert(1, eventStatus="https://schema.org/EventCancelled")])
        replies.append(parse_qs(request.content.decode()).get("in_reply_to_id", []))
        return httpx.Response(404) if replies[-1] else httpx.Response(200, json={"id": "110"})

    site_bot.main("mastodon", httpx.MockTransport(handler))
    assert replies == [["109"], []]
    assert json.loads(state_file.read_text())["concerts"]["concert-1@dates.example"]["cancelled"]


def test_without_a_token_it_only_shows_the_posts(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BOT_SITE_URL", "https://dates.example")
    monkeypatch.delenv("MASTODON_TOKEN", raising=False)
    site_bot.main("mastodon", httpx.MockTransport(lambda request: httpx.Response(200, json=[concert(1)])))
    assert "MADAM: 1 new date" in capsys.readouterr().out
    assert not (tmp_path / "mastodon_bot.json").exists()


def test_bluesky_links_and_hashtags_are_marked_in_bytes():
    text = "Orléans\nhttps://dates.example/madam/\n\n#concert #Orleans"
    link, concert_tag, city = site_bot.facets(text)
    start = len("Orléans\n".encode())  # "é" is two bytes
    assert link["index"] == {"byteStart": start, "byteEnd": start + len("https://dates.example/madam/")}
    assert link["features"] == [{"$type": "app.bsky.richtext.facet#link", "uri": "https://dates.example/madam/"}]
    assert concert_tag["features"] == [{"$type": "app.bsky.richtext.facet#tag", "tag": "concert"}]
    assert text.encode()[city["index"]["byteStart"] : city["index"]["byteEnd"]] == b"#Orleans"


def test_main_posts_to_bluesky_and_replies_in_the_thread(monkeypatch, tmp_path):
    for name, value in {
        "BOT_SITE_URL": "https://dates.example/",
        "BLUESKY_HANDLE": "bot.example",
        "BLUESKY_APP_PASSWORD": "app-secret",
    }.items():
        monkeypatch.setenv(name, value)
    (tmp_path / "bluesky_bot.json").write_text(json.dumps({"concerts": {}, "posted": []}))
    events = [concert(number, band="Splenius", start=f"2027-01-{number:02}") for number in range(1, 11)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "dates.example":
            return httpx.Response(200, json=events)
        if request.url.path.endswith("createSession"):
            return httpx.Response(200, json={"accessJwt": "jwt", "did": "did:plc:bot"})
        return httpx.Response(200, json={"uri": f"at://did:plc:bot/app.bsky.feed.post/{len(requests)}", "cid": "cid"})

    site_bot.main("bluesky", httpx.MockTransport(handler))
    _, session, created = requests
    assert str(session.url) == "https://bsky.social/xrpc/com.atproto.server.createSession"
    assert json.loads(session.content) == {"identifier": "bot.example", "password": "app-secret"}
    assert created.headers["Authorization"] == "Bearer jwt"
    record = json.loads(created.content)["record"]
    assert record["text"].startswith("Splenius: 10 new dates") and "…and " in record["text"]
    assert len(record["text"]) <= site_bot.MAX_LENGTH["bluesky"]
    assert [facet["features"][0]["$type"][-4:] for facet in record["facets"]] == ["link", "#tag", "#tag", "#tag"]
    assert record["langs"] == ["en"] and "reply" not in record

    events[0] = concert(1, band="Splenius", start="2027-01-01", eventStatus="https://schema.org/EventCancelled")
    requests.clear()
    site_bot.main("bluesky", httpx.MockTransport(handler))
    _, _, reply = requests  # signed in again: a new run
    announced = {"uri": "at://did:plc:bot/app.bsky.feed.post/3", "cid": "cid"}
    assert json.loads(reply.content)["record"]["reply"] == {"root": announced, "parent": announced}
