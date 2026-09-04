/* =========================================================================
   MaxPay — the client request flow (spec §7), build-order step 6
   -------------------------------------------------------------------------
   Three screens, driven from the JSON in apps/portal/flow_views.py:

     compose   the whole request on one screen —
               a row of three dropdowns: type, merchant, method
               (built, not native — the icon has to be in the list)
               and, beneath it once all three are answered, the details:
                 deposit:    wallet number + copy, amount with a live IQD
                             figure, proof upload, optional message
                 withdrawal: destination account, amount with a live IQD
                             figure, optional message
     confirm   the reference
     request   status timeline, attachments, thread

   **It was a four-step wizard and is not any more.** Type, merchant, method and
   details were four screens walked in order, and the order was the whole of the
   navigation: `WIZARD`, `advance()`, `retreat()`, a trail, a step counter and a
   progress bar. All of it is gone. The three choices are a row of dropdowns
   across the top, every one of them on screen from the first paint, and the
   details appear underneath the moment the third is answered.

   What made the wizard worth replacing is not that it was slow. It is that a
   client could not see what they had already chosen, and changing the first
   answer was three taps away from where they were standing.

   `CHAIN` is what is left of the order, and it is now the *only* copy of it.
   The wizard kept the order in an array and again in the screen each handler
   named by hand; the two drifted, and screen 1 spent a while sending clients
   past the merchant into a method list that is empty by construction until a
   merchant is chosen. Unlocking, resetting and whether the details are shown
   all read CHAIN. Nothing restates it.

   The merchant comes before the method: the client settles who they are handing
   money to first, and only then sees what that merchant covers. The method
   column is therefore that *merchant's* methods, never the whole catalogue —
   and it stays locked, showing what it is waiting for, until there is one.

   The direction is state, not a second flow (step 10). The row is identical in
   both, and the details show whichever half the server’s `needs` object named —
   never a guess made here, so the form cannot collect a field the submission
   would refuse.

   Plus one screen that is not part of it (build-order step 11): `closed`. Spec
   §7 replaces every submission screen with a notice and a live countdown when
   the desk is shut, so `compose` becomes unreachable and the countdown becomes
   the page. The server decides — the schedule is never evaluated here — and it
   refuses a submission independently, so getting past this screen gets nobody
   anywhere.

   Three rules run through it:

   * **The server owns the money.** The figure this file computes is a preview,
     recomputed authoritatively at submission from the same rate row. The
     submission carries the *ids* of the wallet and rate the client was shown,
     and the backend refuses anything it cannot match — so a rate that moves
     mid-form produces a visible "look again", never a silent re-price.
   * **Nothing is built with innerHTML.** Every merchant name, caption and
     message body arrives from the database and is written with textContent.
   * **No history API.** pushState inside an iframe hijacks the host page's
     back button. Navigation is the in-app back control and nothing else.

   It waits on window.MaxPayEmbed, the surface static/js/embed.js exposes once
   the B2CORE handshake has produced a session.
   ========================================================================= */

