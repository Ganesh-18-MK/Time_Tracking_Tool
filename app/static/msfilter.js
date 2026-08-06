// Generic search box for a checkbox list. Drop
// `<input data-filter-checkboxes="some-container-id">` anywhere on the page
// and it live-filters that container's `.ms-opt` labels by substring match
// against the label's own text (case-insensitive) — same convention as
// tablefilter.js's `data-filter-table`, just for a scrollable checkbox list
// (multi-select filter picker, e.g. Reports -> Time by Project/Task)
// instead of a table. Checked boxes stay checked while hidden by a filter —
// filtering only changes what's visible, never what's selected.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-filter-checkboxes]").forEach(function (input) {
    var box = document.getElementById(input.getAttribute("data-filter-checkboxes"));
    if (!box) return;
    var opts = Array.prototype.slice.call(box.querySelectorAll(".ms-opt"));

    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      opts.forEach(function (opt) {
        var match = !q || opt.textContent.toLowerCase().indexOf(q) !== -1;
        opt.style.display = match ? "" : "none";
        if (match) shown++;
      });
      var noMatch = box.querySelector(".ms-no-match");
      if (noMatch) noMatch.remove();
      if (q && shown === 0) {
        var div = document.createElement("div");
        div.className = "ms-no-match muted small";
        div.textContent = 'No matches for "' + input.value.trim() + '"';
        box.appendChild(div);
      }
    });
  });
});
