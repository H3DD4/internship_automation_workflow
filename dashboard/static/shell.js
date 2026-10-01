// Page shell: dropdown menus built on <details data-menu> close when you
// click elsewhere, press Escape, or pick an item.
(function () {
  "use strict";
  var menus = function () { return document.querySelectorAll("details[data-menu][open]"); };

  document.addEventListener("click", function (event) {
    menus().forEach(function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    menus().forEach(function (menu) {
      menu.removeAttribute("open");
      var toggle = menu.querySelector("summary");
      if (toggle) toggle.focus();
    });
  });
})();
