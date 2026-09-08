// Global double-submit guard (Ganesh, 2026-09-08 bugfix) — a real Task Log
// row was found logged twice, byte-for-byte identical. Root cause: no form
// on the whole site protected against being submitted twice — a
// double-click, a double-tap on mobile, or a slow connection making the
// employee click again, could fire two POSTs before the first one's
// redirect ever navigates the page away. Every page in this app does a
// plain full-page POST-then-redirect (no fetch/AJAX anywhere), so simply
// disabling whichever button triggered the submit — the instant the form
// actually submits — closes off the common single-device case for good;
// the page is about to navigate away either way, so there's nothing to
// "re-enable" on success. See app/routes/employee.py's own
// _is_recent_duplicate_entry() for the server-side backstop this doesn't
// replace (a resubmission that still slips past this, e.g. two different
// devices/tabs racing, or a network-level retry this page never saw).
//
// Must NOT disable the button when a form's own onsubmit="return
// confirm(...)" handler was cancelled (user clicked Cancel) — that
// handler runs before this one (inline attribute handlers are registered
// at parse time, ahead of this file's later addEventListener call), so by
// the time this listener runs, event.defaultPrevented already correctly
// reflects whether the confirm() was accepted. Skipping on
// defaultPrevented also means a form blocked by native HTML5 validation
// (a blank `required` field) is unaffected — the browser never dispatches
// a `submit` event for that case at all, so this listener doesn't even
// run, and the button stays clickable so the employee can fix the field
// and try again.
document.addEventListener("submit", function (e) {
  if (e.defaultPrevented) return;
  var btn = e.submitter || e.target.querySelector('button[type="submit"], button:not([type])');
  if (btn && !btn.disabled) {
    btn.disabled = true;
  }
});
