/* =========================================================================
   The live quote in static/js/flow.js
   -------------------------------------------------------------------------
   The figure a client reads while typing and the figure the server stores at
   submission come out of two different languages, and they are not allowed to
   disagree — a preview that says 234,050 against a confirmation that says
   234,000 is the desk fielding a phone call.

   So both sides are pinned to one committed table, tests/pricing_cases.json:
   apps/portal/tests/test_flow.py asserts pricing.price produces it, and this
   file asserts the shipped flow.js displays it. Either one drifting fails its
   own test, and the table is the thing that says which is wrong.

   The table carries a zero-commission rate as well as a charging one, because
   zero is what this desk actually charges and it is the case that broke when
   the rounding was being taken out of the fee.
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

/** The quote flow.js paints for one case of the table. */
function priced(testCase) {
  const harness = boot({
    options: catalogue(
      rate({
        iqd_per_usd: testCase.iqd_per_usd,
        commission_iqd_per_100usd: testCase.commission_iqd_per_100usd,
        commission_sign: SIGN[testCase.direction],
      })
    ),
  });
  return harness.start().then(() => {
    harness.type(testCase.amount_usd);
    const shown = harness.quote();
    return {
      converted: figure(shown.converted),
      commission: figure(shown.commission),
      rounding: figure(shown.rounding),
      total: figure(shown.total),
      commissionShown: shown.commissionShown,
      roundingShown: shown.roundingShown,
      raw: shown,
    };
  });
}

/** "153,000 د.ع" back to 153000, "− 50 د.ع" to -50; null for the placeholder. */
function figure(text) {
  if (!text || text.indexOf("·") !== -1) { return null; }
  const negative = text.indexOf("−") !== -1 || text.indexOf("-") !== -1;
  const digits = Number(text.replace(/[^0-9.]/g, ""));
  return negative ? -digits : digits;
}

function describe(testCase) {
  return `${testCase.direction} $${testCase.amount_usd} at ${testCase.iqd_per_usd}`
    + ` / fee ${testCase.commission_iqd_per_100usd}`;
}

/* --- the contract both sides are held to --------------------------------- */

test("the quote matches apps/portal/pricing case for case", async () => {
  for (const expected of CONTRACT.cases) {
    const shown = await priced(expected);

    assert.deepEqual(
      {
        converted: shown.converted,
        commission: shown.commission,
        rounding: shown.rounding,
        total: shown.total,
      },
      {
        converted: Number(expected.converted_iqd),
        commission: Number(expected.commission_iqd),
        rounding: Number(expected.rounding_iqd),
        total: Number(expected.total_iqd),
      },
      `${describe(expected)} shown as ${JSON.stringify(shown.raw)}`
    );
  }
});

/* --- and the rule the contract encodes ----------------------------------- */

test("every total shown is a whole thousand dinars", async () => {
  for (const expected of CONTRACT.cases) {
    const shown = await priced(expected);
    assert.equal(
      shown.total % CONTRACT.transfer_step,
      0,
      `${describe(expected)} → ${shown.total}`
    );
  }
});

test("the lines on screen reconcile to the total", async () => {
  for (const expected of CONTRACT.cases) {
    const shown = await priced(expected);
    const sum =
      shown.converted + SIGN[expected.direction] * shown.commission + shown.rounding;
    assert.equal(sum, shown.total, describe(expected));
  }
});

test("the conversion line is left at amount × rate, not rounded with the total", async () => {
  // 155 × 1,510 = 234,050. If the rounding had been applied to the conversion
  // instead of carried on its own line, this would read 234,000 and no client
  // could check it against the published rate.
  const shown = await priced({
    direction: "deposit",
    amount_usd: "155.00",
    iqd_per_usd: "1510.00",
    commission_iqd_per_100usd: "0.00",
  });

  assert.equal(shown.converted, 234050);
  assert.equal(shown.total, 234000);
  assert.equal(shown.rounding, -50);
});

/* --- the rows that disappear --------------------------------------------- */

