// Generic table search box. Drop `<input data-filter-table="some-table-id">`
// anywhere on the page and it live-filters that table's rows by substring
// match against the row's full text (case-insensitive). No page-specific
// wiring needed — every table search box on every admin page uses this.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-filter-table]").forEach(function (input) {
    var table = document.getElementById(input.getAttribute("data-filter-table"));
    if (!table) return;
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var rows = Array.prototype.slice.call(tbody.rows);
    var colCount = table.tHead ? table.tHead.rows[0].cells.length : 1;
    var noMatchRow = null;

    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      var shown = 0;
      rows.forEach(function (row) {
        var match = !q || row.textContent.toLowerCase().indexOf(q) !== -1;
        row.style.display = match ? "" : "none";
        if (match) shown++;
      });
      if (noMatchRow) { noMatchRow.remove(); noMatchRow = null; }
      if (q && shown === 0) {
        noMatchRow = document.createElement("tr");
        var td = document.createElement("td");
        td.colSpan = colCount;
        td.className = "muted";
        td.textContent = 'No matches for "' + input.value.trim() + '"';
        noMatchRow.appendChild(td);
        tbody.appendChild(noMatchRow);
      }
    });
  });
});
