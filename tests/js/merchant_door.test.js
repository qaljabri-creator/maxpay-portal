/* =========================================================================
   The merchant panel's door, as the frame runs it
   -------------------------------------------------------------------------
   Same protocol as the client portal's handshake — the names, the shapes and
   the refusal are B2CORE's and are tested against that in
   embed_handshake.test.js. What is tested here is what this page does
   *differently*, which is everything after the token is accepted, plus the
   one class of failure the portal does not have: a token that is perfectly
   valid and still does not open anything, because the person holding it is
   not a merchant.

   Run:  node --test tests/js
   ========================================================================= */

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { boot, sessionBody } = require("./merchant_harness");

const ORIGIN = "https://b2core.example";

/** A backend with no session, answering the exchange with `body`. */
function exchanges(body, status) {
  return (url, init) =>
    (init && init.method) === "POST"
      ? { status: status || 200, body: body || sessionBody() }
      : { status: 200, body: { authenticated: false } };
}

/* --- the ordinary path --------------------------------------------------- */

test("the frame announces itself before consulting our own backend", async () => {
  const api = boot({ responses: exchanges() });
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready"]);
  await api.settle();
  // Only after the ready does it ask for a token, and only because the probe
  // came back empty.
  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready", "embed-request-jwt-token"]);
});

test("a token opens a session and the door replaces itself with the panel", async () => {
  const api = boot({ responses: exchanges() });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" });

  const post = api.requests.find((r) => r.method === "POST");
  assert.equal(post.url, "/merchant/session/");
  assert.deepEqual(JSON.parse(post.init.body), { token: "jwt.a.b" });
  // `replace`, not `assign`: a back gesture must not land on a spent door.
  assert.deepEqual(api.replaced, ["/merchant/"]);
});

test("an existing session skips the token round trip entirely", async () => {
  const api = boot({ responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();

  assert.deepEqual(api.sentTypes(), ["embed-iframe-ready"]);
  assert.equal(api.requests.filter((r) => r.method === "POST").length, 0);
  assert.deepEqual(api.replaced, ["/merchant/"]);
});

test("the session cookie is asked for by name on every call", async () => {
  const api = boot({ responses: exchanges() });
  await api.settle();
  // A default of "omit" would send every call without the merchant session.
  assert.equal(api.requests[0].init.credentials, "same-origin");
});

/* --- the refusal the portal does not have -------------------------------- */

test("a valid token that belongs to no merchant is not dressed up as expiry", async () => {
  const api = boot({
    responses: exchanges(
      { error: "not_a_merchant", remedy: "contact_support" },
      403
    ),
  });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" });

  assert.equal(api.state(), "error");
  assert.match(api.errorText(), /لوحة التاجر متاحة لحسابات التجار فقط/);
  // Nothing was opened, and nothing invites a retry that cannot succeed.
  assert.deepEqual(api.replaced, []);
  assert.equal(api.nodes["embed-retry"].hidden, true);
});

test("a suspended merchant is told to talk to finance, not to log in again", async () => {
  const api = boot({
    responses: exchanges({ error: "merchant_suspended", remedy: "contact_support" }, 403),
  });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" });

  assert.match(api.errorText(), /موقوف/);
  assert.equal(api.nodes["embed-retry"].hidden, true);
});

test("an expired B2CORE session does invite a retry", async () => {
  const api = boot({
    responses: exchanges({ error: "invalid_token", remedy: "reauthenticate" }, 401),
  });
  await api.settle();
  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" });

  assert.equal(api.nodes["embed-retry"].hidden, false);
});

/* --- what may speak to this page ----------------------------------------- */

test("a message from another origin is ignored outright", async () => {
  const api = boot({ responses: exchanges() });
  await api.settle();
  const before = api.requests.length;

  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" }, { origin: "https://evil.example" });

  assert.equal(api.requests.length, before);
  assert.deepEqual(api.replaced, []);
});

test("a message from the right origin but the wrong window is ignored", async () => {
  // A second B2CORE tab, a popup it opened, a frame it hosts: same origin,
  // not our parent. Only `window.parent` may hand this page a token.
  const api = boot({ responses: exchanges() });
  await api.settle();
  const before = api.requests.length;

  await api.receive(
    { type: "embed-jwt-token", token: "jwt.a.b" },
    { source: { postMessage() {} } }
  );

  assert.equal(api.requests.length, before);
  assert.deepEqual(api.replaced, []);
});

test("the token request names the host rather than being broadcast", async () => {
  const api = boot({ responses: exchanges() });
  await api.settle();

  for (const message of api.sent) {
    assert.equal(message.target, ORIGIN);
  }
});

/* --- the host changes its mind ------------------------------------------- */

test("a refusal from B2CORE is reported at once, not after the timeout", async () => {
  const api = boot({ responses: exchanges() });
  await api.settle();
  await api.receive({ type: "embed-jwt-token-error", error: "sso_expired" });

  assert.equal(api.state(), "error");
  // The session is discarded on the server too, rather than left standing.
  assert.equal(api.requests.some((r) => r.method === "DELETE"), true);
  assert.match(api.nodes["embed-error-code"].textContent, /sso_expired/);
});

test("logout ends the session and says so", async () => {
  const api = boot({ responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  await api.receive({ type: "embed-logout" });

  assert.equal(api.requests.some((r) => r.method === "DELETE"), true);
  assert.equal(api.state(), "logged-out");
});

test("silence from B2CORE is reported as silence", async () => {
  const api = boot({ responses: exchanges() });
  await api.settle();
  await api.fire(15000);

  assert.equal(api.state(), "error");
  assert.match(api.errorText(), /لم يصل ردّ من B2CORE/);
});

/* --- outside the frame --------------------------------------------------- */

test("opened directly, the page says where it belongs instead of asking for a token", async () => {
  const api = boot({ framed: false, responses: exchanges() });
  await api.settle();

  assert.equal(api.state(), "standalone");
  assert.deepEqual(api.sentTypes(), []);
  assert.equal(api.requests.length, 0);
});

test("an unconfigured deployment does not start the handshake at all", async () => {
  const api = boot({ state: "unconfigured", responses: exchanges() });
  await api.settle();

  assert.equal(api.state(), "unconfigured");
  assert.equal(api.requests.length, 0);
});

test("a token arriving after the panel has been handed to is dropped", async () => {
  // The probe found a live session and the door has already left. A token
  // pushed in that moment must not be posted to an endpoint that now wants a
  // session token this page never asked for.
  const api = boot({ responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  const before = api.requests.length;

  await api.receive({ type: "embed-jwt-token", token: "jwt.a.b" });

  assert.equal(api.requests.length, before);
  assert.equal(api.state(), "ready");
});

test("logout carries the session token, or the session would stay open", async () => {
  // The DELETE is an unsafe request like any other: without the per-session
  // token the server refuses it, and the host's logout would be a no-op that
  // only *looked* like one on screen.
  const api = boot({ responses: () => ({ status: 200, body: sessionBody() }) });
  await api.settle();
  await api.receive({ type: "embed-logout" });

  const del = api.requests.find((r) => r.method === "DELETE");
  assert.equal(del.init.headers["X-Merchant-CSRF"], "csrf-abc");
});
