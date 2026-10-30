"""Mastodon and Bluesky bot for an On the Road site: announces the new concert dates an hour after they are
entered, one post per band with hashtags for its genres and the cities, and replies to its post when a date is
cancelled or moved. Like any tool built over the open data, it only reads the site's public /events.json, and
remembers what it posted in a small JSON file of its own, one per network.

Run it every 15 minutes, once per network:  python integrations/bot.py mastodon
                                            python integrations/bot.py bluesky
A network down does not hold up the other. Once a week, the weekend's concerts, one post per genre:
  python integrations/bot.py mastodon weekend   on Thursdays, hourly from 18:00 to 21:00
A busy weekend is then spread over the runs; posted only once, whatever the number of runs. Settings, from the environment or the .env file next to manage.py:
  BOT_SITE_URL          the site to announce, "https://ontheroad.alwaysdata.net"
  MASTODON_URL          the bot's instance, "https://mastodon.example"
  MASTODON_TOKEN        its application's token (write:statuses only)
  MASTODON_VISIBILITY   "unlisted" (default: followers and profile only) or "public" (also the local timeline)
  BLUESKY_HANDLE        the bot's account, "ontheroad.bsky.social"
  BLUESKY_APP_PASSWORD  an app password (Settings > Privacy and security > App passwords), never the account's
  BLUESKY_URL           its server, "https://bsky.social" by default
Without the token or the app password, posts are printed, not sent. What it posted: mastodon_bot.json and
bluesky_bot.json next to manage.py, and mastodon_weekend.json and bluesky_weekend.json for the weekend.

After moving the site to another domain, delete the state files: the concerts' identifiers contain the domain,
so every date would look new. The next run then starts over, without announcing anything.
"""

import hashlib
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent

# A polite pace for a bot, as instances often ask: at most 5 posts an hour and 50 a day; the rest waits for
# the next runs
PER_HOUR, PER_DAY = 5, 50
# Length limits: Mastodon's default, and Bluesky's in graphemes (what the eye sees as one character), never more
# than Python's len() counts
MAX_LENGTH = {"mastodon": 500, "bluesky": 300}
# A new date is announced an hour after it was entered, time to correct a typo; and a tour entered in several
# saves is then one post
WAIT = timedelta(hours=1)

Entry = dict[str, Any]  # one concert of /events.json
# {"concerts": {identifier: {"start", "cancelled", "post"}}, "posted": [times], "modified": the file's Last-Modified}
State = dict[str, Any]
Post = Callable[[str, str | None], str]  # (text, post replied to) → the new post's id


def when(event: dict[str, Any]) -> str:
    """ "Fri 14 Nov, 20:30", or "Fri 14 Nov" when the time is not announced (a date alone)."""
    start = event["startDate"]
    if "T" not in start:
        day = date.fromisoformat(start)
        return f"{day:%a} {day.day} {day:%b}"
    moment = datetime.fromisoformat(start)
    return f"{moment:%a} {moment.day} {moment:%b}, {moment:%H:%M}"


def place(entry: Entry) -> str:
    """ "Hellfest, Clisson": the event's name when it has one, as on the site."""
    event = entry["event"]
    name = event.get("superEvent", {}).get("name") or event["location"]["name"]
    return f"{name}, {event['location']['address']['addressLocality']}"


def line(entry: Entry) -> str:
    """ "Fri 14 Nov, 20:30 – Hellfest, Clisson"."""
    return f"{when(entry['event'])} – {place(entry)}"


