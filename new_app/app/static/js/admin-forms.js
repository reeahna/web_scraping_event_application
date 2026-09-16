/*
 * Declarative form behaviours for the admin.
 *
 * All three replace inline handler attributes, which cannot be used here: the
 * app's Content-Security-Policy is `script-src 'self'` with no 'unsafe-inline'
 * (app.config.Settings.content_security_policy), so an onsubmit= or onchange=
 * attribute is refused by the browser and never runs. A confirmation written
 * that way does not prompt, it just submits.
 *
 *   <form data-confirm="Delete X? This cannot be undone.">
 *       ask before submitting, and cancel if the reader declines
 *
 *   <input data-submit-on-change>
 *       submit the owning form as soon as the value changes
 *
 *   <form data-disable-on-submit>
 *       disable the submit button once submitted, against a double click on a
 *       long-running action
 *
 * Listeners are delegated from the document and registered in the capture
 * phase so a confirmation runs before any other submit handler.
 */
(() => {
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target.closest("form");
      if (!form) return;

      if (form.dataset.confirm !== undefined && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }

      if (form.dataset.disableOnSubmit !== undefined) {
        const button = form.querySelector("button[type=submit], button:not([type])");
        // Deferred: disabling a submit button during its own submit event can
        // drop the button's name/value from the payload in some browsers.
        if (button) setTimeout(() => { button.disabled = true; }, 0);
      }
    },
    true,
  );

  document.addEventListener("change", (event) => {
    const control = event.target.closest("[data-submit-on-change]");
    if (control && control.form) control.form.requestSubmit();
  });
})();
