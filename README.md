# MaxPay Portal

In-house deposit and withdrawal system for MaxiFyFX (MAXMANAG-212).
Django 5 + DRF, PostgreSQL, Arabic RTL UI. See `maxpay-portal-spec.md` for the
full specification.

**Build status: all fourteen steps of the spec's build order (§12) are
complete, and so are two more the spec does not number — user and role
management (step 15) and the reports (step 16).**

---

## Getting started

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements-dev.txt

cp .env.example .env              # then edit it
docker compose up -d db           # or point POSTGRES_* at an existing instance

python manage.py migrate          # also syncs the role groups
python manage.py create_internal_user \
    --email you@maxifyfx.com --full-name "اسمك" --role finance_admin --superuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. The first login walks you through enrolling an
authenticator app — there is no way past it (see *Two-factor* below).

### Trying it in a browser

```bash
python manage.py migrate
python manage.py seed_demo
```

`seed_demo` fills an empty local database with the three internal accounts, a
merchant with a wallet per payment method, deposit and withdrawal rates, and a
demo B2CORE client — then prints the credentials and a runbook. It **refuses to
run unless `DEBUG` is on**: it seeds known passwords and pre-confirmed second
factors, which is fine on a laptop and is exactly what an attacker would want
anywhere else. Re-running converges rather than duplicating.

| | | |
| --- | --- | --- |
| `/finance/` | `admin@maxifyfx.com` / `staff@maxifyfx.com` | the Finance panel |
| `/merchant/` | `merchant@maxifyfx.com` | the merchant panel |
| `/portal/` | the demo client, via the token below | the embedded client flow |