def hashtag(name: str) -> str:
    """ "Saint-Pierre-lès-Elbeuf" → "#SaintPierreLesElbeuf", "Grandma's Ashes" → "#GrandmasAshes": a hashtag is
    letters and digits only, and without accents it matches the way most people type it. "" without a letter,
    "#1349" would not be a hashtag."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().replace("'", "")
    tag = "".join(word[:1].upper() + word[1:] for word in re.split(r"[^A-Za-z0-9]+", plain))
    return "#" + tag if re.search("[A-Za-z]", tag) else ""


def announcement(entries: list[Entry], max_length: int) -> str:
    """One band's new dates in one post; when they do not all fit, the first ones and the page for the rest.
    Hashtags last: on Mastodon, people find posts by following or searching them, there is no algorithm."""
    band = entries[0]["band"]
    title = f"{band['name']}: {len(entries)} new date{'s' if len(entries) > 1 else ''}"
    end = [band["page"]]
    if not band["claimed"]:
        end.append("Listed from the band's public announcements, not by the band.")

    def compose(shown: list[Entry], more: list[str]) -> str:
        cities = (entry["event"]["location"]["address"]["addressLocality"] for entry in shown)
        # The band, its genres (followed by fans of the style anywhere), then each city once, in date order;
        # band.get(): a site from before genres has none
        genres = [hashtag(genre) for genre in band.get("genres", [])]
        tags = dict.fromkeys(["#concert", hashtag(band["name"]), *genres, *(hashtag(city) for city in cities)])
        return "\n".join([title, "", *map(line, shown), *more, "", *end, "", " ".join(tag for tag in tags if tag)])

    shown: list[Entry] = []
    more: list[str] = []
    for count, entry in enumerate(entries):
        rest = len(entries) - count
        # Room kept for "…and N more", in case the next date does not fit
        if len(compose([*shown, entry], [f"…and {rest} more"] if rest > 1 else [])) > max_length:
            more = [f"…and {rest} more"]
            break
        shown.append(entry)
    return compose(shown, more)


def is_cancelled(entry: Entry) -> bool:
    return entry["event"]["eventStatus"].endswith("EventCancelled")


def run(
    events: list[Entry],
    saved: State | None,
    now: datetime,
    post: Post,
    save: Callable[[State], None],
    modified: str = "",
    max_length: int = MAX_LENGTH["mastodon"],
) -> None:
    """Post what changed since the last run, and save the state after each post: if the bot stops halfway,
    nothing is posted twice. modified: the file's Last-Modified, kept once everything in it is posted."""
    current = {entry["event"]["identifier"]: entry for entry in events}
    if saved is None:
        # First run: the dates already on the site are known, not announced, so the account does not open
        # with a flood of old dates
        concerts = {
            key: {"start": entry["event"]["startDate"], "cancelled": is_cancelled(entry)}
            for key, entry in current.items()
        }
        save({"concerts": concerts, "posted": [], "modified": modified})
        return
    state = saved
    known: dict[str, dict[str, Any]] = state["concerts"]
    state["posted"] = [time for time in state["posted"] if now - datetime.fromisoformat(time) < timedelta(days=1)]

    def may_post() -> bool:
        last_hour = [time for time in state["posted"] if now - datetime.fromisoformat(time) < timedelta(hours=1)]
        return len(last_hour) < PER_HOUR and len(state["posted"]) < PER_DAY

    def posted(text: str, reply_to: str | None = None) -> str:
        post_id = post(text, reply_to)
        state["posted"].append(now.isoformat())
        return post_id

    # Cancelled or moved, first: the news that matters most to people who saw the date. A reply to the post that
    # announced it, so the thread tells the story; a date announced before the bot started has no post to reply to
    for key, entry in current.items():
        before = known.get(key)
        if not (before and before.get("post")):
            continue
        cancelled = is_cancelled(entry)
        # The band's name too: the reply is posted alone when the announcement was deleted
        about = f"{entry['band']['name']} · {line(entry)}"
        if cancelled and not before["cancelled"]:
            text = f"Cancelled: {about}"
        elif before["cancelled"] and not cancelled:
            text = f"Back on: {about}"
        # Another day, as on the site: a time announced or corrected on the same day is not a new date
        elif not cancelled and entry["event"]["startDate"][:10] != before["start"][:10]:
            text = f"New date: {about}"
        else:
            continue
        if not may_post():
            return  # the next run goes on from here, with the file downloaded again
        posted(text, before["post"])
        before.update(start=entry["event"]["startDate"], cancelled=cancelled)
        save(state)

    # New dates, one post per band; one already cancelled is not news
    bands: dict[str, list[Entry]] = {}
    for key, entry in current.items():
        if key in known:
            continue
        if is_cancelled(entry):
            known[key] = {"start": entry["event"]["startDate"], "cancelled": True}
        elif now - datetime.fromisoformat(entry["announced_at"]) < WAIT:
            continue  # not known yet: the next runs download the file again until it is announced
        else:
            bands.setdefault(entry["band"]["page"], []).append(entry)
    for entries in bands.values():
        if not may_post():
            break  # not marked as known: announced by a next run, with the file downloaded again
        post_id = posted(announcement(entries, max_length))
        for entry in entries:
            known[entry["event"]["identifier"]] = {
                "start": entry["event"]["startDate"],
                "cancelled": False,
                "post": post_id,
            }
        save(state)

    if all(key in known for key in current):
        # Everything is posted: until the file changes, the next runs need not download it again
        state["modified"] = modified

    # Concerts gone from the file: past, or deleted because entered by mistake
    for key in [key for key in known if key not in current]:
        del known[key]
    save(state)


