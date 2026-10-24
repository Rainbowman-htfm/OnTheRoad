"""Login through a link sent by email, without a password.

The link carries a code from Django's password-reset generator: it is signed with SECRET_KEY, expires after
PASSWORD_RESET_TIMEOUT, and depends on the account's last login, so it stops working once used.
Opening the link only shows a "Log in" button: mail scanners that open links by themselves (to check them)
would otherwise use it up before the person does."""

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMessage
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from .models import Account


def ask_link(request: HttpRequest) -> HttpResponse:
    """Ask for an email address and send a login link to it, if it belongs to an account allowed in."""
    if request.user.is_authenticated:
        return redirect("admin:index")
    sent = False
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        account = Account.objects.filter(email__iexact=email, is_active=True, is_staff=True).first()
        if account:
            send_link(account)
        # ponytail: no limit on requests; someone could fill a member's inbox with links. Add one per address
        # and per hour if it happens
        sent = True  # the same answer either way: the page never tells who has an account
    return render(request, "concerts/login.html", {"sent": sent})


def send_link(account: Account) -> None:
    uid = urlsafe_base64_encode(force_bytes(account.pk))
    token = default_token_generator.make_token(account)
    link = settings.SITE_URL.rstrip("/") + reverse("login_link", args=[uid, token])
    minutes = settings.PASSWORD_RESET_TIMEOUT // 60
    EmailMessage(
        subject="Your login link",
        body=f"To log in, open this link within {minutes} minutes:\n\n{link}\n\n"
        "It works only once. If you did not ask for it, ignore this email.\n\n"
        f"First time? The guide for bands: {settings.SITE_URL.rstrip('/')}/guide.html",
        to=[account.email],
        reply_to=[settings.CONTACT_EMAIL] if settings.CONTACT_EMAIL else [],
    ).send()


def use_link(request: HttpRequest, uid: str, token: str) -> HttpResponse:
    """GET: check the link and show the "Log in" button. POST (the button): log in."""
    try:
        account = Account.objects.get(pk=force_str(urlsafe_base64_decode(uid)), is_active=True, is_staff=True)
    except (ValueError, Account.DoesNotExist):
        account = None
    if account is None or not default_token_generator.check_token(account, token):
        return render(request, "concerts/login_link.html", {"valid": False}, status=400)
    if request.method == "POST":
        # Updates the last login, which makes this link invalid from now on
        login(request, account, backend="django.contrib.auth.backends.ModelBackend")
        return redirect("admin:index")
    return render(request, "concerts/login_link.html", {"valid": True, "email": account.email})
