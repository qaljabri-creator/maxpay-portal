/* =========================================================================
   MaxPay — the B2CORE handshake for the merchant panel
   -------------------------------------------------------------------------
   The same sequence static/js/embed.js runs for the client portal, because
   there is one B2CORE and one protocol:

     1. the listener is attached, then the frame sends `embed-iframe-ready`
     2. it sends `embed-request-jwt-token`; B2CORE replies `embed-jwt-token`
        with a `token`, or refuses with `embed-jwt-token-error`
     3. the token goes to /merchant/session/, which verifies it against the
        JWKS and — this is the part that differs — will only open a session if
        the verified subject is the b2core_id of an active merchant
     4. on success the page replaces itself with the panel

   It is a separate file rather than a flag on embed.js because the two pages
   end differently. The portal's frame *is* the product and stays on screen
   for the whole session; this one is a door, and the last thing it does is
   stop existing. Sharing the file would mean a branch in every state handler
   for which of two surfaces is being served.

   The same two rules run through it:

   * **Nothing is trusted by origin alone, and nothing at all without one.**
     Every inbound message is dropped unless `event.origin` matches the
     configured B2CORE origin exactly *and* `event.source` is the window
     framing this one — a second B2CORE tab, a popup it opened and a nested
     frame it hosts all share that origin, and none of them is our parent.
   * **The token is never stored.** It lives in a local variable for the
     length of one request. The session cookie the backend sets is the only
     thing that outlives the exchange, and this script cannot read it.
   ========================================================================= */