(function () {
  "use strict";

  var configNode = document.getElementById("maxpay-flow-config");
  var app = document.getElementById("app");
  if (!configNode || !app || !window.MaxPayEmbed) { return; }

  var config = JSON.parse(configNode.textContent);
  var embed = window.MaxPayEmbed;

  /* --- vocabulary -------------------------------------------------------- */

  var TITLES = {
    compose: "طلب جديد",
    confirm: "تم الإرسال",
    request: "طلبك",
    closed: "خارج أوقات العمل"
  };

  /* Every string the two directions do not share. Kept together rather than
     scattered through the render functions: a deposit screen that says
     "transfer to this wallet" and a withdrawal screen that says the same thing
     is the kind of mistake nobody notices until a client has paid the desk
     money it was supposed to be sending them. */
  var WORDING = {
    deposit: {
      title: "تفاصيل الإيداع",
      commission: "العمولة",
      total: "الإجمالي المطلوب تحويله",
      submit: "إرسال الطلب",
      totalRow: "الإجمالي المحوّل"
    },
    withdrawal: {
      title: "تفاصيل السحب",
      commission: "العمولة (تُخصم)",
      total: "المبلغ الذي ستستلمه",
      submit: "إرسال طلب السحب",
      totalRow: "المبلغ المُستلم"
    }
  };

  function words() {
    return WORDING[state.type] || WORDING.deposit;
  }

  /* The three answers a request is made of, in the order they depend on each
     other: which direction, then whom, then how. Not a sequence of screens any
     more — all three are on screen at once — but still a chain, because the
     merchants offered depend on the direction and the methods on the merchant.

     **This array is the only declaration of that order.** Unlocking reads it,
     resetting reads it, and whether the details are shown reads it. The wizard
     it replaced kept the order in an array *and* in the screen each handler
     named, the two drifted apart, and screen 1 spent a while sending clients
     straight past the merchant into a method list that is empty by
     construction until a merchant has been chosen. One declaration, so there
     is nothing for it to disagree with. */
  var CHAIN = ["type", "merchant", "method"];

  /* How each answer identifies itself, because the three do not agree: a
     merchant carries an `id`, a method a `code`, and the direction is the
     string. Written once per step here and read by both halves that need it —
     fillOptions() stamping an option's `data-key`, rowFor() turning that
     string back into the row the server sent, and markSelected() deciding
     which row is the answer. Guessing the shape (`answer.id || answer`) would
     have silently broken the method column, which has no `id`. */
  var KEY_OF = {
    type: function (value) { return String(value); },
    merchant: function (value) { return String(value.id); },
    method: function (value) { return String(value.code); }
  };

  /* What a column's placeholder says. Two states, because a dropdown is asking
     one of two different things: "pick one" when it can be used, and "answer
     the one before me" when it cannot. Kept beside CHAIN rather than in the
     template so the locked wording and the ready wording cannot drift apart,
     and so the sentence lives where the rule that shows it lives.

     `type.waiting` is null and never read: the first link in a chain has
     nothing before it to wait for. */
  var PLACEHOLDER = {
    type: { ready: "اختر نوع الطلب", waiting: null },
    merchant: { ready: "اختر التاجر", waiting: "اختر نوع الطلب أولًا" },
    method: { ready: "اختر الطريقة", waiting: "اختر التاجر أولًا" }
  };

  /* How a row is written into an option, and what mark goes beside it. Three
     tables rather than three renderers, so a column is described by what it
     holds instead of by a function that knows how to draw it.

     `ICON_OF` returns `{node}` for markup we own, `{src, text}` for a file
     Finance uploaded (with the caption to fall back to if it will not load),
     or null for a column with nothing to show. The payment method's is the one
     that matters: a client recognises Zain Cash by its mark long before they
     read the word, which is why an `<option>` was not enough and this list is
     built by hand. */
  var LABEL_OF = {
    type: function (value) { return value === "withdrawal" ? "سحب" : "إيداع"; },
    merchant: function (merchant) { return merchant.name; },
    method: function (method) { return method.caption; }
  };

  var NOTE_OF = {
    type: function (value) {
      return value === "withdrawal" ? "اسحب رصيدًا من حسابك" : "أضِف رصيدًا إلى حسابك";
    },
    merchant: function (merchant) {
      return merchant.method_count ? merchant.method_count + " طريقة دفع" : "";
    },
    method: null
  };

  var ICON_OF = {
    type: function (value) { return { node: directionArrow(value) }; },
    merchant: null,
    method: function (method) { return { src: method.icon, text: method.caption }; }
  };

  /* The two directions, drawn rather than uploaded: they are ours and they are
     the same on every install. An arrow into the account and one out of it —
     the only pair in the flow where the mark carries the whole meaning and the
     word merely confirms it. */
  //: Both directions, for the moment before the first catalogue answer.
  var DIRECTIONS = ["deposit", "withdrawal"];

  var ARROW = {
    deposit: "M12 3v11m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2",
    withdrawal: "M12 14V3m0 0L8 7m4-4l4 4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2"
  };

  function directionArrow(value) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    var path = document.createElementNS(ns, "path");
    path.setAttribute("d", ARROW[value] || ARROW.deposit);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "currentColor");
    path.setAttribute("stroke-width", "1.7");
    path.setAttribute("stroke-linecap", "round");
    path.setAttribute("stroke-linejoin", "round");
    svg.appendChild(path);
    return svg;
  }

  //: Which option the list opens on, and the keys that open it from the button.
  var SELECTED_OPTION = '[aria-selected="true"]';
  var OPENING_KEYS = ["ArrowDown", "Down", "ArrowUp", "Up", "Enter", " ", "Spacebar"];

  /* Spec §7: outside business hours every submission screen is replaced by the
     closed notice. There is one now. The confirmation and the request view are
     outcomes rather than submissions and stay reachable — yanking away a
     reference the client is still reading would lose it. */
  var SUBMISSION_SCREENS = ["compose"];

  var MESSAGES = {
    no_session: "انتهت الجلسة. جارٍ إعادة الاتصال بحسابك.",
    rate_limited: "محاولات كثيرة خلال وقت قصير. أمهل قليلًا ثم أعد المحاولة.",
    network: "تعذّر الوصول إلى الخادم. تحقّق من اتصالك ثم أعد المحاولة.",
    not_found: "لا يوجد طلب بهذا المرجع.",
    portal_closed: "النظام مغلق حاليًا. لا يمكن تقديم طلبات جديدة الآن.",
    unknown: "حدث خطأ غير متوقع. أعد المحاولة."
  };

  /* Wording that changes with what the wallet actually carries. Kept beside
     MESSAGES rather than inline, so the two things a client can be asked to do
     are visible side by side. */
  var LABELS = {
    walletNumber: "حوّل المبلغ إلى هذه المحفظة",
    /* Two headings for the QR, because it means two different things. On its
       own it is the whole instruction; beside a number it is the shortcut. */
    qrOnly: "امسح رمز QR بتطبيق الدفع",
    qrAlso: "أو امسح رمز QR بدل نسخ الرقم",
    qrAlt: "رمز QR للدفع إلى هذا التاجر"
  };

  /* Why a choice screen came back with nothing.

     Written out per screen and per direction rather than reduced to one
     "لا توجد خيارات": a client who is told *why* can act — go back, pick
     another merchant, come back tomorrow — and a client shown an empty box can
     only assume the portal is broken. Every one of these is a real state the
     catalogue can be in, not a defensive placeholder. */
  var EMPTY = {
    type: {
      deposit: "لا يمكن تقديم طلب إيداع الآن: لم يُحدَّد سعر صرف للإيداع أو لا يوجد تاجر جاهز. جرّب السحب، أو تواصل مع الدعم.",
      withdrawal: "لا يمكن تقديم طلب سحب الآن: لم يُحدَّد سعر صرف للسحب أو لا يوجد تاجر جاهز. جرّب الإيداع، أو تواصل مع الدعم."
    },
    merchant: {
      deposit: "لا يوجد تاجر يستقبل الإيداع الآن. كل التجار متوقفون أو بلا محفظة نشطة. جرّب لاحقًا أو تواصل مع الدعم.",
      withdrawal: "لا يوجد تاجر ينفّذ السحب الآن. جرّب لاحقًا أو تواصل مع الدعم."
    },
    /* Kept, though the case that produced it is now unreachable by
       construction: a locked column shows what it is waiting for and renders no
       list at all, so an empty method column can only mean the merchant covers
       nothing. This is the answer if one ever arrives out of that order —
       fail-legible rather than a blank box. */
    methodNoMerchant: "لم تختر تاجرًا بعد، ولهذا لا توجد طرق تُعرض. اختر التاجر أولًا.",
    method: {
      deposit: "هذا التاجر لا يغطي أي طريقة تصلح للإيداع الآن. اختر تاجرًا آخر.",
      withdrawal: "هذا التاجر لا يغطي أي طريقة تصلح للسحب الآن. اختر تاجرًا آخر."
    }
  };

  function emptyText(table) {
    return table[state.type] || table.deposit;
  }

  /* Something answered earlier stopped being offerable. The wizard had to name
     a screen to send the client back to; the row does not, because the column
     that fixes it is already in front of them and clearing the answer is what
     makes it obvious which one. So these carry the sentence and nothing else. */
  var UNAVAILABLE = {
    rate: "لم يُحدَّد سعر الصرف بعد. تواصل مع الدعم.",
    merchant: "التاجر الذي اخترته لم يعد متاحًا.",
    method: "هذه الطريقة لم تعد متاحة لدى هذا التاجر.",
    wallet: "لا توجد محفظة نشطة لهذه الطريقة. اختر طريقة أخرى."
  };

  /* --- state ------------------------------------------------------------- */

  var state = {
    screen: "compose",
    trail: [],
    /* Null, not "deposit": the row starts with nothing answered, and the
       merchant column stays locked until it is. The options endpoint defaults
       an absent type to deposit exactly as it always has, and `query()` drops
       a null parameter — so the first catalogue call is byte for byte the one
       the wizard made, while the screen shows no direction as chosen. */
    type: null,
    method: null,
    /* The last catalogue answer for each list. An <option> can only carry a
       string, so the object the server sent has to be findable again when the
       select changes — see rowFor(). Not a second source of truth: every
       payload replaces these wholesale. */
    merchants: [],
    methods: [],
    merchant: null,
    wallet: null,
    rate: null,
    proof: null,
    /* What screen 4 collects in this direction. The server sends it with every
       options payload; this is only the last answer, never a guess. */
    needs: { wallet: true, destination: false, proof: true },
    /* The last answer about business hours (step 11). Null until the first
       options call answers; treated as open until then, because a client who
       arrives during a network hiccup should meet the compose screen and be refused by
       the server, not meet a closed notice the server never sent. */
    hours: null,
    reference: null,
    /* Which request's thread is on screen, so re-rendering after a refresh
       does not wipe a half-typed message, but opening another request does. */
    threadFor: null,
    draft: null
  };

  var toastTimer = null;

  /* --- elements ---------------------------------------------------------- */

  function el(id) { return document.getElementById(id); }

  /* The five nodes a dropdown is made of, found by the one convention the
     template follows: every id is the step name and a suffix. Written once so
     the three columns cannot drift into three slightly different shapes. */
  function dropdownAt(step) {
    return {
      root: el(step + "-dropdown"),
      button: el(step + "-button"),
      icon: el(step + "-icon"),
      value: el(step + "-value"),
      list: el(step + "-list")
    };
  }

  var nodes = {
    back: el("app-back"),
    title: el("app-title"),
    toast: el("toast"),

    /* The row, and the block under it. Keyed by the step names in CHAIN so
       renderPicker() can walk the chain rather than name three columns. */
    picker: el("picker"),
    details: el("details"),
    detailsTitle: el("details-title"),
    /* All three keyed by the step names in CHAIN, so renderPicker() walks the
       chain instead of naming columns one at a time. */
    columns: {
      type: el("pick-type"),
      merchant: el("pick-merchant"),
      method: el("pick-method")
    },
    /* One record per column: the shell, the button that opens it, the slot the
       chosen mark is drawn into, the span holding the chosen text, and the
       listbox. Grouped rather than flattened so the dropdown code takes a step
       name and gets everything it needs, and so adding a fourth column is a
       row here rather than five more lookups. */
    dropdowns: {
      type: dropdownAt("type"),
      merchant: dropdownAt("merchant"),
      method: dropdownAt("method")
    },
    empties: {
      type: el("type-empty"),
      merchant: el("merchant-empty"),
      method: el("method-empty")
    },

    typeEmptyText: el("type-empty-text"),
    history: el("history"),
    historyList: el("history-list"),

    methodEmptyText: el("method-empty-text"),
    merchantEmptyText: el("merchant-empty-text"),


    walletBlock: el("wallet-block"),
    walletLabel: el("wallet-label"),
    walletCopy: el("wallet-copy"),
    walletAccount: el("wallet-account"),
    walletQr: el("wallet-qr"),
    walletQrTitle: el("wallet-qr-title"),
    walletQrImage: el("wallet-qr-image"),
    walletNumber: el("wallet-number"),
    walletOwner: el("wallet-owner"),

    destinationField: el("destination-field"),
    destination: el("destination-input"),
    destinationHint: el("destination-hint"),
    destinationReview: el("destination-review"),
    destinationError: el("destination-error"),

    form: el("request-form"),
    amount: el("amount-input"),
    amountHint: el("amount-hint"),
    amountError: el("amount-error"),
    quoteRate: el("quote-rate"),
    quoteConverted: el("quote-converted"),
    quoteCommission: el("quote-commission"),
    quoteCommissionLabel: el("quote-commission-label"),
    quoteTotal: el("quote-total"),
    quoteTotalLabel: el("quote-total-label"),

    proofField: el("proof-field"),
    proofInput: el("proof-input"),
    proofText: el("proof-text"),
    proofHint: el("proof-hint"),
    proofPreview: el("proof-preview"),
    proofThumb: el("proof-thumb"),
    proofName: el("proof-name"),
    proofClear: el("proof-clear"),
    proofError: el("proof-error"),

    message: el("message-input"),
    messageCount: el("message-count"),

    submitError: el("submit-error"),
    submit: el("submit-button"),
    submitLabel: el("submit-button").querySelector(".button__label"),

    closedMessage: el("closed-message"),
    closedHours: el("closed-hours"),
    closedHistory: el("closed-history"),
    closedHistoryList: el("closed-history-list"),
    countdown: el("countdown"),
    countdownValue: el("countdown-value"),
    countdownAt: el("countdown-at"),

    confirmReference: el("confirm-reference"),
    confirmSummary: el("confirm-summary"),
    confirmTrack: el("confirm-track"),
    confirmNew: el("confirm-new"),

    requestReference: el("request-reference"),
    requestStatus: el("request-status"),
    requestRejection: el("request-rejection"),
    requestSummary: el("request-summary"),
    requestTimeline: el("request-timeline"),
    requestFiles: el("request-files"),
    requestFilesEmpty: el("request-files-empty"),
    requestThread: el("request-thread"),
    requestThreadEmpty: el("request-thread-empty"),

    composer: el("composer"),
    composerInput: el("composer-input"),
    composerAttach: el("composer-attach"),
    composerFile: el("composer-file"),
    composerFileRow: el("composer-file-row"),
    composerFileName: el("composer-file-name"),
    composerFileClear: el("composer-file-clear"),
    composerSend: el("composer-send"),
    composerCount: el("composer-count"),
    composerError: el("composer-error"),
    requestRefresh: el("request-refresh"),
    requestNew: el("request-new")
  };

  /* --- small helpers ----------------------------------------------------- */

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function show(node, visible) {
    if (node) { node.hidden = !visible; }
  }

  function toast(text) {
    nodes.toast.textContent = text;
    nodes.toast.hidden = false;
    if (toastTimer) { window.clearTimeout(toastTimer); }
    toastTimer = window.setTimeout(function () { nodes.toast.hidden = true; }, 2400);
  }

  /* Money is written in Latin digits with a decimal point, matching the
     Finance panel — Django's `ar` locale renders 1450,00, which reads as a
     different number to anyone checking a receipt. */
  function amountOf(value) {
    var number = Number(value);
    if (!isFinite(number)) { return null; }
    return number.toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    });
  }

  /* Dinars, to the whole dinar.

     The fils is out of circulation, so `1,490.00 د.ع` shows a denomination
     that does not exist. Up to two places rather than exactly zero: everything
     priced from now on is whole — apps/portal/pricing quantises it there — but
     a request settled before this rule may hold a real half-dinar, and showing
     it as whole would misstate what actually moved. So the decimals disappear
     exactly where they are not real, and stay where they are. */
  function dinars(value) {
    var number = Number(value);
    if (!isFinite(number)) { return null; }
    return number.toLocaleString("en-US", {
      minimumFractionDigits: 0,
      maximumFractionDigits: 2
    });
  }

  function iqd(value) {
    var text = dinars(value);
    return text === null ? "···" : text + " د.ع";
  }

  function usd(value) {
    var text = amountOf(value);
    return text === null ? "···" : text + " $";
  }

  function when(iso) {
    if (!iso) { return ""; }
    var moment = new Date(iso);
    if (isNaN(moment.getTime())) { return ""; }
    // Latin digits, for the same reason the money figures use them.
    return moment.toLocaleString("en-GB", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit"
    });
  }

  function kilobytes(bytes) {
    var size = Number(bytes) || 0;
    if (size >= 1024 * 1024) { return (size / (1024 * 1024)).toFixed(1) + " MB"; }
    return Math.max(1, Math.round(size / 1024)) + " KB";
  }

  /* --- numbers the client types ------------------------------------------ */

  /* The mirror of apps/portal/pricing.normalise_number. An Arabic keyboard
     produces ١٢٣ and ٫; a paste brings separators and bidi marks. Both sides
     have to agree on what those mean, or the preview and the charge diverge. */
  function normaliseNumber(raw) {
    var text = String(raw == null ? "" : raw).trim();
    var out = "";
    for (var i = 0; i < text.length; i++) {
      var char = text.charAt(i);
      var code = text.charCodeAt(i);

      if (code >= 0x0660 && code <= 0x0669) { out += String(code - 0x0660); continue; }
      if (code >= 0x06F0 && code <= 0x06F9) { out += String(code - 0x06F0); continue; }
      if (char === "." || code === 0x066B) { out += "."; continue; }
      // Thousands separators, spaces and the bidi marks around an RTL number.
      if (char === "," || char === "_" || code === 0x066C) { continue; }
      if (code === 0x0020 || code === 0x00A0 || code === 0x202F || code === 0x2009) { continue; }
      if (code === 0x200E || code === 0x200F || code === 0x061C) { continue; }
      if (code >= 0x2066 && code <= 0x2069) { continue; }
      out += char;
    }
    return out;
  }

  /* Two decimals at most, matching pricing.parse_amount_usd — which refuses a
     finer figure rather than rounding it into something the client never
     typed. Accepting it here would preview a number the server would reject. */
  function parseAmount(raw) {
    var text = normaliseNumber(raw);
    if (!text || !/^\d+(\.\d{1,2})?$/.test(text)) { return null; }
    var value = Number(text);
    return isFinite(value) ? value : null;
  }

  /* The mirror of apps/portal/destinations.normalise, minus the refusals: the
     server decides what is acceptable, this only reduces what the client typed
     to the digits that will actually be stored, so the review line under the
     field shows the number the merchant will be handed rather than the one on
     the keyboard. */
  function normaliseDestination(raw) {
    var text = String(raw == null ? "" : raw).trim();
    var out = "";
    for (var i = 0; i < text.length; i++) {
      var code = text.charCodeAt(i);
      if (code >= 0x0660 && code <= 0x0669) { out += String(code - 0x0660); continue; }
      if (code >= 0x06F0 && code <= 0x06F9) { out += String(code - 0x06F0); continue; }
      if (code >= 0x0030 && code <= 0x0039) { out += text.charAt(i); continue; }
      /* Everything else is either a separator the server drops too, or a
         character it will refuse. Either way it is not a digit, and echoing it
         back into the review line would be showing the client the wrong
         number. */
    }
    return out;
  }

  function groupDigits(number) {
    var out = [];
    for (var i = 0; i < number.length; i += 4) { out.push(number.slice(i, i + 4)); }
    return out.join(" ");
  }

  /* --- business hours (step 11) ------------------------------------------
     Everything about *when* is the server's answer. This file only counts
     down to a moment the server named, and asks again when it arrives.

     The countdown is anchored to `Date.now()` at the moment the answer
     arrived plus the server's own `seconds_until_change`, never to the
     server's wall-clock timestamp: a device whose clock is an hour out would
     otherwise count down to the wrong minute. Only elapsed time is read from
     the device, and elapsed time is the one thing it gets right. */

  var clock = { endsAt: 0, tick: null, recheck: null };

  function isClosed() {
    return !!(state.hours && state.hours.open === false);
  }

  function stopClock() {
    if (clock.tick) { window.clearInterval(clock.tick); clock.tick = null; }
    if (clock.recheck) { window.clearTimeout(clock.recheck); clock.recheck = null; }
    clock.endsAt = 0;
  }

  function applyHours(hours) {
    if (!hours) { return; }
    state.hours = hours;
    stopClock();

    var seconds = hours.seconds_until_change;
    if (typeof seconds === "number" && seconds >= 0) {
      clock.endsAt = Date.now() + seconds * 1000;
      // Ask again just *after* the boundary, not on it, so the answer is
      // computed on the far side of the minute that changes it.
      clock.recheck = window.setTimeout(loadOptions, seconds * 1000 + 1500);
    }

    if (hours.open) {
      // Reopened while the notice was on screen. Start a fresh request rather
      // than resuming it: whatever was chosen before closing is stale by the
      // length of a night.
      if (state.screen === "closed") { restart(); }
      return;
    }

    renderClosed();
    // Only the compose screen is replaced. A confirmation the client is still
    // reading, or a request they have open, is an outcome rather than a
    // submission screen, and yanking either away would lose them a reference
    // (spec §7).
    if (SUBMISSION_SCREENS.indexOf(state.screen) !== -1) {
      go("closed", { replace: true, reset: true });
    }
  }

  function renderClosed() {
    var hours = state.hours || {};
    nodes.closedMessage.textContent = hours.message || MESSAGES.portal_closed;

    clear(nodes.closedHours);
    if (hours.reason === "schedule") {
      addRow(nodes.closedHours, "مواعيد العمل",
        hours.open_time + " إلى " + hours.close_time);
      addRow(nodes.closedHours, "التوقيت", hours.timezone || "");
    }

    var counting = typeof hours.seconds_until_change === "number";
    show(nodes.countdown, counting);
    nodes.countdownAt.textContent = counting && hours.open_time
      ? "يفتح الساعة " + hours.open_time
      : "";
    if (!counting) { return; }

    tick();
    clock.tick = window.setInterval(tick, 1000);
  }

  function tick() {
    if (!clock.endsAt) { return; }
    var left = Math.round((clock.endsAt - Date.now()) / 1000);
    nodes.countdownValue.textContent = duration(left);
    if (left <= 0 && clock.tick) {
      // The recheck timer is what reopens the compose screen; stop counting past zero
      // rather than showing a client a countdown that has plainly finished.
      window.clearInterval(clock.tick);
      clock.tick = null;
    }
  }

  /* HH:MM:SS, with the hours left uncapped rather than rolled into days: a
     desk shut for thirty hours reads "30:00:00", which is unambiguous, where
     "1 day" and "6 hours" on two lines is a sentence to parse. */
  function duration(total) {
    var left = Math.max(0, Math.floor(total));
    return pad(Math.floor(left / 3600)) + ":" +
           pad(Math.floor((left % 3600) / 60)) + ":" +
           pad(left % 60);
  }

  function pad(value) {
    return (value < 10 ? "0" : "") + value;
  }

  /* --- navigation -------------------------------------------------------- */

  function go(screen, options) {
    var settings = options || {};
    // Spec §7: while the desk is shut no submission screen exists. Enforced on
    // the one function every screen change goes through, so a new caller
    // cannot forget it.
    if (isClosed() && SUBMISSION_SCREENS.indexOf(screen) !== -1) {
      screen = "closed";
      settings = { replace: true, reset: true };
    }
    if (!settings.replace && state.screen !== screen) {
      state.trail.push(state.screen);
    }
    if (settings.reset) { state.trail = []; }

    state.screen = screen;
    app.setAttribute("data-screen", screen);
    // The compose screen is titled for the whole request rather than for the
    // step being answered; there are no steps left to name.
    nodes.title.textContent = TITLES[screen] || TITLES.compose;

    show(nodes.back, state.trail.length > 0);

    // The frame does not scroll with the host page, so a new screen has to put
    // itself back at the top explicitly.
    window.scrollTo(0, 0);
    nodes.title.tabIndex = -1;
    nodes.title.focus({ preventScroll: true });
  }

  /* --- the row ------------------------------------------------------------

     Three columns, always on screen, each unlocked by the one before it. The
     whole of that behaviour is derived from CHAIN — no column names its
     neighbour, no handler lists what it invalidates. */

  /* What has been answered at each step. One function, so "is this answered"
     is asked the same way by the unlocking, the resetting and the details. */
  function answerAt(step) {
    if (step === "type") { return state.type; }
    if (step === "merchant") { return state.merchant; }
    return state.method;
  }

  function clearAnswerAt(step) {
    if (step === "type") { state.type = null; return; }
    if (step === "merchant") { state.merchant = null; return; }
    state.method = null;
  }

  /* Changing an answer drops every answer that depended on it, and nothing
     that did not — read off the chain rather than remembered by each handler.
     The wallet goes with any of them: it belongs to a merchant *and* a method,
     so there is no step it survives. */
  function resetAfter(step) {
    CHAIN.slice(CHAIN.indexOf(step) + 1).forEach(clearAnswerAt);
    state.wallet = null;
  }

  /* Draw the row from the state, and decide whether the details belong under
     it. Called after every answer and after every options payload, because the
     catalogue is also what can take an answer away. */
  function renderPicker() {
    var unlocked = true;

    CHAIN.forEach(function (step) {
      var open = unlocked;
      var answer = answerAt(step);
      var column = nodes.columns[step];
      var box = nodes.dropdowns[step];

      if (column) {
        column.classList.toggle("picker__col--locked", !open);
        column.classList.toggle("picker__col--done", Boolean(answer));
      }

      if (box) {
        /* Locked means `disabled` *and* emptied, not merely greyed.

           The merchants sitting behind the merchant column while no direction
           is chosen are the *deposit* ones — that is what the options endpoint
           defaults to — and the catalogue hands them over on every payload
           regardless. Disabling alone would leave them in the DOM, one removed
           attribute from being reachable. So the list is dropped as well, and
           refilled by fillOptions() when the column opens. */
        box.button.disabled = !open;
        if (!open) {
          closeList(step);
          box.list.replaceChildren();
        }
        // The button carries the sentence. A disabled control reading "choose a
        // type first" is the whole explanation, said by the thing that is
        // refusing, which is why no column has a separate waiting line.
        setButtonTo(step, answer, open);
        markSelected(step, answer);
      }

      // An emptiness is only worth explaining in a column the client can act
      // in. A locked one is not empty, it is waiting.
      var empty = nodes.empties[step];
      if (empty) {
        show(empty, open && empty.dataset.empty === "1");
      }

      unlocked = open && Boolean(answer);
    });

    // `unlocked` has walked the whole chain by now, so it is true only when
    // every step is answered. That is exactly when the details describe a real
    // request; before it they would be quoting a rate for nothing.
    show(nodes.details, unlocked);
  }

  /* --- the dropdown -------------------------------------------------------

     A button and a listbox, built rather than native, because a payment
     method's logo has to appear beside its name *inside the list* and an
     <option> renders no markup. A client recognises the rail by its mark.

     Everything a <select> gave away for free is paid for here instead. It is
     written once and all three columns use it, so there is one implementation
     of the keyboard and one of the ARIA — three copies is how one of them ends
     up not closing on Escape. */

  /* What the button shows: the chosen row, or the sentence for this state. */
  function setButtonTo(step, answer, open) {
    var box = nodes.dropdowns[step];
    box.value.textContent = answer
      ? LABEL_OF[step](answer)
      : (open ? PLACEHOLDER[step].ready : PLACEHOLDER[step].waiting);
    box.value.classList.toggle("dropdown__value--empty", !answer);
    paintIcon(box.icon, answer && ICON_OF[step] ? ICON_OF[step](answer) : null);
  }

  /* `aria-selected` belongs on the options, not on a class alone: it is what a
     screen reader reads back, and the mark beside the row is only its visible
     half. */
  function markSelected(step, answer) {
    var key = answer ? KEY_OF[step](answer) : null;
    var list = nodes.dropdowns[step].list;
    Array.prototype.forEach.call(list.children, function (option) {
      var mine = option.getAttribute("data-key") === key;
      option.setAttribute("aria-selected", mine ? "true" : "false");
      option.classList.toggle("dropdown__option--chosen", mine);
    });
  }

  /* Draw a mark into a slot: an uploaded image, a monogram, or nothing.
     One function, because the button and every option in the list need the
     same three cases and a second copy would drift. */
  function paintIcon(slot, art) {
    if (!slot) { return; }
    if (!art) {
      slot.replaceChildren();
      show(slot, false);
      return;
    }
    show(slot, true);
    if (art.node) {
      slot.replaceChildren(art.node);
      return;
    }
    if (art.src) {
      var image = document.createElement("img");
      image.src = art.src;
      image.alt = "";
      image.loading = "lazy";
      // Finance may have uploaded nothing, or the file may have gone. Either
      // way a monogram beats a broken image.
      image.addEventListener("error", function () {
        slot.replaceChildren(monogram(art.text));
      });
      slot.replaceChildren(image);
      return;
    }
    slot.replaceChildren(monogram(art.text));
  }

  function monogram(caption) {
    return make("span", "dropdown__monogram", (caption || "؟").trim().charAt(0));
  }

  /* Replace a column's options. The rows arrive from the server already
     filtered; nothing here decides what may be offered. */
  function fillOptions(step, rows) {
    var box = nodes.dropdowns[step];
    if (!box) { return; }
    box.list.replaceChildren();
    rows.forEach(function (row, index) {
      var option = make("li", "dropdown__option");
      option.setAttribute("role", "option");
      option.setAttribute("data-key", KEY_OF[step](row));
      option.id = step + "-option-" + index;
      option.setAttribute("aria-selected", "false");

      var slot = make("span", "dropdown__icon");
      option.appendChild(slot);
      paintIcon(slot, ICON_OF[step] ? ICON_OF[step](row) : null);

      // textContent, never innerHTML: a merchant's name and a method's caption
      // both come from the database.
      option.appendChild(make("span", "dropdown__label", LABEL_OF[step](row)));
      if (NOTE_OF[step]) {
        var note = NOTE_OF[step](row);
        if (note) { option.appendChild(make("span", "dropdown__note", note)); }
      }
      box.list.appendChild(option);
    });
    markSelected(step, answerAt(step));
  }

  /* Which row a key stands for. The lists live in state because an option can
     only carry a string, and the handlers need the object the server sent. */
  function rowFor(step, key) {
    var rows = step === "merchant" ? state.merchants : state.methods;
    for (var i = 0; i < rows.length; i += 1) {
      if (KEY_OF[step](rows[i]) === key) { return rows[i]; }
    }
    return null;
  }

  /* --- opening, closing, and the keyboard --------------------------------- */

  var openStep = null;

  function optionsOf(step) {
    return Array.prototype.slice.call(nodes.dropdowns[step].list.children);
  }

  function activeOf(step) {
    return nodes.dropdowns[step].list.querySelector(".dropdown__option--active");
  }

  function setActive(step, option) {
    var box = nodes.dropdowns[step];
    optionsOf(step).forEach(function (node) {
      node.classList.toggle("dropdown__option--active", node === option);
    });
    if (option) {
      box.list.setAttribute("aria-activedescendant", option.id);
      // Keeps the active row in view when arrowing past the visible end of a
      // long merchant list.
      if (option.scrollIntoView) { option.scrollIntoView({ block: "nearest" }); }
    } else {
      box.list.removeAttribute("aria-activedescendant");
    }
  }

  function openList(step) {
    var box = nodes.dropdowns[step];
    if (!box || box.button.disabled || openStep === step) { return; }
    // One at a time. Two open lists in a three-column row overlap each other,
    // and only one of them can be the one being answered.
    closeList(openStep);
    openStep = step;
    box.root.classList.add("dropdown--open");
    box.button.setAttribute("aria-expanded", "true");
    show(box.list, true);
    var chosen = box.list.querySelector(SELECTED_OPTION);
    setActive(step, chosen || box.list.firstElementChild);
    box.list.focus({ preventScroll: true });
  }

  function closeList(step, options) {
    if (!step) { return; }
    var box = nodes.dropdowns[step];
    if (!box) { return; }
    box.root.classList.remove("dropdown--open");
    box.button.setAttribute("aria-expanded", "false");
    show(box.list, false);
    setActive(step, null);
    if (openStep === step) { openStep = null; }
    // Focus goes back where it came from, but only when the client is still in
    // the control — not when the list is being torn down by a reset.
    if (options && options.focus) { box.button.focus({ preventScroll: true }); }
  }

  function chooseFrom(step, option) {
    if (!option) { return; }
    closeList(step, { focus: true });
    CHOOSE[step](option.getAttribute("data-key"));
  }

  function moveActive(step, delta) {
    var options = optionsOf(step);
    if (!options.length) { return; }
    var index = options.indexOf(activeOf(step));
    var next = index === -1
      ? (delta > 0 ? 0 : options.length - 1)
      : Math.min(options.length - 1, Math.max(0, index + delta));
    setActive(step, options[next]);
  }

  CHAIN.forEach(function (step) {
    var box = nodes.dropdowns[step];
    if (!box) { return; }

    box.button.addEventListener("click", function () {
      if (openStep === step) { closeList(step, { focus: true }); return; }
      openList(step);
    });

    // Opening from the button with the keyboard, which is what a native select
    // does and what the listbox pattern is expected to do.
    box.button.addEventListener("keydown", function (event) {
      if (OPENING_KEYS.indexOf(event.key) !== -1) {
        event.preventDefault();
        openList(step);
      }
    });

    box.list.addEventListener("keydown", function (event) {
      var key = event.key;
      if (key === "Escape" || key === "Esc") {
        event.preventDefault();
        closeList(step, { focus: true });
      } else if (key === "ArrowDown" || key === "Down") {
        event.preventDefault();
        moveActive(step, 1);
      } else if (key === "ArrowUp" || key === "Up") {
        event.preventDefault();
        moveActive(step, -1);
      } else if (key === "Home") {
        event.preventDefault();
        setActive(step, optionsOf(step)[0]);
      } else if (key === "End") {
        event.preventDefault();
        var all = optionsOf(step);
        setActive(step, all[all.length - 1]);
      } else if (key === "Enter" || key === " " || key === "Spacebar") {
        event.preventDefault();
        chooseFrom(step, activeOf(step));
      } else if (key === "Tab") {
        // Let focus leave, but do not leave an orphaned list open behind it.
        closeList(step);
      }
    });

    box.list.addEventListener("click", function (event) {
      var option = event.target.closest(".dropdown__option");
      if (option) { chooseFrom(step, option); }
    });

    // Hover moves the active row, so the mouse and the keyboard agree about
    // which one Enter would take.
    box.list.addEventListener("mousemove", function (event) {
      var option = event.target.closest(".dropdown__option");
      if (option) { setActive(step, option); }
    });

    box.list.addEventListener("focusout", function (event) {
      if (!box.root.contains(event.relatedTarget)) { closeList(step); }
    });
  });

  // Anywhere else on the page closes it. `mousedown` rather than `click` so the
  // list is gone before whatever was clicked reacts to being clicked.
  document.addEventListener("mousedown", function (event) {
    if (!openStep) { return; }
    if (!nodes.dropdowns[openStep].root.contains(event.target)) { closeList(openStep); }
  });

  /* `advance()` and `retreat()` are gone with the wizard. Forward movement was
     the thing they existed to get right — the order lived in an array *and* in
     the screen each handler named, and the two drifted. There is nowhere to
     advance to now: answering a step unlocks the next column in place, and
     `renderPicker()` reads CHAIN for that. Going back to a step is choosing in
     its column, which is already on screen.

     `back()` stays, for the two screens still outside the compose one. */

  function back() {
    var previous = state.trail.pop();
    if (!previous) { return; }
    state.screen = null;
    go(previous, { replace: true });
  }

  function restart() {
    // The whole chain, including the direction. A new request starts with
    // nothing answered — the row locks back down to its first column, which is
    // what "a new request" looks like now that there is no screen 1 to return
    // to.
    state.type = null;
    state.method = null;
    state.merchant = null;
    state.wallet = null;
    state.rate = null;
    state.proof = null;
    state.reference = null;
    state.threadFor = null;
    nodes.form.reset();
    clearProof();
    clearDestination();
    setFieldError(nodes.amountError, "");
    show(nodes.submitError, false);
    renderDirection();
    renderQuote();
    renderPicker();
    go("compose", { replace: true, reset: true });
    show(nodes.back, false);
    loadHistory();
    loadOptions();
  }

  /* --- talking to the backend -------------------------------------------- */

  function fail(result) {
    var data = (result && result.data) || {};
    var code = data.error || "unknown";
    if (result && result.status === 401) {
      toast(MESSAGES.no_session);
      embed.reauthenticate();
      return code;
    }
    toast(data.detail || MESSAGES[code] || MESSAGES.unknown);
    return code;
  }

  function query(params) {
    var parts = [];
    Object.keys(params).forEach(function (key) {
      if (params[key] !== null && params[key] !== undefined && params[key] !== "") {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
      }
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  function loadOptions() {
    var params = {
      type: state.type,
      method: state.method ? state.method.code : "",
      merchant: state.merchant ? state.merchant.id : ""
    };
    return embed.call(config.optionsUrl + query(params)).then(function (result) {
      if (!result.ok) { fail(result); return null; }
      applyOptions(result.data);
      return result.data;
    }).catch(function () {
      toast(MESSAGES.network);
      return null;
    });
  }

  function applyOptions(data) {
    // First, because everything after it is a submission screen. A closed
    // payload carries no catalogue at all, so reading on would blank the
    // lists the client sees again the moment the desk reopens.
    applyHours(data.hours);
    if (isClosed()) { return; }

    if (data.rate) { state.rate = data.rate; }
    if (data.needs) { state.needs = data.needs; }
    renderDirection();
    renderTypes(data.types || []);
    renderTypeAvailability(data);
    renderMerchants(data.merchants || []);
    renderMethods(data.methods || []);

    if (data.wallet) {
      state.wallet = data.wallet;
      renderWallet();
    }
    renderQuote();
    // After the lists, because it marks the chosen option in each of them and
    // decides whether the details belong under the row.
    renderPicker();

    if (!data.unavailable) { return; }

    var problem = UNAVAILABLE[data.unavailable];
    if (!problem) { return; }

    // Something answered earlier stopped being offerable. Drop what depended
    // on it rather than letting the client submit into a dead end.
    // Everything downstream of the thing that went away, and nothing upstream
    // of it: a merchant who has gone takes their methods with them, but a
    // method that has gone leaves the merchant perfectly choosable.
    if (data.unavailable === "merchant") { state.merchant = null; }
    if (data.unavailable === "merchant" || data.unavailable === "method" ||
        data.unavailable === "wallet") {
      state.method = null;
      state.wallet = null;
    }
    toast(data.detail || problem);
    // No screen to send them back to: the column that fixes it is already in
    // front of them, and clearing the answer above is what makes it the one
    // asking to be answered.
    renderPicker();
  }

  function loadHistory() {
    return embed.call(config.requestsUrl + query({ limit: 5 })).then(function (result) {
      if (!result.ok) { show(nodes.history, false); return; }
      renderHistory((result.data && result.data.requests) || []);
    }).catch(function () { show(nodes.history, false); });
  }

  function loadRequest(reference) {
    var url = config.requestUrlTemplate.replace(
      "{reference}", encodeURIComponent(reference)
    );
    return embed.call(url).then(function (result) {
      if (!result.ok) { fail(result); return null; }
      renderRequest(result.data.request);
      return result.data.request;
    }).catch(function () {
      toast(MESSAGES.network);
      return null;
    });
  }

  /* --- answering a column -------------------------------------------------

     One entry per step, each taking the key the option carried: record the
     answer, drop everything that depended on it, redraw the row, re-ask the
     catalogue. What differs between them is only what "the answer" is, because
     the three columns hold three different kinds of row.

     A table rather than three listeners, because the listening is the
     dropdown's job and it is written once for all three — see `chooseFrom`.
     These are what it calls, and they are the only place a choice changes
     anything.

     Called once per choice, never while the client is moving through the list:
     arrowing to a row makes it *active*, and only Enter or a click makes it the
     answer. Re-asking the server for every row somebody scrolls past would be a
     request per option and a catalogue that flickers. */

  var CHOOSE = {
    type: function (key) {
      // "" is the placeholder, which is not a direction. Nothing offers it as a
      // choice, but a reset can leave the button showing it.
      state.type = key || null;
      // Everything that depended on the direction, read off the chain rather
      // than listed here — the two columns to its right, and the wallet.
      resetAfter("type");
      /* The rate is per direction (spec §5), and so is everything the details
         collect. Dropping both here means a client who switches direction can
         never submit against the other one's figures — loadOptions() fills them
         in again for the direction they just chose. */
      state.rate = null;
      clearProof();
      clearDestination();
      renderDirection();
      renderPicker();
      loadOptions();
    },

    merchant: function (key) {
      state.merchant = rowFor("merchant", key);
      // The method column and the wallet, because the methods this merchant
      // covers are about to replace the list the old choice came from.
      resetAfter("merchant");
      renderPicker();
      loadOptions();
    },

    method: function (key) {
      state.method = rowFor("method", key);
      resetAfter("method");
      renderPicker();
      loadOptions();
    }
  };

  /* Two lists, one renderer. The second sits on the closed notice, because
     the compose screen — where the first one lives — is the screen spec §7
     replaces, and a client who cannot file anything tonight is precisely the
     one who wants to look at what they filed this morning (step 11). */
  function renderHistory(rows) {
    renderHistoryInto(nodes.history, nodes.historyList, rows);
    renderHistoryInto(nodes.closedHistory, nodes.closedHistoryList, rows);
  }

  function renderHistoryInto(section, list, rows) {
    clear(list);
    show(section, rows.length > 0);
    rows.forEach(function (row) {
      var item = document.createElement("li");
      var button = make("button", "history__row");
      button.type = "button";

      var meta = make("div", "history__meta");
      meta.appendChild(make("span", "history__amount", usd(row.amount_usd)));
      // Both directions live in one list from step 10 on, and an amount with
      // no direction beside it is the one thing this row must not be.
      meta.appendChild(make(
        "span", "history__ref", row.type_label + " · " + row.reference
      ));
      button.appendChild(meta);

      var pill = make("span", "pill " + pillModifier(row.status), row.status_label);
      button.appendChild(pill);

      button.addEventListener("click", function () {
        state.reference = row.reference;
        go("request");
        loadRequest(row.reference);
      });

      item.appendChild(button);
      list.appendChild(item);
    });
  }

  function pillModifier(status) {
    if (status === "rejected") { return "pill--rejected"; }
    // Ended, but not refused. A client whose request was cancelled was not
    // turned down, and a red pill would tell them they were.
    if (status === "cancelled") { return "pill--cancelled"; }
    if (status === "closed") { return "pill--closed"; }
    return "";
  }

  /* Screen 1 has no list to come back empty, but it can still be a dead end:
     no rate for this direction, or nobody offering it. Said on the screen the
     client is standing on rather than one tap later, and worded per direction
     because the other one may be perfectly fine. */
  /* The two directions. Rows here are plain strings, not the objects the other
     two columns hold, because `state.type` *is* the string the submission
     carries — so the answer and the row it came from are the same shape, as
     they are in the other two columns.

     The server still decides what may be offered: `available` is its call and
     this filters on it. What stays local is only the wording and the arrow,
     which are ours and identical on every install. */
  function renderTypes(types) {
    fillOptions("type", types.filter(function (row) {
      return row.available !== false;
    }).map(function (row) { return row.value; }));
  }

  function renderTypeAvailability(data) {
    // Only once a direction has been chosen. Before that the catalogue on hand
    // is the server's default one, and "no merchant takes deposits" is not an
    // answer to a question the client has asked yet.
    var blocked = Boolean(state.type)
      && (!state.rate || (data.merchants || []).length === 0);
    show(nodes.empties.type, blocked);
    if (blocked) {
      nodes.typeEmptyText.textContent = emptyText(EMPTY.type);
    }
  }

  /* --- the merchant column ------------------------------------------------ */

  function renderMerchants(merchants) {
    state.merchants = merchants;

    /* Whether the list is empty, and whether the column is unlocked, are two
       different facts and only renderPicker() knows the second. So the answer
       is recorded here and the showing is left to it — a locked column must
       not explain an emptiness the client has not asked about yet. */
    var none = merchants.length === 0;
    nodes.empties.merchant.dataset.empty = none ? "1" : "0";
    if (none) {
      nodes.merchantEmptyText.textContent = emptyText(EMPTY.merchant);
    }

    // The count is a note under the name now rather than glued onto it: the
    // list has room for two lines and the name is what the client is reading.
    fillOptions("merchant", merchants);
  }

  /* --- the method column -------------------------------------------------- */

  function renderMethods(methods) {
    state.methods = methods;

    var none = methods.length === 0;
    nodes.empties.method.dataset.empty = none ? "1" : "0";
    if (none) {
      // Two different emptinesses with two different answers: nothing has been
      // chosen yet, or what was chosen covers nothing. Only the second is the
      // merchant's fault. The first is unreachable — the column is disabled
      // and empty until a merchant is chosen — but it stays worded, because an
      // emptiness that arrives out of order should still say why.
      nodes.methodEmptyText.textContent = state.merchant
        ? emptyText(EMPTY.method)
        : EMPTY.methodNoMerchant;
    }

    fillOptions("method", methods);
  }

  /* The two empty states used to carry a button back to the screen that could
     fix them. There is no screen to go back to — the column that fixes either
     is in the same row, a few centimetres away — so they carry the explanation
     alone now. The chips that stood at the top of the details block are gone
     for the same reason: they existed to show what had been chosen on screens
     the client could no longer see, and to offer a way back to them. The row
     above does both, permanently, and is where the choosing happens anyway. */

  /* --- the details -------------------------------------------------------- */

  /* The details in whichever shape this direction takes, plus the two leads
     that name the transfer's direction. Called on every options payload rather
     than once at the switch, because the payload is also what re-arrives when
     the client changes an answer in the row. */
  function renderDirection() {
    var text = words();

    nodes.quoteCommissionLabel.textContent = text.commission;
    nodes.quoteTotalLabel.textContent = text.total;
    nodes.submitLabel.textContent = text.submit;

    show(nodes.walletBlock, state.needs.wallet !== false);
    show(nodes.destinationField, Boolean(state.needs.destination));
    show(nodes.proofField, state.needs.proof !== false);

    nodes.detailsTitle.textContent = text.title;

    var digits = config.destinationDigits || {};
    nodes.destinationHint.textContent =
      "أدخل الرقم كاملًا كما هو مسجّل لديك · بين " +
      (digits.min || 6) + " و " + (digits.max || 32) + " رقمًا";
  }

  /* --- the destination account (withdrawals) -------------------------------
     Shown back to the client, grouped, before they can submit. Nothing in the
     system can tell a valid account from a mistyped one, and once a merchant
     has paid against it the money is gone — so the last guard is the client's
     own eyes on the digits that will actually be stored. */

  function renderDestinationReview() {
    var digits = normaliseDestination(nodes.destination.value);
    var minimum = (config.destinationDigits || {}).min || 6;
    if (digits.length < minimum) {
      show(nodes.destinationReview, false);
      return;
    }
    nodes.destinationReview.textContent = groupDigits(digits);
    show(nodes.destinationReview, true);
  }

  function clearDestination() {
    nodes.destination.value = "";
    nodes.destinationReview.textContent = "";
    show(nodes.destinationReview, false);
    setFieldError(nodes.destinationError, "");
  }

  nodes.destination.addEventListener("input", function () {
    setFieldError(nodes.destinationError, "");
    show(nodes.submitError, false);
    renderDestinationReview();
  });

  function renderWallet() {
    renderWalletTarget();
    nodes.walletOwner.textContent = state.wallet && state.wallet.label
      ? state.wallet.label
      : (state.merchant ? state.merchant.name : "");
  }

  /* Where the client is being told to pay.

     Two independent facts, not one mode: a wallet may carry a number, a QR, or
     both. Some rails (Super QI) are settled by scanning a static code and have
     no account to type; others have an account and no code. Asking the wallet
     what it actually has, rather than asking the method what kind it is, means
     a merchant who adds a QR to a numbered wallet gets both shown without
     anything else needing to know.

     They render as two blocks, never as one block that changes meaning. The
     account half keeps the copy button; the QR half keeps its own heading and
     is the only large picture on this screen. A method's icon on screen 3 is
     1.9rem beside a caption — small enough that the two can never be read as
     the same kind of thing. */
  function renderWalletTarget() {
    var wallet = state.wallet;
    var number = wallet && wallet.number ? wallet.number : "";
    var qr = wallet && wallet.qr ? wallet.qr : "";

    show(nodes.walletAccount, Boolean(number));
    nodes.walletNumber.textContent = number || "···";
    nodes.walletLabel.textContent = LABELS.walletNumber;
    // Nothing to copy when there is no number, and a copy button that copies
    // "···" is worse than no button.
    show(nodes.walletCopy, Boolean(number));

    if (qr) {
      // Assigned only when it changes: re-setting src restarts the download
      // and blinks the picture on every re-render.
      if (nodes.walletQrImage.getAttribute("src") !== qr) {
        nodes.walletQrImage.setAttribute("src", qr);
      }
      nodes.walletQrImage.alt = LABELS.qrAlt;
      // On its own the code is the instruction; beside a number it is the
      // shortcut, and saying so is the difference between a client scanning
      // and a client wondering which of the two they were meant to use.
      nodes.walletQrTitle.textContent = number ? LABELS.qrAlso : LABELS.qrOnly;
    }
    show(nodes.walletQr, Boolean(qr));
  }

  function renderQuote() {
    if (!state.rate) { return; }

    nodes.quoteRate.textContent = iqd(state.rate.iqd_per_usd) + " / $";
    nodes.amountHint.textContent =
      "بين " + amountOf(state.rate.min_usd) + " و " +
      amountOf(state.rate.max_usd) + " دولار";

    var amount = parseAmount(nodes.amount.value);
    if (amount === null || amount <= 0) {
      nodes.quoteConverted.textContent = "···";
      nodes.quoteCommission.textContent = "···";
      nodes.quoteTotal.textContent = "···";
      return;
    }

    // A preview only. apps/portal/submissions recomputes this from the same
    // rate row and is what the client is actually charged, or paid.
    // Rounded to the whole dinar, the same way and in the same order as
    // apps/portal/pricing.quote — each component on its own, then the total
    // from the rounded pair. A preview that rounded differently would show a
    // figure the submission then contradicts.
    var converted = Math.round(amount * Number(state.rate.iqd_per_usd));
    var commission = Math.round(
      (amount / 100) * Number(state.rate.commission_iqd_per_100usd)
    );
    // Which way the fee points is the server's decision, travelling with the
    // rate: added to what a deposit transfers, deducted from what a withdrawal
    // pays out. Deriving it from state.type here would be a second copy of a
    // rule that must not be able to disagree with apps/portal/pricing.
    var sign = Number(state.rate.commission_sign);
    if (sign !== -1) { sign = 1; }
    var total = converted + sign * commission;

    nodes.quoteConverted.textContent = iqd(converted);
    nodes.quoteCommission.textContent = iqd(commission);
    // A payout the commission swallowed is not a smaller withdrawal; the server
    // refuses it, so there is no figure to show for it here either.
    // A payout under one dinar is nothing; the server refuses it on the same
    // threshold, so there is no figure to show for it here either.
    nodes.quoteTotal.textContent = total >= 1 ? iqd(total) : "···";
  }

  nodes.amount.addEventListener("input", function () {
    setFieldError(nodes.amountError, "");
    renderQuote();
  });

  /* --- copy --------------------------------------------------------------- */

  function copyFrom(button) {
    var source = el(button.getAttribute("data-copy-target"));
    if (!source) { return; }
    var text = source.textContent.trim();
    if (!text || text === "···") { return; }

    var done = function () { toast("تم النسخ"); };
    var fallback = function () { legacyCopy(text, done); };

    // The async clipboard needs a `clipboard-write` permission the host page
    // grants on the iframe, and B2CORE may not. execCommand is the only thing
    // that reliably works inside a frame, so it is the fallback rather than
    // the other way round.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  function legacyCopy(text, done) {
    var buffer = document.createElement("textarea");
    buffer.value = text;
    buffer.className = "copy-buffer";
    buffer.setAttribute("readonly", "readonly");
    document.body.appendChild(buffer);
    buffer.select();
    buffer.setSelectionRange(0, text.length);

    var copied = false;
    try { copied = document.execCommand("copy"); } catch (error) { copied = false; }
    document.body.removeChild(buffer);

    if (copied) { done(); } else { toast("انسخ الرقم يدويًا: " + text); }
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (button) { copyFrom(button); }
  });

  /* --- proof upload -------------------------------------------------------- */

  nodes.proofHint.textContent =
    "الحد الأقصى " + Math.round(config.maxUploadBytes / (1024 * 1024)) +
    " ميغابايت · " + (config.acceptedExtensions || []).join("، ");

  nodes.proofInput.addEventListener("change", function () {
    var file = nodes.proofInput.files && nodes.proofInput.files[0];
    if (!file) { clearProof(); return; }

    if (file.size > config.maxUploadBytes) {
      clearProof();
      setFieldError(nodes.proofError,
        "حجم الملف أكبر من الحد المسموح (" +
        Math.round(config.maxUploadBytes / (1024 * 1024)) + " ميغابايت).");
      return;
    }
    if (config.acceptedTypes.indexOf(file.type) === -1) {
      clearProof();
      setFieldError(nodes.proofError, "نوع الملف غير مسموح. أرفق صورة أو ملف PDF.");
      return;
    }

    state.proof = file;
    setFieldError(nodes.proofError, "");
    nodes.proofName.textContent = file.name + " · " + kilobytes(file.size);
    show(nodes.proofPreview, true);
    nodes.proofText.textContent = "تغيير الملف";

    show(nodes.proofThumb, false);
    if (file.type.indexOf("image/") === 0) {
      // A data URL rather than createObjectURL: the page CSP allows `data:`
      // for images and does not allow `blob:`.
      var reader = new FileReader();
      reader.addEventListener("load", function () {
        nodes.proofThumb.src = reader.result;
        show(nodes.proofThumb, true);
      });
      reader.readAsDataURL(file);
    }
  });

  nodes.proofClear.addEventListener("click", function () {
    clearProof();
    nodes.proofInput.focus();
  });

  function clearProof() {
    state.proof = null;
    nodes.proofInput.value = "";
    nodes.proofThumb.removeAttribute("src");
    show(nodes.proofThumb, false);
    show(nodes.proofPreview, false);
    nodes.proofName.textContent = "";
    nodes.proofText.textContent = "اضغط لاختيار صورة الإيصال أو ملف PDF";
  }

  /* --- optional message ---------------------------------------------------- */

  nodes.message.setAttribute("maxlength", String(config.messageMaxChars));

  nodes.message.addEventListener("input", function () {
    nodes.messageCount.textContent =
      nodes.message.value.length + " / " + config.messageMaxChars;
  });

  /* --- submission ----------------------------------------------------------- */

  function setFieldError(node, text) {
    node.textContent = text || "";
    show(node, Boolean(text));
    var field = node.closest(".field");
    if (field) { field.classList.toggle("field--invalid", Boolean(text)); }
  }

  function busy(isBusy) {
    nodes.submit.disabled = isBusy;
    nodes.submit.classList.toggle("button--busy", isBusy);
  }

  nodes.form.addEventListener("submit", function (event) {
    event.preventDefault();
    show(nodes.submitError, false);
    setFieldError(nodes.amountError, "");
    setFieldError(nodes.proofError, "");
    setFieldError(nodes.destinationError, "");

    if (isClosed()) {
      // Belt and braces: the screen should not be reachable, and the server
      // refuses regardless. Neither is a reason to send the request.
      go("closed", { replace: true, reset: true });
      return;
    }

    var needsWallet = state.needs.wallet !== false;
    if (!state.type || !state.method || !state.merchant || !state.rate ||
        (needsWallet && !state.wallet)) {
      toast("أكمل اختياراتك في الأعلى أولًا.");
      return;
    }

    // The destination is checked before the amount for the same reason the
    // server resolves it first: it is the field whose mistyping cannot be
    // undone, so it is the one the client is sent back to first.
    var destination = "";
    if (state.needs.destination) {
      destination = normaliseDestination(nodes.destination.value);
      var minimum = (config.destinationDigits || {}).min || 6;
      var maximum = (config.destinationDigits || {}).max || 32;
      if (destination.length < minimum || destination.length > maximum) {
        setFieldError(nodes.destinationError,
          "أدخل رقم البطاقة أو المحفظة كاملًا بالأرقام (بين " +
          minimum + " و " + maximum + " رقمًا).");
        nodes.destination.focus();
        return;
      }
    }

    var amount = parseAmount(nodes.amount.value);
    if (amount === null || amount <= 0) {
      setFieldError(nodes.amountError,
        "أدخل مبلغًا صحيحًا بالدولار، بمنزلتين عشريتين على الأكثر.");
      nodes.amount.focus();
      return;
    }
    if (state.needs.proof !== false && !state.proof) {
      setFieldError(nodes.proofError, "أرفق إثبات التحويل.");
      return;
    }

    var body = new FormData();
    body.append("type", state.type);
    body.append("method", state.method.code);
    body.append("merchant", String(state.merchant.id));
    body.append("rate", String(state.rate.id));
    body.append("amount_usd", amount.toFixed(2));
    if (needsWallet && state.wallet) { body.append("wallet", String(state.wallet.id)); }
    if (destination) { body.append("destination_account", destination); }
    if (state.needs.proof !== false && state.proof) { body.append("proof", state.proof); }
    if (nodes.message.value.trim()) {
      body.append("message", nodes.message.value.trim());
    }

    busy(true);
    embed.call(config.requestsUrl, { method: "POST", body: body })
      .then(function (result) {
        busy(false);
        if (result.status === 201 && result.data && result.data.request) {
          onSubmitted(result.data.request);
          return;
        }
        onSubmitRefused(result);
      })
      .catch(function () {
        busy(false);
        showSubmitError(MESSAGES.network);
      });
  });

  function showSubmitError(text) {
    nodes.submitError.textContent = text;
    show(nodes.submitError, true);
  }

  function onSubmitted(payload) {
    state.reference = payload.reference;
    nodes.confirmReference.textContent = payload.reference;

    clear(nodes.confirmSummary);
    addRow(nodes.confirmSummary, "المبلغ", usd(payload.amount_usd));
    addRow(nodes.confirmSummary,
      payload.type === "withdrawal" ? "ما ستستلمه" : "المحوَّل",
      iqd(payload.amount_iqd));
    addRow(nodes.confirmSummary, "طريقة الدفع", payload.method);
    addRow(nodes.confirmSummary, "التاجر", payload.merchant);
    if (payload.destination_account) {
      addRow(nodes.confirmSummary, "رقم الوجهة",
        groupDigits(payload.destination_account));
    }

    renderRequest(payload);
    go("confirm", { replace: true, reset: true });
    show(nodes.back, false);
  }

  function onSubmitRefused(result) {
    var data = (result && result.data) || {};
    var code = data.error || "unknown";
    var detail = data.detail || MESSAGES[code] || MESSAGES.unknown;

    if (result.status === 401) {
      toast(MESSAGES.no_session);
      embed.reauthenticate();
      return;
    }

    // The desk shut between the form opening and the submit landing.
    if (code === "portal_closed") {
      applyHours(data.hours);
      go("closed", { replace: true, reset: true });
      toast(detail);
      return;
    }

    // The wallet or the rate moved while the form was open. Show what changed
    // and let the client look before sending again — never re-price silently.
    if (code === "wallet_changed" && data.wallet) {
      state.wallet = data.wallet;
      renderWallet();
      showSubmitError(detail);
      return;
    }
    if (code === "rate_changed" && data.rate) {
      state.rate = data.rate;
      renderQuote();
      showSubmitError(detail);
      return;
    }

    if (code.indexOf("destination") === 0) {
      setFieldError(nodes.destinationError, detail);
      nodes.destination.focus();
      return;
    }
    if (code.indexOf("amount") === 0) {
      setFieldError(nodes.amountError, detail);
      nodes.amount.focus();
      return;
    }
    /* Ahead of the prefix branch below, which would put this on the proof
       field's own error line — and on a withdrawal that field is hidden, so the
       client would be refused with nothing on screen to say why. It means the
       form is out of step with the direction it is collecting for, so clear the
       file and re-ask the server which half of screen 4 applies. */
    if (code === "proof_not_accepted") {
      clearProof();
      showSubmitError(detail);
      loadOptions();
      return;
    }
    if (code.indexOf("proof") === 0) {
      setFieldError(nodes.proofError, detail);
      return;
    }
    if (code === "method_unavailable" || code === "merchant_unavailable" ||
        code === "wallet_unavailable") {
      showSubmitError(detail);
      loadOptions();
      return;
    }

    showSubmitError(detail);
  }

  /* --- screen 5: confirmation ------------------------------------------------ */

  nodes.confirmTrack.addEventListener("click", function () {
    go("request");
    if (state.reference) { loadRequest(state.reference); }
  });

  nodes.confirmNew.addEventListener("click", restart);
  nodes.requestNew.addEventListener("click", restart);

  nodes.requestRefresh.addEventListener("click", function () {
    if (state.reference) { loadRequest(state.reference); }
  });

  /* --- screen 6: the request view -------------------------------------------- */

  function addRow(list, term, value) {
    list.appendChild(make("dt", null, term));
    var dd = make("dd", null, value);
    dd.dir = "auto";
    list.appendChild(dd);
  }

  function renderRequest(payload) {
    nodes.requestReference.textContent = payload.reference;
    nodes.requestStatus.textContent = payload.status_label;
    nodes.requestStatus.className = "pill " + pillModifier(payload.status);

    show(nodes.requestRejection, Boolean(payload.rejection_reason));
    if (payload.rejection_reason) {
      nodes.requestRejection.textContent = payload.rejection_reason;
    }

    // Worded from the request's own type, not from state.type: this screen is
    // also reached from the history list, where the two need not agree.
    var text = WORDING[payload.type] || WORDING.deposit;

    clear(nodes.requestSummary);
    addRow(nodes.requestSummary, "النوع", payload.type_label);
    addRow(nodes.requestSummary, "المبلغ", usd(payload.amount_usd));
    addRow(nodes.requestSummary, "سعر الصرف", iqd(payload.rate_applied) + " / $");
    addRow(nodes.requestSummary, text.commission, iqd(payload.commission_applied));
    addRow(nodes.requestSummary, text.totalRow, iqd(payload.amount_iqd));
    addRow(nodes.requestSummary, "طريقة الدفع", payload.method);
    addRow(nodes.requestSummary, "التاجر", payload.merchant);
    if (payload.wallet_number) {
      addRow(nodes.requestSummary, "المحفظة", payload.wallet_number);
    }
    if (payload.destination_account) {
      addRow(nodes.requestSummary, "رقم الوجهة",
        groupDigits(payload.destination_account));
    }
    addRow(nodes.requestSummary, "وقت التقديم", when(payload.submitted_at));

    renderTimeline(payload.timeline || []);
    renderFiles(payload.attachments || []);
    renderThread(payload.messages || []);

    poll.signature = signatureOf(payload);

    // Only when the request changed: a refresh must not throw away whatever
    // the client had started typing.
    if (state.threadFor !== payload.reference) {
      state.threadFor = payload.reference;
      clearComposer();
    }
  }

  /* --- the live thread (Finance review, 24 Aug 2026) ----------------------
     Screen 6 used to need the "تحديث" button pressed before a reply appeared.
     It now polls on the same ten seconds spec §10 gives the two panels, and
     follows the same three rules static/js/panel.js does, for the same
     reasons:

     * **Never touch what is being typed.** A re-render replaces the thread and
       leaves the composer alone — `renderRequest` already clears it only when
       the *request* changes, which a poll never does.
     * **Stop when nobody is looking.** A hidden tab polls nothing, and comes
       back with an immediate tick so a returning client sees current rather
       than ten-seconds-stale.
     * **Fail quietly, and back off.** A dropped connection leaves the thread
       as it was, doubles the interval and says nothing. This is a message
       list, not an alarm.

     One thing panel.js does not need: a *signature*. The panels ask a cheap
     pulse endpoint first and only fetch markup when a version token moved.
     There is no pulse on the client surface, so the whole payload is fetched
     and compared instead — and the DOM is only rebuilt when something in it
     actually differs, so a client reading a long thread is not interrupted
     six times a minute by their own scroll position resetting. */

  var POLL_MS = Math.max(2000, Number(config.pollMs) || 10000);
  var POLL_MAX_MS = 6 * 60 * 1000;
  var poll = { timer: null, delay: POLL_MS, signature: "", busy: false };

  function signatureOf(payload) {
    var messages = payload.messages || [];
    var last = messages.length ? messages[messages.length - 1] : null;
    return [
      payload.status,
      messages.length,
      last ? last.created_at || "" : "",
      (payload.attachments || []).length
    ].join("|");
  }

  function schedulePoll() {
    window.clearTimeout(poll.timer);
    poll.timer = window.setTimeout(tickPoll, poll.delay);
  }

  function tickPoll() {
    if (state.screen !== "request" || !state.reference || document.hidden || poll.busy) {
      return schedulePoll();
    }
    poll.busy = true;
    var url = config.requestUrlTemplate.replace(
      "{reference}", encodeURIComponent(state.reference)
    );
    embed.call(url).then(function (result) {
      poll.busy = false;
      if (!result.ok) {
        // A refusal is not a network blip: the session ended, or the request
        // stopped being this client's. Either way the next deliberate action
        // will say so properly, and polling on would be a stream of refusals.
        poll.delay = Math.min(poll.delay * 2, POLL_MAX_MS);
        return schedulePoll();
      }
      poll.delay = POLL_MS;
      var payload = result.data.request;
      // Still on the same request? The client may have navigated away while
      // this was in flight.
      if (state.screen === "request" && state.reference === payload.reference
          && signatureOf(payload) !== poll.signature) {
        renderRequest(payload);
      }
      schedulePoll();
    }).catch(function () {
      poll.busy = false;
      poll.delay = Math.min(poll.delay * 2, POLL_MAX_MS);
      schedulePoll();
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden || state.screen !== "request") { return; }
    // Back in front of somebody: answer now rather than at the end of an
    // interval that has been counting down behind a hidden tab.
    window.clearTimeout(poll.timer);
    poll.delay = POLL_MS;
    tickPoll();
  });

  function renderTimeline(steps) {
    clear(nodes.requestTimeline);
    steps.forEach(function (step) {
      var item = make("li", "step step--" + step.state);
      item.appendChild(make("span", "step__dot"));
      item.appendChild(make("span", "step__label", step.label));
      var stamp = step.at ? when(step.at) : "";
      if (step.reason) { stamp = stamp ? stamp + " · " + step.reason : step.reason; }
      if (stamp) { item.appendChild(make("span", "step__at", stamp)); }
      nodes.requestTimeline.appendChild(item);
    });
  }

  function renderFiles(files) {
    clear(nodes.requestFiles);
    show(nodes.requestFilesEmpty, files.length === 0);

    files.forEach(function (file) {
      var item = make("li", "file");

      if (file.is_image) {
        var thumb = document.createElement("img");
        thumb.className = "file__thumb";
        thumb.src = file.url;
        thumb.alt = file.name || "";
        thumb.loading = "lazy";
        item.appendChild(thumb);
      }

      var body = make("div", "file__body");
      body.appendChild(make("span", "file__name", file.name || "ملف"));
      body.appendChild(make(
        "span", "file__meta",
        file.uploaded_by + " · " + kilobytes(file.size_bytes)
      ));
      item.appendChild(body);

      var link = make("a", "file__open", "فتح");
      link.href = file.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.appendChild(link);

      nodes.requestFiles.appendChild(item);
    });
  }

  function renderThread(messages) {
    clear(nodes.requestThread);
    show(nodes.requestThreadEmpty, messages.length === 0);
    messages.forEach(appendMessage);
  }

  function appendMessage(message) {
    var item = make("li", "message" + (message.mine ? " message--mine" : ""));

    var head = make("div", "message__head");
    head.appendChild(make("span", null, message.sender));
    head.appendChild(make("span", null, when(message.created_at)));
    item.appendChild(head);

    if (message.body) {
      var body = make("p", "message__body", message.body);
      body.dir = "auto";
      item.appendChild(body);
    }

    if (message.attachment) {
      var link = make("a", "file__open", message.attachment.name || "مرفق");
      link.href = message.attachment.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.appendChild(link);
    }

    nodes.requestThread.appendChild(item);
    return item;
  }

  /* --- the composer (step 9) -----------------------------------------------
     Two-way and ungated: the client writes whenever they want, whatever state
     the request is in. What comes back is the server's rendering of the
     message, appended rather than re-fetched, so the thread does not jump. */

  function composerCount() {
    nodes.composerCount.textContent =
      nodes.composerInput.value.length + " / " + config.messageMaxChars;
  }

  function autogrow() {
    // Reset first: without it the box only ever grows, never shrinks back.
    nodes.composerInput.style.height = "auto";
    nodes.composerInput.style.height = nodes.composerInput.scrollHeight + "px";
  }

  function clearComposerFile() {
    state.draft = null;
    nodes.composerFile.value = "";
    nodes.composerFileName.textContent = "";
    show(nodes.composerFileRow, false);
  }

  function clearComposer() {
    nodes.composerInput.value = "";
    clearComposerFile();
    show(nodes.composerError, false);
    autogrow();
    composerCount();
  }

  function composerError(text) {
    nodes.composerError.textContent = text;
    show(nodes.composerError, true);
  }

  function composerBusy(isBusy) {
    nodes.composerSend.disabled = isBusy;
    nodes.composerSend.classList.toggle("button--busy", isBusy);
  }

  nodes.composerInput.setAttribute("maxlength", String(config.messageMaxChars));
  nodes.composerInput.addEventListener("input", function () {
    autogrow();
    composerCount();
    show(nodes.composerError, false);
  });

  nodes.composerAttach.addEventListener("click", function () {
    nodes.composerFile.click();
  });

  nodes.composerFileClear.addEventListener("click", clearComposerFile);

  nodes.composerFile.addEventListener("change", function () {
    var file = nodes.composerFile.files && nodes.composerFile.files[0];
    if (!file) { clearComposerFile(); return; }

    // The same two gates the server applies, checked here so an oversized file
    // is refused before it is uploaded rather than after.
    if (file.size > config.maxUploadBytes) {
      clearComposerFile();
      composerError("حجم الملف أكبر من الحد المسموح (" +
        Math.round(config.maxUploadBytes / (1024 * 1024)) + " ميغابايت).");
      return;
    }
    if (config.acceptedTypes.indexOf(file.type) === -1) {
      clearComposerFile();
      composerError("نوع الملف غير مسموح. أرفق صورة أو ملف PDF.");
      return;
    }

    state.draft = file;
    show(nodes.composerError, false);
    nodes.composerFileName.textContent = file.name + " · " + kilobytes(file.size);
    show(nodes.composerFileRow, true);
  });

  nodes.composer.addEventListener("submit", function (event) {
    event.preventDefault();
    show(nodes.composerError, false);

    var text = nodes.composerInput.value.trim();
    if (!text && !state.draft) {
      composerError("اكتب رسالة أو أرفق ملفًا.");
      nodes.composerInput.focus();
      return;
    }
    if (!state.threadFor) { return; }

    var body = new FormData();
    if (text) { body.append("body", text); }
    if (state.draft) { body.append("attachment", state.draft); }

    var url = config.messagesUrlTemplate.replace(
      "{reference}", encodeURIComponent(state.threadFor)
    );

    composerBusy(true);
    embed.call(url, { method: "POST", body: body })
      .then(function (result) {
        composerBusy(false);
        if (result.status === 201 && result.data && result.data.message) {
          show(nodes.requestThreadEmpty, false);
          var item = appendMessage(result.data.message);
          clearComposer();
          if (item.scrollIntoView) {
            item.scrollIntoView({ block: "nearest" });
          }
          return;
        }
        var data = (result && result.data) || {};
        if (result.status === 401) { embed.reauthenticate(); }
        composerError(data.detail || MESSAGES[data.error] || MESSAGES.unknown);
      })
      .catch(function () {
        composerBusy(false);
        composerError(MESSAGES.network);
      });
  });

  /* --- wiring ----------------------------------------------------------------- */

  nodes.back.addEventListener("click", back);

  // The page was rendered with the desk's state on it (step 11), so the notice
  // is up before the first catalogue call rather than after it.
  applyHours(config.hours);

  embed.onSession(function (payload) {
    if (!payload || !payload.authenticated) {
      // The handshake card takes the stage back; hide the flow rather than
      // leaving a half-filled form visible behind an expired session. The
      // countdown stops with it, and so does the thread poll — nothing behind
      // a hidden frame should be asking the server anything.
      stopClock();
      window.clearTimeout(poll.timer);
      app.hidden = true;
      return;
    }
    if (!app.hidden) { return; }

    app.hidden = false;
    go("compose", { replace: true, reset: true });
    show(nodes.back, false);
    // Before the first catalogue call, so the row paints locked-and-explained
    // rather than blank for as long as the network takes — and so the one
    // column that is open from the start has something in it. The payload
    // refills it a moment later with whatever the server says is available.
    renderTypes(DIRECTIONS.map(function (value) { return { value: value }; }));
    renderPicker();
    nodes.messageCount.textContent = "0 / " + config.messageMaxChars;
    loadOptions();
    loadHistory();
    // Idles on every screen but the request view, where it is the whole point.
    schedulePoll();
  });
})();
