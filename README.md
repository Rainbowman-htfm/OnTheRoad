# On the Road

A band enters its concert dates once, then publishes them everywhere: a public page, a widget for its own
website, calendar and RSS feeds for its fans, and schema.org `MusicEvent` data for search engines.

Free software, without data capture: no cookies on public pages, no analytics, no third-party scripts or fonts.
Light by design: public pages are static files, and the widget weighs a few kilobytes.

Demo: <https://ontheroad.alwaysdata.net>

## What a band gets

- a page at `/<band>/`, with each concert as schema.org `MusicEvent` data;
- a widget to put in an iframe on its website (light or dark theme, accent colour, number of dates);
- `concerts.ics` (calendar), `concerts.xml` (RSS) and `events.json` (schema.org), the open formats to follow
  the band or take its dates elsewhere;
- venues found on MusicBrainz and OpenStreetMap, so addresses and coordinates do not have to be typed.

Bands enter their dates in the Django admin, where each member sees only their own bands. They log in through
a single-use link sent by email; there are no passwords.

Public pages are static files, rebuilt whenever a band changes something, and every night: serving a visitor
needs no database query and no code running, so a small server is enough.

## Running it locally

Requires Python 3.12 or later.

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

The admin is at <http://localhost:8000/admin/>. Login links are printed in the terminal until email sending
is set up.

## Settings

Everything is read from environment variables, or from a `.env` file next to `manage.py` (never committed):

| Variable | Purpose |
|---|---|
| `SITE_URL` | Public address of the instance, e.g. `https://concerts.example.org` |
| `SECRET_KEY` | Django secret key; required outside localhost |
| `CONTACT_EMAIL` | Contact shown on the home page, and sent to MusicBrainz and OpenStreetMap with each request |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USER`, `EMAIL_PASSWORD` | SMTP server for login links; without it, emails are printed in the console |
| `SOURCE_URL` | Where the code this instance runs can be read (the AGPL asks for it); defaults to this repository |
| `SERVE_FILES` | `1` to let Django serve the public pages, when no web server does it |

Run `manage.py build` every night: it rebuilds all public pages, so "upcoming" stays accurate.

## Building over the data

`/events.json` lists every upcoming concert of every band, each with a permanent identifier, for tools built
over an instance. `integrations/bot.py` is one: Mastodon and Bluesky accounts announcing the new dates, one post
per band, and replying when a date is cancelled or moved. It only reads `/events.json`, so it can follow any
instance. Run it every 15 minutes for each network (`python integrations/bot.py mastodon`, then `bluesky`); its
settings are at the top of the file, and without a token or app password it only prints what it would post.

## Development

Before each commit:

```sh
.venv/bin/ruff format .
.venv/bin/ruff check .
.venv/bin/mypy concerts config integrations tests
.venv/bin/pytest
```

Tests never use the network: outside services are tested on recorded responses in `tests/fixtures/`.

## Licences

- Code: [GNU Affero General Public License v3.0](LICENSE) or any later version.
- Concert dates and band information: [CC0](https://creativecommons.org/publicdomain/zero/1.0/).
- Venues imported from OpenStreetMap: © OpenStreetMap contributors, under the
  [Open Database License](https://opendatacommons.org/licenses/odbl/).
