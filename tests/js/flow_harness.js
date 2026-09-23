/* =========================================================================
   A DOM small enough to read, for static/js/flow.js
   -------------------------------------------------------------------------
   The sibling of harness.js, and the same bargain: fake exactly the slice of
   the browser flow.js touches, and nothing else, so the tests stay about the
   flow rather than about a DOM library and this repo stays free of a
   node_modules tree.

   flow.js is a closed IIFE with no exports, so there is no seam to reach past
   the browser — which is the point. A test that imported a copy of the
   arithmetic would prove only that the copy is self-consistent; the thing
   worth asserting is that *the shipped file* prices the way
   apps/portal/pricing does. So the file is booted for real: a session arrives,
   the catalogue answers with a rate, an amount is typed, and the quote is read
   back off the nodes the template would have rendered.

   ========================================================================= */

// Run:  node --test "tests/js/**/*.test.js"

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = path.join(__dirname, "..", "..", "static", "js", "flow.js");

/* Every element is the same shape. flow.js asks elements for a small and
   forgiving set of things — text, a value, a class, a child — and none of the
   cases here depend on layout, so one permissive stub covers the lot. */
function element(id, tag) {
  const node = {
    id: id || "",
    tagName: (tag || "div").toUpperCase(),
    textContent: "",
    value: "",
    hidden: false,
    disabled: false,
    className: "",
    checked: false,
    files: [],
    dataset: {},
    style: {},
    children: [],
    parentNode: null,
    attributes: {},
    listeners: {},

    classList: {
      _set: new Set(),
      add(...names) { names.forEach((n) => this._set.add(n)); },
      remove(...names) { names.forEach((n) => this._set.delete(n)); },
      contains(name) { return this._set.has(name); },
      toggle(name, force) {
        const on = force === undefined ? !this._set.has(name) : !!force;
        if (on) { this._set.add(name); } else { this._set.delete(name); }
        return on;
      },
    },

    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name)
        ? this.attributes[name]
        : null;
    },
    removeAttribute(name) { delete this.attributes[name]; },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name);
    },

    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      return child;
    },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
      return child;
    },
    replaceChildren(...kids) { this.children = kids; },
    remove() {
      if (this.parentNode) { this.parentNode.removeChild(this); }
    },

    /* Deep enough for `el("submit-button").querySelector(".button__label")`
       and the list rebuilds, and no deeper: a fresh stub every time, which is
       what a template lookup would have handed back. */
    querySelector() { return element("", "span"); },
    querySelectorAll() { return []; },
    closest() { return null; },

    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn);
    },
    dispatch(type, event) {
      (this.listeners[type] || []).forEach((fn) => fn(event || { target: node }));
    },

    focus() {}, blur() {}, click() {}, scrollIntoView() {},
  };

  Object.defineProperty(node, "innerHTML", {
    get() { return ""; },
    set() { node.children = []; },
  });

  return node;
}

/**
 * Boot flow.js against a fake page.
 *
 * @param {object} options
 *   config    – the flow config island, merged over sensible defaults
 *   options   – the /portal/options payload the catalogue call answers with
 *   requests  – the rows the history call (/portal/requests?limit=…) answers with
 *   now       – fixed Date.now() in ms
 * @returns {object} nodes(), type(), quote(), and the raw element registry
 */
