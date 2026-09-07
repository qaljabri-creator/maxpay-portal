/* =========================================================================
   A DOM small enough to read, for static/js/embed.js
   -------------------------------------------------------------------------
   embed.js touches a narrow, known slice of the browser: five elements by id,
   one `message` listener, `fetch`, and the two timer functions. Faking exactly
   that — and nothing else — keeps the tests about the handshake rather than
   about a DOM library, and keeps this repo free of a node_modules tree it
   otherwise has no use for.

   Run:  node --test tests/js
   ========================================================================= */

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "..", "static", "js", "embed.js");

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
 * Boot embed.js against a fake page.
 *
 * @param {object} options
 *   config      – the embed config island (merged over sensible defaults)
 *   state       – the stage's initial data-state
 *   framed      – false to make `window.parent === window` (standalone)
 *   responses   – queue or handler for fetch: (url, init, api) => {status, body}
 *   now         – fixed Date.now() in ms
 */
function boot(options = {}) {
  const config = Object.assign(
    {
      parentOrigin: "https://b2core.example",
      sessionUrl: "/portal/session",
      preferencesUrl: "/portal/preferences",
      allowStandalone: false,
      renewMarginSeconds: 90,
      tokenTimeoutMs: 15000,
    },
    options.config || {}
  );

  const nodes = {
    "maxpay-embed-config": Object.assign(element("maxpay-embed-config"), {
      textContent: JSON.stringify(config),
    }),
    "embed-stage": element("embed-stage"),
    "embed-title": element("embed-title"),
    "embed-greeting": element("embed-greeting"),
    "embed-error": element("embed-error"),
    "embed-error-code": element("embed-error-code"),
    "embed-retry": element("embed-retry"),
  };
  nodes["embed-stage"].setAttribute("data-state", options.state || "connecting");

  const root = element("html");
  const sent = [];       // every postMessage to the parent
  const requests = [];   // every fetch
  const timers = [];     // pending setTimeout callbacks
  const notified = [];   // every MaxPayEmbed.onSession payload

  let clock = options.now || 1_700_000_000_000;
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
    location: { origin: "https://portal.example" },
    listeners: {},
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    setTimeout(fn, delay) {
      const id = nextTimer++;
      timers.push({ id, fn, delay, at: clock + delay });
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
      documentElement: root,
      getElementById(id) { return nodes[id] || null; },
    },
  };
  win.parent = options.framed === false ? win : parent;

  const sandbox = {
    window: win,
    document: win.document,
    FormData: class FormData {},
    Promise,
    JSON,
    Math,
    Date: new Proxy(Date, { get: (t, k) => (k === "now" ? () => clock : t[k]) }),
    isNaN,
    isFinite,
    parseInt,
    String,
    Object,
    Array,
    console,
  };
  sandbox.globalThis = sandbox;

  const api = {
    config,
    nodes,
    root,
    sent,
    requests,
    timers,
    notified,
    embed: () => sandbox.window.MaxPayEmbed,
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
      clock = timer.at;
      timer.fn();
      return api.settle();
    },
    /** Advance the clock without firing anything. */
    advance(ms) { clock += ms; },
    delays: () => timers.map((t) => t.delay),
  };

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SOURCE, "utf8"), sandbox, { filename: "embed.js" });

  if (sandbox.window.MaxPayEmbed) {
    sandbox.window.MaxPayEmbed.onSession((payload) => notified.push(payload));
  }
  return api;
}

/** A session payload of the shape our backend returns. */
function sessionBody(overrides = {}) {
  return Object.assign(
    {
      authenticated: true,
      csrf_token: "csrf-abc",
      expires_at: 1_700_003_600, // clock + 1h, in epoch seconds
      client: { display_name: "شركة مثال" },
    },
    overrides
  );
}

module.exports = { boot, sessionBody };
