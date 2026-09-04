/* =========================================================================
   MaxPay — copying a one-time secret off the screen
   -------------------------------------------------------------------------
   Loaded only by the page that has just issued a password, and only while it
   is displaying one. It exists because that password is shown exactly once:
   the alternative is an administrator retyping twenty characters into a chat
   window, which is how a character gets dropped and a new account cannot be
   logged into.

   Deliberately not folded into panel.js. That file is the ten-second poll and
   nothing else, and a clipboard helper living inside it would be loaded on
   every screen in both panels to be used on one.

   No inline handler: the panels ship `script-src 'self'` (spec §11).
   ========================================================================= */

(function () {
  "use strict";

  var button = document.getElementById("secret-copy");
  if (!button) { return; }

  var source = document.getElementById(button.getAttribute("data-copy-target"));
  if (!source) { return; }

  var original = button.textContent;
  var timer = null;

  function said(message) {
    window.clearTimeout(timer);
    button.textContent = message;
    timer = window.setTimeout(function () { button.textContent = original; }, 2500);
  }

  function legacy(text) {
    // The async clipboard needs a permission the browser may withhold from a
    // page it does not consider trusted. execCommand is the fallback rather
    // than the other way round because it works without one.
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
    said(copied ? "تم النسخ" : "انسخها يدويًا");
  }

  button.addEventListener("click", function () {
    var text = source.textContent.trim();
    if (!text) { return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () { said("تم النسخ"); },
        function () { legacy(text); }
      );
    } else {
      legacy(text);
    }
  });
})();
