// Closed-by-default multi-select dropdown (Ganesh, 2026-08-30 — first built
// inline in admin/overtime.html for the Surplus day(s) field: "like
// shortfall day field only but once we click it should open dropdown and
// we can select multiple...by mouse click"; promoted to this shared file
// the same day once Reports -> Time by Project/Task needed the identical
// widget for its Employees/Projects/Tasks filters — same "once a second
// page needs it, it becomes a shared file" precedent as combo.js).
//
// Markup contract (see .msdrop/.msdrop-toggle/.msdrop-menu in app.css):
//   <div class="msdrop" data-empty-label="All Employees" data-noun-plural="employees">
//     <button type="button" class="msdrop-toggle">
//       <span class="msdrop-label">All Employees</span>
//       <span class="msdrop-caret">▾</span>
//     </button>
//     <div class="msdrop-menu" hidden> ...checkboxes... </div>
//   </div>
//
// No inline onclick/onchange needed anywhere — this file auto-wires every
// .msdrop-toggle's click and every checkbox change inside a .msdrop via
// event delegation, same DOMContentLoaded/querySelectorAll convention as
// msfilter.js/tablefilter.js elsewhere in this app.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".msdrop-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var wrap = btn.closest(".msdrop");
      var menu = wrap.querySelector(".msdrop-menu");
      var willOpen = menu.hidden;
      document.querySelectorAll(".msdrop-menu").forEach(function (m) { m.hidden = true; });
      menu.hidden = !willOpen;
    });
  });

  document.addEventListener("click", function (ev) {
    if (!ev.target.closest(".msdrop")) {
      document.querySelectorAll(".msdrop-menu").forEach(function (m) { m.hidden = true; });
    }
  });

  document.addEventListener("change", function (ev) {
    if (ev.target.type !== "checkbox") return;
    var wrap = ev.target.closest(".msdrop");
    if (!wrap) return;
    updateMsDropLabel(wrap);
  });

  // Initialize every toggle's label from whatever's already checked on
  // load (e.g. a filter re-applied from the querystring) — same reason
  // Overtime's own version never needed a separate init pass: there, the
  // field always starts empty. Here it often doesn't.
  document.querySelectorAll(".msdrop").forEach(updateMsDropLabel);
});

function updateMsDropLabel(wrap) {
  var label = wrap.querySelector(".msdrop-label");
  if (!label) return;
  var checked = wrap.querySelectorAll("input[type=checkbox]:checked");
  var emptyLabel = wrap.getAttribute("data-empty-label") || "None selected";
  var nounPlural = wrap.getAttribute("data-noun-plural") || "selected";
  if (checked.length === 0) {
    label.textContent = emptyLabel;
  } else if (checked.length === 1) {
    label.textContent = checked[0].closest("label").textContent.trim();
  } else {
    label.textContent = checked.length + " " + nounPlural + " selected";
  }
}
