/* =========================================================================
   The B2CORE handshake, as B2CORE actually speaks it (spec §4)
   -------------------------------------------------------------------------
   Confirmed against a working Max UP embed: the message names, the `token`
   and `expiresAt` fields, the `embed-jwt-token-error` refusal, and the order
   the three outbound steps happen in.

   Run:  node --test tests/js
   ========================================================================= */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { boot, sessionBody } = require("./harness");

const ORIGIN = "https://b2core.example";
const HOUR = 3600;
const NOW_MS = 1_700_000_000_000;
const NOW_S = NOW_MS / 1000;

/** No stored session; the handshake has to ask B2CORE for a token. */
function fresh(overrides = {}) {
  return boot(
    Object.assign(
      { now: NOW_MS, responses: () => ({ status: 200, body: { authenticated: false } }) },
      overrides
    )
  );
}

/** A backend that answers the session probe empty and the exchange with a session. */
function exchanges(body) {
  return (url, init) =>
    (init && init.method) === "POST"
      ? { status: 200, body: body || sessionBody() }
      : { status: 200, body: { authenticated: false } };
}

/* --- order (point 4) ----------------------------------------------------- */

test("the listener is attached before anything is announced", async () => {
  // A token pushed in the same tick as `embed-iframe-ready` must not be lost:
  // the listener exists from the moment the script runs.
  const api = fresh({ responses: exchanges() });
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready"]);
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b", expiresAt: NOW_S + HOUR });
  assert.equal(api.requests.some((r) => r.method === "POST"), true);
});

test("ready goes out before our own backend is consulted, not after", async () => {
  const seen = [];
  const api = boot({
    now: NOW_MS,
    responses: (url, init, frame) => {
      // Whatever the frame has already said to the host by the time our own
      // session probe is issued.
      seen.push(frame.sentTypes().slice());
      return { status: 200, body: { authenticated: false } };
    },
  });
  assert.deepEqual(seen[0], ["embed-iframe-ready"], "ready must precede the session probe");
  await api.settle();
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready", "embed-request-jwt-token"]);
});

test("the three steps happen in the documented order", async () => {
  const api = fresh();
  await api.settle();
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready", "embed-request-jwt-token"]);
});

test("an existing session is adopted without asking for a token", async () => {
  const api = boot({ now: NOW_MS, responses: [{ status: 200, body: sessionBody() }] });
  await api.settle();
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready"]);
  assert.equal(api.state(), "ready");
});

test("a token that arrives while the session probe is in flight wins", async () => {
  // Ready now goes out first, so the host may answer before our own backend
  // does. The probe's answer must not queue a second, pointless request.
  const api = boot({ now: NOW_MS, responses: exchanges() });
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b", expiresAt: NOW_S + HOUR });
  await api.settle();
  assert.equal(api.state(), "ready");
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready"], "no token request after one already landed");
});

/* --- embed-jwt-token-error (point 1) ------------------------------------- */

