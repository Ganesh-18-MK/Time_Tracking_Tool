/*
 * Progressive-enhancement MM/DD/YYYY date picker (Ganesh, 2026-08-21).
 *
 * Native <input type="date"> displays whichever date format the user's
 * OS/browser locale is set to (dd/mm/yyyy, mm/dd/yyyy, ...) — confirmed
 * there is no HTML/CSS/`lang`-attribute way to override that; only the
 * OS locale controls it. Employees were seeing dd/mm/yyyy and weren't
 * sure which end was the day vs the month. This replaces every
 * <input type="date"> on the page with a small self-built text field
 * that always reads/writes mm/dd/yyyy, plus a lightweight calendar
 * dropdown to pick a date without typing. Plain vanilla JS, no
 * dependency, no CDN — matches combo.js's existing "no new deps"
 * convention.
 *
 * The original <input type="date"> stays in the DOM (visually hidden,
 * never removed) and is still the thing that actually submits with the
 * form, under its original `name`, still holding an ISO (yyyy-mm-dd)
 * value — so every server-side date parse (parse_date_field /
 * dt.date.fromisoformat, per CLAUDE.md's ISO-value hard rule for
 * <input type="date">) needed zero changes. If this script fails to run
 * for any reason, the native input was never removed or disabled — the
 * page degrades to exactly how it looked before this file existed.
 */
