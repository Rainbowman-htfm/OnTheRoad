"""Mastodon bot for an On the Road site: announces the new concert dates, one post per band, and replies to its
post when a date is cancelled or moved. Like any tool built over the open data, it only reads the site's public
/events.json, and remembers what it posted in a small JSON file of its own.

Run it every 15 minutes:  python integrations/mastodon_bot.py
Settings, from the environment or the .env file next to manage.py:
  BOT_SITE_URL         the site to announce, "https://ontheroad.alwaysdata.net"
  MASTODON_URL         the bot's instance, "https://mastodon.example"
  MASTODON_TOKEN       its application's token (write:statuses only). Without it, posts are printed, not sent
  MASTODON_VISIBILITY  "unlisted" (default: followers and profile only) or "public" (also the local timeline)
  BOT_STATE_FILE       what it posted (default: mastodon_bot.json next to manage.py)

After moving the site to another domain, delete the state file: the concerts' identifiers contain the domain,
so every date would look new. The next run then starts over, without announcing anything.
"""

import hashlib
import json
import os
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent

# A polite pace for a bot, as instances often ask: at most 5 posts an hour and 50 a day; the rest waits for
# the next runs
PER_HOUR, PER_DAY = 5, 50
# Mastodon's default length limit
MAX_LENGTH = 500

Entry = dict[str, Any]  # one concert of /events.json
State = dict[str, Any]  # {"concerts": {identifier: {"start", "cancelled", "post"}}, "posted": [times]}
Post = Callable[[str, str | None], str]  # (text, post replied to) → the new post's id


def when(event: dict[str, Any]) -> str:
    """ "Fri 14 Nov, 20:30", or "Fri 14 Nov" when the time is not announced (a date alone)."""
    start = event["startDate"]
    if "T" not in start:
        day = date.fromisoformat(start)
        return f"{day:%a} {day.day} {day:%b}"
    moment = datetime.fromisoformat(start)
    return f"{moment:%a} {moment.day} {moment:%b}, {moment:%H:%M}"


def line(entry: Entry) -> str:
    """ "Fri 14 Nov, 20:30 – Hellfest, Clisson": the event's name when it has one, as on the site."""
    event = entry["event"]
    place = event.get("superEvent", {}).get("name") or event["location"]["name"]
    return f"{when(event)} – {place}, {event['location']['address']['addressLocality']}"


def announcement(entries: list[Entry]) -> str:
    """One band's new dates in one post; when they do not all fit, the first ones and the page for the rest."""
    band = entries[0]["band"]
    title = f"{band['name']}: {len(entries)} new date{'s' if len(entries) > 1 else ''}"
    end = [band["page"]]
    if not band["claimed"]:
        end.append("Listed from the band's public announcements, not by the band.")
    lines: list[str] = []
    for count, entry in enumerate(entries):
        rest = len(entries) - count
        more = [f"…and {rest} more"] if rest > 1 else []
        candidate = "\n".join([title, "", *lines, line(entry), *more, "", *end])
        if len(candidate) > MAX_LENGTH:
            lines.append(f"…and {rest} more")
            break
        lines.append(line(entry))
    return "\n".join([title, "", *lines, "", *end])


def is_cancelled(entry: Entry) -> bool:
    return entry["event"]["eventStatus"].endswith("EventCancelled")


