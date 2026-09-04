# MaxPay Portal — Finance Review Feedback (24 Aug 2026)

Twelve requests from the Finance Manager, grouped into four build phases.

---

## Decisions needed before building

**1. Rounding rule.** Finance asked to remove decimal amounts. Which rule?
- Round to the nearest whole dollar
- Round down always (safer for the company)
- Keep cents on USD, round only the IQD figure

**2. Amount editing and re-pricing.** When Finance or a merchant edits an amount, does the original snapshotted rate stay, or is the request re-priced at today's rate? The rate is currently frozen at submission by design. **Recommendation: keep the original rate.** The client transferred against a quoted figure; re-pricing after the fact changes a settled agreement.

**3. Merchant cancellation model.** Finance described two options and did not pick one:
- (a) Merchant requests cancellation, Finance approves or refuses
- (b) Merchant sends back to Finance as pending with an internal note, Finance reassigns or cancels

**Recommendation: (b).** It matches the existing lifecycle, needs no approval sub-state, and Finance keeps the decision either way.

**4. Client history and anonymity.** Finance wants full per-client history. **This must be Finance-only.** A merchant seeing a client's history would defeat the whole anonymity design, since patterns across requests identify a person even without a name.

---

## Phase 1 — Corrections to existing flows

Small, self-contained, no new surfaces.

**1.1 Remove proof of payment from client withdrawal.** The client does not pay in a withdrawal; the merchant does. The field is wrong there. The merchant's proof upload stays.

**1.2 Remove decimal amounts.** Apply the agreed rounding rule at quote, at submission, and in every display. Currently `1,490.00 د.ع` shows cents that do not exist in the currency.

**1.3 Live message refresh.** Threads currently need a page refresh. They must poll like the queues do.

**1.4 QR code or image per wallet.** Some methods (Super QI) are paid by scanning a code, not by typing a number. A wallet must be able to carry an image, and a method must be able to offer an image with no wallet number at all. The client sees the image where the number is shown today.

---

## Phase 2 — Merchant return and cancellation

**2.1 Merchant sends a request back to Finance** with a mandatory internal note, without cancelling it. The request returns to the Finance queue for reassignment or cancellation.

**2.2 Finance cancels a request outright**, distinct from rejecting it. Rejection means the request failed; cancellation means it was abandoned. Both need a reason and both are audited.

**2.3 Reassignment to another merchant** after a return. The previous merchant's messages do not travel to the replacement, matching the existing rule.

**2.4 An explicit pending state.** Distinct from rejected and from cancelled. A pending request is parked and waiting on something — it has not failed and has not been abandoned. Finance names the reason.

---

## Phase 3 — Amount editing, timing, and operator attribution

**3.1 Editing the amount.** Real case: the client requests $100 and transfers $60. Both Finance and the assigned merchant can correct the amount to what actually arrived. Every edit records the old value, the new value, who changed it, and when. The original submitted amount is never overwritten — it is kept alongside.

**3.2 Timing fields on every request.** Submitted at, last updated at, resolved at, and elapsed duration. Finance's example: a client claims they waited thirty minutes; the record must settle it.

**3.3 Operator attribution.** Which user handled each request, visible on the request, filterable in the queue, and included in reports. Finance was explicit that this is for reviewing staff and merchant performance, not only for audit.

**3.4 Sort by resolution date**, ascending and descending, for daily reconciliation against B2CORE.

**3.5 Merchant note visible on hover.** When a merchant leaves a note on a request, Finance sees it by hovering over the queue row, without opening the request. Taken directly from how Finance works in Zendesk today, and it saves opening dozens of requests during the daily reconciliation.

**3.6 Editable request title.** Each request carries a title generated from its type and amount. When the amount is corrected, the title follows. Finance reads the queue by title during reconciliation, so a request still titled "Deposit $100" when $60 arrived means reconciling against the wrong figure.

---

## Phase 4 — Client history and search

**4.1 Per-client history.** Every request by the same client, in one place, reachable from any of their requests. Finance-only, behind `view_client_identity`.

**4.2 Search.** By request reference, client email, account number, and amount. Finance-only.

**4.3 History in reports.** Client history must be exportable within the existing export gates — the identity columns already depend on `view_client_identity`, so this follows the same rule.

---

## Explicitly confirmed as correct

- Merchants never see withdrawal requests until Finance routes them
- Deposits go straight to the assigned merchant
- Internal notes stay invisible to both client and merchant
- No opening hours restriction on threads
