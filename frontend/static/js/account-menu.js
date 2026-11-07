// Closes the header's "My Account" menu (a <details> element) on Escape or a
// click outside it. Opening and closing by click already works without this.
(function () {
  "use strict";
  var menus = document.querySelectorAll("details.account-menu");
  if (!menus.length) return;

  document.addEventListener("click", function (event) {
    menus.forEach(function (menu) {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    menus.forEach(function (menu) {
      if (!menu.open) return;
      menu.open = false;
      var summary = menu.querySelector("summary");
      if (summary) summary.focus();
    });
  });
})();