def run(events: list[Entry], saved: State | None, now: datetime, post: Post, save: Callable[[State], None]) -> None:
    """Post what changed since the last run, and save the state after each post: if the bot stops halfway,
    nothing is posted twice."""
    current = {entry["event"]["identifier"]: entry for entry in events}
    if saved is None:
        # First run: the dates already on the site are known, not announced, so the account does not open
        # with a flood of old dates
        concerts = {
            key: {"start": entry["event"]["startDate"], "cancelled": is_cancelled(entry)}
            for key, entry in current.items()
        }
        save({"concerts": concerts, "posted": []})
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
        if is_cancelled(entry) and not before["cancelled"]:
            text = f"Cancelled: {line(entry)}"
        # Another day, as on the site: a time announced or corrected on the same day is not a new date
        elif not is_cancelled(entry) and entry["event"]["startDate"][:10] != before["start"][:10]:
            text = f"New date: {line(entry)}"
        else:
            continue
        if not may_post():
            return  # the next run goes on from here
        posted(text, before["post"])
        before.update(start=entry["event"]["startDate"], cancelled=is_cancelled(entry))
        save(state)

    # New dates, one post per band; one already cancelled is not news
    bands: dict[str, list[Entry]] = {}
    for key, entry in current.items():
        if key in known:
            continue
        if is_cancelled(entry):
            known[key] = {"start": entry["event"]["startDate"], "cancelled": True}
        else:
            bands.setdefault(entry["band"]["page"], []).append(entry)
    for entries in bands.values():
        if not may_post():
            break  # not marked as known: announced by a next run
        post_id = posted(announcement(entries))
        for entry in entries:
            known[entry["event"]["identifier"]] = {
                "start": entry["event"]["startDate"],
                "cancelled": False,
                "post": post_id,
            }
        save(state)

    # Concerts gone from the file: past, or deleted because entered by mistake
    for key in [key for key in known if key not in current]:
        del known[key]
    save(state)


def mastodon_post(client: httpx.Client, visibility: str) -> Post:
    def post(text: str, reply_to: str | None) -> str:
        response = client.post(
            "/api/v1/statuses",
            data={"status": text, "visibility": visibility, "language": "en"}
            | ({"in_reply_to_id": reply_to} if reply_to else {}),
            # Mastodon ignores a post sent again with the same key within an hour (a retry after a timeout)
            headers={"Idempotency-Key": hashlib.sha256(f"{reply_to}\n{text}".encode()).hexdigest()},
        )
        response.raise_for_status()
        return response.json()["id"]

    return post


def read_env_file() -> None:
    # The same .env as the site; variables already set in the environment win
    env = ROOT / ".env"
    if env.exists():
        for row in env.read_text().splitlines():
            if "=" in row and not row.lstrip().startswith("#"):
                name, value = row.split("=", 1)
                os.environ.setdefault(name.strip(), value.strip())


def main(transport: httpx.BaseTransport | None = None) -> None:
    read_env_file()
    site = os.environ["BOT_SITE_URL"].rstrip("/")
    state_file = Path(os.environ.get("BOT_STATE_FILE", ROOT / "mastodon_bot.json"))
    headers = {"User-Agent": f"OTR-mastodon-bot/0.1 (+{site})"}
    with httpx.Client(headers=headers, transport=transport, timeout=30) as http:
        response = http.get(f"{site}/events.json")
        response.raise_for_status()
        events = response.json()
    state = json.loads(state_file.read_text()) if state_file.exists() else None
    now = datetime.now().astimezone()
    token = os.environ.get("MASTODON_TOKEN", "")
    if not token:
        # Trying it out: show what would be posted, remember nothing. Without a state yet, as if every date
        # were new, to see the posts
        def show(text: str, reply_to: str | None) -> str:
            print(f"--- {'reply to ' + reply_to if reply_to else 'post'}\n{text}\n")
            return "dry-run"

        run(events, state or {"concerts": {}, "posted": []}, now, show, lambda state: None)
        return

    def save(state: State) -> None:
        state_file.write_text(json.dumps(state))

    with httpx.Client(
        base_url=os.environ["MASTODON_URL"],
        headers=headers | {"Authorization": f"Bearer {token}"},
        transport=transport,
        timeout=30,
    ) as mastodon:
        run(events, state, now, mastodon_post(mastodon, os.environ.get("MASTODON_VISIBILITY", "unlisted")), save)


if __name__ == "__main__":
    main()
