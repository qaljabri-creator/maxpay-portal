/* =========================================================================
   MaxPay — the B2CORE embed handshake (spec §4)
   -------------------------------------------------------------------------
   The documented sequence, in order:

     1. the app loads inside the iframe and sends `embed-iframe-ready`
     2. it sends `embed-request-jwt-token`; B2CORE replies `embed-jwt-token`
     3. the token goes to the backend, which verifies it against the JWKS
     4. the verified subject becomes a local client record and a portal session
     5. `embed-logout` clears the session and discards any cached token
     6. `embed-theme-change` / `embed-language-change` follow the host

   Two rules run through all of it:

   * **Nothing is trusted by origin alone, and nothing at all is trusted
     without one.** Every inbound message is dropped unless `event.origin`
     matches the configured B2CORE origin exactly. Every outbound message names
     that origin as its target rather than `"*"`, so a token request is never
     broadcast to whoever happens to be framing us.
   * **The token is never stored.** It is held in a local variable for the
     length of one request and then dropped — no localStorage, no sessionStorage,
     no cookie. The session cookie the backend sets is the only thing that
     outlives the exchange, and the browser will not let this script read it.
   ========================================================================= */

(function () {
  "use strict";

  var configNode = document.getElementById("maxpay-embed-config");
  if (!configNode) { return; }

  var config = JSON.parse(configNode.textContent);
  var stage = document.getElementById("embed-stage");
  var titleNode = document.getElementById("embed-title");
  var greetingNode = document.getElementById("embed-greeting");
  var errorNode = document.getElementById("embed-error");
  var errorCodeNode = document.getElementById("embed-error-code");
  var retryButton = document.getElementById("embed-retry");

  /* --- protocol vocabulary ---------------------------------------------- */

  var OUT = {
    ready: "embed-iframe-ready",
    requestToken: "embed-request-jwt-token"
  };

  var IN = {
    token: "embed-jwt-token",
    logout: "embed-logout",
    theme: "embed-theme-change",
    language: "embed-language-change"
  };

  /* Copy for each state. The backend deliberately does not send display text
     for failures — it sends a code — so the wording lives here, in the one
     place that also knows what the user was trying to do. */
  var TITLES = {
    connecting: "جارٍ الاتصال بحسابك",
    ready: "أهلاً بك",
    error: "تعذّر الاتصال",
    "logged-out": "انتهت الجلسة",
    standalone: "افتح البوابة من B2CORE",
    unconfigured: "الخدمة غير متاحة"
  };

  var MESSAGES = {
    invalid_token: "انتهت صلاحية جلستك في B2CORE. حدّث الصفحة أو سجّل الدخول من جديد.",
    missing_token: "لم يصل رمز الدخول من B2CORE.",
    token_timeout: "لم يصل ردّ من B2CORE. تحقّق من اتصالك ثم أعد المحاولة.",
    key_unavailable: "تعذّر التحقق من الرمز حاليًا. أعد المحاولة بعد قليل.",
    not_configured: "التكامل مع B2CORE غير مهيّأ. تواصل مع الدعم الفني.",
    client_disabled: "هذا الحساب غير مفعّل. تواصل مع الدعم الفني.",
    forbidden_origin: "طلب من مصدر غير مسموح به.",
    invalid_csrf: "انتهت صلاحية الجلسة. أعد المحاولة.",
    rate_limited: "محاولات كثيرة خلال وقت قصير. انتظر قليلاً ثم أعد المحاولة.",
    network: "تعذّر الوصول إلى الخادم. تحقّق من اتصالك.",
    unknown: "حدث خطأ غير متوقع. أعد المحاولة."
  };

  /* Remedies the backend can name. Only `retry` earns a retry button — telling
     someone to retry something that cannot succeed is worse than saying
     nothing. */
  var RETRYABLE = { retry: true, reauthenticate: true };

  /* --- state ------------------------------------------------------------- */

  var session = null;        // the last session payload the backend returned
  var csrfToken = "";        // echoed back on every state-changing call
  var hostTheme = null;      // the last theme B2CORE announced, if it has
  var tokenTimer = null;     // "B2CORE never answered" timeout
  var renewTimer = null;     // re-request a token before this one expires
  var pending = false;       // a token exchange is in flight
  var listeners = [];        // step 6 hooks, see window.MaxPayEmbed

  function setState(state) {
    stage.setAttribute("data-state", state);
    if (titleNode && TITLES[state]) { titleNode.textContent = TITLES[state]; }
  }

  function fail(code, remedy) {
    clearTokenTimer();
    pending = false;
    if (errorNode) { errorNode.textContent = MESSAGES[code] || MESSAGES.unknown; }
    if (errorCodeNode) { errorCodeNode.textContent = code ? "(" + code + ")" : ""; }
    if (retryButton) { retryButton.hidden = !RETRYABLE[remedy || "retry"]; }
    setState("error");
    notify({ authenticated: false, error: code });
  }

  function notify(payload) {
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](payload); } catch (err) { /* a subscriber must not break the handshake */ }
    }
  }

  /* --- talking to B2CORE ------------------------------------------------- */

  function send(type, payload) {
    if (!config.parentOrigin || window.parent === window) { return; }
    var message = { type: type };
    if (payload) { message.payload = payload; }
    // Never "*": that would hand a token request to whatever page framed us.
    window.parent.postMessage(message, config.parentOrigin);
  }

  function clearTokenTimer() {
    if (tokenTimer) { window.clearTimeout(tokenTimer); tokenTimer = null; }
  }

  function requestToken() {
    if (pending) { return; }
    pending = true;
    setState("connecting");
    send(OUT.requestToken);
    clearTokenTimer();
    tokenTimer = window.setTimeout(function () {
      pending = false;
      fail("token_timeout", "retry");
    }, config.tokenTimeoutMs || 15000);
  }

  /* B2CORE's own embed builds differ in how they shape a message: some send
     `{type, payload}`, some `{event, data}`, some put the token at the top
     level. Reading all three costs nothing and avoids a handshake that fails
     over a key name. The origin check above is what actually guards this. */
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

  function call(url, options) {
    var settings = options || {};
    var headers = { "Accept": "application/json" };
    var body;

    if (settings.body instanceof FormData) {
      // Deliberately no Content-Type: only the browser knows the multipart
      // boundary it is about to generate, and setting the header by hand
      // strips it and makes the body unparseable on the far end.
      body = settings.body;
    } else if (settings.body) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(settings.body);
    }

    if (csrfToken) { headers["X-Portal-CSRF"] = csrfToken; }

    return window.fetch(url, {
      method: settings.method || "GET",
      // Same-origin, but stated rather than assumed: the portal session cookie
      // has to ride along, and a default of "omit" would silently break it.
      credentials: "same-origin",
      cache: "no-store",
      headers: headers,
      body: body
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        return { ok: response.ok, status: response.status, data: data };
      });
    });
  }

  function adopt(payload) {
    session = payload;
    csrfToken = payload.csrf_token || "";
    // What the host announced outranks what the session was stored with.
    // `embed-theme-change` fires as the frame loads and can easily land while
    // the token exchange is still in flight; applying the stored theme here
    // unconditionally would flip the frame back under the client, mid-
    // handshake, to the value the host has just finished correcting.
    var theme = hostTheme || payload.theme;
    if (theme) { document.documentElement.setAttribute("data-theme", theme); }
    if (payload.language) { document.documentElement.setAttribute("lang", payload.language); }
    if (greetingNode) {
      var name = (payload.client && payload.client.display_name) || "";
      greetingNode.textContent = name ? "أهلاً بك، " + name + "." : "";
    }
    scheduleRenewal(payload.expires_at);
    setState("ready");
    notify(payload);
  }

  function exchange(token) {
    clearTokenTimer();
    return call(config.sessionUrl, { method: "POST", body: { token: token } })
      .then(function (result) {
        pending = false;
        if (result.ok && result.data && result.data.authenticated) {
          adopt(result.data);
          return;
        }
        fail((result.data && result.data.error) || "unknown",
             (result.data && result.data.remedy) || "retry");
      })
      .catch(function () {
        pending = false;
        fail("network", "retry");
      });
  }

  /* A session dies with the token that created it, so a new token has to be in
     hand *before* that happens — otherwise the client is thrown back to a
     loading screen mid-task. */
  function scheduleRenewal(expiresAt) {
    if (renewTimer) { window.clearTimeout(renewTimer); renewTimer = null; }
    if (!expiresAt) { return; }
    var margin = (config.renewMarginSeconds || 90) * 1000;
    var delay = (expiresAt * 1000) - Date.now() - margin;
    // setTimeout overflows past ~24.8 days and would fire immediately.
    if (delay <= 0 || delay > 2147483647) { return; }
    renewTimer = window.setTimeout(requestToken, delay);
  }

  function endSession() {
    if (renewTimer) { window.clearTimeout(renewTimer); renewTimer = null; }
    clearTokenTimer();
    return call(config.sessionUrl, { method: "DELETE" }).catch(function () {
      // A failed logout call still means this page must stop showing a session.
    }).then(function () {
      session = null;
      csrfToken = "";
      setState("logged-out");
      notify({ authenticated: false, reason: "logged_out" });
    });
  }

  function savePreference(body) {
    // Applied locally first, and that is the whole switch: one attribute on
    // <html>, the palette in embed.css forks on it, and the frame is repainted
    // in the same frame the message arrived in. Nothing is re-fetched and
    // nothing is re-rendered — a reload here would drop the client out of a
    // half-filled form because the room's toggle was pressed.
    if (body.theme) {
      hostTheme = body.theme;
      document.documentElement.setAttribute("data-theme", body.theme);
    }
    if (body.language) { document.documentElement.setAttribute("lang", body.language); }

    // The round trip is only about *persistence*, and it is sent whether or not
    // a session exists yet: the endpoint accepts a preference before the token
    // does (it is the same store either way, and the CSRF header is only
    // required once there is a session to protect). Skipping it while
    // unauthenticated would mean the theme B2CORE announced at load survived
    // until the handshake finished and then vanished on the next reload.
    call(config.preferencesUrl, { method: "POST", body: body }).catch(function () {});
  }

  /* --- inbound ----------------------------------------------------------- */

  window.addEventListener("message", function (event) {
    // The single most important line in this file.
    if (!config.parentOrigin || event.origin !== config.parentOrigin) { return; }

    var type = messageType(event.data);
    if (!type) { return; }

    if (type === IN.token) {
      var token = messageValue(event.data, "token") || messageValue(event.data, "jwt");
      if (!token) { fail("missing_token", "reauthenticate"); return; }
      exchange(token);
      // The token is not kept anywhere: `token` goes out of scope with this
      // handler, and `exchange` holds it only until the request is sent.
      return;
    }

    if (type === IN.logout) { endSession(); return; }

    if (type === IN.theme) {
      var theme = messageValue(event.data, "theme");
      if (theme === "light" || theme === "dark") { savePreference({ theme: theme }); }
      return;
    }

    if (type === IN.language) {
      var language = messageValue(event.data, "language") || messageValue(event.data, "locale");
      if (language) { savePreference({ language: language }); }
    }
  });

  if (retryButton) {
    retryButton.addEventListener("click", function () { requestToken(); });
  }

  /* --- the surface step 6 builds on -------------------------------------- */

  window.MaxPayEmbed = {
    /** The last session payload, or null. */
    session: function () { return session; },
    /** Subscribe to session changes; fires immediately if one already exists. */
    onSession: function (callback) {
      listeners.push(callback);
      if (session) { callback(session); }
    },
    /** Ask B2CORE for a fresh token — e.g. after a 401 from a portal call. */
    reauthenticate: requestToken,
    /** A fetch that carries the portal session and its token header.
     *  Pass a FormData body for an upload; anything else is sent as JSON. */
    call: call
  };

  /* --- start ------------------------------------------------------------- */

  function start() {
    if (stage.getAttribute("data-state") === "unconfigured") { return; }

    if (window.parent === window && !config.allowStandalone) {
      setState("standalone");
      return;
    }

    // A session may already exist — a reload inside the frame, or a second
    // frame on the same page. Asking first avoids a needless token round trip.
    call(config.sessionUrl).then(function (result) {
      if (result.ok && result.data && result.data.authenticated) {
        adopt(result.data);
        send(OUT.ready);
        return;
      }
      send(OUT.ready);
      requestToken();
    }).catch(function () {
      send(OUT.ready);
      requestToken();
    });
  }

  start();
})();