(function () {
  "use strict";

  var configNode = document.getElementById("maxpay-merchant-embed-config");
  if (!configNode) { return; }

  var config = JSON.parse(configNode.textContent);
  var stage = document.getElementById("embed-stage");
  var titleNode = document.getElementById("embed-title");
  var greetingNode = document.getElementById("embed-greeting");
  var errorNode = document.getElementById("embed-error");
  var errorCodeNode = document.getElementById("embed-error-code");
  var retryButton = document.getElementById("embed-retry");

  var OUT = {
    ready: "embed-iframe-ready",
    requestToken: "embed-request-jwt-token"
  };

  var IN = {
    token: "embed-jwt-token",
    tokenError: "embed-jwt-token-error",
    logout: "embed-logout"
  };

  var TITLES = {
    connecting: "جارٍ فتح لوحة التاجر",
    ready: "أهلاً بك",
    error: "تعذّر فتح اللوحة",
    "logged-out": "انتهت الجلسة",
    standalone: "افتح اللوحة من B2CORE",
    unconfigured: "الخدمة غير متاحة"
  };

  /* The refusals the *door* can return are the interesting ones, and they are
     not authentication failures: the token was good and the person behind it
     is simply not one of our merchants, or no longer one. Saying "session
     expired" for any of these would send somebody to re-log-in forever. */
  var MESSAGES = {
    not_a_merchant: "هذا الحساب غير مرتبط بأي تاجر في MaxPay. لوحة التاجر متاحة لحسابات التجار فقط.",
    merchant_suspended: "حساب التاجر موقوف حاليًا. راجع المالية.",
    merchant_archived: "تمّت أرشفة حساب التاجر. راجع المالية.",
    no_login_account: "هذا التاجر بلا حساب دخول فعّال. راجع المالية.",
    invalid_token: "انتهت صلاحية جلستك في B2CORE. حدّث الصفحة أو سجّل الدخول من جديد.",
    missing_token: "لم يصل رمز الدخول من B2CORE.",
    token_rejected: "رفض B2CORE إصدار رمز الدخول. سجّل الدخول من جديد في B2CORE.",
    token_timeout: "لم يصل ردّ من B2CORE. تحقّق من اتصالك ثم أعد المحاولة.",
    key_unavailable: "تعذّر التحقق من الرمز حاليًا. أعد المحاولة بعد قليل.",
    not_configured: "التكامل مع B2CORE غير مهيّأ. تواصل مع الدعم الفني.",
    forbidden_origin: "طلب من مصدر غير مسموح به.",
    rate_limited: "محاولات كثيرة خلال وقت قصير. انتظر قليلاً ثم أعد المحاولة.",
    network: "تعذّر الوصول إلى الخادم. تحقّق من اتصالك.",
    unknown: "حدث خطأ غير متوقع. أعد المحاولة."
  };

  /* Only these earn a retry button. Telling somebody to retry a refusal that
     cannot succeed — "you are not a merchant" — is worse than saying nothing. */
  var RETRYABLE = { retry: true, reauthenticate: true };

  var tokenTimer = null;
  var pending = false;
  var done = false;
  var csrfToken = "";   // the per-session token, echoed back on DELETE

  function setState(state) {
    stage.setAttribute("data-state", state);
    if (titleNode && TITLES[state]) { titleNode.textContent = TITLES[state]; }
  }

  function clearTokenTimer() {
    if (tokenTimer) { window.clearTimeout(tokenTimer); tokenTimer = null; }
  }

  function fail(code, remedy, detail) {
    clearTokenTimer();
    pending = false;
    if (errorNode) { errorNode.textContent = MESSAGES[code] || MESSAGES.unknown; }
    if (errorCodeNode) {
      var label = code || "";
      if (label && detail) { label += ": " + String(detail).slice(0, 80); }
      errorCodeNode.textContent = label ? "(" + label + ")" : "";
    }
    if (retryButton) { retryButton.hidden = !RETRYABLE[remedy || "retry"]; }
    setState("error");
  }

  /* --- talking to B2CORE ------------------------------------------------- */

  function send(type) {
    if (!config.parentOrigin || window.parent === window) { return; }
    // Never "*": that would hand a token request to whatever page framed us.
    window.parent.postMessage({ type: type }, config.parentOrigin);
  }

  function requestToken() {
    if (pending || done) { return; }
    pending = true;
    setState("connecting");
    send(OUT.requestToken);
    clearTokenTimer();
    tokenTimer = window.setTimeout(function () {
      pending = false;
      fail("token_timeout", "retry");
    }, config.tokenTimeoutMs || 15000);
  }

  /* B2CORE's embed builds differ in how they shape a message: `{type, payload}`,
     `{event, data}`, or the value at the top level. Reading all three costs
     nothing; the origin check is what actually guards this. */
  function messageType(data) {
    if (!data || typeof data !== "object") { return ""; }
    var name = data.type || data.event || data.action || data.name;
    return typeof name === "string" ? name : "";
  }

  function messageValue(data, key) {
    if (!data || typeof data !== "object") { return null; }
    var candidates = [data[key], data.payload, data.data, data.value];
    for (var i = 0; i < candidates.length; i++) {
      var candidate = candidates[i];
      if (typeof candidate === "string" && candidate) { return candidate; }
      if (candidate && typeof candidate === "object" && typeof candidate[key] === "string") {
        return candidate[key];
      }
    }
    return null;
  }

  /* --- talking to our own backend ---------------------------------------- */

  function call(method, body) {
    var headers = { "Accept": "application/json" };
    if (body) { headers["Content-Type"] = "application/json"; }
    // Ending a session is a state change like any other, and the panel's
    // unsafe requests must all carry this. Without it the host's logout would
    // be answered with a refusal and the session would quietly stay open.
    if (csrfToken) { headers["X-Merchant-CSRF"] = csrfToken; }
    return window.fetch(config.sessionUrl, {
      method: method,
      // Same-origin, but stated rather than assumed: the merchant session
      // cookie has to ride along, and a default of "omit" would break it.
      credentials: "same-origin",
      cache: "no-store",
      headers: headers,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function enter(payload) {
    done = true;
    csrfToken = (payload && payload.csrf_token) || csrfToken;
    clearTokenTimer();
    if (greetingNode) {
      var name = (payload && payload.merchant && payload.merchant.name) || "";
      greetingNode.textContent = name ? "أهلاً بك، " + name + "." : "";
    }
    setState("ready");
    // `replace`, not `assign`: the door must not sit in the frame's history
    // where a back gesture would land on a page whose only job is done.
    window.location.replace((payload && payload.panel_url) || config.panelUrl);
  }

  function exchange(token) {
    clearTokenTimer();
    // A token can arrive unsolicited — B2CORE pushes one as soon as it sees a
    // ready — so `pending` means "an exchange is in flight" whether or not we
    // asked for it.
    pending = true;
    return call("POST", { token: token }).then(function (result) {
      pending = false;
      if (result.ok && result.data && result.data.authenticated) {
        enter(result.data);
        return;
      }
      csrfToken = "";
      fail((result.data && result.data.error) || "unknown",
           (result.data && result.data.remedy) || "retry");
    }).catch(function () {
      pending = false;
      fail("network", "retry");
    });
  }

  /* --- inbound ----------------------------------------------------------- */

  window.addEventListener("message", function (event) {
    if (!config.parentOrigin || event.origin !== config.parentOrigin) { return; }
    // An origin is not a frame. Only the window actually framing this page.
    if (event.source !== window.parent) { return; }

    var type = messageType(event.data);
    if (!type) { return; }

    if (type === IN.token) {
      // A token can arrive unsolicited, including after the probe found a live
      // session and this page has already handed over to the panel. Exchanging
      // it then would post to a door that now demands the session token this
      // page never asked for — a 403 flashed over a page that is leaving.
      if (done) { return; }
      var token = messageValue(event.data, "token") || messageValue(event.data, "jwt");
      if (!token) { fail("missing_token", "reauthenticate"); return; }
      exchange(token);
      return;
    }

    if (type === IN.tokenError) {
      // B2CORE has refused to mint a token. Sitting out the timeout here would
      // report a network problem for what is an authentication failure.
      var reason = messageValue(event.data, "error") ||
                   messageValue(event.data, "reason") ||
                   messageValue(event.data, "message");
      call("DELETE").catch(function () {}).then(function () {
        fail("token_rejected", "reauthenticate", reason);
      });
      return;
    }

    if (type === IN.logout) {
      done = true;
      call("DELETE").catch(function () {}).then(function () {
        clearTokenTimer();
        setState("logged-out");
      });
    }
  });

  if (retryButton) {
    retryButton.addEventListener("click", function () { requestToken(); });
  }

  /* --- start ------------------------------------------------------------- */

  function start() {
    if (stage.getAttribute("data-state") === "unconfigured") { return; }

    if (window.parent === window && !config.allowStandalone) {
      setState("standalone");
      return;
    }

    // Announced before any work of our own: B2CORE will not answer a token
    // request from a frame it has not heard a ready from, and putting a round
    // trip to our own backend in front of it delays the handshake behind a
    // call that has nothing to do with the host.
    send(OUT.ready);

    // A session may already exist — a reload inside the frame, or the panel
    // sending an expired visitor back here. Asking first avoids a needless
    // token round trip.
    call("GET").then(function (result) {
      if (result && result.data && result.data.csrf_token) {
        csrfToken = result.data.csrf_token;
      }
      if (done || pending) { return; }
      if (result && result.ok && result.data && result.data.authenticated) {
        enter(result.data);
        return;
      }
      requestToken();
    }).catch(function () {
      if (!done && !pending) { requestToken(); }
    });
  }

  start();
})();
