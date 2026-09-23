/* =========================================================================
   The client's history pills, as static/js/flow.js paints them
   -------------------------------------------------------------------------
   The server words credited and closed the same for the client
   (apps/portal/payloads.status_label). This holds the colour to the words:
   one label, one look. The rows are fed through the real history call and
   the pills read back off the list flow.js built.
   ========================================================================= */

// Run:  node --test "tests/js/**/*.test.js"

"use strict";

const assert = require("node:assert/strict");
const { test } = require("node:test");

const { boot, catalogue, rate } = require("./flow_harness.js");

const ADDED = "أُضيف المبلغ إلى حسابك";

function row(reference, status, label) {
  return {
    reference,
    type: "deposit",
    type_label: "إيداع",
    status,
    status_label: label,
    is_closed: status === "closed",
    amount_usd: "100.00",
    amount_iqd: "152000.00",
    method: "زين كاش",
    submitted_at: "2026-09-20T10:00:00+03:00",
  };
}

/** Every pill in the history list, keyed by the text beside it. */
function pills(harness) {
  const found = [];
  (function walk(node) {
    (node.children || []).forEach((child) => {
      if (/(^|\s)pill(\s|$)/.test(child.className || "")) {
        found.push({ text: child.textContent, className: child.className });
      }
      walk(child);
    });
  })(harness.node("history-list"));
  return found;
}

async function history(rows) {
  const harness = boot({ options: catalogue(rate()), requests: rows });
  await harness.start();
  await Promise.resolve();
  return pills(harness);
}

test("credited and closed wear the same pill when they say the same thing", async () => {
  const shown = await history([
    row("MP-10001", "credited", ADDED),
    row("MP-10002", "closed", ADDED),
  ]);

  assert.equal(shown.length, 2);
  assert.equal(shown[0].text, ADDED);
  assert.equal(shown[1].text, ADDED);
  assert.equal(shown[0].className, shown[1].className);
  assert.match(shown[0].className, /pill--closed/);
});

test("the other statuses keep their own look", async () => {
  const shown = await history([
    row("MP-10003", "assigned", "بانتظار التاجر"),
    row("MP-10004", "rejected", "مرفوض"),
    row("MP-10005", "cancelled", "مُلغى"),
  ]);

  assert.doesNotMatch(shown[0].className, /pill--/);
  assert.match(shown[1].className, /pill--rejected/);
  assert.match(shown[2].className, /pill--cancelled/);
});
