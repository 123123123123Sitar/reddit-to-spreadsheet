/*
  reddit-to-spreadsheet — UI logic (vanilla JS, no frameworks).

    - Load subreddit categories + a wider autocomplete pool from
      GET /api/subreddits.
    - As the user types, show a live suggestions dropdown (pool matches plus an
      "add exactly what I typed" row) — no Enter required.
    - Keep a single source of truth for the selection and mirror it into the
      category checkboxes and a side "Selected" tray.
    - Quick-set preset buttons for the per-subreddit caps.
    - POST the request to /api/collect, stream back the .xlsx, and report how
      long the whole thing took.
*/
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ---- Element handles --------------------------------------------------
  const searchInput      = $("search");
  const suggestList      = $("suggest-list");
  const combo            = searchInput.closest(".combo");
  const categoriesEl     = $("categories");
  const categoriesLoad   = $("categories-loading");
  const trayList         = $("tray-list");
  const trayEmpty        = $("tray-empty");
  const selectedCountEl  = $("selected-count");
  const clearSelectionEl = $("clear-selection");
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

  // ---- State ------------------------------------------------------------
  const POOL = [];                    // autocomplete pool (from the API)
  const curatedByLower = new Map();   // lower -> category checkbox element
  const selected = new Map();         // lower -> display name (source of truth)

  // ---- Date defaults ----------------------------------------------------
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

  // Accept "r/foo", "/r/foo", "foo"; keep only valid subreddit characters.
  function sanitizeName(raw) {
    return raw
      .trim()
      .replace(/^\/?(r\/)?/i, "")
      .replace(/[^A-Za-z0-9_]/g, "")
      .slice(0, 40);
  }

  // ---- Selection (single source of truth) -------------------------------
  function addSel(name) {
    const clean = sanitizeName(name);
    if (!clean) return;
    const lower = clean.toLowerCase();
    if (!selected.has(lower)) {
      // Prefer the curated casing if we know it.
      const display = curatedByLower.has(lower)
        ? curatedByLower.get(lower).value
        : clean;
      selected.set(lower, display);
      syncAfterChange();
    }
  }
  function removeSel(lower) {
    if (selected.delete(lower)) syncAfterChange();
  }
  function toggleSel(name) {
    const lower = sanitizeName(name).toLowerCase();
    if (selected.has(lower)) removeSel(lower);
    else addSel(name);
  }
  function clearSelection() {
    selected.clear();
    syncAfterChange();
  }

  function syncAfterChange() {
    // Mirror the selection into the category checkboxes.
    curatedByLower.forEach((cb, lower) => { cb.checked = selected.has(lower); });
    renderTray();
    selectedCountEl.textContent = String(selected.size);
  }

  function renderTray() {
    trayList.innerHTML = "";
    const items = Array.from(selected.entries())
      .sort((a, b) => a[1].toLowerCase().localeCompare(b[1].toLowerCase()));

    items.forEach(([lower, name]) => {
      const li = el("li", { class: "tray-item" });
      li.appendChild(el("span", { class: "tray-name", text: "r/" + name }));
      const remove = el("button", {
        attrs: { type: "button", "aria-label": "Remove r/" + name },
        text: "×",
      });
      remove.addEventListener("click", () => removeSel(lower));
      li.appendChild(remove);
      trayList.appendChild(li);
    });

    trayEmpty.hidden = selected.size > 0;
    clearSelectionEl.hidden = selected.size === 0;
  }

  // ---- Load + render categories ----------------------------------------
  function loadCategories() {
    fetch("/api/subreddits", { headers: { Accept: "application/json" } })
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((data) => {
        (data.pool || []).forEach((n) => POOL.push(n));
        renderCategories(data.categories || {});
      })
      .catch((err) => {
        categoriesLoad.textContent =
          "Could not load the subreddit list (" + err.message +
          "). You can still search for and add any subreddit above.";
      });
  }

  function renderCategories(categories) {
    categoriesLoad.remove();
    const names = Object.keys(categories);

    if (!names.length) {
      categoriesEl.appendChild(
        el("p", { class: "loading", text: "No suggested subreddits; search above to add your own." })
      );
      return;
    }

    names.forEach((catName, idx) => {
      const subs = categories[catName] || [];
      const group = el("div", { class: "category" });
      if (idx > 0) group.classList.add("collapsed");   // expand only the first

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

      const body = el("div", { class: "category-body" });
      subs.forEach((sub) => {
        const lower = sub.toLowerCase();
        const label = el("label", { class: "sub-item" });
        const cb = el("input", { attrs: { type: "checkbox", value: sub } });
        cb.checked = selected.has(lower);
        cb.addEventListener("change", () => toggleSel(sub));
        label.appendChild(cb);
        label.appendChild(el("span", { class: "sub-name", text: sub }));
        body.appendChild(label);
        if (!curatedByLower.has(lower)) curatedByLower.set(lower, cb);
      });
      group.appendChild(body);
      categoriesEl.appendChild(group);
    });
  }

  // ---- Suggestions dropdown --------------------------------------------
  let matches = [];    // [{ name, add }]
  let activeIdx = -1;

  function computeMatches(q) {
    const clean = q.trim();
    const ql = clean.toLowerCase();
    if (!ql) return [];

    const starts = [];
    const contains = [];
    for (const name of POOL) {
      const nl = name.toLowerCase();
      if (nl.startsWith(ql)) starts.push(name);
      else if (nl.indexOf(ql) !== -1) contains.push(name);
    }
    const pooled = starts.concat(contains).slice(0, 8).map((name) => ({ name, add: false }));

    // Always let the user add exactly what they typed (unless it's already a
    // pool entry with that exact name).
    const typed = sanitizeName(clean);
    const exactInPool = POOL.some((n) => n.toLowerCase() === ql);
    if (typed && !exactInPool) pooled.unshift({ name: typed, add: true });
    return pooled;
  }

  function renderSuggest(q) {
    matches = computeMatches(q);
    activeIdx = -1;
    if (!matches.length) { hideSuggest(); return; }

    suggestList.innerHTML = "";
    matches.forEach((m, i) => {
      const li = el("li", { class: "suggest-item", attrs: { role: "option", "data-idx": String(i) } });
      const already = !m.add && selected.has(m.name.toLowerCase());
      if (m.add) {
        li.classList.add("suggest-add");
        li.appendChild(el("span", { class: "suggest-plus", text: "+" }));
        li.appendChild(el("span", { text: "Add r/" + m.name }));
      } else {
        li.appendChild(el("span", { class: "suggest-r", text: "r/" }));
        li.appendChild(el("span", { text: m.name }));
        if (already) li.appendChild(el("span", { class: "suggest-check", text: "✓ added" }));
      }
      li.addEventListener("mousedown", (e) => { e.preventDefault(); chooseSuggest(i); });
      suggestList.appendChild(li);
    });
    suggestList.hidden = false;
    searchInput.setAttribute("aria-expanded", "true");
  }

  function chooseSuggest(i) {
    const m = matches[i];
    if (!m) return;
    addSel(m.name);
    searchInput.value = "";
    hideSuggest();
    searchInput.focus();
  }

  function hideSuggest() {
    suggestList.hidden = true;
    suggestList.innerHTML = "";
    searchInput.setAttribute("aria-expanded", "false");
    matches = [];
    activeIdx = -1;
  }

  function highlightActive() {
    Array.from(suggestList.children).forEach((li, i) => {
      li.classList.toggle("active", i === activeIdx);
    });
    if (activeIdx >= 0 && suggestList.children[activeIdx]) {
      suggestList.children[activeIdx].scrollIntoView({ block: "nearest" });
    }
  }

  function moveActive(delta) {
    if (!matches.length) return;
    activeIdx = (activeIdx + delta + matches.length) % matches.length;
    highlightActive();
  }

  searchInput.addEventListener("input", () => renderSuggest(searchInput.value));
  searchInput.addEventListener("focus", () => { if (searchInput.value) renderSuggest(searchInput.value); });
  searchInput.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); moveActive(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveActive(-1); }
    else if (e.key === "Enter") {
      e.preventDefault();
      if (matches.length) chooseSuggest(activeIdx >= 0 ? activeIdx : 0);
    } else if (e.key === "Escape") {
      hideSuggest();
    }
  });
  document.addEventListener("click", (e) => {
    if (!combo.contains(e.target)) hideSuggest();
  });

  // ---- Cap preset buttons ----------------------------------------------
  // Date presets resolve to a YYYY-MM-DD value at click time. "Earliest" is
  // Reddit's launch date (the practical floor for pullpush/Pushshift data).
  const REDDIT_EPOCH = "2005-06-23";
  function isoFromDate(d) {
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${mm}-${dd}`;
  }
  function isoAgo(years) {
    const d = new Date();
    d.setFullYear(d.getFullYear() - years);
    return isoFromDate(d);
  }
  function resolveDatePreset(token) {
    switch (token) {
      case "earliest": return REDDIT_EPOCH;
      case "today":    return isoToday();
      case "minus1y":  return isoAgo(1);
      case "minus5y":  return isoAgo(5);
      default:         return "";
    }
  }
  // A preset button carries either data-val (numeric caps) or data-preset (dates).
  function presetValue(btn) {
    return btn.dataset.val != null ? btn.dataset.val : resolveDatePreset(btn.dataset.preset);
  }
  function markActivePreset(wrap, target) {
    wrap.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", presetValue(b) === String(target.value));
    });
  }
  document.querySelectorAll(".presets").forEach((wrap) => {
    const target = $(wrap.dataset.target);
    wrap.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (target.disabled) return;
        target.value = presetValue(btn);
        markActivePreset(wrap, target);
      });
    });
    // Manual edits clear the active preset unless they land on one.
    target.addEventListener("input", () => markActivePreset(wrap, target));
    markActivePreset(wrap, target);
  });

  // Disable the comments cap (and its presets) when comments are excluded.
  function syncCommentsCap() {
    const on = includeCommentsEl.checked;
    maxCommentsEl.disabled = !on;
    const field = $("max-comments-field");
    if (field) {
      field.classList.toggle("is-disabled", !on);
      field.querySelectorAll(".presets button").forEach((b) => { b.disabled = !on; });
    }
  }

  // ---- Status / error helpers ------------------------------------------
  function showStatus(msg, level) {
    statusEl.textContent = msg;
    statusEl.className = "status" + (level ? " " + level : "");
    statusEl.hidden = false;
  }
  function hideStatus() { statusEl.hidden = true; statusEl.textContent = ""; }
  function showError(msg) { errorEl.textContent = msg; errorEl.hidden = false; }
  function hideError() { errorEl.hidden = true; errorEl.textContent = ""; }

  function setBusy(busy) {
    generateBtn.disabled = busy;
    spinnerEl.hidden = !busy;
    generateLabel.textContent = busy ? "Collecting…" : "Generate spreadsheet";
  }

  // ---- Download trigger -------------------------------------------------
  function filenameFromDisposition(header, fallback) {
    if (!header) return fallback;
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
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  // ---- Build request payload -------------------------------------------
  function buildPayload() {
    const toInt = (v, def) => {
      const n = parseInt(v, 10);
      return Number.isFinite(n) && n >= 1 ? n : def;  // server requires 1..100000
    };
    return {
      subreddits: Array.from(selected.values()),
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

    if (selected.size === 0) {
      showError("Select at least one subreddit — search above or pick from the list.");
      return;
    }
    if (!startDateEl.value || !endDateEl.value) {
      showError("Choose both a start and end date.");
      return;
    }
    if (startDateEl.value > endDateEl.value) {
      showError("The start date must be on or before the end date.");
      return;
    }

    setBusy(true);
    showStatus(
      "Collecting from pullpush… this can take a minute; pullpush is a flaky " +
      "community mirror, so retries are normal."
    );

    const t0 = performance.now();
    fetch("/api/collect", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "*/*" },
      body: JSON.stringify(buildPayload()),
    })
      .then((res) => {
        const ctype = res.headers.get("Content-Type") || "";
        if (!res.ok) {
          if (ctype.indexOf("application/json") !== -1) {
            return res.json().then((j) => {
              throw new Error(j.error || j.message || ("Request failed (HTTP " + res.status + ")."));
            });
          }
          return res.text().then((t) => {
            throw new Error(t || ("Request failed (HTTP " + res.status + ")."));
          });
        }
        const fname = filenameFromDisposition(
          res.headers.get("Content-Disposition"), "reddit_export.xlsx"
        );
        const num = (h) => { const n = parseInt(res.headers.get(h), 10); return Number.isFinite(n) ? n : 0; };
        const counts = {
          posts: num("X-Collect-Posts"),
          comments: num("X-Collect-Comments"),
          errors: num("X-Collect-Errors"),
          serverSecs: res.headers.get("X-Collect-Seconds") || null,
        };
        return res.blob().then((blob) => ({ blob, fname, counts }));
      })
      .then((out) => {
        if (!out) return;
        const secs = ((performance.now() - t0) / 1000).toFixed(1);
        triggerDownload(out.blob, out.fname);
        const c = out.counts;
        console.log(
          "[reddit-to-spreadsheet] workflow completed in " + secs + "s",
          { posts: c.posts, comments: c.comments, errors: c.errors, serverSeconds: c.serverSecs }
        );
        if (c.posts + c.comments === 0) {
          showStatus(
            "Finished in " + secs + "s, but no posts or comments matched that window, so " +
              out.fname + " is empty. " +
              (c.errors
                ? "pullpush returned " + c.errors + " error(s) — it may be down right now; try again shortly."
                : "Try a wider date range or different subreddits."),
            "warn"
          );
        } else {
          let msg = "Done in " + secs + "s — exported " + c.posts + " posts and " +
            c.comments + " comments to " + out.fname + ".";
          if (c.errors) {
            msg += " Note: pullpush returned " + c.errors + " error(s), so some data may be missing.";
          }
          showStatus(msg, c.errors ? "warn" : "ok");
        }
      })
      .catch((err) => {
        hideStatus();
        showError(err.message || "Something went wrong while collecting data.");
      })
      .finally(() => { setBusy(false); });
  }

  // ---- Wire up events ---------------------------------------------------
  clearSelectionEl.addEventListener("click", clearSelection);
  generateBtn.addEventListener("click", onGenerate);
  includeCommentsEl.addEventListener("change", syncCommentsCap);

  // ---- Init -------------------------------------------------------------
  syncCommentsCap();
  renderTray();
  loadCategories();
})();
