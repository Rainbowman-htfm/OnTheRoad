"""Login through a link sent by email: single use, time-limited, never telling who has an account."""

from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest
from django.contrib.auth.tokens import default_token_generator
from django.core import mail
from django.core.exceptions import ValidationError

from concerts.models import Account, Band

pytestmark = pytest.mark.django_db


@pytest.fixture
def member():
    return Account.objects.create_user("member@example.org", is_staff=True)


def ask(client, email: str) -> str:
    return client.post("/login/", {"email": email}).content.decode()


def link_in_email() -> str:
    """The link's path, for the test client, which does not take full addresses."""
    [message] = mail.outbox
    return urlparse(next(word for word in str(message.body).split() if "/login/" in word)).path


def test_link_sent_to_a_member(client, member, settings):
    settings.CONTACT_EMAIL = "contact@example.org"
    ask(client, "Member@Example.org")  # case does not matter
    [message] = mail.outbox
    assert message.to == ["member@example.org"] and message.reply_to == ["contact@example.org"]
    assert "/login/" in message.body
    assert "/guide.html" in message.body


def test_same_answer_whether_the_address_has_an_account_or_not(client, member):
    unknown = ask(client, "nobody@example.org")
    assert not mail.outbox
    known = ask(client, "member@example.org")
    assert known == unknown


@pytest.mark.parametrize("fields", [{"is_active": False}, {"is_staff": False}])
def test_no_link_for_inactive_or_non_staff_accounts(client, fields):
    Account.objects.create_user("someone@example.org", **{"is_staff": True, **fields})
    ask(client, "someone@example.org")
    assert not mail.outbox


def test_opening_the_link_does_not_log_in_the_button_does(client, member):
    ask(client, "member@example.org")
    link = link_in_email()
    page = client.get(link)
    assert page.status_code == 200 and b"member@example.org" in page.content
    assert "_auth_user_id" not in client.session  # a mail scanner opening the link logs nobody in
    response = client.post(link)
    assert response.url == "/admin/" and client.session["_auth_user_id"] == str(member.pk)


def test_link_works_only_once(client, member):
    ask(client, "member@example.org")
    link = link_in_email()
    client.post(link)
    client.logout()
    assert client.post(link).status_code == 400
    assert "_auth_user_id" not in client.session


def test_link_expires_after_30_minutes(client, member, monkeypatch):
    ask(client, "member@example.org")
    link = link_in_email()
    later = datetime.now() + timedelta(minutes=31)
    monkeypatch.setattr(default_token_generator, "_now", lambda: later)
    assert client.post(link).status_code == 400


def test_made_up_links_are_refused(client, member):
    assert client.get("/login/xyz/abc-def/").status_code == 400


def test_admin_login_page_points_to_email_login(client):
    assert b'href="/login/"' in client.get("/admin/login/").content


def test_bands_cannot_take_the_site_own_addresses():
    with pytest.raises(ValidationError):
        Band(name="Login", slug="login").full_clean()
