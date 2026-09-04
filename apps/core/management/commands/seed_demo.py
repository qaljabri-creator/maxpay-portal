"""Fill an empty local database with enough to click through the whole system.

Development tooling, not part of the product. It creates the accounts, the
merchant network, the rates and the demo client that spec §§7–9 need in order to
have anything to show, and — because a client cannot sign in without B2CORE —
it also stands up a local stand-in for B2CORE: an RSA key, the JWKS document a
real endpoint would publish, and one signed token.

**It refuses to run unless ``DEBUG`` is on.** It seeds known passwords and,
optionally, confirmed second factors with printed secrets. Both are fine on a
laptop and are exactly what an attacker would want anywhere else.

Re-running is safe: everything is looked up before it is created, so the command
converges rather than duplicating. To start over, delete the database and
migrate again — exchange rates and audit entries are append-only by design and
cannot be deleted, here or anywhere else.

    python manage.py seed_demo                # seed, and mint a portal token
    python manage.py seed_demo --token-only   # just a fresh token, when one expires
"""

import json
import secrets
import time
from datetime import time as clock_time
from decimal import Decimal
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice
from jwt.algorithms import OKPAlgorithm

from apps.accounts.models import Client, Role, User
from apps.accounts.permissions import sync_role_groups
from apps.core.models import SystemSettings
from apps.merchants.models import Merchant, MerchantMethod, PaymentMethod, Wallet
from apps.rates.models import ExchangeRate, RateType

DEFAULT_PASSWORD = "MaxPayDemo!2026"

#: Not cosmetic, and not ours to choose. ``two_factor.utils.default_device``
#: matches on ``device.name == "default"`` and nothing else, and the login
#: wizard offers its token step only when that lookup succeeds. A device under
#: any other name leaves the account *enrolled but never challenged*: the wizard
#: signs the user in on the password alone, ``EnforceTwoFactorMiddleware`` then
#: finds the session unverified, and bounces them back to the login form with no
#: message — because the middleware only explains itself to users who have not
#: enrolled at all. Silent login loop. The library's own setup wizard uses this
#: same name, which is why an account enrolled by hand never hits it.
DEVICE_NAME = "default"

#: Fixed so every developer's demo behaves identically. Hex, as
#: ``TOTPDevice.key`` stores it. Demo-only, and unreachable outside DEBUG.
TOTP_KEYS = {
    "admin@maxifyfx.com": "1111111111111111111111111111111111111111",
    "staff@maxifyfx.com": "2222222222222222222222222222222222222222",
    "merchant@maxifyfx.com": "3333333333333333333333333333333333333333",
}

#: The local stand-in for B2CORE, shaped like the real one.
#:
#: It used to sign RS256, mint an `aud`, and send a combined `name` — none of
#: which B2CORE does. A stand-in that models a different service than the one it
#: stands in for is worse than no stand-in: the whole verification path passed
#: locally for weeks while it would have refused every real token. So this now
#: signs EdDSA, carries no audience, splits the name in two, and uses an issuer
#: with a path and a trailing slash, because each of those was a separate way
#: the integration failed.
#:
#: The issuer is still invented — it is not MaxiFyFX's, and pointing local
#: development at a production issuer would be a way to end up trusting a
#: production token by accident. Its *shape* is what matters here.
DEV_ISSUER = "https://api.b2core.local/srvsz/auth/clients/v1/"
DEV_KID = "maxpay-dev-key-1"
DEV_ALGORITHM = "EdDSA"

CLIENT_SUBJECT = "b2core-demo-client-1"

#: Prints the six digits each seeded account's authenticator would be showing.
#: Useful when there is no phone to hand; ``unhexlify`` because ``key`` is hex.
TOTP_CODES_SNIPPET = (
    'python manage.py shell -c "'
    "from django_otp.plugins.otp_totp.models import TOTPDevice; "
    "from django_otp.oath import totp; from binascii import unhexlify; "
    "[print(d.user.email, str(totp(unhexlify(d.key), step=d.step, digits=d.digits))"
    '.zfill(d.digits)) for d in TOTPDevice.objects.all()]"'
)