function boot(options = {}) {
  const config = Object.assign(
    {
      optionsUrl: "/portal/options",
      requestsUrl: "/portal/requests",
      requestUrlTemplate: "/portal/requests/__ref__",
      messagesUrlTemplate: "/portal/requests/__ref__/messages",
      messageMaxChars: 1000,
      maxUploadBytes: 5 * 1024 * 1024,
      acceptedTypes: ["image/png"],
      acceptedExtensions: [".png"],
      destinationDigits: { min: 6, max: 32 },
      pollMs: 10000,
      hours: {},
    },
    options.config || {}
  );

  const registry = new Map();
  function lookup(id) {
    if (!registry.has(id)) { registry.set(id, element(id)); }
    return registry.get(id);
  }

  // The two the IIFE refuses to start without. The stage starts hidden, the
  // way the template ships it — an already-visible #app makes the session
  // handler conclude it has run before and return without loading anything.
  lookup("maxpay-flow-config").textContent = JSON.stringify(config);
  lookup("app").hidden = true;

  const document = {
    hidden: false,
    body: element("", "body"),
    listeners: {},
    getElementById: (id) => lookup(id),
    createElement: (tag) => element("", tag),
    createElementNS: (_ns, tag) => element("", tag),
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
    execCommand: () => true,
  };

  /* Timers are recorded and never fired. Everything under test is synchronous
     once the catalogue promise settles; letting the hours re-check or the
     thread poll run would only add noise the assertions would have to ignore. */
  const timers = [];
  let sessionHandler = null;
  const calls = [];

  const embed = {
    call(url, init) {
      calls.push({ url, init });
      const payload = String(url).startsWith(config.optionsUrl)
        ? options.options || {}
        : String(url).startsWith(config.requestsUrl + "?")
          ? { requests: options.requests || [], has_more: false }
          : { results: [] };
      return Promise.resolve({ ok: true, status: 200, data: payload });
    },
    onSession(fn) { sessionHandler = fn; },
    reauthenticate() {},
  };

  const window = {
    MaxPayEmbed: embed,
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearInterval: () => {},
    scrollTo: () => {},
  };

  const sandbox = {
    window,
    document,
    console,
    JSON,
    Math,
    Number,
    String,
    Boolean,
    Date: options.now ? fixedDate(options.now) : Date,
    Promise,
    Array,
    Object,
    Error,
    isFinite,
    isNaN,
    parseFloat,
    parseInt,
    encodeURIComponent,
    decodeURIComponent,
    FormData: function FormData() { this.append = () => {}; },
    Intl,
  };
  sandbox.globalThis = sandbox;

  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SOURCE, "utf8"), sandbox, { filename: SOURCE });

  if (!sessionHandler) {
    throw new Error("flow.js did not subscribe to the session");
  }

  return {
    node: lookup,
    calls,
    timers,

    /** Deliver a session, which is what sets the catalogue — and the rate — going. */
    start() {
      sessionHandler({ authenticated: true, client: { display_name: "زينب" } });
      // One turn for the options promise, one for anything it chained.
      return Promise.resolve().then(() => Promise.resolve());
    },

    /** Type an amount and let the live quote recompute, as a keystroke would. */
    type(amount) {
      const input = lookup("amount-input");
      input.value = String(amount);
      input.dispatch("input");
    },

    /** The figures the client reads, as text, and whether each row is shown. */
    quote() {
      return {
        converted: lookup("quote-converted").textContent,
        commission: lookup("quote-commission").textContent,
        rounding: lookup("quote-rounding").textContent,
        total: lookup("quote-total").textContent,
        commissionShown: !lookup("quote-commission-row").hidden,
        roundingShown: !lookup("quote-rounding-row").hidden,
      };
    },
  };
}

function fixedDate(ms) {
  return class extends Date {
    constructor(...args) {
      if (args.length === 0) { super(ms); } else { super(...args); }
    }
    static now() { return ms; }
  };
}

/** The catalogue payload flow.js needs before it will quote anything. */
function catalogue(rate) {
  return {
    hours: { open: true },
    types: [{ value: "deposit" }, { value: "withdrawal" }],
    rate,
    merchants: [],
    methods: [],
  };
}

/** A rate payload shaped exactly as apps.portal.pricing.rate_payload builds it.
 *  The default charges no commission, because that is what the desk charges. */
function rate(overrides = {}) {
  return Object.assign(
    {
      id: 1,
      iqd_per_usd: "1510.00",
      commission_iqd_per_100usd: "0.00",
      commission_sign: 1,
      effective_from: "2026-09-01T00:00:00+00:00",
      min_usd: "1.00",
      max_usd: "100000.00",
    },
    overrides
  );
}

module.exports = { boot, catalogue, rate, element };