def weekend_posts(genre: str, entries: list[Entry], site: str, max_length: int) -> list[str]:
    """One genre's concerts of the weekend: one post, or a thread when they do not fit."""

    def compose(shown: list[Entry]) -> str:
        cities = (entry["event"]["location"]["address"]["addressLocality"] for entry in shown)
        tags = dict.fromkeys(["#concert", hashtag(genre), *(hashtag(city) for city in cities)])
        lines = (f"{when(entry['event'])} – {entry['band']['name']} at {place(entry)}" for entry in shown)
        title = f"{genre or 'Concerts'} this weekend"
        return "\n".join([title, "", *lines, "", site + "/", "", " ".join(tag for tag in tags if tag)])

    posts: list[str] = []
    shown: list[Entry] = []
    for entry in entries:
        if shown and len(compose([*shown, entry])) > max_length:
            posts.append(compose(shown))
            shown = []
        shown.append(entry)
    return [*posts, compose(shown)]


def weekend(
    events: list[Entry],
    saved: State | None,
    today: date,
    post: Post,
    save: Callable[[State], None],
    site: str,
    max_length: int = MAX_LENGTH["mastodon"],
) -> None:
    """Thursday's posts: the concerts from Friday to Sunday, one per genre, followed by the fans of the style.
    A band in two genres is in both; a band without genre in "Concerts this weekend". At most PER_HOUR posts a
    run, a genre's thread never cut: the next run goes on, and the state remembers the genres done."""
    friday = today + timedelta(days=4 - today.weekday())  # this week's
    if not saved or saved["weekend"] != friday.isoformat():
        saved = {"weekend": friday.isoformat(), "done": []}
    state = saved
    days = {(friday + timedelta(days=n)).isoformat() for n in range(3)}
    genres: dict[str, list[Entry]] = {}
    for entry in sorted(events, key=lambda entry: entry["event"]["startDate"]):
        if entry["event"]["startDate"][:10] in days and not is_cancelled(entry):
            for genre in entry["band"].get("genres") or [""]:
                genres.setdefault(genre, []).append(entry)
    left = PER_HOUR
    for genre, entries in genres.items():
        if genre in state["done"]:
            continue
        thread = weekend_posts(genre, entries, site, max_length)
        # Wait for the next run when this thread does not fit in what is left of the hour, unless nothing was
        # posted yet: a thread longer than PER_HOUR must go out some time
        if len(thread) > left and left < PER_HOUR:
            break
        # The rest of the thread replies to its first post
        first = post(thread[0], None)
        for text in thread[1:]:
            post(text, first)
        left -= len(thread)
        state["done"].append(genre)
        save(state)
    save(state)


def mastodon_post(client: httpx.Client) -> Post:
    url = os.environ["MASTODON_URL"].rstrip("/") + "/api/v1/statuses"
    token = os.environ["MASTODON_TOKEN"]
    visibility = os.environ.get("MASTODON_VISIBILITY", "unlisted")

    def post(text: str, reply_to: str | None) -> str:
        response = client.post(
            url,
            data={"status": text, "visibility": visibility, "language": "en"}
            | ({"in_reply_to_id": reply_to} if reply_to else {}),
            headers={
                "Authorization": f"Bearer {token}",
                # Mastodon ignores a post sent again with the same key within an hour (a retry after a timeout)
                "Idempotency-Key": hashlib.sha256(f"{reply_to}\n{text}".encode()).hexdigest(),
            },
        )
        if response.status_code == 404 and reply_to:
            # The announcement was deleted by hand: posted alone rather than blocking every next run
            return post(text, None)
        response.raise_for_status()
        return response.json()["id"]

    return post


