/* =========================================================================
   The live quote in static/js/flow.js
   -------------------------------------------------------------------------
   The figure a client reads while typing and the figure the server stores at
   submission come out of two different languages, and they are not allowed to
   disagree — a preview that says 152,000 and a confirmation that says 151,847
   is the desk fielding a phone call.

   So both sides are pinned to one committed table, tests/pricing_cases.json:
   apps/portal/tests/test_flow.py asserts pricing.price produces it, and this
   file asserts the shipped flow.js displays it. Either one drifting fails its
   own test, and the table is the thing that says which is wrong.

   ========================================================================= */

// Run:  node --test "tests/js/**/*.test.js"

"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");

const { boot, catalogue, rate } = require("./flow_harness.js");

const CONTRACT = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "pricing_cases.json"), "utf8")
);

const SIGN = { deposit: 1, withdrawal: -1 };

/** The quote flow.js paints for one amount, as three plain integers. */
function priced(direction, amount) {
  const harness = boot({
    options: catalogue(
      rate({
        iqd_per_usd: CONTRACT.iqd_per_usd,
        commission_iqd_per_100usd: CONTRACT.commission_iqd_per_100usd,
        commission_sign: SIGN[direction],
      })
    ),
  });
  return harness.start().then(() => {
    harness.type(amount);
    const shown = harness.quote();
    return {
      converted: figure(shown.converted),
      commission: figure(shown.commission),
      total: figure(shown.total),
      raw: shown,
    };
  });
}

/** "153,000 د.ع" back to 153000; null for the "nothing to show" placeholder. */
function figure(text) {
  if (!text || text.indexOf("·") !== -1) { return null; }
  return Number(text.replace(/[^0-9.-]/g, ""));
}

/* --- the contract both sides are held to --------------------------------- */

test("the quote matches apps/portal/pricing case for case", async () => {
  for (const expected of CONTRACT.cases) {
    const shown = await priced(expected.direction, expected.amount_usd);

    assert.deepEqual(
      {
        converted: shown.converted,
        commission: shown.commission,
        total: shown.total,
      },
      {
        converted: Number(expected.converted_iqd),
        commission: Number(expected.commission_iqd),
        total: Number(expected.total_iqd),
      },
      `${expected.direction} $${expected.amount_usd} shown as ${JSON.stringify(shown.raw)}`
    );
  }
});

/* --- and the rule the contract encodes ----------------------------------- */

test("every total shown is a whole thousand dinars", async () => {
  for (const expected of CONTRACT.cases) {
    const shown = await priced(expected.direction, expected.amount_usd);
    assert.equal(
      shown.total % CONTRACT.transfer_step,
      0,
      `${expected.direction} $${expected.amount_usd} → ${shown.total}`
    );
  }
});

test("the three lines on screen add up to the total", async () => {
  for (const expected of CONTRACT.cases) {
    const shown = await priced(expected.direction, expected.amount_usd);
    const sum = shown.converted + SIGN[expected.direction] * shown.commission;
    assert.equal(sum, shown.total, `${expected.direction} $${expected.amount_usd}`);
  }
});

test("the conversion line is left at amount × rate, not rounded with the total", async () => {
  // 100.55 × 1,470 = 147,808.5 → 147,809. If the rounding had been applied to
  // the conversion instead of absorbed by the fee, this would read 148,000 and
  // no client could check it against the published rate.
  const shown = await priced("deposit", "100.55");

  assert.equal(shown.converted, 147809);
  assert.notEqual(shown.converted % CONTRACT.transfer_step, 0);
});

test("it rounds to the nearest thousand, down as readily as up", async () => {
  // 14,700 + 500 = 15,200, which is nearer 15,000.
  assert.equal((await priced("deposit", "10.00")).total, 15000);
  // 16,170 + 550 = 16,720, which is nearer 17,000.
  assert.equal((await priced("deposit", "11.00")).total, 17000);
});

test("a halfway total goes up, the way Decimal ROUND_HALF_UP does", async () => {
  const harness = boot({
    options: catalogue(
      rate({ iqd_per_usd: "1500.00", commission_iqd_per_100usd: "0.00" })
    ),
  });
  await harness.start();

  harness.type("1.00"); // 1,500 exactly, with no fee to tip it either way.

  assert.equal(figure(harness.quote().total), 2000);
});

test("a payout the fee rounds away to nothing shows no figure at all", async () => {
  // The server refuses this one outright, so the preview must not offer a
  // number for it either — 1,470 converted against a 1,470 fee leaves zero.
  const harness = boot({
    options: catalogue(
      rate({ commission_iqd_per_100usd: "147000.00", commission_sign: -1 })
    ),
  });
  await harness.start();

  harness.type("1.00");

  assert.equal(figure(harness.quote().total), null);
});

test("an empty or nonsense amount quotes nothing rather than zero", async () => {
  const harness = boot({ options: catalogue(rate()) });
  await harness.start();

  for (const typed of ["", "مئة", "-5"]) {
    harness.type(typed);
    assert.equal(figure(harness.quote().total), null, `typed ${typed}`);
  }
});