#: The private key never leaves here, and ``devdata/`` is git-ignored. The JWKS
#: goes under ``static/`` so ``runserver`` serves it at a real HTTP URL —
#: PyJWT's JWKS client accepts http/https only, so a file path would not do.
KEY_PATH = Path("devdata") / "b2core-dev-key.pem"
JWKS_PATH = Path("static") / "dev" / "b2core-jwks.json"
TOKEN_PATH = Path("devdata") / "b2core-demo-token.txt"


class Command(BaseCommand):
    help = "Seed demo data for local browser testing. Refuses to run unless DEBUG."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            default=DEFAULT_PASSWORD,
            help=f"Password for every seeded internal account (default: {DEFAULT_PASSWORD}).",
        )
        parser.add_argument(
            "--no-2fa-devices",
            action="store_true",
            help="Skip pre-enrolling authenticators, so first login walks the real "
            "two-factor wizard instead.",
        )
        parser.add_argument(
            "--token-only",
            action="store_true",
            help="Mint a fresh portal token against the existing key and exit. "
            "Use when the last one expired.",
        )
        parser.add_argument(
            "--token-hours",
            type=int,
            default=12,
            help="How long the portal token stays valid (default: 12).",
        )
        parser.add_argument(
            "--host",
            default="http://127.0.0.1:8000",
            help="Origin the dev server will be reachable on (default: %(default)s). "
            "Only affects the printed settings and the JWKS URL.",
        )

    # -- entry point -------------------------------------------------------

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo refuses to run with DEBUG off. It creates accounts with "
                "known passwords and known second factors; that is only ever "
                "acceptable on a development machine."
            )

        self.base_dir = Path(settings.BASE_DIR)
        self.host = options["host"].rstrip("/")

        if options["token_only"]:
            client = Client.objects.filter(b2core_id=CLIENT_SUBJECT).first()
            if client is None:
                raise CommandError("No demo client yet. Run `seed_demo` without --token-only first.")
            token = self.mint_token(client, hours=options["token_hours"])
            self.report_portal(token, hours=options["token_hours"])
            return

        with transaction.atomic():
            sync_role_groups(strict=False)
            admin = self.make_user(options, Role.FINANCE_ADMIN, "admin@maxifyfx.com", "مدير المالية", superuser=True)
            self.make_user(options, Role.FINANCE_STAFF, "staff@maxifyfx.com", "موظف المالية")
            merchant_user = self.make_user(options, Role.MERCHANT, "merchant@maxifyfx.com", "تاجر بغداد")

            zaincash, fastpay = self.make_payment_methods()
            merchant = self.make_merchant(merchant_user, zaincash, fastpay, created_by=admin)
            self.make_rates(admin)
            self.make_business_hours()
            client = self.make_client()

        token = self.mint_token(client, hours=options["token_hours"])

        self.report_accounts(options)
        self.report_network(merchant)
        self.report_hours()
        self.report_portal(token, hours=options["token_hours"])

    # -- seeding -----------------------------------------------------------

    def make_user(self, options, role, email, full_name, *, superuser=False) -> User:
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            user = User.objects.create_user(
                email=email,
                password=options["password"],
                full_name=full_name,
                role=role,
                is_superuser=superuser,
            )
            self.note(f"created {role} {email}")
        else:
            # Re-running resets the password, so a forgotten demo password is
            # one command away rather than a database edit.
            user.set_password(options["password"])
            user.save(update_fields=["password"])
            self.note(f"reset password for {email}")

        if not options["no_2fa_devices"]:
            self.enrol_authenticator(user)
        return user

    def enrol_authenticator(self, user) -> None:
        """Pre-confirm a TOTP device so login only asks for the six digits.

        Spec §11 makes the second factor mandatory and nothing here relaxes
        that — the login flow still demands a valid code. What is skipped is
        the enrolment wizard, so the demo does not start with a QR scan.

        The device must be called ``default``; see :data:`DEVICE_NAME` for what
        happens when it is not.
        """
        key = TOTP_KEYS.get(user.email)
        if key is None:
            return
        # A device under any other name is a leftover from an earlier seed. It
        # would sit alongside the real one and keep the account in exactly the
        # state DEVICE_NAME describes, so re-running the command clears it.
        TOTPDevice.objects.filter(user=user).exclude(name=DEVICE_NAME).delete()
        device, created = TOTPDevice.objects.get_or_create(
            user=user, name=DEVICE_NAME, defaults={"key": key, "confirmed": True}
        )
        if not created and (device.key != key or not device.confirmed):
            device.key = key
            device.confirmed = True
            device.save(update_fields=["key", "confirmed"])

    def make_payment_methods(self):
        zaincash, _ = PaymentMethod.objects.get_or_create(
            code="zaincash",
            defaults={
                "caption_ar": "زين كاش",
                "caption_en": "ZainCash",
                "supports_deposit": True,
                "supports_withdrawal": True,
                "sort_order": 10,
            },
        )
        fastpay, _ = PaymentMethod.objects.get_or_create(
            code="fastpay",
            defaults={
                "caption_ar": "فاست باي",
                "caption_en": "FastPay",
                "supports_deposit": True,
                "supports_withdrawal": True,
                "sort_order": 20,
            },
        )
        self.note("payment methods: zaincash, fastpay")
        return zaincash, fastpay

    def make_merchant(self, user, *methods, created_by) -> Merchant:
        merchant, _ = Merchant.objects.get_or_create(
            name="تاجر بغداد",
            defaults={"user": user, "notes": "بيانات تجريبية — seed_demo"},
        )
        if merchant.user_id != user.pk:
            merchant.user = user
            merchant.save(update_fields=["user"])

        numbers = {"zaincash": "07700000001", "fastpay": "07800000002"}
        caps = {"zaincash": Decimal("5000000.00"), "fastpay": None}
        for method in methods:
            link, _ = MerchantMethod.objects.get_or_create(
                merchant=merchant, payment_method=method
            )
            if not link.wallets.filter(is_active=True).exists():
                Wallet.objects.create(
                    merchant_method=link,
                    number=numbers[method.code],
                    label=f"محفظة {method.caption_ar}",
                    daily_cap=caps[method.code],
                    created_by=created_by,
                )
        self.note("merchant 'تاجر بغداد' with one active wallet per method")
        return merchant

    def make_rates(self, admin) -> None:
        """Only if none is in force — ``ExchangeRate`` is append-only (spec §5)."""
        if ExchangeRate.current(RateType.DEPOSIT) is None:
            ExchangeRate.objects.create(
                rate_type=RateType.DEPOSIT,
                iqd_per_usd=Decimal("1480.00"),
                commission_iqd_per_100usd=Decimal("2500.00"),
                set_by=admin,
                note="بيانات تجريبية — seed_demo",
            )
            self.note("deposit rate 1480 IQD/USD, commission 2500 IQD per 100 USD")
        if ExchangeRate.current(RateType.WITHDRAWAL) is None:
            ExchangeRate.objects.create(
                rate_type=RateType.WITHDRAWAL,
                iqd_per_usd=Decimal("1450.00"),
                commission_iqd_per_100usd=Decimal("3000.00"),
                set_by=admin,
                note="بيانات تجريبية — seed_demo",
            )
            self.note("withdrawal rate 1450 IQD/USD, commission 3000 IQD per 100 USD")

    def make_business_hours(self) -> SystemSettings:
        """Open the desk round the clock (step 11).

        The shipped default is 09:00–21:00 Baghdad, which is right for
        production and wrong for a laptop: a developer who runs this at
        midnight would meet the closed notice and reasonably conclude the
        seeding had failed. Equal open and close times read as "never shuts",
        so this leaves the ordinary schedule path in force rather than pinning
        the manual override — the override is a thing to try from the panel,
        not a thing to arrive already thrown.
        """
        hours = SystemSettings.load()
        hours.open_time = clock_time(0, 0)
        hours.close_time = clock_time(0, 0)
        hours.is_open_override = None
        hours.save()
        self.note("business hours 00:00-00:00 (open around the clock)")
        return hours

    def make_client(self) -> Client:
        """The demo B2CORE client.

        Created here so Finance has something to look at before anyone signs in,
        but note that this row is not what authenticates anyone: the portal
        derives the client from the verified token's ``sub`` and would create
        this same record on first use (spec §4).
        """
        client, _ = Client.objects.get_or_create(
            b2core_id=CLIENT_SUBJECT,
            defaults={
                "display_name": "زينب الجبوري",
                "email": "zainab@example.com",
                "account_number": "MX-90210",
                "preferred_language": "ar",
            },
        )
        self.note(f"demo client {client.display_name} (sub={CLIENT_SUBJECT})")
        return client

    # -- the local stand-in for B2CORE ------------------------------------

    def signing_key(self):
        """Load the dev Ed25519 key, generating and saving it on first run.

        A key already on disk from before the switch to EdDSA is regenerated:
        it is an RSA key, and signing EdDSA with it raises rather than quietly
        producing something. Nothing is lost — the only thing it ever signed is
        a demo token.
        """
        path = self.base_dir / KEY_PATH
        if path.exists():
            existing = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if isinstance(existing, ed25519.Ed25519PrivateKey):
                return existing
            self.note("replacing the old RSA dev key; B2CORE signs EdDSA")

        key = ed25519.Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.note(f"generated a dev signing key at {KEY_PATH}")
        return key

    def write_jwks(self, key) -> None:
        """Publish the public half exactly as a real JWKS endpoint would.

        An `OKP`/`Ed25519` JWK, which is the shape B2CORE's endpoint serves and
        the shape PyJWK has to be able to read for any of this to work.
        """
        jwk = json.loads(OKPAlgorithm.to_jwk(key.public_key()))
        jwk.update({"kid": DEV_KID, "use": "sig", "alg": DEV_ALGORITHM})
        path = self.base_dir / JWKS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"keys": [jwk]}, indent=2), encoding="utf-8")

    def mint_token(self, client, *, hours: int) -> str:
        key = self.signing_key()
        self.write_jwks(key)

        now = int(time.time())
        # The claim set observed in a real B2CORE token, and nothing else. No
        # `aud`, no account number, no combined `name`, no `locale` — every one
        # of those was in the old fixture and none of them is real, which is
        # exactly how the integration came to be tested against a service that
        # does not exist.
        first, _, last = client.display_name.partition(" ")
        claims = {
            "sub": client.b2core_id,
            "iss": DEV_ISSUER,
            "iat": now,
            "nbf": now,
            "exp": now + hours * 3600,
            "email": client.email,
            "first_name": first,
            "last_name": last,
            "aal": "aal1",
            "amr": ["pwd"],
            "sid": secrets.token_hex(8),
            "jti": secrets.token_hex(16),
        }
        token = jwt.encode(claims, key, algorithm=DEV_ALGORITHM, headers={"kid": DEV_KID})

        path = self.base_dir / TOKEN_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        return token

    # -- output ------------------------------------------------------------
    #
    # A Windows console is often cp1252, and the data seeded here is Arabic.
    # Every line therefore goes through `out()`, which degrades an unencodable
    # character to "?" rather than letting a print statement abort a command
    # that has already written to the database.

    def out(self, message: str = "", style=None) -> None:
        encoding = getattr(self.stdout, "encoding", None) or "utf-8"
        try:
            message.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            message = message.encode(encoding, "replace").decode(encoding, "replace")
        self.stdout.write(style(message) if style else message)

    def note(self, message: str) -> None:
        self.out(f"  - {message}")

    def heading(self, title: str) -> None:
        self.out("")
        self.out(title, style=self.style.MIGRATE_HEADING)

    def report_accounts(self, options) -> None:
        self.heading("Accounts")
        rows = [
            ("finance_admin", "admin@maxifyfx.com", "/finance/"),
            ("finance_staff", "staff@maxifyfx.com", "/finance/"),
            ("merchant", "merchant@maxifyfx.com", "/merchant/"),
        ]
        for role, email, where in rows:
            self.out(f"  {role:<14} {email:<24} password: {options['password']}   -> {where}")

        if options["no_2fa_devices"]:
            self.out(
                "\n  No authenticators enrolled. First login walks the two-factor wizard\n"
                "  at /account/two_factor/setup/ - you will need an authenticator app."
            )
            return

        self.out("\n  Authenticators are pre-enrolled. Add the setup key below to your app,")
        self.out("  or print the codes that are valid right now with:\n")
        self.out(f"    {TOTP_CODES_SNIPPET}\n")
        for device in TOTPDevice.objects.filter(user__email__in=TOTP_KEYS).select_related("user"):
            self.out(f"    {device.user.email:<24} {device.config_url}")

    def report_network(self, merchant) -> None:
        self.heading("Merchant network")
        # English captions on purpose: the seeded data is Arabic, the terminal
        # printing it may not be. The Arabic names are what the screens show.
        for link in merchant.methods.select_related("payment_method"):
            wallet = link.wallets.filter(is_active=True).first()
            cap = f"cap {wallet.daily_cap:,.0f} IQD/day" if wallet and wallet.daily_cap else "no cap"
            self.out(
                f"  merchant #{link.merchant_id} - {link.payment_method.caption_en:<10} "
                f"wallet {wallet.number if wallet else '-'}  ({cap})"
            )

    def report_hours(self) -> None:
        self.heading("Business hours")
        self.out(
            "  The desk is seeded open around the clock so the portal works at any\n"
            "  time of day. Change it at /finance/hours/ (finance_admin) to see the\n"
            "  closed notice and its countdown; the client screens are replaced and\n"
            "  the submission endpoint refuses independently."
        )

    def report_portal(self, token: str, *, hours: int) -> None:
        jwks_url = f"{self.host}/static/dev/b2core-jwks.json"

        self.heading("Client portal without B2CORE")
        self.out(
            "  There is no real B2CORE here, so this stands one up: a local Ed25519 key,\n"
            "  the JWKS a real endpoint would publish, and one token shaped like a real\n"
            "  one - EdDSA, no audience claim, the name in two halves. Nothing in the\n"
            "  verification path is bypassed - the signature is genuinely checked.\n"
        )
        self.out("  1. Put these in .env, then restart runserver:\n")
        for line in (
            f"B2CORE_ORIGIN={self.host}",
            f"B2CORE_JWKS_URL={jwks_url}",
            f"B2CORE_JWT_ISSUER={DEV_ISSUER}",
            # Empty, and it must stay empty: B2CORE mints no `aud`, so setting
            # this refuses every real token. portal.E006 says so at deploy time.
            "B2CORE_JWT_AUDIENCE=",
            "PORTAL_ALLOW_STANDALONE=true",
            "PORTAL_SESSION_COOKIE_SAMESITE=Lax",
            "PORTAL_SESSION_COOKIE_SECURE=false",
        ):
            self.out(f"       {line}")

        self.out(
            "\n  Note: the JWKS above is served by this same runserver, and a dev server\n"
            "  handling a request cannot reliably fetch from itself - a session POST can\n"
            "  come back 401 for that reason alone, with nothing wrong with the token.\n"
            "  It is an artefact of the stand-in; B2CORE's real JWKS is on another host.\n"
            "  If you hit it, start a second runserver on :8001 and use that one.\n"
        )
        self.out(
            f"\n  2. Open {self.host}/portal/ , then paste this into the DevTools console\n"
            "     (it hands the token to the session endpoint the way B2CORE would):\n"
        )
        self.out(
            "       await fetch('/portal/session/', {method:'POST', "
            "headers:{'Content-Type':'application/json'}, body: JSON.stringify({token:"
            f"'{token}'"
            "})}).then(r => r.json())\n"
        )
        self.out(f"  3. Reload {self.host}/portal/ - the flow picks up the session.\n")
        self.out(
            f"  The token is valid for {hours}h and is also saved at {TOKEN_PATH}.\n"
            "  When it expires: python manage.py seed_demo --token-only"
        )
        self.out("")
        self.out(
            "Seeded. Everything above is demo data and DEBUG-only.",
            style=self.style.SUCCESS,
        )
