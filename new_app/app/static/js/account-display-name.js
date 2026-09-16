/*
 * Display-name editing on the account page.
 *
 * Progressive enhancement: without this the edit button still works, because
 * it submits GET /account?edit=1 and the server renders the form. This just
 * skips the round trip.
 *
 * External rather than an inline <script> because the app's CSP is
 * `script-src 'self'` with no 'unsafe-inline', which blocks inline blocks.
 */
(() => {
  const editRequest = document.getElementById("display-name-edit-request");
  const readOnlyView = document.getElementById("display-name-readonly");
  const editForm = document.getElementById("display-name-edit-form");
  const displayNameInput = document.getElementById("display_name");

  if (!editRequest || !readOnlyView || !editForm || !displayNameInput) return;

  editRequest.addEventListener("submit", (event) => {
    event.preventDefault();
    readOnlyView.hidden = true;
    editForm.hidden = false;
    displayNameInput.focus();
  });
})();