(function () {
  "use strict";

  function pad2(n) { return String(n).padStart(2, "0"); }

  function isoToMdy(iso) {
    if (!iso) return "";
    const parts = iso.split("-");
    if (parts.length !== 3) return "";
    const [y, mo, d] = parts;
    if (!y || !mo || !d) return "";
    return mo + "/" + d + "/" + y;
  }

  // "" for anything that isn't a real, complete mm/dd/yyyy date —
  // callers treat "" as "leave the underlying value blank", same as
  // every other optional date field in this app already does.
  function mdyToIso(mdy) {
    const match = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec((mdy || "").trim());
    if (!match) return "";
    const mm = parseInt(match[1], 10), dd = parseInt(match[2], 10), yyyy = parseInt(match[3], 10);
    if (mm < 1 || mm > 12 || dd < 1 || dd > 31 || yyyy < 1000) return "";
    // Round-trip through Date to reject e.g. Feb 30 instead of silently
    // rolling it into March.
    const check = new Date(yyyy, mm - 1, dd);
    if (check.getFullYear() !== yyyy || check.getMonth() !== mm - 1 || check.getDate() !== dd) return "";
    return yyyy + "-" + pad2(mm) + "-" + pad2(dd);
  }

  // Auto-insert the two slashes as digits are typed; never touches
  // anything once four slash-separated groups aren't the shape anymore
  // (so pasting an already-formatted date still works).
  function autoSlash(raw) {
    const digits = raw.replace(/\D/g, "").slice(0, 8);
    if (digits.length > 4) return digits.slice(0, 2) + "/" + digits.slice(2, 4) + "/" + digits.slice(4);
    if (digits.length > 2) return digits.slice(0, 2) + "/" + digits.slice(2);
    return digits;
  }

  const MONTHS = ["January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"];
  const DOW = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"];

  function buildCalendar(getIso, setIso) {
    let pop = null;

    function onDocClick(e) {
      if (pop && !pop.contains(e.target) && e.target.dataset.mdyCalBtn === undefined) close();
    }

    function close() {
      if (pop) {
        pop.remove();
        pop = null;
        document.removeEventListener("click", onDocClick, true);
        document.removeEventListener("keydown", onKeydown, true);
      }
    }

    function onKeydown(e) {
      if (e.key === "Escape") close();
    }

    function open(anchorWrap) {
      close();
      const iso = getIso();
      const base = iso ? new Date(iso + "T00:00:00") : new Date();
      let viewYear = base.getFullYear();
      let viewMonth = base.getMonth();

      pop = document.createElement("div");
      pop.className = "mdy-cal";

      function render() {
        pop.innerHTML = "";
        const head = document.createElement("div");
        head.className = "mdy-cal-head";
        const prev = document.createElement("button");
        prev.type = "button";
        prev.className = "mdy-cal-nav";
        prev.textContent = "‹";
        prev.setAttribute("aria-label", "Previous month");
        const label = document.createElement("span");
        label.textContent = MONTHS[viewMonth] + " " + viewYear;
        const next = document.createElement("button");
        next.type = "button";
        next.className = "mdy-cal-nav";
        next.textContent = "›";
        next.setAttribute("aria-label", "Next month");
        prev.addEventListener("click", function () {
          viewMonth--;
          if (viewMonth < 0) { viewMonth = 11; viewYear--; }
          render();
        });
        next.addEventListener("click", function () {
          viewMonth++;
          if (viewMonth > 11) { viewMonth = 0; viewYear++; }
          render();
        });
        head.appendChild(prev);
        head.appendChild(label);
        head.appendChild(next);
        pop.appendChild(head);

        const grid = document.createElement("div");
        grid.className = "mdy-cal-grid";
        DOW.forEach(function (d) {
          const el = document.createElement("span");
          el.className = "mdy-cal-dow";
          el.textContent = d;
          grid.appendChild(el);
        });
        const firstDow = new Date(viewYear, viewMonth, 1).getDay();
        const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
        for (let i = 0; i < firstDow; i++) grid.appendChild(document.createElement("span"));
        const now = new Date();
        for (let d = 1; d <= daysInMonth; d++) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "mdy-cal-day";
          btn.textContent = String(d);
          const thisIso = viewYear + "-" + pad2(viewMonth + 1) + "-" + pad2(d);
          if (thisIso === iso) btn.classList.add("sel");
          if (viewYear === now.getFullYear() && viewMonth === now.getMonth() && d === now.getDate()) {
            btn.classList.add("today");
          }
          btn.addEventListener("click", function () { setIso(thisIso); close(); });
          grid.appendChild(btn);
        }
        pop.appendChild(grid);
      }

      render();
      anchorWrap.appendChild(pop);
      // Deferred so the click that opened the popup doesn't immediately
      // close it via the same listener.
      setTimeout(function () {
        document.addEventListener("click", onDocClick, true);
        document.addEventListener("keydown", onKeydown, true);
      }, 0);
    }

    return { open: open, close: close };
  }

  function enhance(native) {
    if (native.dataset.mdyEnhanced) return;
    native.dataset.mdyEnhanced = "1";

    const wasRequired = native.hasAttribute("required");

    const wrap = document.createElement("span");
    wrap.className = "mdy-wrap";
    native.parentNode.insertBefore(wrap, native);
    wrap.appendChild(native);

    const text = document.createElement("input");
    text.type = "text";
    text.className = "mdy-text";
    text.placeholder = "mm/dd/yyyy";
    text.inputMode = "numeric";
    text.autocomplete = "off";
    text.maxLength = 10;
    text.value = isoToMdy(native.value);
    if (native.disabled) text.disabled = true;
    if (native.id) text.id = native.id + "_mdy";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mdy-cal-btn";
    btn.dataset.mdyCalBtn = "1";
    btn.setAttribute("aria-label", "Open calendar");
    btn.innerHTML = '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">'
      + '<rect x="1.5" y="2.5" width="13" height="12" rx="1.5" fill="none" stroke="currentColor"/>'
      + '<line x1="1.5" y1="6" x2="14.5" y2="6" stroke="currentColor"/>'
      + '<line x1="4.5" y1="1" x2="4.5" y2="3.5" stroke="currentColor"/>'
      + '<line x1="11.5" y1="1" x2="11.5" y2="3.5" stroke="currentColor"/>'
      + "</svg>";

    // The native input keeps its name/value and is still what submits —
    // just hidden. `display:none` also bars it from the browser's own
    // constraint validation (a required-but-empty hidden field would
    // otherwise silently block submission with a native tooltip the
    // employee can't see and can't dismiss), so `required` is removed
    // here and re-enforced ourselves on submit instead (see below).
    native.style.display = "none";
    native.removeAttribute("required");
    if (wasRequired) native.dataset.mdyRequired = "1";

    wrap.appendChild(text);
    wrap.appendChild(btn);

    const cal = buildCalendar(
      function () { return native.value; },
      function (iso) {
        native.value = iso;
        text.value = isoToMdy(iso);
        text.classList.remove("mdy-invalid");
        native.dispatchEvent(new Event("change", { bubbles: true }));
      }
    );

    text.addEventListener("input", function () {
      const cursorFromEnd = text.value.length - text.selectionStart;
      const before = text.value;
      text.value = autoSlash(text.value);
      if (text.value.length !== before.length) {
        const pos = Math.max(0, text.value.length - cursorFromEnd);
        text.setSelectionRange(pos, pos);
      }
      text.classList.remove("mdy-invalid");
    });
    text.addEventListener("change", function () {
      const iso = mdyToIso(text.value);
      native.value = iso;
      text.classList.toggle("mdy-invalid", text.value.trim() !== "" && iso === "");
      native.dispatchEvent(new Event("change", { bubbles: true }));
    });
    btn.addEventListener("click", function () { cal.open(wrap); });
  }

  function enhanceAll() {
    document.querySelectorAll('input[type="date"]').forEach(enhance);
  }

  document.addEventListener("DOMContentLoaded", enhanceAll);

  // Re-enforce "required" ourselves at submit time — see the
  // removeAttribute("required") note in enhance() above.
  document.addEventListener("submit", function (e) {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    const dateInputs = form.querySelectorAll('input[type="date"][data-mdy-enhanced]');
    for (let i = 0; i < dateInputs.length; i++) {
      const native = dateInputs[i];
      if (native.dataset.mdyRequired && !native.value) {
        e.preventDefault();
        const wrap = native.closest(".mdy-wrap");
        const text = wrap && wrap.querySelector(".mdy-text");
        if (text) {
          text.classList.add("mdy-invalid");
          text.focus();
        }
        return;
      }
    }
  }, true);
})();
