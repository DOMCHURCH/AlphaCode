/* /blog: filter by section and re-order, without a page load.
 *
 * Progressive enhancement. The server renders every post under its section
 * heading, so with this file blocked the page is a complete list. The tools
 * ship `hidden`; this script shows them and hides the jump links they replace.
 */
(function () {
  "use strict";
  var tools = document.getElementById("blog-tools");
  if (!tools) return;

  var jump = document.getElementById("blog-jump");
  var status = document.getElementById("blog-status");
  var sections = [].slice.call(document.querySelectorAll(".blog-sec"));
  var filters = [].slice.call(tools.querySelectorAll("[data-filter]"));
  var sorts = [].slice.call(tools.querySelectorAll("[data-sort]"));
  var current = "all";

  tools.hidden = false;
  if (jump) jump.hidden = true;

  function num(li, key) {
    return parseInt(li.getAttribute(key), 10) || 0;
  }

  var ORDER = {
    // newest first; the registry order (also newest first) breaks ties
    "new": function (a, b) {
      var da = a.getAttribute("data-date"), db = b.getAttribute("data-date");
      return da === db ? num(a, "data-i") - num(b, "data-i") : (da < db ? 1 : -1);
    },
    "old": function (a, b) {
      var da = a.getAttribute("data-date"), db = b.getAttribute("data-date");
      return da === db ? num(b, "data-i") - num(a, "data-i") : (da < db ? -1 : 1);
    },
    "short": function (a, b) {
      var d = num(a, "data-min") - num(b, "data-min");
      return d !== 0 ? d : ORDER["new"](a, b);
    }
  };

  function mark(buttons, test) {
    buttons.forEach(function (b) {
      var on = test(b);
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function applyFilter(name, remember) {
    if (!sections.some(function (s) { return s.getAttribute("data-section") === name; })) {
      name = "all";
    }
    current = name;
    var shown = 0;
    sections.forEach(function (s) {
      var show = name === "all" || s.getAttribute("data-section") === name;
      s.hidden = !show;
      if (show) shown += s.querySelectorAll(".post-item").length;
    });
    mark(filters, function (b) { return b.getAttribute("data-filter") === name; });
    if (status) {
      status.textContent = shown + (shown === 1 ? " note" : " notes") +
        (name === "all" ? "" : " in this section");
    }
    if (remember) {
      try {
        history.replaceState(null, "", name === "all" ? location.pathname : "#" + name);
      } catch (e) { /* the filter still works without a URL for it */ }
    }
  }

  function applySort(key) {
    var cmp = ORDER[key] || ORDER["new"];
    sections.forEach(function (s) {
      var list = s.querySelector(".post-list");
      var items = [].slice.call(list.children).sort(cmp);
      items.forEach(function (li) { list.appendChild(li); });
    });
    mark(sorts, function (b) { return b.getAttribute("data-sort") === key; });
  }

  filters.forEach(function (b) {
    b.addEventListener("click", function () {
      applyFilter(b.getAttribute("data-filter"), true);
    });
  });
  sorts.forEach(function (b) {
    b.addEventListener("click", function () { applySort(b.getAttribute("data-sort")); });
  });

  // A link such as /blog#data-quality opens with that section chosen.
  var start = (location.hash || "").replace(/^#/, "");
  applyFilter(start || "all", false);
})();
