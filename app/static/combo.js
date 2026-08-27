/*
 * Searchable dropdown for pickers backed by a fixed, small-to-medium list
 * (project/task, and anything similar later). Plain vanilla JS, no
 * dependency — keeps the project's no-new-deps convention.
 *
 * Markup contract, see today.html:
 *   <div class="combo" data-placeholder="a project">
 *     <input type="text" class="combo-input">
 *     <input type="hidden" name="project_id">
 *     <div class="combo-menu" hidden></div>
 *   </div>
 *
 * initCombo(rootEl, items, preselectId, onSelect) where items is
 * [{id, name}, ...]. The hidden input is what actually gets submitted; the
 * text input is just for typing/filtering and never carries the real value
 * on its own. preselectId (optional — string/number, or null/undefined for
 * none) is matched loosely against item.id (Ganesh, 2026-08-14: a failed
 * Add Row submission re-shows the form with whatever Project/Task was
 * already picked, instead of resetting to blank — see today.html's
 * reopen_* vars). onSelect (optional) fires with the picked item whenever
 * select() runs (by click, Enter, or preselect) — added 2026-08-27 so a
 * paired combo can react to this one's selection (see
 * initProjectTaskCombo() below). Returns { setItems(newItems),
 * getSelectedId() } so a caller can swap the item pool later (project-
 * scoped task filtering) without re-running the whole setup.
 */
(function () {
  function initCombo(root, items, preselectId, onSelect) {
    const input = root.querySelector(".combo-input");
    const hidden = root.querySelector('input[type="hidden"]');
    const menu = root.querySelector(".combo-menu");
    let filtered = items;
    let active = -1;

    function render() {
      menu.innerHTML = "";
      if (!filtered.length) {
        const div = document.createElement("div");
        div.className = "combo-empty";
        div.textContent = "No matches";
        menu.appendChild(div);
      } else {
        filtered.forEach((item, i) => {
          const div = document.createElement("div");
          div.className = "combo-opt" + (i === active ? " active" : "");
          div.textContent = item.name;
          // mousedown (not click) fires before the input's blur handler,
          // so the selection registers before the menu gets hidden.
          div.addEventListener("mousedown", (e) => {
            e.preventDefault();
            select(item);
          });
          if (i === active) div.scrollIntoView({ block: "nearest" });
          menu.appendChild(div);
        });
      }
      menu.hidden = false;
    }

    function select(item) {
      input.value = item.name;
      hidden.value = item.id;
      input.setCustomValidity("");
      menu.hidden = true;
      active = -1;
      if (onSelect) onSelect(item);
    }

    function clearSelection() {
      input.value = "";
      hidden.value = "";
    }

    function filter() {
      const q = input.value.trim().toLowerCase();
      filtered = q ? items.filter((it) => it.name.toLowerCase().includes(q)) : items;
      active = -1;
      // typed text no longer matches the previously-selected item — don't
      // silently submit a stale id for a name the user has since edited
      if (hidden.value) {
        const sel = items.find((it) => String(it.id) === String(hidden.value));
        if (!sel || sel.name !== input.value) hidden.value = "";
      }
      render();
    }

    input.addEventListener("focus", () => {
      filtered = items;
      active = -1;
      render();
    });
    input.addEventListener("input", filter);
    input.addEventListener("keydown", (e) => {
      if (menu.hidden && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
        filtered = items;
        render();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        active = Math.min(active + 1, filtered.length - 1);
        render();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        active = Math.max(active - 1, 0);
        render();
      } else if (e.key === "Enter") {
        if (!menu.hidden && active >= 0) {
          e.preventDefault();
          select(filtered[active]);
        }
      } else if (e.key === "Escape") {
        menu.hidden = true;
      }
    });
    // slight delay so a menu-item mousedown can run before blur hides it
    input.addEventListener("blur", () => {
      setTimeout(() => {
        menu.hidden = true;
      }, 120);
    });

    if (preselectId !== undefined && preselectId !== null && preselectId !== "") {
      const pre = items.find((it) => String(it.id) === String(preselectId));
      if (pre) select(pre);
    }

    const form = root.closest("form");
    if (form) {
      form.addEventListener("submit", (e) => {
        // A combo that's conditionally shown/hidden (Ganesh, 2026-08-27 —
        // the "Suggest a new task" form's Project picker, only relevant
        // when Type=Task) isn't required while hidden, same as any other
        // HTML form field a script has hidden — the toggling code is
        // responsible for clearing its value when it hides it.
        if (root.hidden) return;
        if (!hidden.value) {
          e.preventDefault();
          const label = root.dataset.placeholder || "an option";
          input.setCustomValidity("Choose " + label + " from the list.");
          input.reportValidity();
        } else {
          input.setCustomValidity("");
        }
      });
    }

    return {
      setItems(newItems) {
        items = newItems;
        filtered = newItems;
        // the previously-picked item may not exist in the new pool (e.g.
        // the paired Project combo changed to one this Task isn't linked
        // to) — don't silently keep submitting a now-invalid id.
        if (hidden.value && !items.some((it) => String(it.id) === String(hidden.value))) {
          clearSelection();
        }
      },
      getSelectedId() {
        return hidden.value;
      },
    };
  }

  /*
   * Project-scoped tasks (Ganesh, 2026-08-27) — wires a Project combo and
   * its paired Task combo together: picking a project narrows the task
   * pool to whatever's linked to it, plus every task that has no links at
   * all (unrestricted — see ProjectTask's docstring in app/models.py and
   * validation.task_allowed_for_project(), which enforces the identical
   * rule server-side so this is a real filter, not just a UI nicety).
   * Before any project is picked, the task pool is left unfiltered (every
   * task, restricted or not) since there's nothing yet to filter against.
   *
   * allTasks items carry a "project_ids" key: null/undefined means
   * unrestricted, an array means restricted to exactly those project ids
   * (see _combo_items() in app/routes/employee.py).
   */
  function initProjectTaskCombo(projectRoot, taskRoot, allProjects, allTasks, preselectProjectId, preselectTaskId) {
    function tasksFor(projectId) {
      if (projectId === undefined || projectId === null || projectId === "") return allTasks;
      return allTasks.filter(
        (t) => !t.project_ids || t.project_ids.some((pid) => String(pid) === String(projectId))
      );
    }
    const taskCombo = initCombo(taskRoot, tasksFor(preselectProjectId), preselectTaskId);
    initCombo(projectRoot, allProjects, preselectProjectId, (item) => {
      taskCombo.setItems(tasksFor(item.id));
    });
  }

  window.initCombo = initCombo;
  window.initProjectTaskCombo = initProjectTaskCombo;
})();
