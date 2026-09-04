# MaxPay Portal — Technical Specification

**Project:** In-house deposit & withdrawal system for MaxiFyFX
**Reference:** MAXMANAG-212
**Stack:** Django 5 + DRF, PostgreSQL, Arabic RTL UI
**Date:** 18 August 2026

---

## 1. Purpose

Replace the current Zendesk-based local deposit and withdrawal process with a purpose-built system. Clients submit requests from inside their B2CORE portal, Finance routes each request to a merchant, and the merchant executes it without ever seeing who the client is.

## 2. Core design principle — client anonymity

**Merchants never see client identity.** No name, no account number, no email, no B2CORE ID.

A merchant sees only: request reference, type, amount, payment method, wallet used, attachments, and the message thread. In withdrawals the merchant additionally sees the destination card or wallet number, because it is required to execute the payment, but never the identity of its owner.

This is enforced at the serializer level, not the template level. A merchant-scoped API response must never contain client identifying fields.

**Rationale:** protects client data from third parties and prevents merchants from building direct relationships with, or poaching, MaxiFyFX clients.

## 3. Roles

| Role | Description |
| --- | --- |
| `finance_admin` | Root role. Creates all other users, assigns roles and permissions, manages merchants, wallets, exchange rates, business hours, and payment methods. Full visibility. |
| `finance_staff` | Handles the request queue, reviews submissions, routes requests to merchants, approves and rejects, supervises all conversations. Permissions granted by `finance_admin`. |
| `merchant` | Sees only requests assigned to them, with client identity masked. Confirms execution, uploads proof for withdrawals, participates in the request thread. |
| `client` | Authenticated via B2CORE. Submits requests, uploads proof, chats, views own request history. |

All merchant and staff permissions are granted and revoked by `finance_admin`. There is no self-registration for any internal role.

## 4. Authentication

### Client
Identity comes from B2CORE via the documented iframe `postMessage` protocol:

1. App loads inside the B2CORE iframe and sends `embed-iframe-ready`.
2. App sends `embed-request-jwt-token`; B2CORE replies with `embed-jwt-token`.
3. Backend verifies the JWT signature against B2CORE's JWKS endpoint (`https://api.<domain>/.well-known/jwks.json`).
4. The verified subject claim maps to a local `Client` record, created on first use.
5. On `embed-logout`, the session is cleared and any cached token discarded.
6. Theme and language follow `embed-theme-change` and `embed-language-change`.

**Never trust a client-supplied account number.** Client identity is derived from the verified token only.

### Internal users
Standard Django session authentication with mandatory 2FA for `finance_admin` and `finance_staff`.

## 5. Data model

### `PaymentMethod`
`code`, `caption_ar`, `caption_en`, `icon`, `supports_deposit`, `supports_withdrawal`, `requires_wallet_number`, `is_active`, `sort_order`
`icon` is a small identifying mark shown beside the method's name. It is never a code to scan.

### `Merchant`
`name`, `is_active`, `user` (OneToOne to internal user), `notes`, `archived_at`, `archived_by`
Archiving retires a merchant from every list and from the client's choice. Their existing requests are untouched and no new one is routed to them. Merchants are never deleted — the audit log needs the reference.

### `MerchantMethod`
`merchant` (FK), `payment_method` (FK), `is_active`
Defines which methods each merchant covers. Unique together.

### `Wallet`
`merchant_method` (FK), `number`, `qr_image`, `label`, `is_active`, `daily_cap` (nullable), `created_by`, `deactivated_at`, `archived_at`, `archived_by`
`qr_image` is the scannable code, shown large on screen 4 under a heading of its own. A wallet may carry a number, a QR, or both; it may not carry neither.
A wallet no request was ever submitted against is deleted outright; one that was used is archived and disappears from every screen. Which happened is reported to the operator.
Only one wallet per `merchant_method` may be active at a time — enforced in the model's save logic.

### `ExchangeRate`
`rate_type` (`deposit` / `withdrawal`), `iqd_per_usd`, `commission_iqd_per_100usd`, `effective_from`, `set_by`
Never updated in place. Each change creates a new row, preserving history.

### `Request`
| Field | Notes |
| --- | --- |
| `public_ref` | Short human-readable reference, e.g. `MP-24817`. This is the only identifier a merchant sees. |
| `type` | `deposit` / `withdrawal` |
| `client` | FK — **never exposed to merchants** |
| `payment_method` | FK |
| `merchant_selected` | Merchant chosen by the client |
| `merchant_assigned` | Merchant actually routed by Finance — may differ |
| `wallet_number_snapshot` | Wallet number as shown at submission time, stored as text |
| `destination_account` | Withdrawal only — client's card or wallet number |
| `amount_usd`, `amount_iqd` | |
| `rate_applied`, `commission_applied` | Snapshotted at submission |
| `status` | See section 6 |
| `rejection_reason` | |
| Timestamps | `submitted_at`, `assigned_at`, `merchant_actioned_at`, `closed_at` |

Rate and wallet number are **snapshotted at submission**. Later changes to rates or wallets never alter an existing request.

### `Attachment`
`request` (FK), `file`, `uploaded_by_role`, `uploaded_at`
Stored with signed URLs, not public paths. Type and size validated on upload.

### `Message`
`request` (FK), `sender_role`, `sender_id`, `body`, `attachment` (nullable), `created_at`
Merchant-scoped serialization labels the client as "العميل" with no identifying data.

