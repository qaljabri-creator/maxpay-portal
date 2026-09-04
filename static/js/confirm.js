/* =========================================================================
   MaxPay — a confirmation step for the two actions that do not come back
   -------------------------------------------------------------------------
   Archiving a merchant and getting rid of a wallet are the only controls in
   the panels a stray click cannot be undone from. Everything else here toggles
   something that can be toggled straight back.

   `data-confirm` on a submit button, and the form does not go anywhere until
   the operator has read it. Deliberately `window.confirm`: a modal built here
   would be a dialog to keep accessible, focus-trapped and translated, and the
   browser already ships one that is all three.

   No inline handlers — the internal panels ship a CSP with `script-src 'self'`
   (spec §11), and an `onsubmit` attribute would need it relaxed. One delegated
   listener on the document, so a control rendered by a live-refreshed fragment
   is covered without anything re-binding it.
   ========================================================================= */

(function () {
  "use strict";

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.tagName !== "FORM") { return; }

    // The button that was pressed, because one form can hold two of them with
    // different consequences. `submitter` is unset for a form submitted by
    // pressing Enter in a field, so the form's own attribute is the fallback.
    var trigger = event.submitter || form.querySelector("[data-confirm]");
    if (!trigger) { return; }

    var question = trigger.getAttribute("data-confirm") ||
                   form.getAttribute("data-confirm");
    if (!question) { return; }

    if (!window.confirm(question)) {
      event.preventDefault();
    }
  });
})();