test("the commission row is hidden when there is no commission", async () => {
  const shown = await priced({
    direction: "deposit",
    amount_usd: "155.00",
    iqd_per_usd: "1510.00",
    commission_iqd_per_100usd: "0.00",
  });

  assert.equal(shown.commission, 0);
  assert.equal(shown.commissionShown, false, "a fee of zero is not news");
});

test("the commission row is shown when there is one", async () => {
  const shown = await priced({
    direction: "deposit",
    amount_usd: "100.00",
    iqd_per_usd: "1470.00",
    commission_iqd_per_100usd: "5000.00",
  });

  assert.equal(shown.commission, 5000);
  assert.equal(shown.commissionShown, true);
});

test("the rounding row is hidden when the total landed on a thousand by itself", async () => {
  // 100 × 1,510 = 151,000 exactly, with no fee: nothing to round.
  const shown = await priced({
    direction: "deposit",
    amount_usd: "100.00",
    iqd_per_usd: "1510.00",
    commission_iqd_per_100usd: "0.00",
  });

  assert.equal(shown.rounding, 0);
  assert.equal(shown.roundingShown, false);
});

test("the rounding row is shown, with its sign, when the company moved the total", async () => {
  const down = await priced({
    direction: "deposit",
    amount_usd: "155.00",
    iqd_per_usd: "1510.00",
    commission_iqd_per_100usd: "0.00",
  });
  assert.equal(down.roundingShown, true);
  assert.equal(down.rounding, -50);
  assert.match(down.raw.rounding, /−/, "a negative rounding reads as a subtraction");

  const up = await priced({
    direction: "deposit",
    amount_usd: "100.55",
    iqd_per_usd: "1510.00",
    commission_iqd_per_100usd: "0.00",
  });
  assert.equal(up.roundingShown, true);
  assert.equal(up.rounding, 169);
  assert.match(up.raw.rounding, /\+/, "a positive rounding says so out loud");
});

/* --- rounding behaviour --------------------------------------------------- */

test("it rounds to the nearest thousand, down as readily as up", async () => {
  const free = { iqd_per_usd: "1510.00", commission_iqd_per_100usd: "0.00" };

  // 15,100 is nearer 15,000.
  assert.equal(
    (await priced({ direction: "deposit", amount_usd: "10.00", ...free })).total,
    15000
  );
  // 16,610 is nearer 17,000.
  assert.equal(
    (await priced({ direction: "deposit", amount_usd: "11.00", ...free })).total,
    17000
  );
});

test("a fee landing exactly on a half dinar rounds up, as Decimal does", async () => {
  // $99.99 at 5,000 per 100 is exactly 4,999.5. In binary `(99.99 / 100) * 5000`
  // comes out a hair under and Math.round took it to 4,999 — one dinar below
  // what the server charges. The arithmetic runs in hundredths to stop that.
  const shown = await priced({
    direction: "deposit",
    amount_usd: "99.99",
    iqd_per_usd: "1470.00",
    commission_iqd_per_100usd: "5000.00",
  });

  assert.equal(shown.commission, 5000);
});

test("a halfway total goes up, the way Decimal ROUND_HALF_UP does", async () => {
  const harness = boot({
    options: catalogue(
      rate({ iqd_per_usd: "1500.00", commission_iqd_per_100usd: "0.00" })
    ),
  });
  await harness.start();

  harness.type("1.00"); // 1,500 exactly.

  assert.equal(figure(harness.quote().total), 2000);
});

test("a payout the fee rounds away to nothing shows no figure at all", async () => {
  // The server refuses this one outright, so the preview must not offer a
  // number for it either — 1,470 converted against a 1,470 fee leaves zero.
  const harness = boot({
    options: catalogue(
      rate({
        iqd_per_usd: "1470.00",
        commission_iqd_per_100usd: "147000.00",
        commission_sign: -1,
      })
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
    const shown = harness.quote();
    assert.equal(figure(shown.total), null, `typed ${typed}`);
    // And the rounding row does not linger from whatever was typed before it.
    assert.equal(shown.roundingShown, false, `typed ${typed}`);
  }
});
