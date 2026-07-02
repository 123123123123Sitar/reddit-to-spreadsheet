/*
  reddit-to-spreadsheet — UI logic (vanilla JS, no frameworks).

  Responsibilities:
    - Load subreddit categories from GET /api/subreddits and render collapsible
      groups of labeled checkboxes.
    - Live-filter those checkboxes from the search bar (case-insensitive substring).
    - Let the user add arbitrary custom subreddits as checked chips.
    - Track a "selected: N" counter with a clear-selection link.
    - POST the collection request to /api/collect and stream back an .xlsx download.
*/
(function () {
  "use strict";

  // ---- Element handles --------------------------------------------------
  const $ = (id) => document.getElementById(id);

  const searchInput      = $("search");
  const categoriesEl     = $("categories");
  const categoriesLoad   = $("categories-loading");
  const noResultsEl      = $("no-results");
  const selectedCountEl  = $("selected-count");
  const clearSelectionEl = $("clear-selection");
  const customInput      = $("custom-input");
  const customChipsEl    = $("custom-chips");
  const startDateEl      = $("start-date");
  const endDateEl        = $("end-date");
  const maxPostsEl       = $("max-posts");
  const maxCommentsEl    = $("max-comments");
  const includeCommentsEl= $("include-comments");
  const excludeNamesEl   = $("exclude-usernames");
  const generateBtn      = $("generate");
  const generateLabel    = $("generate-label");
  const spinnerEl        = $("spinner");
  const statusEl         = $("status");
  const errorEl          = $("error");

  // Track which subreddit names already have a curated checkbox, so a custom
  // entry that duplicates a curated one just toggles the existing checkbox
  // instead of creating a redundant chip. Keyed by lowercased name.
  const curatedByLower = new Map();   // lower -> checkbox input element
  const customChips    = new Map();   // lower -> { name, li, checkbox }

  // ---- Date defaults ----------------------------------------------------
  // Default window: 2018-02-07 .. today (per spec).
  function isoToday() {
    const d = new Date();
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${mm}-${dd}`;
  }
  startDateEl.value = "2018-02-07";
  endDateEl.value   = isoToday();
  endDateEl.max     = isoToday();

  // ---- Small DOM helper -------------------------------------------------
  function el(tag, opts) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.class) node.className = opts.class;
      if (opts.text != null) node.textContent = opts.text;
      if (opts.attrs) for (const k in opts.attrs) node.setAttribute(k, opts.attrs[k]);
    }
    return node;
  }

  // ---- Load + render categories ----------------------------------------
  function loadCategories() {
    fetch("/api/subreddits", { headers: { Accept: "application/json" } })
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((data) => renderCategories(data.categories || {}))
      .catch((err) => {
        categoriesLoad.textContent =
          "Could not load the subreddit list (" + err.message +
          "). You can still add custom subreddits above.";
      });
  }

  function renderCategories(categories) {
    categoriesLoad.remove();
    const names = Object.keys(categories);

    if (!names.length) {
      categoriesEl.appendChild(
        el("p", { class: "loading", text: "No suggested subreddits; add your own above." })
      );
      return;
    }

    names.forEach((catName, idx) => {
      const subs = categories[catName] || [];

      const group = el("div", { class: "category" });
      // Collapse all but the first group by default to keep things tidy.
      if (idx > 0) group.classList.add("collapsed");

      // Collapsible header (button for keyboard accessibility).
      const header = el("button", { class: "category-header", attrs: { type: "button" } });
      header.setAttribute("aria-expanded", idx === 0 ? "true" : "false");
      header.appendChild(el("span", { class: "category-caret", text: "▼" }));
      header.appendChild(el("span", { class: "category-name", text: catName }));
      header.appendChild(el("span", { class: "category-badge", text: String(subs.length) }));
      header.addEventListener("click", () => {
        const collapsed = group.classList.toggle("collapsed");
        header.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
      group.appendChild(header);

      // Checkbox body.
      const body = el("div", { class: "category-body" });
      subs.forEach((sub) => {
        const lower = sub.toLowerCase();
        const label = el("label", { class: "sub-item" });
        label.dataset.name = lower;

        const cb = el("input", { attrs: { type: "checkbox", value: sub } });
        cb.addEventListener("change", updateSelectedCount);

        const span = el("span", { class: "sub-name", text: sub });
        label.appendChild(cb);
        label.appendChild(span);
        body.appendChild(label);

        // Remember first checkbox for each name (avoid duplicate collisions).
        if (!curatedByLower.has(lower)) curatedByLower.set(lower, cb);
      });
      group.appendChild(body);
      categoriesEl.appendChild(group);
    });

    updateSelectedCount();
  }

  // ---- Search / live filter --------------------------------------------
  function applyFilter() {
    const q = searchInput.value.trim().toLowerCase();
    let anyVisible = false;

    categoriesEl.querySelectorAll(".category").forEach((group) => {
      let groupHasMatch = false;

      group.querySelectorAll(".sub-item").forEach((item) => {
        const match = !q || item.dataset.name.indexOf(q) !== -1;
        item.hidden = !match;
        if (match) groupHasMatch = true;
      });

      // Hide whole group if nothing inside matches; auto-expand matches so the
      // user can see filtered results without manually opening each group.
      group.hidden = !groupHasMatch;
      if (groupHasMatch) {
        anyVisible = true;
        if (q) group.classList.remove("collapsed");
      }
    });

    noResultsEl.hidden = anyVisible || !q;
  }

  // ---- Custom subreddit chips ------------------------------------------
  function sanitizeName(raw) {
    // Accept "r/foo", "/r/foo", "foo" and trim to a valid-ish token.
    return raw
      .trim()
      .replace(/^\/?(r\/)?/i, "")   // strip leading r/ or /r/
      .replace(/[^A-Za-z0-9_]/g, "") // subreddit names: letters, digits, underscore
      .slice(0, 40);
  }

  function addCustomSubreddit(raw) {
    const name = sanitizeName(raw);
    if (!name) return;
    const lower = name.toLowerCase();

    // If it matches a curated checkbox, just check that one instead.
    if (curatedByLower.has(lower)) {
      const cb = curatedByLower.get(lower);
      cb.checked = true;
      updateSelectedCount();
      return;
    }
    // Already added as a chip? no-op.
    if (customChips.has(lower)) return;

    const li = el("li", { class: "chip" });
    // A hidden checkbox keeps chip selection consistent with getSelected().
    const cb = el("input", { attrs: { type: "checkbox", value: name } });
    cb.checked = true;
    cb.hidden = true;

    li.appendChild(cb);
    li.appendChild(el("span", { text: "r/" + name }));

    const remove = el("button", { attrs: { type: "button", "aria-label": "Remove r/" + name }, text: "×" });
    remove.addEventListener("click", () => {
      li.remove();
      customChips.delete(lower);
      updateSelectedCount();
    });
    li.appendChild(remove);

    customChipsEl.appendChild(li);
    customChips.set(lower, { name, li, checkbox: cb });
    updateSelectedCount();
  }

  // ---- Selection accounting --------------------------------------------
  function getSelected() {
    const set = new Map(); // lower -> original-cased name (dedupe, keep first)
    curatedByLower.forEach((cb, lower) => {
      if (cb.checked && !set.has(lower)) set.set(lower, cb.value);
    });
    customChips.forEach((chip, lower) => {
      if (chip.checkbox.checked && !set.has(lower)) set.set(lower, chip.name);
    });
    return Array.from(set.values());
  }

  function updateSelectedCount() {
    const n = getSelected().length;
    selectedCountEl.textContent = "Selected: " + n;
  }

  function clearSelection() {
    curatedByLower.forEach((cb) => { cb.checked = false; });
    // Remove all custom chips too.
    customChips.forEach((chip) => chip.li.remove());
    customChips.clear();
    updateSelectedCount();
  }

  // ---- Status / error helpers ------------------------------------------
  function showStatus(msg) { statusEl.textContent = msg; statusEl.hidden = false; }
  function hideStatus()    { statusEl.hidden = true; statusEl.textContent = ""; }
  function showError(msg)  { errorEl.textContent = msg; errorEl.hidden = false; }
  function hideError()     { errorEl.hidden = true; errorEl.textContent = ""; }

  function setBusy(busy) {
    generateBtn.disabled = busy;
    spinnerEl.hidden = !busy;
    generateLabel.textContent = busy ? "Collecting…" : "Generate spreadsheet";
  }

  // ---- Download trigger -------------------------------------------------
  function filenameFromDisposition(header, fallback) {
    if (!header) return fallback;
    // Handle RFC5987 (filename*=UTF-8''...) and plain filename="...".
    let m = /filename\*=(?:UTF-8'')?["']?([^"';]+)["']?/i.exec(header);
    if (m && m[1]) { try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; } }
    m = /filename=["']?([^"';]+)["']?/i.exec(header);
    return (m && m[1]) ? m[1] : fallback;
  }

  function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = el("a", { attrs: { href: url, download: filename } });
    document.body.appendChild(a);
    a.click();
    a.remove();
    // Revoke slightly later so the download has a chance to start.
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  // ---- Build request payload -------------------------------------------
  function buildPayload(subreddits) {
    const toInt = (v, def) => {
      const n = parseInt(v, 10);
      return Number.isFinite(n) && n >= 0 ? n : def;
    };
    return {
      subreddits: subreddits,
      start_date: startDateEl.value,
      end_date: endDateEl.value,
      include_comments: includeCommentsEl.checked,
      exclude_usernames: excludeNamesEl.checked,
      max_posts_per_sub: toInt(maxPostsEl.value, 500),
      max_comments_per_sub: toInt(maxCommentsEl.value, 2000),
    };
  }

  // ---- Generate handler -------------------------------------------------
  function onGenerate() {
    hideError();
    hideStatus();

    const subreddits = getSelected();
    if (subreddits.length === 0) {
      showError("Please select at least one subreddit (check a box or add a custom one).");
      return;
    }
    if (!startDateEl.value || !endDateEl.value) {
      showError("Please choose both a start and end date.");
      return;
    }
    if (startDateEl.value > endDateEl.value) {
      showError("The start date must be on or before the end date.");
      return;
    }

    setBusy(true);
    showStatus(
      "Collecting from pullpush… this can take a minute; pullpush is a flaky " +
      "community mirror so retries are normal."
    );

    fetch("/api/collect", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "*/*" },
      body: JSON.stringify(buildPayload(subreddits)),
    })
      .then((res) => {
        const ctype = res.headers.get("Content-Type") || "";
        if (!res.ok) {
          // Server returns JSON error bodies for 400/500.
          if (ctype.indexOf("application/json") !== -1) {
            return res.json().then((j) => {
              throw new Error(j.error || j.message || ("Request failed (HTTP " + res.status + ")."));
            });
          }
          return res.text().then((t) => {
            throw new Error(t || ("Request failed (HTTP " + res.status + ")."));
          });
        }
        const disp = res.headers.get("Content-Disposition");
        const fname = filenameFromDisposition(disp, "reddit_export.xlsx");
        return res.blob().then((blob) => ({ blob, fname }));
      })
      .then((out) => {
        if (!out) return; // error path already threw
        triggerDownload(out.blob, out.fname);
        showStatus("Done. Your spreadsheet (" + out.fname + ") has been downloaded.");
      })
      .catch((err) => {
        hideStatus();
        showError(err.message || "Something went wrong while collecting data.");
      })
      .finally(() => {
        setBusy(false);
      });
  }

  // ---- Wire up events ---------------------------------------------------
  searchInput.addEventListener("input", applyFilter);

  clearSelectionEl.addEventListener("click", (e) => {
    e.preventDefault();
    clearSelection();
  });

  customInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      addCustomSubreddit(customInput.value);
      customInput.value = "";
    }
  });

  generateBtn.addEventListener("click", onGenerate);

  // ---- Init -------------------------------------------------------------
  loadCategories();
})();
