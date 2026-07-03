/*
  reddit-to-spreadsheet — UI logic (vanilla JS, no frameworks).

  Health-focused picker:
    - Load condition themes + an autocomplete pool + starter "popular" list.
    - Search box shows live suggestions as you type (no Enter needed).
    - Theme chips browse communities by condition; the suggested panel also
      recommends communities RELATED to whatever is already selected (pick a
      women's-health community and it surfaces other women's-health ones).
    - A single selection set feeds a side "Selected" tray.
    - Quick-set presets for the caps and the date window.
    - POST to /api/collect, stream back the .xlsx, and report the run time.
*/
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // ---- Element handles --------------------------------------------------
  const searchInput      = $("search");
  const suggestList      = $("suggest-list");
  const combo            = searchInput.closest(".combo");
  const themeChipsEl     = $("theme-chips");
  const suggestedEl      = $("suggested");
  const suggestedLabel   = $("suggested-label");
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

  const SUGGEST_CAP = 16;   // most pills to show at once

  // ---- State ------------------------------------------------------------
  const POOL = [];                   // every health subreddit (autocomplete)
  let THEMES = {};                   // theme name -> [subreddit]
  let POPULAR = [];                  // starter suggestions
  const poolByLower = new Map();     // lower -> canonical name
  const subThemes = new Map();       // lower -> [theme name] (for relatedness)
  const selected = new Map();        // lower -> display name (source of truth)
  let activeTheme = null;            // currently browsed theme, or null

  // ---- Date defaults ----------------------------------------------------
  function isoFromDate(d) {
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    return `${d.getFullYear()}-${mm}-${dd}`;
  }
  function isoToday() { return isoFromDate(new Date()); }
  startDateEl.value = "2018-02-07";
  endDateEl.value   = isoToday();
  endDateEl.max     = isoToday();

  // ---- Small DOM helper -------------------------------------------------
  function el(tag, opts) {
    const node = document.createElement(tag);
    if (opts) {
      if (opts.class) node.className = opts.class;
      if (opts.text != null) node.textContent = opts.text;
      if (opts.html != null) node.innerHTML = opts.html;
      if (opts.attrs) for (const k in opts.attrs) node.setAttribute(k, opts.attrs[k]);
    }
    return node;
  }

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
      selected.set(lower, poolByLower.get(lower) || clean); // prefer canonical casing
      syncAfterChange();
    }
  }
  function removeSel(lower) {
    if (selected.delete(lower)) syncAfterChange();
  }
  function clearSelection() {
    selected.clear();
    syncAfterChange();
  }
  function syncAfterChange() {
    renderTray();
    selectedCountEl.textContent = String(selected.size);
    renderSuggested();
  }

  function renderTray() {
    trayList.innerHTML = "";
    Array.from(selected.entries())
      .sort((a, b) => a[1].toLowerCase().localeCompare(b[1].toLowerCase()))
      .forEach(([lower, name]) => {
        const li = el("li", { class: "tray-item" });
        li.appendChild(el("span", { class: "tray-name", text: "r/" + name }));
        const remove = el("button", {
          attrs: { type: "button", "aria-label": "Remove r/" + name }, text: "×",
        });
        remove.addEventListener("click", () => removeSel(lower));
        li.appendChild(remove);
        trayList.appendChild(li);
      });
    trayEmpty.hidden = selected.size > 0;
    clearSelectionEl.hidden = selected.size === 0;
  }

  // ---- Load data --------------------------------------------------------
  function loadData() {
    fetch("/api/subreddits", { headers: { Accept: "application/json" } })
      .then((res) => {
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      })
      .then((data) => {
        THEMES = data.themes || {};
        POPULAR = data.popular || [];
        (data.pool || []).forEach((n) => { POOL.push(n); poolByLower.set(n.toLowerCase(), n); });
        // Build the subreddit -> themes index for relatedness.
        Object.keys(THEMES).forEach((theme) => {
          (THEMES[theme] || []).forEach((sub) => {
            const l = sub.toLowerCase();
            if (!poolByLower.has(l)) poolByLower.set(l, sub);
            if (!subThemes.has(l)) subThemes.set(l, []);
            subThemes.get(l).push(theme);
          });
        });
        renderThemeChips();
        renderSuggested();
      })
      .catch((err) => {
        suggestedEl.innerHTML = "";
        suggestedEl.appendChild(el("p", {
          class: "suggested-empty",
          text: "Could not load communities (" + err.message +
            "). You can still search for and add any subreddit above.",
        }));
      });
  }

  // ---- Theme chips ------------------------------------------------------
  function renderThemeChips() {
    themeChipsEl.innerHTML = "";
    Object.keys(THEMES).forEach((theme) => {
      const chip = el("button", {
        class: "theme-chip", text: theme,
        attrs: { type: "button", "data-theme": theme, "aria-pressed": "false" },
      });
      chip.addEventListener("click", () => setActiveTheme(activeTheme === theme ? null : theme));
      themeChipsEl.appendChild(chip);
    });
  }
  function setActiveTheme(theme) {
    activeTheme = theme;
    themeChipsEl.querySelectorAll(".theme-chip").forEach((c) => {
      const on = c.dataset.theme === theme;
      c.classList.toggle("active", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    renderSuggested();
  }

  // ---- Related-suggestion engine ---------------------------------------
  function themesOf(lower) { return subThemes.get(lower) || []; }

  // Communities that share a theme with something already selected, ranked by
  // how many current picks they relate to.
  function computeRelated() {
    if (selected.size === 0) return [];
    const activeThemes = new Set();
    selected.forEach((_, lower) => themesOf(lower).forEach((t) => activeThemes.add(t)));

    const candidates = new Map();  // lower -> canonical
    activeThemes.forEach((t) => {
      (THEMES[t] || []).forEach((sub) => {
        const l = sub.toLowerCase();
        if (!selected.has(l)) candidates.set(l, sub);
      });
    });

    const score = (lower) => {
      const ts = new Set(themesOf(lower));
      let n = 0;
      selected.forEach((_, sLower) => {
        if (themesOf(sLower).some((t) => ts.has(t))) n += 1;
      });
      return n;
    };

    return Array.from(candidates.entries())
      .map(([lower, name]) => ({ name, score: score(lower) }))
      .sort((a, b) => b.score - a.score || a.name.toLowerCase().localeCompare(b.name.toLowerCase()))
      .map((c) => c.name);
  }

  function renderSuggested() {
    let names;
    let label;

    if (activeTheme) {
      label = activeTheme;
      names = (THEMES[activeTheme] || []).filter((n) => !selected.has(n.toLowerCase()));
    } else if (selected.size > 0) {
      names = computeRelated();
      if (names.length) {
        label = "Related to your selection";
      } else {
        label = "Popular communities";
        names = POPULAR.filter((n) => !selected.has(n.toLowerCase()));
      }
    } else {
      label = "Popular communities";
      names = POPULAR.filter((n) => !selected.has(n.toLowerCase()));
    }

    suggestedLabel.textContent = label;
    suggestedEl.innerHTML = "";

    if (!names.length) {
      suggestedEl.appendChild(el("p", {
        class: "suggested-empty",
        text: activeTheme
          ? "All " + activeTheme + " communities are selected."
          : "Everything suggested is already selected — search above to add more.",
      }));
      return;
    }

    names.slice(0, SUGGEST_CAP).forEach((name) => {
      const pill = el("button", { class: "suggest-pill", attrs: { type: "button" } });
      pill.appendChild(el("span", { class: "plus", text: "+" }));
      pill.appendChild(el("span", { html: '<span class="r">r/</span>' + escapeHtml(name) }));
      pill.addEventListener("click", () => addSel(name));
      suggestedEl.appendChild(pill);
    });
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---- Search autocomplete dropdown ------------------------------------
  let matches = [];
  let activeIdx = -1;

  function computeMatches(q) {
    const clean = q.trim();
    const ql = clean.toLowerCase();
    if (!ql) return [];
    const starts = [], contains = [];
    for (const name of POOL) {
      const nl = name.toLowerCase();
      if (nl.startsWith(ql)) starts.push(name);
      else if (nl.indexOf(ql) !== -1) contains.push(name);
    }
    const pooled = starts.concat(contains).slice(0, 8).map((name) => ({ name, add: false }));
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
      if (m.add) {
        li.classList.add("suggest-add");
        li.appendChild(el("span", { class: "suggest-plus", text: "+" }));
        li.appendChild(el("span", { text: "Add r/" + m.name }));
      } else {
        li.appendChild(el("span", { class: "suggest-r", text: "r/" }));
        li.appendChild(el("span", { text: m.name }));
        if (selected.has(m.name.toLowerCase())) {
          li.appendChild(el("span", { class: "suggest-check", text: "✓ added" }));
        }
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
    Array.from(suggestList.children).forEach((li, i) => li.classList.toggle("active", i === activeIdx));
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
    } else if (e.key === "Escape") { hideSuggest(); }
  });
  document.addEventListener("click", (e) => { if (!combo.contains(e.target)) hideSuggest(); });

  // ---- Cap + date presets ----------------------------------------------
  const REDDIT_EPOCH = "2005-06-23";  // Reddit launch — practical data floor
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
    target.addEventListener("input", () => markActivePreset(wrap, target));
    markActivePreset(wrap, target);
  });

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
      return Number.isFinite(n) && n >= 1 ? n : def;
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
      showError("Select at least one community — search above or pick a suggestion.");
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
        const fname = filenameFromDisposition(res.headers.get("Content-Disposition"), "reddit_export.xlsx");
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
        console.log("[reddit-to-spreadsheet] workflow completed in " + secs + "s",
          { posts: c.posts, comments: c.comments, errors: c.errors, serverSeconds: c.serverSecs });
        if (c.posts + c.comments === 0) {
          showStatus(
            "Finished in " + secs + "s, but no posts or comments matched that window, so " +
              out.fname + " is empty. " +
              (c.errors
                ? "pullpush returned " + c.errors + " error(s) — it may be down right now; try again shortly."
                : "Try a wider date range or different communities."),
            "warn"
          );
        } else {
          let msg = "Done in " + secs + "s — exported " + c.posts + " posts and " +
            c.comments + " comments to " + out.fname + ".";
          if (c.errors) msg += " Note: pullpush returned " + c.errors + " error(s), so some data may be missing.";
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
  loadData();
})();