### `AuditLog`
`actor`, `action`, `target_type`, `target_id`, `before`, `after`, `ip`, `created_at`
Every status change, wallet change, rate change, and permission change is logged. Append-only.

### `SystemSettings`
`open_time`, `close_time`, `timezone`, `is_open_override`, `closed_message_ar`

## 6. Request lifecycle

### Deposit

| Status | Actor | Action |
| --- | --- | --- |
| `submitted` | Client | Selects merchant → selects one of that merchant's methods → sees active wallet number with copy action, or the wallet's QR to scan → enters amount → uploads proof → optional message → submits |
| `under_review` | Finance | Reviews submission and proof |
| `assigned` | Finance | Routes to a merchant. Merchant is notified in their panel. |
| `merchant_confirmed` | Merchant | Confirms funds received, marks as done. Finance is notified. |
| `credited` | Finance | Records the deposit manually in the B2CORE Back Office |
| `closed` | System | Request closed |
| `rejected` | Finance or Merchant | Rejection reason posted as a message in the thread |

### Withdrawal

| Status | Actor | Action |
| --- | --- | --- |
| `submitted` | Client | Selects merchant → selects one of that merchant's methods → enters destination card/wallet number → enters amount → optional message → submits |
| `under_review` | Finance | Verifies client balance and eligibility, debits the client's B2CORE wallet |
| `assigned` | Finance | Routes to a merchant |
| `merchant_paid` | Merchant | Pays the client, uploads proof of transfer |
| `closed` | Finance | Confirms and closes |
| `rejected` | Finance or Merchant | Reason posted in the thread; any debit reversed |

**Open decision:** the exact point at which the client's B2CORE balance is debited for a withdrawal. Debiting at `under_review` prevents the client from trading funds already committed to a withdrawal. Confirm with Finance before implementation.

## 7. Client flow — screens

1. **Entry** — deposit or withdrawal
2. **Merchant** — only merchants who can take this direction, each with a count of what they cover
3. **Payment method** — only the methods *that merchant* covers, each with a small identifying icon beside its name
4. **Details**
   - Deposit: active wallet number displayed prominently with a one-tap copy button, amount input showing live IQD/USD conversion using the snapshotted rate, proof upload, optional message
   - Withdrawal: destination account input, amount input with conversion, optional message
5. **Confirmation** — request reference displayed
6. **Request view** — status timeline, message thread, attachments

Outside business hours all submission screens are replaced by a closed notice with a live countdown to opening.

## 8. Merchant panel

- Queue of assigned requests only, auto-refreshing via polling every 10 seconds
- Request detail: reference, type, amount, method, wallet, attachments, message thread
- **No client identity anywhere in the interface or the API responses backing it**
- Actions: confirm execution, upload proof (withdrawals), send message, reject with reason
- Wallet visibility: read-only view of own wallets and their active status

## 9. Finance panel

- Full request queue with filters by status, type, method, merchant, and date
- Full request detail including client identity
- Actions: route to merchant, approve, reject, message, close
- Merchant management: create merchants, assign methods, add and edit wallets, activate and deactivate, archive and restore
- Archiving a merchant and removing a wallet sit behind `archive_merchants`, separate from `manage_merchants`
- Rate management: set deposit and withdrawal rates and commission; changes apply to new requests only
- Business hours configuration
- User and role management (`finance_admin` only)
- Full read access to every message thread
- Audit log viewer

## 10. Notifications

- **In-app**: polling every 10 seconds on merchant and finance panels. No WebSocket in phase 1.
- **Merchant**: new assigned request appears without page refresh, with an unread badge.
- **Finance**: new submission and merchant confirmation both raise a badge.
- **Client**: status change and new message visible on the request view.

## 11. Security requirements

- All merchant-facing serializers explicitly whitelist fields. Client identity fields must never be serializable in a merchant context.
- JWT signature verified against JWKS on every request. No trust in client-supplied identifiers.
- Uploaded files: extension and MIME validated, size capped, stored outside the web root, served through signed time-limited URLs.
- Rate limiting on submission endpoints.
- All internal accounts require 2FA.
- Audit log is append-only, with no delete or update path exposed anywhere in the application.
- HTTPS enforced. `Content-Security-Policy: frame-ancestors` set to the B2CORE origin only.
- Daily automated database backups with restore tested before go-live.
- Third-party penetration test before production rollout.

## 12. Build order

1. Project scaffold, settings, PostgreSQL, base models, admin
2. Roles and permissions, internal auth with 2FA
3. Merchant, method, and wallet management in the Finance panel
4. Exchange rate management with history
5. B2CORE JWT verification and client session handling
6. Client deposit flow, end to end
7. Finance queue, routing, and approval
8. Merchant panel with masked serializers
9. Message threads with Finance supervision
10. Withdrawal flow
11. Business hours and countdown
12. Audit log and its viewer
13. Polling-based live updates
14. Security hardening and test suite

Steps 1 to 7 constitute a working core. Everything after that is additive.

## 13. Out of scope — phase 1

- General support ticketing beyond deposit and withdrawal threads
- USDT rails
- Automatic B2CORE transaction creation (pending API access confirmation)
- Automatic merchant routing rules
- Mobile app integration
- Automated reconciliation with merchant statements