Because a client cannot sign in without B2CORE, the command also stands up a
local stand-in for it: an RSA key under `devdata/`, the JWKS document a real
endpoint would publish written to `static/dev/` (so `runserver` serves it at a
real http URL — PyJWT's JWKS client accepts http/https only), and one signed
token. **Nothing in the verification path is bypassed** — the signature is
genuinely checked against a genuine key set. Paste the printed settings into
`.env`, restart, and hand the token to `/portal/session/` from the browser
console exactly as B2CORE would. `--token-only` mints a fresh one when it
expires.

Both `devdata/` and `static/dev/` are git-ignored.

### Starting the demo over

```bash
python manage.py reset_demo                   # everything below the accounts
python manage.py reset_demo --keep-merchants  # the traffic only
```

The companion to `seed_demo`, and the reason it exists rather than
`rm dev.sqlite3 && migrate` is the second factors: re-enrolling a TOTP device by
hand is the slowest part of setting this project up, and deleting the database
takes them with it.

| | |
| --- | --- |
| **Cleared** | requests, messages, attachments, read markers, the audit log, the merchant network, the payment methods, the client records |
| **Kept** | internal users, their roles and groups, their TOTP devices, the exchange rates, the business-hours settings |

`--keep-merchants` narrows it to the traffic alone and leaves the network and
the clients standing — the one to reach for between two run-throughs of the same
demo. It prints what it is about to delete and how many of each, then waits for
the word `reset` to be typed; `--no-input` skips the prompt for scripts. Like
`seed_demo` it **refuses to run unless `DEBUG` is on**, and for the mirror-image
reason: that one creates accounts with known passwords, this one deletes a
merchant network and an audit log.

The audit log is append-only in the database (spec §11), so clearing it means
dropping those triggers and putting them back. The command calls the migration's
own `forward`/`backward` rather than keeping a second copy of the SQL, restores
in a `finally`, and refuses to report success until it has asked the database
that the triggers are actually back. There is **no button for any of this**, in
the admin or anywhere else — it is a command on a laptop, and a reset that can
be reached by a misclick is a different kind of tool.

It resets the primary-key counters, so the next run-through numbers from 1
again. It does not reset `public_ref`, because there is no counter behind it:
the reference is five random digits precisely so a merchant cannot read the
desk's volume off it, and giving it a sequence to restart would undo that.

### Tests and checks

```bash
python manage.py test
python manage.py check --deploy --database default
ruff check apps config manage.py
node --test "tests/js/**/*.test.js"
```

The first three, and the middle one is not optional in CI. `--deploy` runs the
settings checks in `apps/core/checks.py` and `apps/portal/checks.py`;
`--database default` additionally asks the database whether the audit log's
append-only triggers are still installed. Neither is reachable from
`manage.py check` alone, which is the point — see *Security hardening*.

The suite runs on whatever `DATABASE_URL`/`POSTGRES_*` points at. With no
PostgreSQL to hand you can run it against SQLite:

```bash
DATABASE_URL=sqlite:///smoke.sqlite3 python manage.py test
```

The fourth covers the two scripts the Django suite cannot reach because they run
in the browser: `static/js/embed.js`, the B2CORE handshake, and the live quote in
`static/js/flow.js`. It needs Node and nothing else — `tests/js/harness.js` and
`tests/js/flow_harness.js` each fake the narrow slice of the DOM their script
touches, so there is no `node_modules` tree and no build step. See *The B2CORE
embed* and *And the transferred figure is rounded to the nearest 1,000*.

That is a smoke-test convenience only. **CI and every shared environment must
run PostgreSQL**, which is the database the spec targets. On Windows, set
`PYTHONIOENCODING=utf-8` first: the test names and assertion messages are
Arabic, and the default console codepage cannot encode them — with
`--parallel` this surfaces as an unrelated-looking pickling error rather than
as the encoding failure it is.

Four files in the suite are about the suite rather than about a feature, and
are worth knowing before adding a route, a serializer or a template:

| | |
| --- | --- |
| `apps/core/test_routes.py` | Walks the URLconf. Every route is public-with-a-reason, portal, or closed to anonymous callers — a new one that is none of those fails the build. |
| `apps/merchant_panel/tests/test_api.py` | Enumerates the merchant URLconf and sweeps every response for client identity, by key and by value. A merchant route added without being listed fails. |
| `apps/core/test_security.py` | The spec §11 promises: the audit log's triggers, one CSP per surface, and each deploy check firing on what it is for. |
| `apps/core/test_templates.py` | Walks every template for a `{# … #}` comment that runs past its own line — Django's hash comment is single-line, and the rest of it renders to the page. Also keeps the global `[hidden]` rule in `system.css`. Both are defects that shipped more than once. |

---

## Layout

```
config/
  settings/{base,dev,prod}.py   env-driven; nothing secret is committed
  urls.py                       two-factor login, Finance panel, admin, /portal/
apps/
  core/         TimeStampedModel, AppendOnlyModel, SystemSettings, AuditLog,
                audit service, business hours (hours.py), upload validators,
                attachment streaming, the security-header middleware and the
                deployment checks, the backup command
  accounts/     User (internal), Client (B2CORE), the permission matrix,
                account provisioning (provisioning.py) — generated passwords,
                permission overrides, 2FA resets, all audited — the 2FA and
                forced-password-change middlewares, the login lockout and the
                permission-denial backend (throttling.py), the password screen
                every role shares (views.py), admin, bootstrap commands
  merchants/    PaymentMethod, Merchant, MerchantMethod, Wallet, daily-cap
                accounting (capacity.py)
  rates/        ExchangeRate (immutable history)
  transactions/ Request, Attachment, Message, RequestRead, the lifecycle
                state machine (services.py) — the only writer of
                Request.status — messaging.py, the only writer of Message and
                the one place that decides who may read which one, and
                reads.py, which decides what counts as unread and for whom
  finance/      the Finance panel — views, the request queue, forms, access
                mixins, business hours, the audit-log viewer, the users and
                roles panel (user_views.py, user_forms.py), the report and its
                export (report_views.py), the live-update endpoints
                (live_views.py), Finance-scoped attachment URLs (no models)
  merchant_panel/
                the merchant panel — the masked serializers spec §2 requires,
                the anonymity guard behind them, merchant scoping, the JSON API,
                the screens, merchant-scoped attachment URLs (no models)
  reports/      reporting and export — the filter, the aggregates and the
                workbook writer, shared by both panels and scoped by neither
                (a plain package: no models, no templates, no migrations)
  portal/       the embedded client portal — B2CORE JWT verification, the client
                session and its separate cookie, the iframe handshake, and the
                client request flow in both directions: catalogue, pricing,
                destination normalisation, submission, signed attachment URLs
                (no models)
static/
  fonts/                        Cairo, self-hosted; every surface ships
                                `default-src 'self'` (spec §11)
  css/system.css                the shared foundation: palette, typeface, reset
  css/portal.css                the Finance panel, the 403 and the login
  css/merchant.css              the merchant panel
  css/embed.css                 the embedded client surface
  css/flow.css                  the six client screens
  js/embed.js                   the postMessage handshake
  js/flow.js                    the six client screens, both directions
  js/panel.js                   the ten-second poll, shared by both panels
  js/secret.js                  copying a one-time password, on the one screen
                                that ever shows one
templates/
  base.html                     RTL shell; loads system.css plus one panel sheet
  two_factor/_base.html         login/enrolment, no third-party requests
  accounts/                     the password screen, shared by every role
  finance/                      dashboard, request queue and detail,
                                merchants, wallets, methods, rates,
                                business hours, audit log, users and roles,
                                and the two _fragments the poll swaps in
  merchant/                     the merchant's queue, request detail, wallets,
                                and the same two _fragments
  portal/                       the bootstrap page B2CORE frames, and the flow
```

Inside `apps/portal/`, the step-6 work is deliberately split so each piece is
testable without a request: `pricing.py` (a quote from a rate and a string),
`catalog.py` (what may be offered), `submissions.py` (validate, then write),
`payloads.py` (a request as its own client may see it), `attachments.py`
(signed URLs), and `flow_views.py` (the endpoints that glue them together).
Step 10 added one more to that list rather than branching the existing ones:
`destinations.py`, which reduces a typed card or wallet number to the digits
that get stored.

`apps/finance/` follows the same split for step 7: `queue_views.py` (the
queue, the detail, the action endpoint), `queue_forms.py` (filters and the
inputs each action needs), and `attachments.py` (Finance-scoped signed URLs).
The rules those screens drive live in `apps/transactions/services.py`, away from
any view, because merchants drive the same rules from a different screen.

Steps 11 and 12 added two more to `apps/finance/`: `audit.py` (turning a stored
snapshot into something a person can read) and `audit_views.py`/`audit_forms.py`
(the viewer and its filters). The business-hours *rule* lives in
`apps/core/hours.py` rather than in the panel, because the portal enforces it
and the panel only configures it.

`apps/merchant_panel/` is step 8, split the same way again: `anonymity.py` (the
rule, and the three guards that keep it), `serializers.py` (what a merchant may
see), `scoping.py` (which rows exist for them), `api.py` and `views.py` (the two
surfaces), `forms.py` and `actions.py` (their moves), `attachments.py`
(merchant-scoped signed URLs).

`transactions` rather than `requests`, so the app can never shadow the
third-party `requests` package on the import path.

---

## Roles and permissions

Three internal roles, each backed by a Django group of the same name.
`apps/accounts/permissions.py` is the single source of truth; `User.role` is
mirrored into group membership by a `post_save` signal, and the groups are
rebuilt after every `migrate` and by:

```bash
python manage.py bootstrap_roles            # apply
python manage.py bootstrap_roles --dry-run  # show what each role would get
```

| Role | Baseline |
| --- | --- |
| `finance_admin` | Every managed permission, minus the global deny-list. Manages users, merchants, wallets, rates, business hours, payment methods. |
| `finance_staff` | The request queue and message threads, client identity, read-only reference data, audit log viewer. |
| `merchant` | Assigned requests only, thread participation, read-only view of own wallets. |

Re-running `bootstrap_roles` resets the group baseline; per-user permissions a
`finance_admin` added on top are left alone. There is no self-registration for
any internal role (spec §3).

The fourth role in spec §3, `client`, is deliberately **not** a Django user.
Clients are `accounts.Client` records keyed on the verified B2CORE `sub` claim,
so there is no client password to steal and no way to authenticate as one
without a valid upstream token (spec §4).

### Client anonymity is enforced in the permission matrix

`MERCHANT_FORBIDDEN` lists the permissions a merchant must never hold.
`sync_role_groups()` refuses to build the merchant group if the matrix would
grant any of them, so a future edit cannot quietly break spec §2. There is also
`assert_merchant_anonymity(user)` for merchant-scoped views and serializers to
call, and a test asserting the property directly.

Serializer-level masking — the primary enforcement point named in spec §2 —
arrived with the merchant panel in build-order step 8 and has its own section
below. The permission matrix and the serializers are two independent locks on
the same door: the matrix says a merchant may not *hold* an identity permission,
the serializers say a merchant-facing payload may not *contain* an identity
field, and neither depends on the other being right. The Django admin shows
client identity, so `transactions` and `accounts.Client` admin access stays
restricted to Finance.

### Records that are never deleted

`GLOBAL_DENY` withholds `delete_*` on requests, messages, attachments, clients,
users, merchants, wallets and payment methods from every role. Those records are
deactivated or archived, not removed, so the audit trail keeps its references
intact. `AuditLog` and `ExchangeRate` go further: they have no `change` or
`delete` permission at all, and their models raise on any attempt to update or
delete a saved row.

**One narrow exception**, added in the review of 25 Aug 2026: a wallet **no
request was ever submitted against**. The rule this invariant exists to protect
is *references keep resolving*, and a row nothing ever pointed at has no
reference to protect — it is a typo, not a business record. Everything else on
that list is unchanged, and the wallet case is decided by asking rather than by
trusting: see [Archiving, and the one thing that is really deleted](#archiving-and-the-one-thing-that-is-really-deleted).

---

## Two-factor authentication

Every internal account requires 2FA. Spec §4 mandates it for `finance_admin`
and `finance_staff`; spec §11 states it for *all* internal accounts, so the
broader rule is the one implemented — merchants included. The role list lives in
`TWO_FACTOR_REQUIRED_ROLES`.

`django_otp` only *records* whether a session is OTP-verified; on its own an
account that never enrols still gets a working session. Three things close that:

1. `EnforceTwoFactorMiddleware` redirects any non-compliant internal user into
   the enrolment wizard, and returns `403` with a JSON body for API clients.
2. The admin runs on `AdminSiteOTPRequired`, so it refuses an unverified session
   even if the middleware were removed.
3. `LOGIN_URL` points at the two-factor login flow, not Django's.

TOTP authenticator apps plus static backup tokens. No SMS or phone gateway is
enabled, so no internal user's phone number is stored for authentication.

A locked-out user is recovered by a `finance_admin` from the user admin —
select them and run *"إعادة تعيين المصادقة الثنائية"*. The reset is written to
the audit log, and the user re-enrols on next login.

---

## The Finance panel

Lives at `/finance/` and is where a signed-in Finance user lands. Steps 3, 4
and 7 build out most of what spec §9 lists; business hours (step 11), the audit
viewer (step 12), user management (step 15) and the reports (step 16) fill in
the rest.

| Section | What it does |
| --- | --- |
| Overview | What is waiting to be worked on, the deposit and withdrawal rates currently in force, counts, and a list of merchant methods with **no active wallet** — those cannot be offered to a client, so they are surfaced rather than left to be discovered. |
| Requests | The queue, the full request detail including client identity, and every lifecycle action. Its own section below. |
| Merchants | Create and edit merchants, record the B2CORE identifier a payout is reconciled against, activate/deactivate, assign payment methods, and manage the wallets under each method. |
| Payment methods | The catalogue: captions, icon, direction support, display order, active state. |
| Exchange rates | The rates in force, the full immutable history, and the form for setting a new one. |
| B2CORE integration | Read-only. What the embed is pointed at and how the key lookup is faring. Its own section below. |

### Two access gates

Seeing the panel needs a Finance role — a merchant gets a 403, because this is
Finance's surface and merchants have their own at `/merchant/`. The refusal runs
in both directions: Finance gets a 403 from the merchant panel too.

Every *write* is gated on a specific permission rather than on the role:
`merchants.manage_merchants`, `merchants.add/change_paymentmethod`,
`rates.add_exchangerate`, and one permission per lifecycle action. A `finance_admin` holds all of them by default, and
can delegate any of them to a `finance_staff` account with no code change —
which is what spec §3's "permissions granted by `finance_admin`" means in
practice. Staff without a permission see the pages read-only, with the write
controls absent rather than merely disabled.

### `Merchant.b2core_id`: optional and unique, which is a harder pair than it looks

Finance types the merchant's own account identifier in B2CORE so a payout here
can be matched by hand against a movement over there. It is optional — plenty of
merchants have no account there — and unique, because two merchants pointing at
one B2CORE account is a reconciliation error waiting to be made.

Those two together are a well-known Django trap. A `unique=True` CharField that
is `blank=True` stores `''` for empty, and the unique index treats `''` as an
ordinary value: the *first* merchant without an identifier takes the empty slot
and the second one cannot be saved at all. So absent is `NULL` here and only
`NULL`, which SQL counts as distinct from every other `NULL`. Blank input is
folded to `None` in `clean()` — which `full_clean` runs *before*
`validate_unique`, so no two blanks are ever compared — and again in `save()`,
which is the path `Merchant.objects.create(b2core_id="")` takes. A check
constraint refuses `''` at the table for anything that reaches it by a third
road, such as a queryset `update()`.

It shares a name with `Client.b2core_id` and nothing else. That one is the
verified `sub` claim off a signed token and is the only identifier the portal
trusts; this one is reference data an operator typed, is never verified against
B2CORE, and must never be promoted into an authentication decision. It rides the
merchant screen, so it rides `manage_merchants`, and `AuditedFormMixin` records
it on both sides of every edit — which identifier a payout was matched against,
and who moved it, is exactly the question an audit log is asked afterwards.

It is not in `merchant_payload`. The client's merchant list is an explicit
whitelist of `id`, `name` and `method_count`, so the field cannot reach the merchant column
by being added to the model.

### The B2CORE integration screen, and why it is read-only

`/finance/integration/b2core/` answers one question, asked by whoever is holding
the phone when a client says the portal will not load: *is the B2CORE side
working, and what is it pointed at?* Until it existed the only answers were the
server's environment and its log file, and Finance can reach neither.

It shows the JWKS URL, the issuer, the audience and the frame origin, with the
signing algorithms and clock leeway beside them — and it will not edit any of
them. There is no form on the page and no route behind it that writes, the same
way the audit viewer is read-only. That is not squeamishness: this configuration
decides who may frame the portal and whose tokens are believed, it has to be
reviewed and rolled back like code, and it has to be identical across every
worker. A text box on a web page is none of those things. The page says so on
its face rather than leaving an operator hunting for a save button that was
never there.

It also surfaces the two gaps `manage.py check --deploy` raises — an unset
issuer or audience — to the people who would actually notice them. Neither is an
error; a correctly-signed token is still required. Each one just widens *whose*
correctly-signed token this deployment believes, which is not a thing to
discover from a checklist nobody ran.

**The status half is deliberately passive.** `jwks.status()` reports what
ordinary traffic already found out and never touches the network. A status page
that probes on load is one that can be refreshed until it takes the endpoint
down, and it would end up reporting on its own probe rather than on the path
clients travel. Two consequences are printed on the page rather than left to be
inferred:

- *"Last successful key resolution"* is not *"last fetch"*. PyJWT serves a
  cached key set for `B2CORE_JWKS_CACHE_SECONDS`, so a success may have touched
  no network at all — which is why the cache lifetime sits next to it. A success
  four minutes old against a ten-minute cache says nothing about whether B2CORE
  is reachable right now.
- The reading is **per worker**. The observations are module globals beside the
  `PyJWKClient` they describe, so under several workers each holds its own and a
  refresh may land on a different one with different numbers. A shared store is a
  great deal of machinery to put behind a diagnostic; a figure presented as
  global while quietly being per-worker is worse than one that is labelled.

`jwks.py` now keeps two clocks on purpose. The refetch throttle stays on
`time.monotonic()`, which cannot be dragged backwards by an NTP correction and
so can never have its gate opened early by a clock adjustment; the reporting
timestamps are `time.time()`, because monotonic's zero point is arbitrary and
cannot be turned into a date. `reset_cache()` clears the observations along with
the client, since a success still showing against an endpoint the process no
longer points at is exactly the reassuring lie a status screen must not tell.

### Wallets: one active at a time

The single active wallet per merchant method is the number clients are shown, so
the panel makes that state loud — the live wallet carries a stamp, not a badge.
Activating a wallet stands down the previous holder through the model's own save
logic, and **both** wallets get an audit entry: the new one as activated, the old
one with `reason: superseded_by`. The operator is told which number was
deactivated, because it changes what clients see from that moment.

### Rates: the history is the record

Setting a rate always inserts a row; the model raises on any attempt to update a
saved one. The history page renders each revision as one of three states — in
force, scheduled (`effective_from` in the future), or superseded. Backdating and
future-dating both work, and `ExchangeRate.current()` resolves what is actually
in force at any moment.

Existing requests are untouched by a rate change: they carry `rate_applied` and
`commission_applied` snapshotted at submission (spec §5, §9). Same for wallet
numbers — editing a wallet never rewrites `wallet_number_snapshot` on a request
that already used it. Both properties have tests.

### Number formatting

Django's `ar` locale renders decimals as `1450,00`. For IQD/USD figures that
reads wrong, so every money value uses `floatformat:"2u"` to bypass locale
formatting, and every numeric cell is monospaced, tabular, and isolated to LTR
inside the RTL page.

---

## The client portal and B2CORE

Lives at `/portal/`, inside an iframe on B2CORE. Step 5 delivers the
authentication half — the handshake, the verification, the session — step 6 the
deposit flow it exists for, and step 10 the withdrawal flow beside it. Both
directions use the same screens and the same endpoints; the type is a
field, not a route.

```
GET    /portal/                     the page B2CORE frames — runs the handshake, renders no client data
GET    /portal/session/             the current session, or {"authenticated": false}
POST   /portal/session/             a B2CORE JWT in, a portal session out
DELETE /portal/session/             embed-logout
POST   /portal/preferences/         embed-theme-change, embed-language-change

GET    /portal/options/             what can be chosen, resolved as deep as the query goes
POST   /portal/requests/            submit a deposit or a withdrawal (multipart: a deposit's proof rides along)
GET    /portal/requests/            the client's own history
GET    /portal/requests/<ref>/      one request in full
GET    /portal/attachments/<pk>/<token>/   a stored file, signed and time-limited
GET    /portal/method-icon/<code>/  a payment-method icon
```

### The handshake

`static/js/embed.js` runs spec §4 in order: the `message` listener is attached
first, then `embed-iframe-ready` goes out, then `embed-request-jwt-token`, and
the `embed-jwt-token` reply goes straight to `POST /portal/session/`. Ready is
announced before the script asks our own backend whether a session already
exists: B2CORE will not answer a request from a frame it has not heard a ready
from, and putting a round trip of ours in front of that delays the whole
handshake behind a call the host knows nothing about. A token may therefore
arrive while that probe is still in flight; the token wins, and the probe's
answer is dropped rather than queueing a second request.

Every inbound message is dropped unless `event.origin` is exactly
`B2CORE_ORIGIN` **and** `event.source` is `window.parent`. Origin alone does not
identify a window: a second B2CORE tab, a popup it opened or a frame it nests
all share that origin, and only the window actually framing this page has any
business speaking to it. Every outbound message names that origin as its target
rather than `"*"` — a token request broadcast to whatever page happens to be
framing us is a token handed to that page.

B2CORE can also refuse, with `embed-jwt-token-error`. That is handled where it
arrives: the session is dropped immediately, here and on the server, rather than
left standing until the fifteen-second `token_timeout` fires and reports a
network problem for what is an authentication failure. Whatever reason the host
named is shown beside the code, bounded and as text, because it is the only
diagnostic support will have.

`embed-jwt-token` carries an `expiresAt` beside the token, and the tokens
B2CORE mints last an hour. The renewal is scheduled against whichever dies
first — that expiry, or the session ceiling `apps/portal/session.py` applies —
ninety seconds before it does. A renewal runs under a live session, so it does
*not* flip the stage back to its connecting state: dropping the client out of a
half-filled form is the very thing renewing early exists to prevent.

The token is never stored. It lives in a local variable for the length of one
request. The session cookie the backend sets is the only thing that outlives the
exchange, and it is `HttpOnly`, so the script cannot read it either.

The page carries no inline script — configuration reaches it through a
`json_script` island — which is what lets the CSP stay `default-src 'self'`
alongside the `frame-ancestors` spec §11 asks for.

`tests/js/embed_handshake.test.js` covers all of the above: the send order, the
refusal, the three shapes `expiresAt` arrives in, and the messages that come
from the right origin but the wrong window. It runs the real
`static/js/embed.js` in a `node:vm` context against the hand-written DOM in
`tests/js/harness.js` — five elements, one listener, `fetch` and the timers,
which is everything that script touches. No `node_modules`, no build step:
`node --test "tests/js/**/*.test.js"`.

### Verification is the whole of client authentication

`apps/portal/b2core/` is the boundary. Nothing outside it sees a token; it hands
back a verified `Identity` or raises. Three failure modes, deliberately
distinct, because the remedies differ: `B2CoreTokenError` sends the client back
to B2CORE for a fresh token, `B2CoreKeyError` is worth retrying, and
`B2CoreConfigurationError` needs an operator.

What is checked, in order: the size ceiling, then the header algorithm against
`B2CORE_JWT_ALGORITHMS`, then the signature against the key the `kid` resolves
to in the JWKS, then `exp`, `iss`, `aud` and the presence of a usable `sub`.

The algorithm check comes *before* key resolution on purpose. `jwt.decode` would
reject a bad algorithm anyway, but only after a key lookup — and an `alg: none`
token has no `kid` to look up, so the failure would surface as a key error
rather than as what it is. Checking first also means a garbage token cannot
provoke an outbound JWKS fetch at all.

`apps/portal/tests/test_tokens.py` signs real tokens with real RSA and EC keys
and puts a real JWKS in front of PyJWT; only the HTTP fetch is stubbed. It
covers the invalid signature, the expired token, the wrong issuer, the wrong
audience, `alg: none`, the unknown `kid`, the HMAC-signed-with-the-published-
public-key confusion attack, key rotation, the missing `exp`, the missing `sub`,
and the refresh throttle.

That throttle is worth naming: the `kid` comes off an unverified token, and
PyJWT refetches the JWKS whenever it meets an unknown one. Without a floor on
how often a *forced* refresh may happen, anyone who can reach the session
endpoint could make us hammer B2CORE with a stream of random `kid` values.

### Two sessions, deliberately

The internal panel's session cookie stays `SameSite=Lax`, which is a large part
of what protects it from cross-site request forgery. The client session runs
inside a third-party iframe, where `Lax` means *never sent* — so it needs
`SameSite=None; Secure`. That is a weaker cookie, and precisely why it is a
**different** cookie:

| | internal | client portal |
| --- | --- | --- |
| name | `SESSION_COOKIE_NAME` | `PORTAL_SESSION_COOKIE_NAME` |
| SameSite | `Lax` | `None` |
| path | `/` | `/portal/` |
| attribute | `request.session` | `request.portal_session` |
| identity | `request.user` | `request.portal_client` |

`PortalSessionMiddleware` manages the second store itself rather than running
Django's session middleware twice, which would have the two instances fighting
over `request.session` and over which cookie to write on the way out. A system
check refuses to start if the two names ever collide, or if `SameSite=None` is
ever paired with a non-`Secure` cookie — browsers drop that cookie outright, so
the symptom would be "no client can ever log in" with nothing in the logs.

A client is not a Django user: no password, no `auth` session, no row in
`auth_user`. `Client` records are keyed on the verified `sub` claim. A portal
session stores the client id, the subject alongside it (so a recycled primary
key can never hand one client another's session), and the moment the token
expires — capped at `PORTAL_SESSION_MAX_SECONDS` regardless of what the token
claims, so a long-lived token cannot buy a long-lived session.

### CSRF, replaced rather than disabled

Django's CSRF cookie is `SameSite=Lax` and so never arrives inside the frame.
The portal endpoints are therefore `csrf_exempt` and carry two guards instead:

1. Every unsafe request must present an `Origin` we accept — our own, or
   B2CORE's. A missing `Origin` is a rejection, not a pass.
2. Every unsafe request against an *existing* session must echo the per-session
   token issued when that session was created, in `X-Portal-CSRF`. A cross-site
   page can make the browser send the cookie; it cannot read the response that
   carried the token.

Creating a session is exempt from the second rule and needs no protection from
it: it requires a valid B2CORE JWT, which an attacker cannot obtain.

The session endpoint is also rate-limited per IP (`PORTAL_SESSION_RATE`): it is
unauthenticated and does public-key cryptography. The limiter fails *open* — a
broken cache backend must not lock every client out.

### Framing

`EmbedFrameHeadersMiddleware` sets `Content-Security-Policy: frame-ancestors
<B2CORE origin>` on portal responses and `frame-ancestors 'none'` everywhere
else, and strips the `X-Frame-Options: DENY` that Django would otherwise send
alongside it. It is listed early in `MIDDLEWARE` for that reason: the response
phase runs in reverse, so an early entry is the last to touch the headers.

If `B2CORE_ORIGIN` is unset, the portal is framed by nobody. That is the safe
posture, and `manage.py check --deploy` says so.

### Configuration

`B2CORE_ORIGIN` and `B2CORE_JWKS_URL` are required in a real deployment. The
other two used to be described here as "verified when set, skipped when blank,
and you should set both". One of them must never be set, and the sentence that
said otherwise was an instruction to break the product:

| | |
| --- | --- |
| `B2CORE_JWT_ISSUER` | **Required, and it is not the origin.** B2CORE mints `iss` as its auth service's URL — `api.*`, with a path, trailing slash included. The origin is the portal host with no path. PyJWT compares the claim by string equality, so the origin here, or the right URL with the slash trimmed, refuses every client. Unset is the other failure: any token signed by any key in that JWKS is then believed. `portal.E005` reports both, and `config/settings/prod.py` refuses to boot on either. |
| `B2CORE_JWT_AUDIENCE` | **Must stay empty. B2CORE sends no `aud` claim.** Not a value waiting to be discovered — there is none. Setting it turns the audience check on, which then rejects every real token for a claim that is never minted. `portal.E006` fires if it is set. |

`portal.E006` is the inversion of a warning that used to say the opposite. It
warned that an unset audience was unverified and told the operator to set it
"once B2CORE confirms the audience value". An operator clearing deploy warnings
before go-live would have taken the portal down for every client. A check that
gives wrong advice is worse than no check, because it is followed.

Those live in the **deploy** checks rather than the default ones, so a developer
building the Finance panel can still run `manage.py check` without holding
B2CORE credentials. The cookie-collision and signing-algorithm checks are
unconditional: those are wrong everywhere, not just in production.

**Testing the embed needs HTTPS.** `SameSite=None` without `Secure` is dropped
by every current browser, so a plain `http://localhost` run will never hold a
portal session. `PORTAL_ALLOW_STANDALONE=true` lets the bootstrap page run
outside an iframe for local work on the page itself.


---

## The client request flow

Three screens (spec §7), and one page. They cannot be three pages: the portal is
framed cross-site, Django's CSRF cookie never arrives, and the per-session token
that stands in for it can only travel in a header — which only `fetch` can set.
So the flow navigates in the browser and talks to JSON.

```
طلب جديد     the whole request, on one screen:

             نوع الطلب        التاجر           طريقة الدفع
             ┌───────────┬┐  ┌───────────┬┐  ┌───────────┬┐
             │ ↓ إيداع   │⌄│  │ اختر…     │⌄│  │ ▣ زين كاش │⌄│
             └───────────┴┘  └───────────┴┘  └───────────┴┘
              deposit or      merchants who    the methods
              withdrawal      take this        *that* merchant
                              direction        covers
                          ↓ once all three are answered
             التفاصيل
               deposit:    wallet number + copy, or the QR to scan,
                           amount in USD with a live IQD figure,
                           proof upload, optional message
               withdrawal: destination card or wallet, amount in USD
                           with a live IQD figure, optional message

التأكيد      the reference, MP-xxxxx
عرض الطلب    status timeline, attachments, message thread
```

**It was a four-step wizard until 4 Sep 2026.** Type, merchant, method and
details were four screens walked in order, with a back control, a step counter
and a progress bar. The complaint was not speed. A client could not see what they
had already chosen without walking back through it, and changing the first
answer was three taps from wherever they were standing.

Now the three choices are a row of dropdowns across the top, all of them on
screen from the first paint, and the details appear underneath the moment the
third is answered. Any answer is one tap from being changed, and what changing
it costs is visible before you do it, because the columns to its right are right
there.

**They are built, not native, and the reason is the payment method's logo.**
They were `<select>` elements for a day, which was the right instinct and the
wrong control: an `<option>` renders no markup, so the logo could not appear
beside the name *in the list*, and the logo is how a client recognises the rail.
They know the mark long before they read the string. Putting it beside the
closed control instead only confirms a choice already made — it does not help
make one, which is the job it is actually for.

So each column is a button and a `role="listbox"`, with the icon inside every
option. What that costs is everything the native control gave away for free,
and it is paid for rather than skipped: `aria-expanded`, `aria-selected`,
`aria-activedescendant`, the arrow keys roving the active option, Home and End,
Enter and Space to take it, Escape to close and return focus to the button, a
click elsewhere to dismiss, and `disabled` on the button when the column is
locked. Written once and used by all three columns — three copies is how one of
them ends up not closing on Escape.

The icon stays deliberately small, at the size it had on the tiles. The large
picture on this screen is the wallet's QR in the details, and a client should
never be in doubt about which of the two is a thing to point a camera at.

**The row is a chain, and `CHAIN` in `flow.js` is the only place that says so.**
Which column is unlocked, what an answer invalidates, and whether the details
belong on screen are all derived from that one array. The wizard kept the order
in an array *and* in the screen each handler named by hand; the two drifted, and
screen 1 spent a while sending clients past the merchant into a method list that
is empty by construction until a merchant is chosen. `advance()`, `retreat()`,
`WIZARD` and the trail are gone — there is nowhere left to advance to.

Changing an answer clears every answer after it and hides the details.
A form quoting a rate and a wallet belonging to a merchant the client has just
swapped is a form describing a request nobody is making.

A **locked** column is `disabled` *and* emptied, and its button reads the
sentence saying what it is waiting for — the explanation lives in the control
that is refusing, which is why no column carries a separate line of it.

**No column carries a line of explanation at all.** There used to be one under
each label — "choose the merchant you will transfer to" under a control labelled
"the merchant" — which is the screen reading itself out loud. They were also
three sentences of three different lengths, which is what stopped the three
columns lining up.

Emptied matters as much as disabled. While no direction is chosen the merchants
on hand are the *deposit* ones, because that is what `/portal/options/` defaults
to and it sends them with every payload regardless. Disabling alone would leave
them in the DOM, one removed attribute from being offered as the answer to a
question nobody asked.

**The three columns line up by construction, not by luck.** Above the
breakpoint `.picker__col` is `display: contents`, so the label, the control and
the empty state become direct grid items and every label can sit in row 1, every
control in row 2, every empty state in row 3. Each item is given its row and its
column explicitly, which is not belt-and-braces: auto-placement flows the nine
flattened items *across* the rows rather than down the columns, and the empty
states are `display: none` almost always, so they occupy no cell and anything
relying on flow order puts the next column's label in the gap they left. Both
were true here before the coordinates were written down, and the row was
visibly ragged.

**On a phone the row is a column** — one dropdown above the next, which is the
same order the wizard walked. Written that way round in the stylesheet: the
three-column grid is what appears above 46rem, not what gets squeezed below it,
and `display: contents` applies only inside that breakpoint because below it it
would flatten the grouping into one undifferentiated stack. Each control clears
`--tap` at every width.

The details are the only part that differs by direction, and *how* they differ
is the server's answer, not the script's guess: every `/portal/options/` payload
carries a `needs` object (`wallet`, `destination`, `proof`) and `flow.js` shows
whichever half it names. A screen that collected a field the submission would
refuse, or omitted one it requires, would need those two to disagree first.

Underneath it all sits the client's last few requests, so a returning client can
reach the request view without submitting anything.

**Nothing behind the row moved.** The endpoints, the payloads, the filtering
rules and the order the catalogue is asked in are exactly what they were — which
is why `WalkTests` in `apps/portal/tests/test_navigation.py`, which drives the
server through the same sequence, is unchanged. What was rewritten beside it is
`PickerChainTests`, which used to assert the wizard's order and now asserts the
chain's: declared once, derived from everywhere, three columns always in the DOM,
and the details not on screen until every answer is in.

### The amount

The client enters **USD** in both directions — the figure that moves in their
trading account — and is shown **IQD**. The commission is the only thing that
changes sign:

```
deposit     total_iqd = amount_usd × iqd_per_usd  +  amount_usd / 100 × commission_iqd_per_100usd
withdrawal  total_iqd = amount_usd × iqd_per_usd  −  amount_usd / 100 × commission_iqd_per_100usd
```

On a deposit the client hands over the money, so the desk's fee is added to what
they transfer; on a withdrawal the desk pays out, so it comes off what they
receive. Adding it in both directions would charge the fee on the way in and
hand it back on the way out. A withdrawal whose commission would swallow the
whole payout is **refused** (`amount_below_commission`) rather than clamped to
zero — and the rate payload carries `commission_sign` so the browser's preview
cannot hold a second, divergent copy of that rule.

`Request.amount_iqd` stores that **total** either way, because it is the figure
that actually moves between the client and the merchant and therefore the only
number a receipt can be checked against. `rate_applied` and
`commission_applied` keep the two components, so the breakdown stays
reconstructible after any later rate change.

Each direction has its own bounds — `PORTAL_DEPOSIT_MIN_USD`/`MAX_USD` and
`PORTAL_WITHDRAWAL_MIN_USD`/`MAX_USD` — and `pricing.parse_amount_usd` takes the
type as a required argument rather than defaulting it, so a withdrawal cannot be
silently measured against the deposit ceiling.

An amount finer than a cent is **refused, not rounded**. Rounding `0.999` up to
`1.00` would charge for a figure nobody typed, and silently changing an amount
is worse than asking for it again. Arabic-Indic digits (`١٢٣٫٥٠`), the Arabic
decimal separator, thousands separators and pasted bidi marks all parse as the
number they look like — `pricing.normalise_number` and its mirror in `flow.js`
have to agree, or the preview and the charge diverge.

### Nothing in the details is trusted, and nothing is silently re-priced

The submission carries the **ids** of the wallet and the rate the client was
shown. Both are checked against what is in force at that moment:

- wallet swapped mid-form → `409 wallet_changed`, with the new number in the
  body. The screen updates and the client looks before sending again.
- rate revised mid-form → `409 rate_changed`, with the new rate and the new
  total.
- merchant deactivated, or their last wallet stood down → `409` naming the
  screen to go back to.

Those ids are consent, not data. Every figure actually written is recomputed
server-side from the row that was verified, inside one transaction with the
proof and the opening message — a request whose proof failed to store is a
request Finance cannot review and one the client believes they filed.

Snapshotting then does the rest: `wallet_number_snapshot`, `rate_applied` and
`commission_applied` are frozen at submission, so a later wallet swap or rate
revision cannot rewrite a request that already happened (spec §5, §9). Both
have tests.

### The merchant comes before the method

The client settles **who** they are handing money to before **how**. That is the
order the reassurance actually runs in: a name they recognise first, then what
that name covers. The second column is the merchant list and the third is the
methods *that merchant* covers, so a method is never offered by somebody who
cannot serve it — which the other order could only guarantee one step later.

It costs nothing at the back: `catalog.available_merchants(type)` and
`catalog.available_methods(type, merchant)` narrow the same
`_offerable_methods` queryset from opposite ends, and both still take the
optional other half, because the submission endpoint has to check the *pair* and
that question has no order at all.

It cost something at the front, and the bill is worth reading, because it is the
reason the order is now declared exactly once. It used to live in two places —
the `WIZARD` array and four hardcoded `go("...")` calls — and reversing it
updated the array and three of the four. Screen 1 went on sending the client to
`"method"` by name, over the top of the merchant screen, into a method list that
is *correctly* empty until a merchant has been chosen. Every test passed: they
check the steps one at a time, and none of them asked what the client is shown
next.

The wizard that made that mistake possible is gone (4 Sep 2026), and with it
`advance()`, `retreat()`, `WIZARD` and the trail. What replaced them keeps the
lesson rather than the machinery: `CHAIN` is the single declaration of the
order, and unlocking, resetting and showing the details all read it.
`PickerChainTests` in `apps/portal/tests/test_navigation.py` asserts the order,
that each of those three derives from it rather than naming columns, that no
`go("type"|"merchant"|"method"|"details")` survives anywhere, and that the dead
machinery is actually gone rather than left lying about as a second way to
express the same thing.

### Every empty column says why

The blank box was the second half of that bug, and it would have been bad even
without it. A list that came back empty is a fact the client is owed a reason
for; a bordered box with nothing in it is the portal saying "something went
wrong" in a way nobody can act on.

All three columns carry one, written from `flow.js` because the reason depends
on the direction and on what has been answered so far:

| Column | Empty because | What it says |
| --- | --- | --- |
| نوع الطلب | no rate for this direction, or nobody offering it | that this direction is closed, and the other one may not be |
| التاجر | every merchant is stopped, or has no active wallet on a deposit | which of the two directions is affected, and to try later |
| طريقة الدفع | the chosen merchant covers nothing in this direction | to choose a different merchant |

A **locked** column is a different thing from an empty one and says something
different: it renders no list at all and names what it is waiting for. That
distinction is what retired the fourth row of this table. "No merchant chosen
yet, so no methods" was the state the reversal produced and the one a client
could not make sense of; it is now unreachable, because the column is locked
until there is a merchant. The wording survives in `EMPTY.methodNoMerchant`
anyway — an emptiness arriving out of order should still say why rather than
render blank.

The empty states used to carry a button back to the screen that could fix them.
There is no screen to go back to: the column that fixes either is in the same
row, a few centimetres away.

### The catalogue offers nothing that leads nowhere

A merchant with no active method, or a method of theirs with no active wallet,
is a dead end the client would only discover on the next screen. The
availability rule lives once, in `catalog.py`, and every screen reads the same
one: method active and supporting the direction, merchant active, their
`MerchantMethod` active, and — for a deposit, because the client is about to be
shown a number to pay into — an active wallet. A withdrawal needs no wallet, so
that last clause is derived from the request type rather than assumed.

`GET /portal/options/` answers as deep as the query goes: nothing for the
merchants, `?merchant=` for their methods, `?method=` for the wallet. When
something chosen earlier has since gone, the answer is not an error — it is the
shallower answer plus `unavailable`, naming the screen that can fix it. A
pairing that has come apart is named a **method** problem rather than a merchant
one: the merchant is still offerable, and sending the client back a screen
further than they need to go would make them re-choose something that is fine.

### Uploads

The proof is **required** for a deposit and the message is optional, which is
the way spec §6 words it. A withdrawal carries no upload at submission at all:
nothing has moved yet, and the proof of transfer is the *merchant's*, filed when
they mark it paid (spec §6, §8).

Three gates, cheapest first: the size cap, then the extension and declared
content type, then the file's **actual leading bytes**. A PNG renamed
`receipt.pdf` fails, and so does anything whose bytes match nothing on the
allow-list — `core.validators.validate_real_content_type`, which is what the
existing validator's docstring had always promised the upload path would do.

### Serving a stored file

`MEDIA_ROOT` is still never mapped to a URL prefix. A file is reachable only
through `GET /portal/attachments/<pk>/<token>/`, and that needs **four** things
at once:

1. a valid, unexpired signature (`TimestampSigner`, five minutes by default);
2. a live portal session — the signature authenticates the *link*, not the
   caller, and a link that leaked out of the frame must not stand in for a
   login;
3. the client the token was minted for;
4. ownership of the request the attachment hangs off, checked in the database
   rather than trusted from the token.

Every failure is the same 404. Images are served inline; everything else is
sent as a download, so a client-uploaded file never renders inside our origin.
The response carries `default-src 'none'; sandbox; frame-ancestors 'none'`, and
`EmbedFrameHeadersMiddleware` now defers to a response that sets its own policy
rather than widening it back out to the page policy.

Payment-method icons need a route of their own for the same reason, but
deliberately **without** a session: they are brand marks, the only thing they
disclose is which rails MaxPay offers, and requiring a session would make every
tile uncacheable to protect nothing.

### What the client is not told

`payloads.py` is the mirror image of `apps/merchant_panel/serializers.py`.
Those whitelist to keep client identity out; this one whitelists to keep the
desk's internals out:

- `Message.is_internal_note` never leaves. Finance talk among themselves on the
  same thread, and a filter that forgets it hands the client their private
  reasoning. Since step 9 that filter is not written here at all — it is one
  rule in `apps/transactions/messaging.py`, shared by all three surfaces.
- `merchant_assigned` is never disclosed. The client chose `merchant_selected`
  and paid into that merchant's wallet, so they see that one; who Finance
  actually routed to is an internal decision.

Both have tests, including one that asserts the routed merchant's name appears
nowhere in the serialised body.

### The screens themselves

Mobile first, RTL, and built on `embed.css`'s custom properties, so the flow
follows the host's theme through `embed-theme-change` without knowing anything
about colour. No inline script, no inline style, no inline handler — the CSP is
still `default-src 'self'`, and a test walks the rendered page asserting every
`<script>` has a `src`.

Three deliberate choices worth knowing about:

- **No History API.** `pushState` inside an iframe hijacks the host page's back
  button. Navigation is the in-app back control and nothing else.
- **Copy falls back to `execCommand`.** The async clipboard needs a
  `clipboard-write` permission the *host* grants on the iframe, and B2CORE may
  not. `execCommand` is the thing that reliably works inside a frame, so it is
  the fallback rather than the other way round; if both fail the number is put
  on screen to copy by hand.
- **Image previews use `data:` URLs, not `blob:`.** The page CSP allows the
  first and not the second.

---

## The withdrawal flow

Build-order step 10. Not a second flow — the same screens, the same
endpoints, the same submission — because a withdrawal and a deposit share their
first three screens exactly. Four things genuinely differ, and everything in
this section is one of them.

### No wallet

A deposit needs somewhere to pay *into*, so the catalogue refuses to offer a
merchant whose last wallet stood down. A withdrawal needs nothing of the sort:
the merchant pays out. So a merchant with no active wallet still appears on a
withdrawal's merchant column, `wallet_number_snapshot` stays empty, and a wallet
sitting at its **daily cap** does not block one — the cap governs money paid
into that wallet, and this is not that. `catalog.requires_wallet` derives the
whole difference from the request type; nothing downstream re-decides it.

### The destination account

The one field a deposit does not have, and the most dangerous string in the
system. Spec §2 hands it to the merchant — they cannot pay without it — and once
they have transferred against it a mistyped digit is money gone to a stranger.
There is no undo.

`apps/portal/destinations.py` does the two things that actually help:

- **Normalise.** `٠٧٧٠ ١٢٣-٤٥٦٧` and `07701234567` are the same account.
  Arabic-Indic and Eastern Arabic-Indic digits fold to ASCII, separators and
  bidi marks are dropped, and what is stored is digits and nothing else — one
  spelling for the merchant to read off the screen and type into a banking app.
- **Refuse what cannot be an account.** A letter stops the whole string rather
  than being swallowed, and the length is bounded by
  `PORTAL_DESTINATION_MIN_DIGITS`/`MAX_DIGITS` (6–32 by default, deliberately
  loose: an 11-digit mobile wallet and a 16-digit card are both legitimate).

That is a typo guard, not a validation. No checksum covers every Iraqi rail, and
nothing in software can tell a valid account from the wrong person's. So the
last guard is not in the code: the details show the **normalised digits back to
the client, grouped**, before they can submit. That is the final moment at which
a wrong number is still free to fix. The resolver runs the destination *before*
the amount for the same reason — the refusal that matters most is the one the
client should be sent back to first.

### The commission points the other way

Covered under [The amount](#the-amount): deducted from what a withdrawal client
receives, added to what a deposit client transfers. `payloads.converted_iqd`
undoes it with the matching sign, so the breakdown on the request view adds back up to
the figure the client can see in their own bank app.

### The B2CORE debit

Spec §6 puts it inside `under_review`: Finance verifies the client's balance and
eligibility and debits their B2CORE wallet before routing. **The system cannot
do it** — automatic B2CORE transaction creation is out of scope for phase 1
(spec §13) — so, exactly like a deposit's `credit` step, what the software owns
is the *record* that a human did it.

That is carried by the confirmation an operator reads before pressing the
button. `Transition.confirm_by_type` gives two moves a per-direction wording:

- **`review`** on a withdrawal tells the operator to check the balance and take
  the debit in the Back Office first, and says why it happens now — so the
  client cannot trade funds already committed to a withdrawal.
- **`reject`** on a withdrawal tells them to put it back ("any debit reversed",
  spec §6). It is worded conditionally, because a withdrawal rejected at
  `submitted` was never debited, and telling someone to reverse a debit that
  never happened is how a client gets paid twice.

A deposit sees neither. Both are asserted in
`apps/transactions/test_services.py`, so the instruction cannot quietly fall out
of the transition table.

**This timing is still Finance's call to confirm.** Debiting at `under_review`
is what spec §6 describes and what is implemented; if Finance would rather debit
at `assigned`, that is a change to two strings and a source status, not to the
model — `RequestStatus` covers both candidate points either way.

### What the merchant and Finance see

Nothing here was new work: both panels have handled withdrawals since steps 7
and 8, because the lifecycle covers both directions. The merchant's `pay` move
already required proof of transfer, `destination_account` was already on the
whitelist spec §2 allows them, and the queue already filtered by type. Step 10
opened the client end of a path that was otherwise complete.

---

## The request queue, routing and approval

Build-order step 7. Lives at `/finance/requests/` and is where a deposit stops
being a row in a table and starts being work.

### One writer of `Request.status`

`apps/transactions/services.py` holds the whole lifecycle as a table, and
`apply_transition()` is the only code in the project that writes a status. A
status change is never only a status change — it stamps a timestamp, writes an
audit entry, and on a rejection posts the reason into the thread — so putting it
in one place is what stops those three from drifting apart per view.

Each row of the table answers three questions:

| | |
| --- | --- |
| **who** | a permission, not a role, so a `finance_admin` can hand any single step to `finance_staff` without a code change (spec §3) |
| **from where** | the source statuses, so a request cannot skip review, be credited before the merchant confirmed, or be reopened after it closed |
| **for which direction** | deposits and withdrawals share statuses but not paths |

| Action | Deposit | Withdrawal | Permission |
| --- | --- | --- | --- |
| `auto_route` | `submitted` → `assigned`, at submission | — | none; the system |
| `review` | `submitted` → `under_review` | same | `transactions.approve_request` |
| `route` | `under_review`/`assigned`/`pending` → `assigned` | same | `transactions.route_request` |
| `hand_back` | `assigned` → `pending`, by the merchant | same | `transactions.return_request` |
| `park` | anything in flight → `pending`, by Finance | same | `transactions.approve_request` |
| `cancel` | anything in flight → `cancelled` | same | `transactions.cancel_request` |
| `confirm` | `assigned` → `merchant_confirmed` | — | `transactions.confirm_request` |
| `pay` | — | `assigned` → `merchant_paid` | `transactions.confirm_request` |
| `credit` | `merchant_confirmed` → `credited` | — | `transactions.credit_request` |
| `close` | `credited` → `closed` | `merchant_paid` → `closed` | `transactions.close_request` |
| `reject` | anything in flight → `rejected` | same | `transactions.reject_request` |

`confirm` and `pay` are the merchant's own moves. They were defined here before
the merchant panel existed, because they are part of the lifecycle rather than
part of a screen: Finance cannot reach `credited` unless a merchant can reach
`merchant_confirmed`, and the queue's "waiting on the merchant" tab means
nothing without them. **Step 8 added the interface, not the rules** — no row of
this table changed when the panel arrived. The Finance action endpoint refuses
them outright, even for an admin holding every permission, and the merchant
endpoint refuses Finance's moves the same way.

Merchant scoping is enforced on the same path: a merchant may only move a
request routed to *them*. That is an object-level rule no permission can
express, so it is checked in `apply_transition()` against the locked row rather
than left to whichever queryset a screen happens to use.

### Two operators, one request

Every move re-reads its row under `select_for_update()` before deciding
anything. Two staff working the same queue and both pressing "route" is not
hypothetical, and the second press is told the request already moved rather than
silently overwriting the first one's decision. The refusal carries the status it
actually found.

### A deposit routes itself

A deposit does not wait for a desk. The moment it is submitted it is assigned to
the merchant the client already chose, and appears in that merchant's queue.

The reasoning is that the review it used to wait for could not do very much. The
client has already transferred the money into one specific merchant's wallet and
uploaded a receipt for it. Whether that transfer actually arrived is a question
only that merchant can answer — Finance cannot verify a receipt either — so
putting a desk in front of it added a delay without adding a check. Routing
anywhere but to the merchant whose wallet was paid would also be routing the
request away from where the money went.

Finance loses nothing by it:

- the request is in the queue the moment it lands, and the queue opens on
  everything in flight;
- **rerouting stays available** the whole time the merchant has not acted, and
  is the same `route` move it always was;
- rejection stays available from any status in flight;
- and nothing reaches `credited` without Finance's own approval, taken after the
  merchant confirms. The approval moved later in the sequence; it did not go
  away.

**Withdrawals are untouched.** Review is where Finance verifies the client's
balance and takes the B2CORE debit (spec §6), and a merchant asked to pay out
before that is how money leaves twice.

`auto_route` is a row in the transition table rather than three lines in the
submission code, because it needs everything every other move needs: the locked
re-read, the source-status check, the merchant validation, the `assigned_at`
stamp and an audit entry. It runs as `SYSTEM_ACTOR`, a sentinel that skips the
role and permission gates — there is no operator to check them against — and is
audited as `system` with a null actor, because attributing it to the client
would be a lie about who decided.

It is **best effort**. The merchant was offerable when the row drew the
screen and may not be by the time the submission lands: deactivated, method
switched off, wallet retired. The refusal is swallowed, the deposit stays
`submitted`, and it shows up as work waiting on Finance — which is what it now
is. Losing the client's request over it would be the worse failure. Finance
picks it up with `review` and then `route`, the path every deposit used to take.

Two screens learned the same lesson: `under_review` is off the deposit track, in
Finance's `track()` and in the client's timeline both. A deposit that did land
there by hand is shown as still at submission — which is where it is — through
the fallback both already had for an unrecognised status.

### Routing

The merchant dropdown offers only merchants who could actually execute the
request: active, covering its payment method, and — for a deposit — holding an
active wallet. That is the same availability rule the client's own merchant list
uses. `apply_transition()` re-checks it regardless of what the form allowed.

Re-routing is the same move as routing: a request already assigned can be moved
to another merchant while the first has not acted. `merchant_selected` is never
rewritten — the client paid into that merchant's wallet, and overwriting it
would lose where the money actually went. The detail page flags the difference,
and the client is never told about it (`payloads.py` has never disclosed
`merchant_assigned`).

### Rejection is a message, not a field

Spec §6: the reason is posted into the thread. So it is — as a real `Message`
from the rejecting role, which the client reads on the request view. A `rejection_reason`
column nobody renders would not be telling anyone anything.

Every action also takes an optional **internal note**, posted with
`is_internal_note` set. That is how Finance records *why* without telling the
client or the merchant; every client-facing serialiser already filters on that
flag, and there is a test that a note never reaches the client payload.

### Client identity

This is the identity-aware side of the system, and it is gated rather than
assumed. The client panel, the client column in the queue, searching by
client name, and the per-client history page are all behind
`accounts.view_client_identity`; a Finance user without it sees what a merchant
would. Searching is gated too, because a search box that matches on a name is a
way to confirm one without ever displaying it. Searching by *amount* is not
gated — an amount is not identity, and the desk still has to find the request
it is being asked about.

The identity panel is visually marked as the one place client data appears, so
nobody screen-shares it without noticing what is on it.

### Proof files

Finance is already authenticated by a session with a verified second factor, so
the signature on an attachment URL is not what proves who is asking — the view
checks the session, the Finance role and `transactions.view_attachment` on every
hit. What the signature adds is that a *link* stops working: a proof URL pasted
into a chat or left in a browser history is inert within ten minutes, and inert
for anyone but the person it was minted for.

Deliberately a separate salt and a separate holder from the client's URLs in
`apps/portal/attachments.py`. A client token does not open a Finance URL and a
Finance token does not open a client's, even though both name the same file. The
bytes are streamed by `apps/core/attachments.py`, shared by both, so the headers
that stop an upload acting as a page cannot diverge between the two surfaces.

### Wallet daily caps, in dinars

`Wallet.daily_cap` is **Iraqi dinars**, and what is measured against it is
`Request.amount_iqd` — the total the client actually transfers, commission
included, because that is what lands in the account being capped. The day is the
local calendar day in the configured business timezone, not UTC, because a cap
is something Finance and the merchant reconcile against a working day. Rejected
requests are excluded; nothing was ever credited against them.

A request does not point at a `Wallet` — it snapshots the number it was shown
(spec §5) — so consumption is matched the same way the money was: the merchant
the client chose, the method they chose, and the number they were given.

Two refusals, and they are different on purpose:

- **`wallet_cap_reached`** — the wallet has no headroom at all, so no amount
  would fit and the client is sent back to the merchant list.
- **`wallet_cap_exceeded`** — this amount does not fit, and the client is told
  what still does, so they are not left guessing on the next attempt.

The second check runs again inside the submission transaction: the headroom was
measured before the upload was read, and two clients can fill the same wallet in
that gap.

The request detail shows the cap as a bar rather than a figure, because "how
close is this wallet to being unusable today" is a proportion.

### The queue itself

Filters by status, type, method, merchant and date, plus a reference/client
search (spec §9). The status control carries three group values —
`awaiting_finance`, `awaiting_merchant`, `open` — alongside the concrete
statuses, because "what needs me?" is the question a desk actually asks, and the
tabs above the table are that same control rather than a second mechanism.

It opens on everything still in flight. A worklist whose first page is last
month's closed requests is one nobody works from.

Filtering by merchant matches both the merchant the client chose and the one
Finance routed to, since a human asking for "this merchant's requests" means
both and they may differ.

The rail carries a badge of how many requests sit with Finance, and the
dashboard leads with the same counts. Both are server-rendered on load and then
kept current by the ten-second poll — see *Live updates* below — so the numbers
are right on arrival with scripting off and right afterwards with it on.

---

## The merchant panel

Build-order step 8. Lives at `/merchant/`, and is where spec §2 stops being a
principle and becomes code:

> **Merchants never see client identity.** No name, no account number, no email,
> no B2CORE ID. […] This is enforced at the serializer level, not the template
> level. A merchant-scoped API response must never contain client identifying
> fields.

### What a merchant sees

Exactly spec §2's list — reference, type, amount, payment method, wallet,
attachments, message thread — plus the status and its timestamps, because a
worklist without them is not a worklist. A withdrawal adds `destination_account`,
which spec §2 permits explicitly: the merchant cannot pay without it, and it
says nothing about who owns it.

What is left out, and why:

| Not serialized | Because |
| --- | --- |
| `client` | The whole point (spec §2). |
| `merchant_selected` | Which merchant the client originally chose. Finance may route elsewhere (spec §5); that decision is Finance's and does not travel. |
| `rejection_reason` | Not withheld — spec §6 posts it into the thread, which the merchant reads. A second structured copy would be one more thing to keep masked for no gain. |
| `is_internal_note` | Notes Finance writes to itself are filtered out of the thread rather than flagged in it, so the merchant is not even told they exist. |
| Another merchant's messages | A re-routed request carries whatever the first merchant wrote. That is a leak between third parties even with no client named in it, so it does not travel either. |
| `Wallet.created_by`, `Merchant.notes` | Finance's own metadata on the merchant. |

### The limits of anonymity (حدود إخفاء الهوية)

Everything above is about **fields**, and within that scope it is airtight: no
merchant-facing payload in the system can carry a client identifier, and three
independent guards fail closed rather than open if one ever tries.

There is one thing field-level masking cannot reach, and automatic deposit
routing widened it. It is written down here rather than left implicit, because
an unstated limit is one nobody can decide about.

**A merchant sees a deposit's receipt before anyone at MaxiFyFX has looked at
it.** The client uploads proof of transfer at submission; the deposit is routed
to that merchant immediately; the merchant opens the request and the attachment
is there. A bank or wallet receipt commonly shows the sender's name.

So a merchant may learn the name of the person who paid them, from the image,
on a deposit they were going to be handed anyway.

What changed, exactly, is **when** — not whether:

| | Before | Now |
| --- | --- | --- |
| Merchant receives the request | after Finance reviewed and routed | at submission |
| Merchant can open the receipt | yes | yes |
| Finance sees the receipt first | usually | not necessarily |

The receipt was always going to reach the merchant. What Finance used to have
was an opportunity to look at it first, and in practice no policy of acting on
what they saw there — the review neither redacted images nor refused requests
over them. Removing the delay removed an opportunity that was not being used.

**This is an accepted business decision, not an oversight.** It buys the client
a deposit that starts being worked immediately instead of queueing behind a desk.

What it does **not** change, and what is still enforced exactly as before:

- no client identifying **field** reaches a merchant, from any endpoint, in any
  serializer, on any screen — the three guards above are untouched;
- `merchant_selected`, internal notes, another merchant's messages and Finance's
  own metadata all still stop at the boundary;
- the anonymity test suite, including the value sweep over raw response bytes,
  is unchanged and still passes;
- Finance still sees every receipt, and can still reject a request over one at
  any point before it closes.

If the exposure is ever judged too wide, the levers are: hold deposits at
`submitted` for methods whose receipts name the sender, strip the attachment
from the merchant payload until a status is reached, or require Finance to
release the proof separately from the request. None of them is implemented, and
each is a product decision rather than a technical one.

### Three guards, each catching what the last cannot

`apps/merchant_panel/anonymity.py` holds one rule and three places it is
applied. Every one of them **fails closed** — a leak becomes a 500, which is an
incident, rather than a payload, which is a breach.

1. **Class-definition time.** `MerchantSafeSerializer.__init_subclass__` refuses
   to build a serializer that declares an identifying field, that reaches one
   through `source=` (a `reference` sourced from `client.account_number` passes
   every check that looks only at names), or that tries `Meta.fields = "__all__"`
   or `Meta.exclude`. The failure is an import error in the run that introduced
   it, not a name on a merchant's screen in production.
2. **Serialisation time.** Every `MerchantSafeSerializer` walks what it just
   produced. This is what covers `SerializerMethodField` — the one field type
   whose output no static check can predict.
3. **Response time.** The API's base view walks the finished payload once more,
   so anything *not* built by a guarded serializer is covered: the paginator's
   wrapper, DRF's error bodies, any future endpoint returning a hand-built dict.

The check is on **key names**, matched exactly and by substring, so a future
`customerEmail` or `client_reference` is caught without anyone having had to
predict the spelling. It is deliberately not on *values*: the message thread is
free text a merchant is meant to read (spec §5), and a value filter there would
break the feature rather than protect anything. The value sweep lives in the
tests instead, where the fixture's client is seeded with strings that appear
nowhere else in the system.

### Serializer level, not template level — demonstrably

The screens do not render `Request` objects. Every view hands the template the
**dictionary the serializer produced** and puts no model instance in the context
at all, so a template cannot reach `req.client.email`: there is nothing in scope
to reach it through, and the worst a careless template edit can do is print a
key that does not exist.

That is also why the queue paginates by hand instead of through `ListView`,
which helpfully leaves `object_list` and `page_obj.object_list` in the context —
both querysets of live rows, which is precisely the object this step exists to
keep out of reach. A test asserts that no `Request` or `Client` instance appears
in any merchant template context.

### Scope: assigned only

`scoping.py` narrows every screen and every endpoint the same way, so none of
them has to remember to. Only `merchant_assigned` counts — a merchant the client
picked but Finance routed elsewhere has no business with the request and is not
even told it exists. A request that is not theirs is a **404, not a 403**: a 403
would confirm the reference names something real.

`require_merchant()` also refuses a non-merchant role, an account with no
merchant record, and a suspended merchant, and calls the matrix's own
`assert_merchant_anonymity()` on the way through.

### Two surfaces, one scope

| | |
| --- | --- |
| `/merchant/` | The screens: queue, request detail, wallets. Server-rendered from serializer output. |
| `/merchant/api/` | The same data as JSON — read-only. This is what the ten-second poll reads, and what the anonymity tests sweep. |

Writes are not on the API. A lifecycle move is a form post, for the same reason
the Finance panel posts its moves: there is one place that changes
`Request.status`, and giving it two entry points would mean two places to keep
honest.

The rail badge is server-rendered on load, exactly as the Finance panel's is,
and then kept current by the poll — see *Live updates* below.

### A merchant's moves

`confirm` for a deposit, `pay` for a withdrawal, `reject` for either — read from
the shared transition table, so the panel renders exactly the buttons that will
work and the API advertises exactly the same set. Finance's moves are refused
outright.

`pay` requires proof of transfer (spec §6, §8), and requires it to really be an
image or a PDF — the bytes are read, not the declared type. The transition and
the file land in one transaction: a status that says "paid" with nothing behind
it is what Finance would have to chase later.

Rejection needs a reason, which is posted into the thread as a real message.
There is no internal-note field on this panel: an internal note is Finance's
private record, and there is no such thing as a merchant's private note on
somebody else's desk.

### Proof files

A third salt, alongside the client's and Finance's. A token minted on one
surface is inert on the others even though all three name the same stored file,
and the merchant view additionally checks in the database that the file hangs
off a request routed to them. Four things must hold: a live merchant session,
`transactions.view_attachment`, an unexpired token minted for this user, and
ownership of the request. Every failure is the same 404.

### The tests

`apps/merchant_panel/tests/` is where the guarantee is asserted rather than
described:

- **`test_serializers.py`** — the whitelist written out field by field, so
  growing one is a decision somebody makes on purpose; the static guard refusing
  a leaky serializer; the runtime guard catching a method field; and the payload
  itself swept.
- **`test_api.py`** — every merchant-context response swept twice: for
  identifying **keys** anywhere in the JSON, and for the client's identifying
  **values** anywhere in the raw bytes and the headers. Error bodies included,
  because DRF writes those, not us. `SurfaceCoverageTests` enumerates the
  merchant URLconf and fails on any route the sweep does not visit, so a new
  endpoint cannot ship without someone deciding in writing that it is masked.
- **`test_views.py`** — that the template context holds no live rows, that the
  rendered pages carry no identity, that scoping holds, and that each move does
  what the lifecycle says.

Tests that pass because the payload was empty prove nothing, so the sweeps run
against a request that has a thread, an internal note, an attachment, and a
merchant on either side of it.

---

## Message threads

Build-order step 9. An ordinary conversation on every request, open at both
ends: the client writes to the merchant whenever they want, the merchant writes
back whenever they want, and Finance reads everything and writes into the thread
as a third participant.

### Not gated on anything

No status check, and no coupling to a lifecycle move. A question asked after a
request closed is still a question, and refusing it would only move the
conversation to some channel nobody can audit. `messages/` is deliberately not a
route on the transition table for the same reason: a message has no source
status, no target status, and nothing about the request changes because one was
sent.

Both panels' URLconfs put the `messages/` route **ahead** of the action route,
which matches `[a-z_]+` / `<slug:action>` and would otherwise swallow it and
turn a reply into an unknown lifecycle move.

### One writer, one visibility rule

`apps/transactions/messaging.py` is to `Message` what `services.py` is to
`Request.status` — the only place that writes one. Posting a message is never
only inserting a row: it may carry a file, which has to pass the same three
gates every other upload does (spec §11) and land in the same transaction, or
neither should exist. `apply_transition()` posts its rejection reasons and
internal notes through it too, so the invariant is not "almost always".

The other half of the module is who may *read* what, and it lives there because
three surfaces ask the same question and three copies of an answer is three
chances to disagree:

| Audience | Sees |
| --- | --- |
| `finance` | Everything, both kinds of note included. Spec §9 — the desk's notes are theirs to begin with. |
| `client` | Everything except notes of either kind. Senders are labelled by role, so "التاجر" and never which merchant. |
| `merchant` | Everything except **Finance's** notes, except messages written by a *different* merchant, and except *another* merchant's handback note. Their own handback note they read back. |

There are **two kinds of internal note**, told apart by who wrote them rather
than by a second field:

| Kind | Written by | Read by |
| --- | --- | --- |
| **Finance note** | `finance_admin`, `finance_staff`, `system` | Finance only. No merchant sees one, ever, on any request — and none is told one exists. |
| **Handback note** | a merchant, when returning a request | Finance, and the merchant who wrote it. No other merchant, and no client. |

The second kind arrived with the Finance review of 24 Aug 2026 and is the only
note a merchant can write. It exists because handing a request back requires a
reason and that reason is desk business — "no cash today", "I do not trust this
receipt" — which the client has no part in. Its author reads it back because a
mandatory field whose content vanishes from the person who wrote it is a field
nobody trusts; and because a returned request can be routed back to the same
merchant later, when their own earlier note is exactly what they need.

Finance's own notes did not move an inch. The rule that a merchant never sees
one, and is not told one exists, is unchanged and still tested.

That last exclusion is a re-route (spec §5): the request carries whatever the
previous merchant wrote, and handing that to their replacement is a leak between
two third parties even when no client is named in it.

The rule is a whitelist of what each audience may see, not a blacklist of what
to hide, so a sender role added to `ActorRole` later is invisible to everyone
until somebody decides otherwise.

### Attachments, from either side

Same rules as the proof upload: size cap, extension and declared type checked,
and then the **bytes read**, which is what actually decides what the file is. A
PNG renamed to `.pdf` fails, and so does a JSON file claiming to be an image.
Files are served through the same signed, time-limited, per-surface URLs
everything else uses — a client token does not open a merchant URL.

### Masking survives the thread

Spec §2 keeps client identity out of every *field* a merchant receives, and
`apps/merchant_panel/anonymity.py` enforces exactly that — messages and their
nested attachments included, because the guard walks the payload recursively.
`test_threads.py` re-runs the whole `test_api.py` sweep over a thread with one
of everything in it: messages both ways, files both ways, and an internal note
carrying the client's real name. A masking guarantee that only holds for a
request with an empty thread is not a guarantee.

**What masking cannot reach is prose.** A message body is text a merchant is
meant to read (spec §5), so no filter can run over it without breaking the
feature. Two things stand in its place:

- Sender labels are roles, never people. The client is "العميل"; both Finance
  roles collapse to "المالية", because which desk member replied is no more the
  merchant's business than who the client is.
- **A standing reminder on Finance's reply box.** It is always on — a message
  written today is read by whichever merchant is routed tomorrow, so "not
  assigned yet" is not "safe to name them" — and it names the merchant who is
  reading when there is one. It points at the internal-note checkbox as the way
  to say something the merchant should not see, and CSS stands it down while
  that checkbox is ticked rather than removing it, because the box can be
  unticked again.

### The screens

Arabic RTL and mobile-first throughout, on the design system each surface
already uses.

The client's thread is a real conversation view: their own messages sit against
the start edge, everyone else's against the end, with `margin-inline` doing the
sidedness so RTL and LTR both work without a directional property. The composer
is one thumb-reachable row — attach, file name, send — with an autogrowing
textarea, a character counter, and the same size and type checks the server
applies, so an oversized file is refused before it is uploaded rather than
after. A sent message is appended rather than re-fetched, so the thread does not
jump; a refresh re-renders it, and only *opening a different request* clears a
half-typed draft.

Both panels share one `.composer` component that collapses to a single column
under 34rem, because the merchant panel is read on a phone as often as on a
desk. Neither panel gained any JavaScript: they post a form and redirect, like
every other write on them.

Finance's confirmation says who can now read what was just written — the client,
or the client and a named merchant, or nobody outside the desk. "Saved" would
not tell the operator the one thing worth confirming.

---

## Business hours and the countdown

Spec §7: *"outside business hours all submission screens are replaced by a
closed notice with a live countdown to opening."* Everything about *when* is
decided once, in `apps/core/hours.py`, so the portal, the panel and the
submission guard can never disagree about whether the desk is open.

`SystemSettings` (spec §5) holds `open_time`, `close_time`, `timezone`,
`is_open_override` and `closed_message_ar`, as one row that is always `pk=1`.
`hours.evaluate()` turns those into an `Hours` answer: open or not, why, and the
single next moment that changes it.

### Three states, not two

`is_open_override` is nullable on purpose, and the three values are three
different things:

| value | meaning |
| --- | --- |
| `None` | follow the schedule |
| `True` | open regardless of the schedule |
| `False` | closed regardless of the schedule |

Django renders a nullable boolean as *Unknown / Yes / No*, and "Unknown" is not
what an empty override means — a Finance user reading it as "the system does not
know" would be reading it exactly backwards. So `BusinessHoursForm` gives the
three states their own named choices and sets the boolean on save.

**An override does not expire.** It is a switch a human throws and a human has
to throw back. An override with a timer would be a second, invisible schedule,
and the point of the switch is that it is the answer regardless of the schedule.
That also means a forced close has *no countdown*: it reopens when someone says
so, and a countdown to an unknown moment would name a time nobody promised.

### The window may cross midnight

`open_time` later than `close_time` is an overnight shift — 20:00 to 02:00 — and
is a normal configuration, not a swapped pair. Equal times read as
round-the-clock: a desk configured 00:00–00:00 never shuts.

### The countdown is measured by the server's clock

The payload carries `seconds_until_change` alongside the ISO timestamps, and
`static/js/flow.js` anchors to `Date.now()` at the moment the answer arrived
plus that number. A device whose clock is an hour out would otherwise count down
to the wrong minute; only *elapsed* time is read from the device, and elapsed
time is the one thing it gets right.

When the countdown reaches zero the browser does not decide it is open — it asks
again. The server is the only thing that ever answers that question.

### Two enforcement points, not one

* `OptionsView` sends the `hours` block with every catalogue answer and, when
  the desk is shut, **returns nothing else**: no method, no merchant, no wallet
  number, no rate. A wallet number the client cannot pay into tonight is a
  number they should not be looking at.
* `submissions.check_business_hours()` runs first in `build_draft()`, so both
  directions and any future submission surface inherit it — and it runs *before*
  a proof file is read off the wire. A client who kept a form open across
  closing time gets a `portal_closed` refusal, not a request.

The bootstrap page also renders the current state into its config island, so the
notice is up on first paint rather than after the first catalogue call. Spec §7
says the submission screens are *replaced*; a screen that flashes up for half a
second has not been replaced.

### Closing stops new requests. It does not lock anyone out.

A request already filed stays readable, and its thread stays writable — the
conversation is gated on nothing (see *Message threads* above). Only the four
compose screen is replaced. The confirmation screen is not: a client who
submitted at 20:59 and watched the desk close at 21:00 keeps the reference they
were just given.

The client's closed notice repeats their recent requests, because the list
normally lives on the compose screen, which is the screen being replaced — and a
client who cannot file anything tonight is exactly the one who wants to look at
what they filed this morning.

### On the panel

`/finance/hours/` is readable by anyone on the desk and writable with
`core.change_systemsettings`, which a `finance_admin` holds by default and can
delegate (spec §3). A reader gets the settings as text rather than a 403: the
hours govern whether the queue fills at all. Every Finance page carries
`portal_closed` in its context and the rail shows it, because a queue that has
stopped filling looks exactly like a quiet morning.

The panel screen has no JavaScript. The countdown belongs where a client is
waiting for a door to open; here the next change is a timestamp.

---

## Users, roles and the password tool

Build-order step 15. Lives at `/finance/users/`, and exists because spec §3's
"all merchant and staff permissions are granted and revoked by `finance_admin`"
previously meant a shell on the production host and a Django admin screen that
knows nothing about roles, merchant records or second factors.

The screens are thin. Every operation is a call into
`apps/accounts/provisioning.py`, which owns the rules and writes the audit entry
in the same transaction as the change, so an account cannot be created,
disabled, re-roled, re-passworded or have its second factor cleared without a
row in a log nobody can edit or delete (spec §11).

### Three permissions, not one

| Permission | Buys |
| --- | --- |
| `accounts.manage_internal_users` | the panel: create, edit, enable, disable, link a merchant record, issue a password |
| `accounts.manage_permissions` | the permission editor |
| `accounts.reset_user_two_factor` | clearing somebody's OTP devices |

Different blast radii, so they are different gates and any of them can be handed
to a `finance_staff` account without a code change. All three sit in the
`finance_admin` baseline and none is in the merchant one, which
`MERCHANT_FORBIDDEN` enforces for the first two.

### The password is generated, never typed

A password an administrator chooses for somebody else is a password they know,
paste into a chat, and reuse. So there is no password field on the create form.
The system generates one, shows it once, and marks the account
`must_change_password`.

The alphabet leaves out `l`, `1`, `O`, `0` and `I`. That string is read off one
screen and typed into another, sometimes over a phone call, and telling `l` from
`1` is a support ticket waiting to happen. What is left is 55 symbols, so the
20-character default still carries about 115 bits — the readability costs
roughly three bits a character and buys every one of them back in not being
retyped wrong.

Generation is rejection sampling over the whole alphabet rather than
"one of each class, then shuffle". Both produce a valid password; only the first
leaves the distribution uniform, and at twenty characters the loop practically
never runs twice.

**Shown exactly once.** The password goes into the session on the POST that
created it and is *popped* by the view that renders it, so it survives the
redirect and nothing else — not a refresh, not the back button, not another
account's page. There is a test for each of those. Neither the password nor its
hash is ever written to the audit log: the log is read by people, and a hash in
it is a hash to grind offline.

### `must_change_password`, enforced at the choke point

`ForcePasswordChangeMiddleware` refuses to serve a flagged account anything but
the password screen. It is middleware rather than a check in the login view
because there is more than one way in — the two-factor wizard, the admin's own
login, a session that was already open when an administrator reset the password —
and a guard in any one of them leaves the others open.

It runs *after* `EnforceTwoFactorMiddleware`, deliberately: a password being
changed over a session whose second factor was never verified is a password
being changed by whoever holds the password.

The client portal prefix is exempt, like it is for the two-factor gate, so an
internal user logged in in the same browser cannot bounce a *client* out of the
portal.

### Revoking a permission, and why it needed a backend

Granting an extra permission per user is what `user_permissions` already does.
Revoking one it could not, and the gap was real rather than theoretical: Django
unions the user's own permissions with every group's, and `sync_user_role_group`
re-attaches the role group on every save, so anything the role grants comes
straight back. A "revoke" button built on `user_permissions` would have done
nothing at all.

So a refusal is stored explicitly — `User.denied_permissions` — and subtracted
in `ThrottledModelBackend.get_all_permissions`, the one place every `has_perm`
call in the project passes through.

Putting it in the authentication path is safe because it only ever *subtracts*:
the worst a bug there can do is refuse somebody a permission they should have,
which is visible and complained about within the hour. And superusers never
reach it — `PermissionsMixin.has_perm` short circuits for them before any
backend is consulted — so the deny list cannot lock the last administrator out
of the system. Both properties have tests.

The editor is one tri-state control per managed permission: **from the role**
(no override, and the default), **granted**, **denied**. The role's own baseline
is shown beside each one so the effect of an override is visible before it is
saved.

### Guards against locking the product out

`finance_admin` is the only role that can restore any of this, so an
administrator who disables themselves, demotes themselves or revokes their own
management permission has locked the whole product out of being administered,
and the remedy would be the shell this panel exists to make unnecessary.

Refused, therefore: disabling your own account, changing your own role, editing
your own permissions. Renaming yourself is fine — the guard is about power, not
about every field. Another administrator can do any of it to you.

Two more, for different reasons:

- a merchant account cannot be **granted** anything in `MERCHANT_FORBIDDEN`.
  `sync_role_groups` already refuses to build a merchant *group* holding an
  identity permission; this is the same rule for the other route in, an
  administrator granting one to a single account from a form (spec §2).
- an account linked to a merchant record cannot be re-roled out from under it.
  Unlink first, which is a deliberate second decision rather than a silent
  consequence of the first.

### Resetting a second factor

Deletes every OTP device the account holds and records that it happened. The
account is not left unprotected: `EnforceTwoFactorMiddleware` refuses to serve
an internal user with no verified device, so the next login lands in the
enrolment wizard and nothing else is reachable until it is finished.

A password reset deliberately does **not** end the account's open sessions.
Ending them is right for a *compromised* account and wrong for the ordinary case
this serves — somebody who locked themselves out — and the panel cannot tell the
two apart. Disabling the account is the lever for the first, and it is one click
away on the same screen.

### Accounts are disabled, never deleted

`accounts.delete_user` is in `GLOBAL_DENY` and there is no delete route on the
panel, for the same reason the audit log has none: spec §11 wants the trail
intact, and a deleted row breaks every entry that pointed at it.

---

## Corrections from the Finance review (24 Aug 2026)

Phase 1 of `maxpay-finance-feedback-v2.md`. Four items, all inside flows that
already existed.

### The dinar carries no fils

Finance asked for decimals to be removed. The rule chosen — and it was a choice,
so it is written down where it can be argued with:

| | |
| --- | --- |
| **USD** | keeps its cents |
| **IQD amounts** | whole dinars |
| **The rate** | keeps its two places |

The fils is out of circulation, so `1,490.00 د.ع` shows a denomination nobody
can transfer. That is the actual defect and it is entirely on the dinar side.

The cent is *not* in that position. USD is the currency the client's trading
balance is held in and the figure they type; rounding it would silently change
the amount they asked for, and rounding it *down* — the option offered as
"safer for the company" — is only safer on a deposit. On a withdrawal it means
paying the client less than they asked for, which is the company shaving the
client rather than protecting itself.

The rate is a ratio, not an amount. Finance may legitimately set 1470.25, and
rounding it would change every conversion computed from it.

**Where it is applied.** `apps/portal/pricing.py` quantises to the whole dinar,
and that one module backs both the quote on screen and the figures written at
submission — which is what stops the two from disagreeing.

`static/js/flow.js` rounds the same way in the same order, because it draws the
live preview and a preview that rounded differently would show a figure the
submission then contradicts.

### And the transferred figure is rounded to the nearest 1,000

Finance, 15 September 2026, and this one is not about denominations at all.

Every transfer in this system lands in a **personal** wallet or card in Iraq.
A personal account taking 151,847 then 74,312 then 208,655 across a month does
not read as a person being paid; it reads as a business trading through a
personal account, and that is what gets one frozen. Round thousands are what
ordinary transfers between people look like, so that is what these are.

| | |
| --- | --- |
| **USD** | exactly what the client typed — untouched |
| **The transferred IQD** | rounded to the nearest 1,000, both directions |
| **The gap (≤ 500)** | the company's, and a line of its own |

To the *nearest*, not up: always rounding up would quietly overcharge every
deposit and always down would give money away. And in both directions, deposit
and withdrawal alike — the withdrawal is the one that actually arrives in the
client's own account, so if only one were rounded it would have to be that one.

**Who pays for it, and where that shows.** A quote carries four figures, and
each one is exactly one thing:

| | |
| --- | --- |
| `converted_iqd` | `amount × rate`, and nothing is allowed to move it — it is what a client checks against the published rate |
| `commission_iqd` | what the rate prorates. Zero when the rate says zero |
| `rounding_iqd` | the company's own contribution, ≤ 500, either sign. The only one the rate has no say in |
| `total_iqd` | what moves. `converted ± commission + rounding` |

The rounding was first taken **out of the commission**, on the reasoning that
the fee is the company's money and so is the gap. That was wrong, and the way it
was wrong is worth keeping written down: **this desk's commission is normally
zero** — the channel is not a revenue line — and a fee of zero has nothing to
absorb a rounding with. $155 at 1,510 came out as a commission of −50 dinars,
which is not a fee anybody charged. So the adjustment is its own figure now,
kept beside the commission rather than inside it.

**Two rows disappear when they are zero.** A commission of zero and a rounding
that did not happen are not facts about a request, and a row reading `0 د.ع`
only invites a question it has no answer to. Both the live quote and the request
summary omit them.

That took two goes. `flow.js` set `node.hidden = true` correctly and the
JavaScript tests asserted it, and the row stayed on screen anyway: the user
agent's `[hidden] { display: none }` is beaten by any author rule that sets
`display`, and `.quote__row` is a flex row. It had been patched a component at a
time up to then. `system.css` now carries
`[hidden] { display: none !important }` once for every surface, and
`apps/core/test_templates.py::HiddenAttributeTests` keeps it there.

**Recovering the breakdown afterwards.** `payloads.converted_iqd` computes
`amount_usd × rate_applied` rather than undoing the commission. Subtraction was
how it worked and it had two faults the rounding turned from latent into real:
it needed the sign of the direction, which a second caller can get wrong — the
Finance queue kept its own copy and had it backwards for withdrawals — and it
silently swallowed anything else inside `amount_iqd`, which since the rounding is
up to 500 dinars of it. Multiplication needs neither. `payloads.rounding_iqd`
then recovers the adjustment as what the total has left over, so no column had to
be added for it; a request filed before the rule reports zero, which is the truth
about it.

**The arithmetic runs in hundredths, not floats.** `(99.99 / 100) * 5000` is
4999.4999… in binary and `Math.round` took it to 4,999, where `Decimal` makes it
exactly 4,999.5 and rounds to 5,000. Python is the side that charges, so
`flow.js` scales to whole sub-units first and the halves land where `Decimal` has
them.

**It is stored, not formatted.** `Request.amount_iqd` holds the rounded figure,
because it is the number a merchant matches the receipt against and the number
`Wallet.daily_cap` is measured in. A template filter that rounded on the way to
the screen would put a figure in front of the client that no receipt and no cap
agrees with. Corrections go through the same `pricing.price`, so a corrected
request is no less round than a fresh one.

**How the two implementations are held together.** `tests/pricing_cases.json` is
a committed table of 48 cases: both directions, across a zero-commission rate and
a charging one. `apps/portal/tests/test_flow.py::PricingContractTests` asserts
`pricing.price` produces it *and* that the table itself obeys the rule — checked
against the rule rather than against what the code returns, so regenerating it
cannot launder a bug into the contract. `tests/js/flow_quote.test.js` boots the
real `flow.js` against a fake DOM and asserts the quote it paints matches the
same table. Either side drifting fails its own test, and the table says which one
is wrong.

The zero-commission rows are there because that is the desk's real configuration
and the earlier table did not cover it — which is exactly how the −50 got out.

**Display shows up to two places and hides them when zero.** Not exactly zero
places: a request settled *before* this rule may hold a real half-dinar, and
restating it as whole would misstate what actually moved. So the decimals
disappear exactly where they are not real and stay where they are. Nothing
existing was migrated, for the same reason the original rate is kept on an
edit — a settled figure is not rewritten.

### Withdrawals never asked for proof

Item 1.1, and it was recorded here as needing no work — `needs.proof` is `false`
for a withdrawal, `build_withdrawal_draft` never reads one, and
`test_a_proof_is_not_asked_for` asserts a submission without one is accepted.
Two of those three are still true. The claim built on them was not.

**The field was on screen the whole time.** `static/js/flow.js` hides it by
setting the `hidden` attribute, and `.field` sets `display: flex` in
`static/css/flow.css` with no `[hidden]` companion to undo it. An author rule
beats the user agent's `[hidden] { display: none }` whatever its specificity, so
the attribute did nothing and every `show(node, false)` on a field was inert.
The stylesheet carries that companion rule for twelve other components,
including `.field__error` and `.field__review` — the two *children* of the class
that was missing it.

It was never specific to the proof upload. The details are one block serving two
directions (step 10) and the same defect showed `#destination-field`, the
client's card number, on a deposit. One line fixes both.

**And a proof sent anyway was accepted.** Not stored — `build_withdrawal_draft`
genuinely never read `files` — but not refused either, so the request was
created and the file dropped silently. From the client's side that is
indistinguishable from a proof that was kept, which is the worse of the two
failures: they attached evidence, the submission succeeded, and there would be
nothing to produce when a payment was disputed. A guarantee made of *not reading
an input* is a guarantee made of an omission, and this is what that costs. It is
now an explicit refusal (`proof_not_accepted`), ahead of every other field on
that screen: a question that should not have been asked outranks an answer the
client got wrong.

The merchant's own proof upload at `pay` is untouched, which is what spec §6
asks for.

`HidingWhatIsNotCollectedTests` holds the display half. No browser
runs in this suite, so it asserts the contract a browser would enforce: no
element the flow hides by id may carry a class that sets a `display` without a
`[hidden]` rule undoing it. `WithdrawalProofIsRefusedTests` holds the other
half, including that the refusal writes nothing and that the deposit direction
still requires and stores its own proof.

### The client's thread is live

The request view needed the refresh button pressed before a reply appeared. It now polls
on the same ten seconds spec §10 gives the panels, and follows the same three
rules `static/js/panel.js` does — never touch what is being typed, stop when the
tab is hidden, back off quietly on failure.

One thing it needs that the panels do not: a **signature**. The panels ask a
cheap pulse endpoint and only fetch markup when a version token moves. There is
no pulse on the client surface, so the whole payload is fetched and compared
instead, and the DOM is rebuilt only when status, message count, last message
time or attachment count actually differ. Without that, a client reading a long
thread would have their scroll position reset six times a minute.

Verified in a browser rather than by inspection: five polls in nine seconds, a
reply appearing with no interaction, and a half-typed message surviving seven
fetches.

### A wallet can be a code instead of a number

Some rails — Super QI, and any wallet issuing a static QR — are paid by scanning,
not by typing an account.

Two fields, and the split between them is the point:

- `PaymentMethod.requires_wallet_number` — the **method** decides, because it is
  the method that is either paid by number or not.
- `Wallet.qr_image` — the code, stored *alongside* the number rather than
  instead of it. A rail can perfectly well issue a QR and an account, and a
  merchant reconciling wants both.

`Wallet.clean` refuses a wallet that gives the client nothing to pay into, and
refuses a numberless wallet on a method that is paid by number. The form marks
neither field required and leaves both decisions there, so the rule lives in one
place.

The client screen asks the wallet what it has rather than asking the method what
kind it is, so a merchant who adds a QR to a numbered wallet gets both shown
without anything else needing to know.

### The icon and the QR are not the same kind of picture

They were confusable, and the confusion has a cost that runs one way: a client
who points a camera at a brand mark. So they are separated in four places at
once.

| | Payment method icon | Wallet QR |
| --- | --- | --- |
| What it is | a brand mark, identification only | a code to point a camera at |
| Where it renders | 1.9rem beside the method's name in the method column | its own block in the details, up to 16rem, on a white plate |
| Its heading | none; it sits inside the row | an explicit one, and it changes: *scan the QR* on its own, *or scan the QR instead of copying the number* beside a number |
| Where it is uploaded | Payment methods | that merchant's wallet |
| Its route | `portal:method_icon`, cached long | `portal:wallet_qr`, cached 300s, active wallets only |

The field was called `Wallet.image` and is now `Wallet.qr_image` — one letter
away from `PaymentMethod.icon`, with nothing in either name to say which was
which. The payload key moved with it: `wallet.image` is now `wallet.qr`. Both
forms in the Finance panel say plainly what belongs there and what does not, and
each links to the other, because the mistake is symmetric — a QR uploaded as a
method icon is unscannable, and a logo uploaded as a wallet's QR is handed to a
client as something to scan.

The two blocks in the details are two blocks, never one that changes meaning.
The account half keeps the copy button and hides when there is no number — a
button that copies `···` is worse than no button. The QR half is separated by a
rule rather than merely spaced, because a client with both in front of them is
choosing between two ways to pay, and a heading with a line above it is what
makes that a choice rather than a sequence of instructions.

The QR is served by its own route, like the method icon, because `MEDIA_ROOT`
is never mapped to a URL (spec §11). It differs from the icon in two ways: a
shorter cache, and it is served only for a wallet that is still active, so a QR
that was stood down stops working for whoever kept the URL.

---

### Three ways off the track, and they are not each other

Phase 2 of the review. `rejected` used to be the only way a request could end
badly, which meant it was doing three jobs. Now:

| | Means | Ends the request? | The reason goes to |
| --- | --- | --- | --- |
| `pending` | parked, waiting on something | **no** | an internal note |
| `rejected` | somebody looked and said no | yes | the client, in the thread |
| `cancelled` | nobody needs it any more | yes | the client, in the thread |

A desk reconciling a month needs "how many did we turn away" and "how many went
away" to be two numbers rather than one. They are, and in the report both are
counted while neither is added up — no money moved either way, and a total that
included them would overstate every figure being reconciled against. `pending`
*is* added up, because a parked request is still live and dropping it would
understate what the desk is carrying.

**Where a reason lands is the transition's decision, not the form's.**
`Transition.reason_is_note` says whether the mandatory reason is owed to the
client or is desk business. A rejection or a cancellation posts it as an
ordinary message and stores it on `rejection_reason`, which is the column that
says why a request ended. A handback or a park posts it as an internal note and
touches that column not at all — a parked request has not ended, and rendering
"no cash today" to a client as the reason their request stopped would be a lie
about who said it and why.

### Handing a request back

`hand_back` is the merchant's third option and the one they had been missing:
take it, refuse it, or say it is not for them. It parks the request, writes the
mandatory reason as a handback note, and **clears `merchant_assigned`** —
without that last part a returned request stays in the merchant's queue and
stays rejectable by them, which is exactly what handing it back undoes. Who
returned it is in the audit entry and is not lost by clearing it.

Finance then routes it onward or cancels it. Routing from `pending` is the same
`route` move it always was, so a reassignment does not have to be reviewed from
scratch — and the existing rule that a previous merchant's messages do not
travel to their replacement covers the handback note too, which is the most
pointed example of it there is.

---

### Correcting an amount

Phase 3, and the one part of the review with money in it. Finance's case: the
client asks for \$100 and transfers \$60. The request becomes \$60, because that
is what a merchant confirmed and what Finance will credit;
`submitted_amount_usd` keeps the \$100, because *what was asked for* and *what
turned up* are two different questions and a desk reconciling needs both.

Two decisions Finance took by hand, and neither is derivable from the other:

| | |
| --- | --- |
| **The rate does not move** | Repriced at `rate_applied` — the revision this request was quoted on — never at today's. The client transferred against a quoted figure; re-pricing after the fact changes a settled agreement. |
| **The commission does** | It is defined *per 100 dollars*, so 5,000 dinars of fee on a request that turned out to be \$60 is not what that rate says. Re-prorated from the same row. |

The second needed a field. A request stored the commission's *answer*
(`commission_applied`) and not its *rule*, and re-prorating a fee for a new
amount needs the rule — so `commission_rate_applied` now sits beside it, both
snapshots, backfilled for existing rows by dividing the answer back out.

`pricing.price()` was split out of `pricing.quote()` for the same reason: a
correction has no `ExchangeRate` row to hand, only the two numbers the request
snapshotted (spec §5). One implementation, so a corrected request and a fresh
one cannot be priced by two different rules.

**Who, and until when.** Finance and the merchant the request is routed to —
both were asked for by name, and the merchant is the one who watches the money
land. Allowed while the request is live, except once it is `credited`: at that
point the figure here and the figure in the B2CORE Back Office have to agree,
and this system cannot change the one over there. A correction after that is a
Back Office correction first.

It is **not** a lifecycle move and is deliberately not in the transition table:
it changes what a request is worth, not where it is, and those are different
questions of the audit log. It gets its own `AuditAction.AMOUNT_CHANGE`, so
"who moved this along" and "who changed what it is worth" are two filters.

### Timing, attribution, and the title

- `resolved_at` is `closed_at` named for the question Finance asks it — not
  "when was it closed" but "when did this stop being my problem". A parked
  request has not resolved, which is why `pending` is not terminal.
- `elapsed` runs to resolution, or to now while the request is open. Finance's
  example was a client claiming they waited thirty minutes; the second half is
  the more useful one, which is how long the open ones have been waiting.
- `handled_by` is the last internal account to move the request. The audit log
  holds every step; this holds the latest, because a queue cannot be filtered
  and a report cannot be grouped on a log. The system's own `auto_route` sets
  it to nobody — a performance report must not say a person did something
  nobody did.
- `title` stores only an override. Blank means "generate it from the type and
  amount", so a title follows a corrected amount by default and stops following
  it the moment somebody writes their own — both true without a second "is this
  custom?" flag to keep in step.

### The note on hover

3.5, taken from how Finance works in Zendesk today. The queue row carries the
merchant's latest handback note as its `title` attribute, so the daily
reconciliation reads it by pausing the cursor instead of opening dozens of
requests to find the two that have one. A hollow dot marks the rows that have
one — the solid dot beside it already means "unread", and two solid dots would
be two things shouting the same way about different facts.

It is a `Subquery`, not a prefetch: the row needs one string, and a prefetch
would pull every message on every request on the page to find it. And it is an
internal note, so it renders on this surface and nowhere a client or another
merchant reads.

---

### One client, all of them

Phase 4.1. A client rings about a request, and the desk's real question is
rarely about that request — it is *is this the third time this week*. Answering
it meant searching the queue four ways and reading the results by eye, so
`finance:client_history` puts every request one person has filed on one page,
with lifetime totals, and links to it from the client's name wherever it
appears: the queue's client column and the request detail's identity panel.

It is behind `accounts.view_client_identity` and **not merely styled that
way**. Elsewhere that permission subtracts: the queue's client column
disappears, the export drops four columns, and what is left is still a working
screen. Here there is nothing left. A page whose entire subject is *who is this
person* has no masked version, so the view refuses outright — and the links
into it are not rendered for anyone who would be refused.

The rows come from the queue's own `_base_queryset()` and render through the
queue's own row partial, extracted into `finance/_queue_table.html` for this.
A request that looked one way in the worklist and another in the history would
be two renderings of one row, and the second one is always the one nobody
remembers to update. The client column is dropped on this page — every row is
the same person, and the column would carry nothing while pushing the ones that
do off a narrow screen.

The audit log still does not link to it. `accounts.Client` stays out of
`LINKED_TARGETS` for the reason it always did: the viewer's own permission
(`accounts.view_audit_log`) says nothing about identity, and linking to a page
the reader may not be allowed to open is not an improvement on not linking.

### Search, and the one term that is a number

Phase 4.2. Reference and client email were already there. Amount is the
addition, and it is the one Finance actually types — a client quotes "the
hundred dollars from Tuesday" far more often than they read a reference back.

**The account number was there too, and is not any more.** B2CORE's token
carries none — checked against a real one on 4 Sep 2026 — so
`Client.account_number` is blank for every client authenticated since, and the
clause could only ever have matched rows filed before the integration. A search
box that invites an operator to type something it will never find is worse than
one that does not offer it: the operator concludes the request does not exist.
Email is what identifies a client now, and it is the one identity claim B2CORE
does send. The verified `sub` still matches too, for an operator holding a
support ticket.

One box, not four. A search screen that asks *which kind* of thing you are
about to type is a screen that makes the operator do the parsing, so the term
is matched against everything it could plausibly be and skipped for everything
it could not: `MX-0042` is never compared against a numeric column, because
`apps/finance/search.py:as_amount` says it is not a number.

That function is stricter than `Decimal()` is, and deliberately. `Decimal`
accepts `nan`, `Infinity` and `1e40` without complaint; a `numeric(12,2)`
column does not, and comparing against one is an error rather than a miss. It
also refuses three decimal places — rounding a term to find a near miss would
be the search answering a question nobody asked.

An amount search covers **both** the figure a request carries now and the
figure it was submitted with. After a correction those differ, and the person
on the phone will quote the one they typed. That is what keeping the original
was for.

The identity half of the query stays behind `view_client_identity`, unchanged
and for the reason it was written: matching on an email is a way of confirming
an email without ever displaying it (spec §2). Amount search is not — an amount
is not identity — so a Finance user without the permission can still find the
request they are being asked about.

### The history in a report

Phase 4.3, and the smallest of the three because it deliberately builds
nothing. The report gained one dimension, `ReportFilter.client`, and the
history page links to `finance:report?client=…` and `finance:report_export?client=…`.
Both gates that were already there apply unchanged: `transactions.export_reports`
to take a copy out of the building, `accounts.view_client_identity` for the
four identity columns, and one audit entry per export either way.

The field is `HiddenInput`, which is a decision rather than a shortcut. A
select listing every client would be a roster of everyone who has ever used
the portal, rendered on a screen whose own permission is about reports; the way
into this dimension is from a client's history page, where the identity
question has already been answered.

It is dropped from the form entirely for a user without the permission, and
`?client=` from such a user is **refused rather than ignored**. Silently
honouring nothing would hand back every client's rows to somebody who asked for
one client's, which is a worse answer than saying no. A merchant's report form
never had the field, so `ReportFilter.from_form` reads it with `.get` and that
surface is kept out of the dimension by its absence rather than by a check
elsewhere that could be forgotten.

---

### Archiving, and the one thing that is really deleted

Finance could deactivate a merchant or a wallet and never be rid of either. A
merchant who stopped working with the desk two years ago still sat in every
dropdown; a wallet created by a typo sat under its method forever. Deactivation
is a state a thing comes back from, and a list of things nobody intends to come
back to is a list that stops being read.

`apps/merchants/lifecycle.py` holds both operations, because they are the same
question answered two ways: a merchant always has history, so archiving is the
only option; a wallet may or may not, so the question gets asked and the answer
decides.

**Archiving a merchant** stamps `archived_at`/`archived_by` and clears
`is_active` in the same transaction. Both, not either: archiving alone would
leave a merchant invisible everywhere and still routable by anything that only
asked `is_active`, and there are several such places. Their in-flight requests
are deliberately untouched — that is somebody's money, and ending it is
`cancel`'s job, taken per request with a reason the client reads.

Gone means gone. The client's catalogue, `eligible_merchants`, the queue filter,
the report filter, the user-linking form, the dashboard count and Finance's own
merchant list all exclude them, and `_validate_merchant` refuses a route to one
by name — `merchant_archived`, checked ahead of `merchant_inactive` so the
message does not send Finance looking for a switch that will not bring them
back. The catalogue asserts both conditions rather than relying on `is_active`
alone, so clearing one by hand is not enough to bring a merchant back in front
of clients.

The one screen they still appear on is `?status=archived`, asked for by name. An
operation with no way back and nothing to look at is not a retirement, it is a
deletion with extra steps. Restoring takes them out of the archive and
**leaves them deactivated** — coming back and being open for business are two
decisions, and doing the second silently on the strength of somebody undoing a
mis-click is how a merchant ends up in front of clients unintentionally.

**Removing a wallet** is one button and two outcomes:

| | |
| --- | --- |
| No request ever submitted against it | deleted outright |
| Any request was | archived, and gone from every screen including the merchant's own |

The test is `Wallet.was_used`, asked inside the transaction after a row lock,
because a request submitted between the question and the delete is exactly the
race this guards. It reads two eras of data: `Request.wallet`, a `PROTECT`
foreign key added for this, and — for rows filed before that field existed — the
snapshot matched back on number, merchant and method. Fail-closed by
construction, so an unresolvable match archives rather than deletes.

`PROTECT` is the belt and `was_used` is the braces. The database refuses the
delete even if the application ever forgets to ask; the braces are what produce
a sentence the operator can read.

That sentence is the point of the feature as much as the deletion is. The two
outcomes look identical from the screen otherwise, and the operator would have
no way to tell whether the history they may need later still exists. So the
confirmation says both possibilities up front, and the message afterwards says
which one happened and why — *deleted permanently, no request was ever submitted
against it*, or *archived rather than deleted: requests were submitted against
it, and deleting it would break a reference the audit log needs*.

The deletion's audit entry is written **before** the row goes and carries the
whole of it — number, label, merchant, method, whether it had a QR — because in
a moment there will be nothing left to look it up in. It is recorded against the
merchant rather than against a primary key that is about to stop existing, so
the entry still resolves to a page.

Both operations sit behind `merchants.archive_merchants`, which is **not**
`manage_merchants`. The latter is delegable to a `finance_staff` account so they
can add a wallet, and adding a wallet is not the same size of act as retiring
the merchant it belongs to (spec §3). `finance_admin` holds it by virtue of
holding everything; nobody else holds it by default.

---

## Reports and export

Build-order step 16. Two reports over one query: `/finance/reports/` and
`/merchant/reports/`, each with an `.xlsx` export beside it.

Filters are the ones spec §9 lists — date range, status, type, payment method,
merchant — and live in `apps/reports/filters.py`, applied to whatever queryset
the calling surface supplies. That is the whole shape of the shared code: it is
handed a queryset it is allowed to report on and never widens it, so it cannot
leak a row by getting the scope wrong because it never sees the scope.

The date range is on `submitted_at` rather than on whichever timestamp each
status happens to stamp. A report answers "what came in between these dates and
what happened to it", and anchoring to anything later would drop the requests
still in flight — the ones most worth looking at.

### Rejected requests are counted, never added up

They appear in the status breakdown, because "how many did we turn away" is a
real question. They are out of every money total, because no money moved and a
total that includes them overstates every figure a desk would reconcile
against. The screen says so under the filter row; the workbook says so in its
own summary sheet.

### Two gates on the Finance export

| | Needs |
| --- | --- |
| Read the report | what the queue needs |
| Export it | `transactions.export_reports` |
| Client identity columns in the file | `accounts.view_client_identity` |
| Narrow either to one client | `accounts.view_client_identity` |

A report on screen is inside the system. A workbook on somebody's laptop is
outside every control this system has, which is why taking one is its own
permission rather than a consequence of being able to read the numbers. It is
in the `finance_admin` baseline and not the `finance_staff` one, and like every
other permission here it can be delegated without a code change.

The identity gate is the third and it is independent of the other two: a
Finance user without `view_client_identity` exports the same file minus four
columns. Both facts — that an export happened, and whether it carried identity
— go into the audit log, because who took a copy of the client list out of the
building is exactly what spec §11's trail is for.

The merchant export has no extra permission. A merchant exporting their own
worklist *is* the feature, and what protects it is the scope.

### The merchant export, and four layers under it

An export is the one artefact that leaves the building. A screen gets closed, a
JSON response dies with the tab, and a spreadsheet gets forwarded — so spec §2
is enforced on it four times over:

1. `MerchantReportRowSerializer` derives from `MerchantSafeSerializer`, so it
   **cannot be defined** naming an identifying field or reaching one through
   `source=`. That is an `ImportError` at start-up, not a cell in a file.
2. The same base **cannot run** producing one — the output of every field,
   including the method fields, is walked before it is returned.
3. `workbook.build(masked=True)` sweeps the finished rows once more, and
   refuses any *column* whose key is forbidden. It runs before the workbook
   object exists, so a refusal leaves no half-written file.
4. The route is in `SurfaceCoverageTests.SURFACE`, which fails the suite if a
   merchant route is added without being added to the anonymity sweep.

Nothing in the merchant report builds a row by hand. The rows come out of the
same serializers the screens use, which is why layers 1 and 2 apply at all.

### The test opens the file

`apps/reports/tests.py` does not assert on status codes and content types and
call it a day. `MerchantExportContentTests` downloads the workbook, parses it
with openpyxl, and walks **every cell of every sheet** looking for four identity
markers seeded into the fixture and spelled nowhere else in the system — not in
a status label, not in a reference, not in a wallet number, not in a merchant
name. A sweep for them is therefore a genuine test rather than a coincidence.

There is an equality sweep and a substring sweep, because a name concatenated
into a longer string would pass the first. The column headers are checked
separately, and so is the scope: another merchant's reference must not be in the
file, and re-routing a request must take it out of the file.

The same suite checks the file is *usable*, which is a different property from
being safe: amounts arrive as numbers and timestamps as dates, not as text. That
matters because DRF renders a `DecimalField` as a string and a serializer
renders a datetime as an ISO string — right for JSON, and a column nobody can
sum or sort in Excel. Which columns are figures is declared by the column spec's
`number_format`, so a wallet number stays a string and keeps its leading zero.

### Why openpyxl

Chosen over xlsxwriter for one reason: the tests read the file back, and
openpyxl is the library that both writes and reads. A test that can only assert
on the bytes it just produced is a test of nothing.

Written in `write_only` mode — a report over a year of requests streams rows out
rather than building the sheet in memory — which means cells have no addresses
and styling travels with each value instead of being applied to a range
afterwards.

Every workbook has two sheets: **الملخّص**, which states the filter it answers
and the totals, and **الطلبات**, one row per request with the header frozen. The
summary sheet exists because an exported file outlives the screen it came from,
and a spreadsheet of numbers with no record of what was filtered is one somebody
reads the wrong way three months later.

---

## Auditing

`apps/core/services.record_audit()` is the one way to write an audit entry. It
is already wired to logins, failed logins, user changes, permission changes,
merchant changes, wallet changes, rate changes, system-settings changes and
every request submission and lifecycle move.

Failed logins record the submitted username and never the password.

### The viewer

`/finance/audit/` (spec §9), behind `accounts.view_audit_log` — a permission
`MERCHANT_FORBIDDEN` lists, so a merchant account cannot hold it even if someone
edits the matrix to try (spec §2). Filters by action, target type, actor, date
range, and a search across actor, target id and IP.

**It is read-only by construction, and that is a requirement.** Spec §11: *"no
delete or update path exposed anywhere in the application."* So there are four
independent reasons nothing here can write:

1. no form, no action endpoint, no `post()` — `ListView` and `DetailView` answer
   `GET` and return 405 to everything else;
2. `AppendOnlyModel.save()` refuses a second save and `delete()` always raises;
3. `default_permissions = ("add", "view")` means the `change` and `delete`
   permissions **do not exist** to be granted;
4. the admin registration is read-only too.

### Making a snapshot legible

`AuditLog` is written to be durable, not readable: `target_type` is text so an
entry survives the deletion of what it points at, and `before`/`after` are raw
JSON. `apps/finance/audit.py` is the translation layer.

* `diff_rows()` pairs the two snapshots field by field, labels each with the
  model's own Arabic `verbose_name` where the model can still be resolved, and
  marks only the fields that actually moved. A creation marks nothing: every
  field would otherwise be "changed", which tells a reader nothing.
* Keys that are not fields are normal, not errors — `record_audit` callers add
  context like `reason` and `replaced` to say *why* something happened.
* `resolve_targets()` links an entry back to its subject — a request to its
  queue page by reference, a wallet to the merchant that holds it — in one
  query per target type, not one per row.

`accounts.Client` is deliberately absent from that map. It has no panel page,
and resolving one there would put a client's name on a screen whose own
permission says nothing about client identity.

---

## Live updates

Build-order step 13. Spec §10 fixes both the mechanism and its ceiling:

> **In-app**: polling every 10 seconds on merchant and finance panels. No
> WebSocket in phase 1.

So there is no channel layer and no long poll — three ordinary `GET` endpoints
per panel and one 180-line script, `static/js/panel.js`, shared by both.

### Two tiers, because ten seconds is not long

A poll that re-renders is a page render every ten seconds per open tab. So the
thing that runs six times a minute is not a render:

| | |
| --- | --- |
| `pulse` | Counts, an unread total, and a short **version token**. Aggregates only — no serialisation, no template. |
| `rows` / `thread` | HTML fragments, fetched **only when the version token has moved**, and swapped in whole. |

The token is `"<newest activity timestamp>:<row count>"` over the queue *the
caller is actually looking at* — the script sends the page's own query string
back, so a desk filtered to one merchant is not told to refresh because
something moved on a request their filter excludes. The count is in the token
because a request leaving the filtered set moves no timestamp forward but does
change what the screen should show.

### Fragments, not JSON plus a renderer

The fragments are rendered by the same Django templates the full pages
`{% include %}`. That is the whole reason they are HTML: the alternative puts
the queue's markup in two places, and on the merchant panel it puts spec §2's
masking in two places. Nothing in `panel.js` knows what a request looks like —
it finds its work through `data-live-rows` and `data-live-thread` attributes and
paints numbers into whatever declares `data-pulse="counts.awaiting_me"`.

`merchant_panel:queue_rows` and `merchant_panel:request_thread` are swept for
client identity exactly like every other merchant route: a fragment is a
merchant-facing response whatever its content type, and
`SurfaceCoverageTests` fails if a new one is added without being listed.

### The composer is never replaced

The thread refreshes; the reply box beside it does not. Replacing a textarea
somebody is typing into, six times a minute, is a worse feature than no live
thread at all — so the fragment stops at `</ul>` and the form is not in it.

### Unread is per person, and only a person clears it

`apps/transactions/reads.py` answers one question — *has anything happened on
this request since I last looked at it?* — and answers it differently depending
on who is asking:

| audience | what starts the clock | whose messages count |
| --- | --- | --- |
| merchant | being routed the request | everyone else's, internal notes excluded |
| finance | the submission, and the merchant's move | the client's and the merchant's |

Neither is woken by its own writing. A `RequestRead` row is **per user**, not
per role: two Finance staff work the same queue, and a desk-wide marker would
let whoever opened a request first silently clear the badge for everybody.

The marker is written by `get()` on the two detail views and nowhere else.
Deliberately not in `get_context_data` — the thread fragment borrows that
method, six times a minute, and a side effect in a context builder is a side
effect every reuser inherits without meaning to. That exact bug shipped and was
caught by `test_the_thread_fragment_does_not_mark_the_request_read`: the poll
was clearing the unread badge for a tab nobody was looking at.

### What it does when things go wrong

A hidden tab polls nothing and resumes with an immediate tick, so a returning
operator sees current numbers rather than ten-second-old ones. A failed request
doubles the interval up to six minutes and says nothing — the numbers on screen
stay as they were, because this is a badge and not an alarm. A `401` or `403`
stops the poll outright rather than streaming refusals into somebody's log.

`PANEL_POLL_SECONDS` is a setting, read from the page rather than hard-coded, so
a deployment under load can widen it without a code change. A deploy check
complains if it is set below two seconds or above two minutes.

---

## Security hardening

Build-order step 14. Spec §11's list, item by item, with what holds each one.

### The audit log is append-only in the storage engine

Three layers were already inside Django: `AppendOnlyModel` refuses a second
`save()` and any `delete()`, `default_permissions = ("add", "view")` means the
`change` and `delete` permissions **do not exist** to be granted, and nothing in
any URLconf routes a write.

All three are Python, and `QuerySet.update()` and `QuerySet.delete()` call
neither model method. `psql` calls nothing at all. So migration
`core.0002_auditlog_append_only_triggers` adds `BEFORE UPDATE` and
`BEFORE DELETE` triggers, plus `BEFORE TRUNCATE` on PostgreSQL — the one
destructive statement that fires no row-level trigger and exactly what a "let me
just clear this table" reaches for. Both backends are handled, because a
guarantee only tested on the backend nobody runs is not tested.

`manage.py check --database default` asks the database whether the triggers are
still there, rather than asking the migration history whether they were once
installed. A trigger dropped by hand leaves the migration recorded as applied.

### One policy per surface

`apps/core/middleware.py` sets `Content-Security-Policy` for all three surfaces,
because "who may frame this, and what may it load" is one question and answering
it in two modules is how two answers diverge.

| surface | policy |
| --- | --- |
| `/portal/` | `frame-ancestors <B2CORE origin>` (spec §11), and `X-Frame-Options` stripped — `DENY` would win and break the embed. |
| the panels | `default-src 'self'`, `script-src 'self'`, `frame-ancestors 'none'`, plus `nosniff` and `Cross-Origin-Opener-Policy`. |
| `/admin/` | The same, except `script-src` keeps `'unsafe-inline'`. |

`style-src` keeps `'unsafe-inline'` everywhere internal, because the templates
carry `style=""` attributes for row animation delays and the two-factor pages
ship a `<style>` block. That is a real weakening and it is named rather than
hidden: a style attribute cannot execute, and the directive that matters is
`script-src`. The admin's exemption is the honest kind — its widgets ship inline
handlers we do not control, and a policy that breaks the admin is a policy
somebody switches off.

A view that sets its own policy keeps it. The signed attachment views serve
client-uploaded bytes under `default-src 'none'; sandbox`, which the page policy
would otherwise silently widen back out.

### Grinding a password stops working

2FA already makes a stolen password insufficient. What it does not stop is
somebody grinding through passwords against a known email until one works — and
filling the audit log with `login_failed` rows on the way.

`ThrottledModelBackend` is an authentication *backend* rather than a check in a
view, because the two-factor wizard and the admin have different forms and both
end at `authenticate()`. Failures are counted per **(username, IP)** pair:
per-username alone would hand an attacker a way to lock a colleague out of their
own account, which is a denial of service you have gift-wrapped. A locked-out
pair never reaches the password hasher, so the lockout also stops hashing cost
being used as a load amplifier. It fails **open** if the cache is down.

### Backups

`manage.py backup_database` is the one command cron and a person both call, so
the backup taken at 3am is the backup somebody tested. Custom-format `pg_dump`,
timestamped, pruned to `BACKUP_RETENTION_DAYS`, with `PGPASSWORD` in the
subprocess environment and never in `argv`. It refuses SQLite rather than
quietly doing a different thing under the same name.

**It does not encrypt and it does not ship the file anywhere.** A dump on the
same disk as its database is not a backup of anything that takes the disk with
it. Point `BACKUP_DIR` at a mounted volume; a deploy check warns while it is
unset.

### Misconfiguration is loud

`apps/core/checks.py` covers the settings that are silent at run time and
expensive later. The one worth reading twice:

> **`core.W010`** — `CACHES['default']` is per-process. Both rate limiters in
> this project count in the cache. Behind four Gunicorn workers with
> `LocMemCache`, an attacker gets four times the allowance and the login lockout
> resets whenever the balancer picks a different process.

Setting `REDIS_URL` is the fix, and it adds no dependency — Django ships the
Redis backend. The default stays `LocMemCache`, which is right for a laptop and
for the suite: one process, and no Redis to run. `IGNORE_EXCEPTIONS` is on, so
a Redis blip degrades both limiters to fail-open rather than turning into a 500
on an unrelated page.

The rest: an unset or single-day backup directory, `ALLOWED_HOSTS` containing
`*`, the development `SECRET_KEY` reaching a deployment, a poll interval that is
absurd in either direction, and the login lockout having been removed from
`AUTHENTICATION_BACKENDS`.

With `REDIS_URL`, `BACKUP_DIR` and the B2CORE settings in place,
`manage.py check --deploy --database default` is clean under
`config.settings.prod`. That is the bar for a deployment, and it is worth
putting in CI rather than in a runbook.

### Every route has a door

`apps/core/test_routes.py` walks the URLconf and refuses to pass while any route
answers a caller who has not said who they are. Routes are classified in three
lists and **each entry carries its reason**, so a new surface cannot be opened
without somebody writing down why.

The failure this guards is a Finance view shipped without `FinancePanelMixin` —
plausible, silent, and serious, because the Finance panel is the identity-aware
side of the system. It also checks the shape of every refusal: a redirect must
end at the login and must never leave the site on the way, because an open
redirect towards a login form points at the one page a user is primed to trust.

Two routes bounce through another of ours before arriving: `home` sends a caller
to whichever panel is theirs, and the `disable` view sends anyone
without a device to `LOGIN_REDIRECT_URL`. Both are chains, not leaks, and the
test follows them to the end rather than judging the first hop.

`portal:session` is the single declared exception — it answers without a token
because it is how the embed asks *am I signed in?*, and a separate test pins its
whole body to `{"authenticated": false, …}`.

### Still outstanding

Spec §11's last line is not code: **a third-party penetration test before
production rollout**. Nor is "restore tested before go-live" — the command
exists, the restore has not been rehearsed. Both are gates, not features, and
neither is closed.

---

## Notes for the next build steps

- **Nothing has yet been run against PostgreSQL.** Migrations and the whole
  suite have only been exercised on SQLite. Everything used here — the partial
  unique constraint on `Wallet`, the `CheckConstraint` on `PaymentMethod`,
  `JSONField` — is supported by both, but the first PostgreSQL run is still
  unverified. Do that before relying on the wallet constraint in anger.
- **Deposit limits are placeholders.** `PORTAL_DEPOSIT_MIN_USD=1` and
  `PORTAL_DEPOSIT_MAX_USD=100000` only stop a zero and an overflow; the spec
  names no figure. Confirm the real bounds with Finance before go-live.
- **The direction of the conversion is still unconfirmed.** The client enters
  USD and is shown IQD, which is what step 6 built and what spec §7 reads as.
  Finance may prefer the reverse (enter IQD, see USD); `pricing.py` is the only
  place that would change.
- **`Wallet.daily_cap` is now enforced, in Iraqi dinars.** Finance settled the
  unit, so step 7 wired it into the client flow as well as the panel — see the
  cap section above. Two things to confirm before go-live: that the cap is meant
  to count the *total* transferred (commission included) rather than the
  converted amount alone, and that the day should roll at local midnight rather
  than at some agreed cut-off with the merchants.
- **A message body is free text, and merchants read it.** Spec §5 says the
  merchant sees the thread, so the anonymity guard covers field *names* and the
  tests cover values — but nothing can stop a Finance user typing a client's
  name into a reply, or a client naming themselves in their own message. Step 9
  put a standing reminder on Finance's reply box, which is the most a system can
  do about prose. It is worth confirming with Finance that the desk also agrees
  a convention, and worth deciding whether a client naming *themselves* in a
  message to a merchant is acceptable — today it is, and it is the one
  disclosure route the design cannot close.
- **Business hours are one window per day, every day.** Spec §5 gives
  `SystemSettings` a single `open_time`/`close_time` pair, so there is no
  weekend, no public holiday and no per-day schedule. Finance closes for a
  Friday by throwing the manual override, which somebody then has to throw
  back. If the desk keeps different hours on different days, that needs a
  second model, and it is worth settling before go-live rather than after the
  first Eid.
- **The audit log has no export.** Spec §9 asks for a viewer and that is what
  step 12 built. An auditor who wants the log as a file has the Django admin's
  list view and nothing better. If Finance wants CSV it is a small addition, but
  it is also the first path by which the log leaves the system, so it needs a
  decision about who may take one.
- **A permission cannot be revoked from one user.** Spec §3 says a
  `finance_admin` grants *and revokes*; granting works per user
  (`user_permissions`), but revoking does not, because Django has no deny and
  because signing in re-attaches the role group — `update_last_login` fires
  `post_save`, which mirrors `User.role` back into membership. Today a
  permission is withdrawn from a whole role or not at all. Worth settling with
  Finance before the user-management screens in a later step: either narrow the
  `finance_staff` baseline and grant upward per user, or add an explicit
  per-user deny list.
- **A withdrawal's commission is deducted, and that is an inference.** Spec §5
  gives the withdrawal rate a `commission_iqd_per_100usd` and never says which
  way it points. Deducting it from the payout is the only reading in which the
  desk actually collects it — added, the fee would be handed back on the way out
  — so that is what `pricing.quote` does, and the client sees it on screen as
  «العمولة (تُخصم)». Worth one sentence of confirmation from Finance, because
  the alternative is not visibly wrong to anyone reading a single request.
- **Nothing checks the client's balance before a withdrawal is submitted.**
  Spec §6 puts that check on Finance at `under_review`, and there is no B2CORE
  API to do it earlier (spec §13). So a client can submit a withdrawal for more
  than they hold and only be told at review, by a rejection. If Finance wants it
  refused at submission instead, that needs balance access first.
- **A destination account is checked for shape, not for validity.** Digits, and
  a length band wide enough for every Iraqi rail. Nothing tells a valid account
  from the wrong person's, which is why the details read the normalised number
  back to the client before they commit. If Finance wants per-method formats —
  an 11-digit wallet for ZainCash, 16 for a card — `PaymentMethod` would need to
  carry the rule, and a wrong rule refuses legitimate clients.
- **A merchant cannot upload proof outside a `pay`.** Spec §8 lists "upload
  proof (withdrawals)" as an action, and it is wired to the transition that
  needs it rather than offered as a free-standing upload. If Finance wants a
  merchant to be able to add a second document after the fact, that is a small
  addition — but it needs a rule about which statuses still accept one.
- **The postMessage payload shape is assumed, not documented to us.**
  `static/js/embed.js` reads the message name from `type`, `event`, `action` or
  `name`, and the token from `token`, `jwt`, `payload`, `data` or `value`,
  because B2CORE embed builds differ. Narrow it to whatever B2CORE actually
  sends once someone can watch a real frame. The origin check is what guards
  the handshake; this tolerance costs nothing.
- **A real B2CORE token has now been read; the handshake still has not run.**
  On 4 Sep 2026 a live token was examined in the browser, and four things this
  project had assumed turned out to be wrong: the algorithm is `EdDSA` and not
  RS256, there is no `aud` claim at all, `iss` is `api.*` with a path and a
  trailing slash rather than a bare origin, and the name arrives as
  `first_name`/`last_name` with no combined claim. There is no account number
  and no client type. All four are fixed and tested — `B2CoreShapedTokenTests`
  in `apps/portal/tests/test_tokens.py` works from that observed payload, and
  `seed_demo` now stands up a local B2CORE shaped the same way, because a
  stand-in that models a different service than the one it stands in for is
  worse than no stand-in: the suite was green throughout while the integration
  would have refused every real client.

  What is still untested is the *handshake*: the postMessage exchange has never
  run against the real portal, the JWKS fetch is still stubbed in the suite, and
  the message names are still assumed. The token's own shape is no longer a
  guess.
- **Withdrawal debit timing (spec §6) is implemented, not yet confirmed.** Step
  10 debits at `under_review` — what the spec's own table describes, and what
  stops a client trading funds already committed to a withdrawal. The system
  cannot perform the debit (spec §13), so what it owns is the instruction in the
  `review` confirmation and the reversal reminder in the `reject` one. Moving it
  to `assigned` is two strings and a source status; `RequestStatus` covers both
  candidate points either way. Still Finance's call.
- **The rate limiters are cost ceilings, not security controls.** Both the
  portal's submission limiter and the login lockout are fixed windows in the
  cache, and both fail *open* if the cache is down — a broken limiter must not
  take client submissions or the desk offline with it. Behind a proxy the
  portal's is only as good as `X-Forwarded-For`. And with `LocMemCache` neither
  is shared between workers, which is why `core.W010` exists.
- **No penetration test has been run.** Spec §11's last line, and the one item
  on it that no amount of code closes. Same for "restore tested before
  go-live": `manage.py backup_database` exists and has never been restored
  from. Both are go-live gates.
- **The unread badge counts requests, not messages.** Spec §10 asks for a badge
  when something arrives, and a request with three unread messages counts once.
  That is the right grain for a worklist — the badge answers "how many things
  need me" — but if Finance wants a message count instead, `reads.py` is the
  only place that would change.
- **Nothing polls the client's request view.** Spec §10's third bullet says a
  client should see a status change and a new message on the request view; today
  they see both on a manual refresh, and the view has a refresh button for
  exactly that reason. Extending `panel.js` to the embed was deliberately not
  done: the portal is a different surface with a different session and a
  different CSP, and sharing a poller across them would couple the two.
- **`panel.js` has never run in a browser, and the client flow only has on
  localhost.** The suite covers the whole project through the Django test
  client — the queue, the filters, every transition, the refusals, the identity
  gating, the signed URLs, the live fragments and every route's door — and none
  of that runs a line of JavaScript.

  `flow.js` was driven in Chrome against `seed_demo` data on 4 Sep 2026, with
  the compose screen restructure: the row unlocking column by column, an earlier
  answer resetting the later ones, the withdrawal hiding the proof upload
  (`display: none` confirmed on the live node, which is how the `.field[hidden]`
  defect was closed for good), a request submitted end to end to its reference,
  and the row collapsing to one column at 400px. No console errors.

  What that does **not** cover is the host. Nobody has watched these screens
  inside a real B2CORE frame, and three things depend on it specifically: the
  copy button (the host may not grant `clipboard-write`), the frame's height on
  a long request view, and whether `inputmode="numeric"` gets a numeric keypad
  there. And `panel.js` — the poller's visibility pause, its backoff, its
  fragment swap — is still asserted on the server side only: what the endpoints
  return is tested, that the script does the right thing with it is not.
