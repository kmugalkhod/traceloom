/* Run comparison: the Only-regressed switch, auto-submitting controls, and expand/collapse cells. */
(function () {
  var form = document.querySelector("[data-compare-form]");
  if (!form) return;

  var sw = form.querySelector("[data-regressed-switch]");
  var hidden = form.querySelector("[data-regressed-input]");
  if (sw && hidden) {
    sw.addEventListener("click", function () {
      var on = sw.getAttribute("aria-checked") !== "true";
      sw.setAttribute("aria-checked", on ? "true" : "false");
      hidden.value = on ? "1" : "";
      form.requestSubmit();
    });
  }

  form.querySelectorAll("[data-autosubmit]").forEach(function (el) {
    el.addEventListener("change", function () { form.requestSubmit(); });
  });

  form.querySelectorAll("[data-expand]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var cell = btn.closest("td");
      var open = btn.getAttribute("aria-expanded") !== "true";
      cell.querySelectorAll(".out, .inp").forEach(function (p) { p.classList.toggle("open", open); });
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      btn.textContent = open ? "Show less" : "Show more";
    });
  });

  // The first selects of the picker must not submit empty slots as run ids.
  form.addEventListener("submit", function () {
    form.querySelectorAll('select[name="runs"]').forEach(function (s) { if (!s.value) s.disabled = true; });
  });
})();
