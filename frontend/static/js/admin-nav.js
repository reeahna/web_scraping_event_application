/*
 * Admin navigation menus.
 *
 * The CSS already opens a menu on :hover (pointer) and :focus-within
 * (keyboard), so the navigation works with this file absent or blocked. This
 * only refines that behaviour: it keeps aria-expanded truthful, closes on
 * Escape or an outside click, gives touch devices — which have no hover — a
 * tap target, and drives the small-screen Menu toggle.
 *
 * Loaded as an external file rather than an inline <script> because the app's
 * Content-Security-Policy is `script-src 'self'` with no 'unsafe-inline'
 * (app.config.Settings.content_security_policy), so an inline block is refused
 * by the browser and never runs.
 */
(() => {
  const nav = document.querySelector(".admin-nav");
  if (!nav) return;

  const groups = [...nav.querySelectorAll(".admin-nav-group")];
  const toggle = nav.querySelector(".admin-nav-toggle");

  const setOpen = (group, open) => {
    group.classList.toggle("is-open", open);
    group.querySelector(".admin-nav-trigger").setAttribute("aria-expanded", String(open));
  };

  const closeAll = (except) => {
    groups.forEach((group) => group !== except && setOpen(group, false));
  };

  groups.forEach((group) => {
    const trigger = group.querySelector(".admin-nav-trigger");

    trigger.addEventListener("click", () => {
      const open = !group.classList.contains("is-open");
      closeAll(group);
      setOpen(group, open);
    });

    group.addEventListener("pointerenter", (event) => {
      if (event.pointerType === "mouse") setOpen(group, true);
    });
    group.addEventListener("pointerleave", (event) => {
      if (event.pointerType === "mouse") setOpen(group, false);
    });
    group.addEventListener("focusin", () => setOpen(group, true));
    group.addEventListener("focusout", (event) => {
      if (!group.contains(event.relatedTarget)) setOpen(group, false);
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const open = groups.find((group) => group.classList.contains("is-open"));
    if (!open) return;
    open.querySelector(".admin-nav-trigger").focus();
    setOpen(open, false);
  });

  document.addEventListener("click", (event) => {
    if (!nav.contains(event.target)) closeAll(null);
  });

  if (toggle) {
    toggle.addEventListener("click", () => {
      const open = nav.classList.toggle("is-expanded");
      toggle.setAttribute("aria-expanded", String(open));
      if (!open) closeAll(null);
    });
  }
})();