def facets(text: str) -> list[dict[str, Any]]:
    """Bluesky shows links and hashtags as plain text unless the post says where they are, in bytes of UTF-8."""
    found = []
    for match in re.finditer(r"https?://\S+|(?<!\S)#\w+", text):
        piece = match.group()
        start = len(text[: match.start()].encode())
        feature = (
            {"$type": "app.bsky.richtext.facet#tag", "tag": piece[1:]}
            if piece.startswith("#")
            else {"$type": "app.bsky.richtext.facet#link", "uri": piece}
        )
        found.append({"index": {"byteStart": start, "byteEnd": start + len(piece.encode())}, "features": [feature]})
    return found


def bluesky_post(client: httpx.Client) -> Post:
    server = os.environ.get("BLUESKY_URL", "https://bsky.social").rstrip("/") + "/xrpc/"
    session: dict[str, str] = {}

    def post(text: str, reply_to: str | None) -> str:
        if not session:
            # Signed in at the first post only: most runs post nothing, and Bluesky limits sign-ins
            response = client.post(
                server + "com.atproto.server.createSession",
                json={"identifier": os.environ["BLUESKY_HANDLE"], "password": os.environ["BLUESKY_APP_PASSWORD"]},
            )
            response.raise_for_status()
            session.update(response.json())
        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "facets": facets(text),
            "langs": ["en"],
            "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if reply_to:
            # A reply names its thread's first post and the post it answers: here the same, the announcement
            uri, cid = reply_to.split()
            record["reply"] = {"root": {"uri": uri, "cid": cid}, "parent": {"uri": uri, "cid": cid}}
        response = client.post(
            server + "com.atproto.repo.createRecord",
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record},
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
        )
        response.raise_for_status()
        created = response.json()
        # Both are needed to reply: the post's address, and its content's fingerprint
        return f"{created['uri']} {created['cid']}"

    return post


POSTERS = {"mastodon": (mastodon_post, "MASTODON_TOKEN"), "bluesky": (bluesky_post, "BLUESKY_APP_PASSWORD")}


def read_env_file() -> None:
    # The same .env as the site; variables already set in the environment win
    env = ROOT / ".env"
    if env.exists():
        for row in env.read_text().splitlines():
            if "=" in row and not row.lstrip().startswith("#"):
                name, value = row.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip())


def main(network: str, transport: httpx.BaseTransport | None = None, weekend_post: bool = False) -> None:
    read_env_file()
    site = os.environ["BOT_SITE_URL"].rstrip("/")
    state_file = ROOT / f"{network}_{'weekend' if weekend_post else 'bot'}.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else None
    headers = {"User-Agent": f"OTR-{network}-bot/0.1 (+{site})"}
    poster, secret = POSTERS[network]
    max_length = MAX_LENGTH[network]
    with httpx.Client(headers=headers, transport=transport, timeout=30) as client:
        # "Not modified", without the file, when it has not changed since everything in it was posted
        since = {"If-Modified-Since": state["modified"]} if state and state.get("modified") else {}
        response = client.get(f"{site}/events.json", headers=since)
        if response.status_code == 304:
            return
        response.raise_for_status()
        events = response.json()
        modified = response.headers.get("Last-Modified", "")
        now = datetime.now().astimezone()
        sending = bool(os.environ.get(secret))

        # Trying it out, without the secret: show what would be posted, remember nothing
        def show(text: str, reply_to: str | None) -> str:
            print(f"--- {'reply to ' + reply_to if reply_to else 'post'}\n{text}\n")
            return "dry-run"

        def save(state: State) -> None:
            if sending:
                state_file.write_text(json.dumps(state))

        post = poster(client) if sending else show
        if not sending:
            # Without a state yet, as if every date were new, to see the posts
            state = state if weekend_post else state or {"concerts": {}, "posted": []}
            modified = ""
        if weekend_post:
            weekend(events, state, now.date(), post, save, site, max_length)
        else:
            run(events, state, now, post, save, modified, max_length)


if __name__ == "__main__":
    if sys.argv[1:] not in (["mastodon"], ["bluesky"], ["mastodon", "weekend"], ["bluesky", "weekend"]):
        sys.exit("Usage: python integrations/bot.py mastodon|bluesky [weekend]")
    main(sys.argv[1], weekend_post=sys.argv[2:] == ["weekend"])
