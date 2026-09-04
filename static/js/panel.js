/* =========================================================================
   MaxPay — live updates for the internal panels (spec §8, §10)
   build-order step 13
   -------------------------------------------------------------------------
   Spec §10 fixes both the mechanism and its ceiling: *in-app polling every ten
   seconds on the merchant and finance panels. No WebSocket in phase 1.* So this
   is a setInterval and three fetches, and it is meant to stay that way.

   Two tiers, because a ten-second poll that re-renders is a page render every
   ten seconds per open tab:

     pulse     counts + a version token. Cheap, and the only thing that runs on
               every tick.
     fragment  the queue's <tbody> or a request's thread, fetched from the
               server *only when the version token has moved*, and swapped in
               whole.

   The fragments are HTML rendered by the same Django templates the full page
   includes. That is the point: the alternative — JSON plus a renderer here —
   would put the markup in two places, and on the merchant panel it would put
   spec §2's masking in two places too. Nothing in this file knows what a
   request looks like.

   Three rules it follows:

   * **Never touch an input.** The thread refreshes; the composer beside it does
     not. Replacing a textarea somebody is typing into, six times a minute, is
     worse than not refreshing at all.
   * **Stop when nobody is looking.** A hidden tab polls nothing, and resumes
     with an immediate tick so the first thing a returning operator sees is
     current rather than ten seconds stale.
   * **Fail quietly, and back off.** A panel whose network dropped shows the
     numbers it last knew, doubles its interval, and says nothing. It is a
     badge, not an alarm.

   No inline handlers and no inline script: the internal panels ship a CSP with
   `script-src 'self'` (spec §11), and either would need it relaxed.
   ========================================================================= */

(function () {
  "use strict";

  var configNode = document.getElementById("maxpay-poll-config");
  if (!configNode || !window.fetch) { return; }

  var config = JSON.parse(configNode.textContent);
  var interval = Math.max(2000, config.intervalMs || 10000);

  /* How far the interval is allowed to grow while the server is unreachable.
     Six minutes: long enough to stop hammering a broken deployment, short
     enough that a desk does not have to reload to recover. */
  var MAX_BACKOFF_MS = 6 * 60 * 1000;

  var state = {
    timer: null,
    delay: interval,
    version: null,
    stopped: false
  };

  /* --- the pieces of the page that are allowed to change ------------------ */

  var rowsHost = document.querySelector("[data-live-rows]");
  var threadHost = document.querySelector("[data-live-thread]");

  /* Badges and counters declare what they display, so this file never has to
     know which panel it is on. `data-pulse="counts.awaiting_me"` reads that
     path out of the pulse payload. */
  function targets() {
    return document.querySelectorAll("[data-pulse]");
  }

  function dig(payload, path) {
    return path.split(".").reduce(function (value, key) {
      return value === null || value === undefined ? undefined : value[key];
    }, payload);
  }

  function paint(payload) {
    Array.prototype.forEach.call(targets(), function (node) {
      var value = dig(payload, node.getAttribute("data-pulse"));
      if (value === undefined || value === null) { return; }
      var text = String(value);
      if (node.textContent !== text) { node.textContent = text; }
      // A badge showing zero is noise. Hiding it is the same decision the
      // server-rendered version makes with `{% if queue_waiting %}`.
      if (node.hasAttribute("data-pulse-hide-zero")) {
        node.hidden = (Number(value) === 0);
      }
    });
  }

  /* --- fetching ----------------------------------------------------------- */

  function get(url) {
    return window.fetch(url, {
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
      cache: "no-store"
    });
  }

  function swap(host, url) {
    // The query string the page was loaded with travels with the fragment
    // request, so a filtered queue refreshes to the same filter and page 2
    // does not silently become page 1.
    var separator = url.indexOf("?") === -1 ? "?" : "&";
    var search = window.location.search.replace(/^\?/, "");
    return get(search ? url + separator + search : url)
      .then(function (response) {
        if (!response.ok) { throw new Error(String(response.status)); }
        return response.text();
      })
      .then(function (html) {
        host.innerHTML = html;
      });
  }

  function refreshFragments() {
    var work = [];
    if (rowsHost && rowsHost.getAttribute("data-live-rows")) {
      work.push(swap(rowsHost, rowsHost.getAttribute("data-live-rows")));
    }
    if (threadHost && threadHost.getAttribute("data-live-thread")) {
      work.push(swap(threadHost, threadHost.getAttribute("data-live-thread")));
    }
    return Promise.all(work);
  }

  function tick() {
    if (state.stopped || document.hidden) { return schedule(); }

    get(config.pulseUrl)
      .then(function (response) {
        if (response.status === 401 || response.status === 403) {
          // The session ended, or the account lost the panel. Polling on would
          // be a stream of refusals in somebody's log; the page is stale and
          // the next click will say so properly.
          state.stopped = true;
          throw new Error("unauthorised");
        }
        if (!response.ok) { throw new Error(String(response.status)); }
        return response.json();
      })
      .then(function (payload) {
        state.delay = interval;
        paint(payload);

        var version = payload.queue_version;
        if (state.version === null) {
          // First answer of the session: the page was rendered from the same
          // data a moment ago, so there is nothing to refresh yet.
          state.version = version;
          return null;
        }
        if (version === state.version) { return null; }
        state.version = version;
        return refreshFragments();
      })
      .catch(function () {
        // Quiet on purpose. The numbers on screen stay as they were.
        state.delay = Math.min(state.delay * 2, MAX_BACKOFF_MS);
      })
      .then(schedule, schedule);
  }

  function schedule() {
    if (state.stopped) { return; }
    window.clearTimeout(state.timer);
    state.timer = window.setTimeout(tick, state.delay);
  }

  /* --- lifecycle ----------------------------------------------------------- */

  document.addEventListener("visibilitychange", function () {
    if (document.hidden || state.stopped) { return; }
    // Back in front of somebody: answer now rather than at the end of an
    // interval that has been counting down behind a hidden tab.
    window.clearTimeout(state.timer);
    state.delay = interval;
    tick();
  });

  schedule();
})();
