/*
 * Admin navigation menus.
 *
 * Without this file the CSS opens a menu on :hover (pointer) and
 * :focus-within (keyboard), so the navigation works with it absent or blocked.
 * When it runs it takes over opening entirely: only one menu is open at a
 * time, aria-expanded stays truthful, Escape or an outside click closes, touch
 * devices — which have no hover — get a tap target, and the small-screen Menu
 * toggle works.
 *
 * Loaded as an external file rather than an inline <script> because the app's
 * Content-Security-Policy is `script-src 'self'` with no 'unsafe-inline'
 * (app.config.Settings.content_security_policy), so an inline block is refused
 * by the browser and never runs.
 */
(() => {
  const nav = document.querySelector(".admin-nav");
  if (!nav) return;

  // With the script running, .is-open is the only thing that shows a menu:
  // style.css drops its :hover/:focus-within fallback once this class is set.
  // Otherwise the CSS and the script each kept a different menu open (one
  // clicked and still focused, another hovered), so two showed at once.
  nav.classList.add("has-script");

  const groups = [...nav.querySelectorAll(".admin-nav-group")];
  const toggle = nav.querySelector(".admin-nav-toggle");

  const setOpen = (group, open) => {
    group.classList.toggle("is-open", open);
    group.querySelector(".admin-nav-trigger").setAttribute("aria-expanded", String(open));
  };

  const closeAll = (except) => {
    groups.forEach((group) => group !== except && setOpen(group, false));
  };

  // Opening any menu closes the rest, so only one is ever visible.
  const openOnly = (group) => {
    closeAll(group);
    setOpen(group, true);
  };

  groups.forEach((group) => {
    const trigger = group.querySelector(".admin-nav-trigger");
    // Recorded on pointerdown, before the press moves focus here and the
    // focusin handler opens the menu, so the click below can tell what the
    // press was and whether the menu was open beforehand.
    let press = null;

    trigger.addEventListener("pointerdown", (event) => {
      press = { type: event.pointerType, wasOpen: group.classList.contains("is-open") };
    });

    trigger.addEventListener("click", () => {
      const { type, wasOpen } = press || { type: "", wasOpen: group.classList.contains("is-open") };
      press = null;
      // A mouse click lands on a menu the hover already opened; toggling would
      // shut it under the pointer. A tap or Enter/Space toggles.
      if (type !== "mouse" && wasOpen) setOpen(group, false);
      else openOnly(group);
    });

    group.addEventListener("pointerenter", (event) => {
      if (event.pointerType === "mouse") openOnly(group);
    });
    group.addEventListener("pointerleave", (event) => {
      // A menu opened by click or keyboard holds focus; leave it open until
      // another menu opens, focus moves on, Escape, or an outside click.
      if (event.pointerType === "mouse" && !group.contains(document.activeElement)) {
        setOpen(group, false);
      }
    });
    group.addEventListener("focusin", () => openOnly(group));
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
