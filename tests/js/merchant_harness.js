/* =========================================================================
   A DOM small enough to read, for static/js/merchant-embed.js
   -------------------------------------------------------------------------
   The merchant door touches an even narrower slice of the browser than the
   portal's embed does: six elements by id, one `message` listener, `fetch`,
   the two timer functions — and `location.replace`, which is the one thing
   the portal's harness has no reason to fake. That last one is the whole
   point of the door: its success condition is that it stops existing.

   Kept beside harness.js rather than folded into it. The two scripts end
   differently, and a harness that served both would need a flag for which
   ending to expect in every assertion that matters.

   Run:  node --test tests/js
   ========================================================================= */

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "..", "static", "js", "merchant-embed.js");

function element(id) {
  return {
    id: id,
    textContent: "",
    hidden: false,
    attributes: {},
    listeners: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    dispatch(type, event) { (this.listeners[type] || []).forEach((fn) => fn(event || {})); },
  };
}

/**
 * Boot merchant-embed.js against a fake page.
 *
 * @param {object} options
 *   config     – the embed config island (merged over sensible defaults)
 *   state      – the stage's initial data-state
 *   framed     – false to make `window.parent === window` (standalone)
 *   responses  – queue or handler for fetch: (url, init, api) => {status, body}
 */
function boot(options = {}) {
  const config = Object.assign(
    {
      parentOrigin: "https://b2core.example",
      sessionUrl: "/merchant/session/",
      panelUrl: "/merchant/",
      allowStandalone: false,
      tokenTimeoutMs: 15000,
    },
    options.config || {}
  );

  const nodes = {
    "maxpay-merchant-embed-config": Object.assign(
      element("maxpay-merchant-embed-config"),
      { textContent: JSON.stringify(config) }
    ),
    "embed-stage": element("embed-stage"),
    "embed-title": element("embed-title"),
    "embed-greeting": element("embed-greeting"),
    "embed-error": element("embed-error"),
    "embed-error-code": element("embed-error-code"),
    "embed-retry": element("embed-retry"),
  };
  nodes["embed-stage"].setAttribute("data-state", options.state || "connecting");

  const sent = [];      // every postMessage to the parent
  const requests = [];  // every fetch
  const timers = [];    // pending setTimeout callbacks
  const replaced = [];  // every location.replace — the door's only exit

  let nextTimer = 1;

  const responder = typeof options.responses === "function"
    ? options.responses
    : (() => {
        const queue = (options.responses || []).slice();
        return () => queue.shift() || { status: 200, body: { authenticated: false } };
      })();

  const parent = {
    postMessage(message, target) { sent.push({ message, target }); },
  };

  const win = {
    parent: null, // set below
    location: {
      href: "https://panel.example/merchant/embed/",
      replace(url) { replaced.push(url); },
      assign(url) { replaced.push("ASSIGN:" + url); },
    },
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    setTimeout(fn, delay) {
      const id = nextTimer++;
      timers.push({ id, fn, delay });
      return id;
    },
    clearTimeout(id) {
      const i = timers.findIndex((t) => t.id === id);
      if (i >= 0) { timers.splice(i, 1); }
    },
    fetch(url, init) {
      const record = { url, init, method: (init && init.method) || "GET" };
      requests.push(record);
      const reply = responder(url, init || {}, api) || { status: 200, body: {} };
      if (reply.reject) { return Promise.reject(new Error("network")); }
      return Promise.resolve({
        ok: reply.status >= 200 && reply.status < 300,
        status: reply.status,
        json: () => Promise.resolve(reply.body),
      });
    },
    document: {
      documentElement: element("html"),
      getElementById(id) { return nodes[id] || null; },
    },
  };
  win.parent = options.framed === false ? win : parent;

  const sandbox = {
    window: win,
    document: win.document,
    Promise,
    JSON,
    String,
    Object,
    Array,
    console,
  };
  sandbox.globalThis = sandbox;

  const api = {
    config,
    nodes,
    sent,
    requests,
    timers,
    replaced,
    state: () => nodes["embed-stage"].getAttribute("data-state"),
    /** Every message type sent to the parent, in order. */
    sentTypes: () => sent.map((s) => s.message.type),
    /** Deliver a postMessage as the framing window does. */
    receive(data, overrides = {}) {
      const event = Object.assign(
        { origin: config.parentOrigin, source: sandbox.window.parent, data },
        overrides
      );
      (win.listeners.message || []).forEach((fn) => fn(event));
      return api.settle();
    },
    /** Run the pending microtask queue to completion. */
    settle() { return new Promise((resolve) => setImmediate(() => setImmediate(resolve))); },
    /** Fire the timer scheduled for `delay` ms (or the only pending one). */
    fire(delay) {
      const i = delay === undefined ? 0 : timers.findIndex((t) => t.delay === delay);
      if (i < 0) { throw new Error("no timer scheduled for " + delay + "ms"); }
      const timer = timers.splice(i, 1)[0];
      timer.fn();
      return api.settle();
    },
    delays: () => timers.map((t) => t.delay),
    /** The error code the page is currently showing, from its own text. */
    errorText: () => nodes["embed-error"].textContent,
  };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SOURCE, "utf8"), sandbox, { filename: "merchant-embed.js" });
  return api;
}

/** A session payload of the shape /merchant/session/ returns. */
function sessionBody(overrides = {}) {
  return Object.assign(
    {
      authenticated: true,
      merchant: { name: "تاجر أ" },
      csrf_token: "csrf-abc",
      expires_at: 1_700_003_600,
      panel_url: "/merchant/",
    },
    overrides
  );
}

module.exports = { boot, sessionBody };