test("a refusal clears the session at once instead of waiting out the timeout", async () => {
  const api = boot({ now: NOW_MS, responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  assert.equal(api.state(), "ready");

  await api.receive({ type: "embed-jwt-token-error", error: "user_not_authorized" });

  assert.equal(api.state(), "error");
  assert.equal(api.embed().session(), null, "the session must not survive the refusal");
  const deletes = api.requests.filter((r) => r.method === "DELETE");
  assert.equal(deletes.length, 1, "the server session is dropped too, not just the local one");
  assert.match(api.nodes["embed-error-code"].textContent, /token_rejected/);
});

test("a refusal is not reported as a timeout", async () => {
  const api = fresh();
  await api.settle();
  await api.receive({ type: "embed-jwt-token-error", error: "session_expired" });

  assert.equal(api.state(), "error");
  assert.match(api.nodes["embed-error-code"].textContent, /token_rejected: session_expired/);
  // And the pending "B2CORE never answered" timer is gone, so it cannot fire
  // fifteen seconds later and overwrite the real reason with a wrong one.
  assert.equal(api.delays().includes(15000), false);
});

test("a refusal offers a retry and reports itself to step 6", async () => {
  const api = fresh();
  await api.settle();
  await api.receive({ type: "embed-jwt-token-error", reason: "mfa_required" });
  assert.equal(api.nodes["embed-retry"].hidden, false);
  const last = api.notified[api.notified.length - 1];
  assert.equal(last.authenticated, false);
  assert.equal(last.error, "token_rejected");
  assert.equal(last.detail, "mfa_required");
});

test("a refusal with no reason still fails cleanly", async () => {
  const api = fresh();
  await api.settle();
  await api.receive({ type: "embed-jwt-token-error" });
  assert.equal(api.state(), "error");
  assert.equal(api.nodes["embed-error-code"].textContent, "(token_rejected)");
});

test("a refusal during a renewal ends the live session", async () => {
  const api = boot({ now: NOW_MS, responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  await api.fire();                       // the renewal timer
  assert.equal(api.state(), "ready", "a renewal is silent while the session still stands");
  await api.receive({ type: "embed-jwt-token-error", error: "revoked" });
  assert.equal(api.embed().session(), null);
  assert.equal(api.state(), "error");
});

/* --- expiresAt (point 2) -------------------------------------------------- */

test("renewal is scheduled from expiresAt, a margin before the hour is up", async () => {
  const api = fresh({ responses: exchanges() });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b", expiresAt: NOW_S + HOUR });

  assert.equal(api.state(), "ready");
  const expected = (HOUR - 90) * 1000;
  assert.equal(
    api.delays().includes(expected),
    true,
    "expected a renewal " + expected + "ms out, got " + api.delays()
  );
});

test("expiresAt is read in milliseconds as well as seconds", async () => {
  const api = fresh({ responses: exchanges() });
  await api.settle();
  await api.receive({
    type: "embed-jwt-token",
    payload: { token: "jwt.a.b", expiresAt: (NOW_S + HOUR) * 1000 },
  });
  assert.equal(api.delays().includes((HOUR - 90) * 1000), true);
});

test("expiresAt is read as an ISO-8601 string", async () => {
  const api = fresh({ responses: exchanges() });
  await api.settle();
  const iso = new Date((NOW_S + HOUR) * 1000).toISOString();
  await api.receive({ type: "embed-jwt-token", data: { token: "jwt.a.b", expiresAt: iso } });
  assert.equal(api.delays().includes((HOUR - 90) * 1000), true);
});

test("the sooner of the token and the session decides the renewal", async () => {
  // Our backend clamps the session to its own ceiling. Renewing against the
  // token alone would leave the client on a dead session for the difference.
  const api = fresh({ responses: exchanges(sessionBody({ expires_at: NOW_S + 600 })) });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b", expiresAt: NOW_S + HOUR });
  assert.equal(api.delays().includes((600 - 90) * 1000), true, "got " + api.delays());
});

test("a renewal asks B2CORE again without disturbing the client", async () => {
  const api = fresh({ responses: exchanges() });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b", expiresAt: NOW_S + HOUR });
  api.sent.length = 0;

  await api.fire((HOUR - 90) * 1000);

  assert.deepEqual(api.sentTypes(), ["embed-request-jwt-token"]);
  assert.equal(api.state(), "ready", "the stage must not fall back to a loading screen mid-task");
});

test("a token with no expiresAt still leaves the session usable", async () => {
  const api = fresh({ responses: exchanges() });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" });
  assert.equal(api.state(), "ready");
  // The backend's own expiry carries the renewal instead.
  assert.equal(api.delays().includes((HOUR - 90) * 1000), true);
});

test("an expiry already past schedules nothing rather than looping", async () => {
  const api = fresh({ responses: exchanges(sessionBody({ expires_at: NOW_S - 10 })) });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b", expiresAt: NOW_S - 10 });
  assert.deepEqual(api.delays(), []);
});

/* --- event.source (point 3) ---------------------------------------------- */

test("a message from the right origin but the wrong window is ignored", async () => {
  // A second B2CORE tab, a popup it opened, a frame it nests: same origin,
  // different window. Only the window framing this page may speak.
  const api = fresh();
  await api.settle();
  const before = api.requests.length;

  await api.receive(
    { type: "embed-jwt-token", token: "attacker.jwt", expiresAt: NOW_S + HOUR },
    { source: { name: "some other window" } }
  );

  assert.equal(api.requests.length, before, "no token was exchanged");
  assert.equal(api.embed().session(), null);
});

test("a logout from the wrong window cannot end the session", async () => {
  const api = boot({ now: NOW_MS, responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  await api.receive({ type: "embed-logout" }, { source: {} });
  assert.equal(api.state(), "ready");
  assert.notEqual(api.embed().session(), null);
});

test("a refusal from the wrong window cannot end the session", async () => {
  const api = boot({ now: NOW_MS, responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  await api.receive({ type: "embed-jwt-token-error", error: "spoofed" }, { source: {} });
  assert.equal(api.state(), "ready");
  assert.notEqual(api.embed().session(), null);
});

test("the origin check still stands on its own", async () => {
  const api = fresh();
  await api.settle();
  const before = api.requests.length;
  await api.receive(
    { type: "embed-jwt-token", token: "attacker.jwt" },
    { origin: "https://b2core.example.evil" }
  );
  assert.equal(api.requests.length, before);
});

/* --- what was already true, and must stay true --------------------------- */

test("a token request is addressed to B2CORE, never broadcast", async () => {
  const api = fresh();
  await api.settle();
  assert.equal(api.sent.every((s) => s.target === ORIGIN), true);
});

test("a token that never arrives is still reported as a timeout", async () => {
  const api = fresh();
  await api.settle();
  await api.fire(15000);
  assert.equal(api.state(), "error");
  assert.match(api.nodes["embed-error-code"].textContent, /token_timeout/);
});

test("a message with no token at all is a missing token, not a refusal", async () => {
  const api = fresh();
  await api.settle();
  await api.receive({ type: "embed-jwt-token", expiresAt: NOW_S + HOUR });
  assert.match(api.nodes["embed-error-code"].textContent, /missing_token/);
});

test("logout ends the session and says so", async () => {
  const api = boot({ now: NOW_MS, responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  await api.receive({ type: "embed-logout" });
  assert.equal(api.state(), "logged-out");
  assert.equal(api.embed().session(), null);
  assert.equal(api.delays().length, 0, "a logged-out session must not renew itself");
});

test("nothing is announced when the page is not framed", async () => {
  const api = boot({ now: NOW_MS, framed: false });
  await api.settle();
  assert.equal(api.state(), "standalone");
  assert.deepEqual(api.sentTypes(), []);
});

test("an unconfigured server does not start the handshake", async () => {
  const api = boot({ now: NOW_MS, state: "unconfigured" });
  await api.settle();
  assert.deepEqual(api.sentTypes(), []);
  assert.equal(api.requests.length, 0);
});
