/*
 * Confirmation prompts for destructive admin actions.
 *
 * Put the message in a `data-confirm` attribute on the <form>:
 *
 *     <form method="post" action="..." data-confirm="Delete X? This cannot be undone.">
 *
 * NOT `onsubmit="return confirm(...)"`. The app's Content-Security-Policy is
 * `script-src 'self'` with no 'unsafe-inline'
 * (app.config.Settings.content_security_policy), which blocks inline event
 * handler attributes as well as inline <script> blocks — a form written that
 * way submits immediately with no prompt at all.
 */
(() => {
  document.addEventListener(
    "submit",
    (event) => {
      const form = event.target.closest("form[data-confirm]");
      if (!form) return;
      if (!window.confirm(form.dataset.confirm)) {
        event.preventDefault();
      }
    },
    true,
  );
})();
