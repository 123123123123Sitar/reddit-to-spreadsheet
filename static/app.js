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
  const suggestedEl      = $("suggested");
  const suggestedNote    = $("suggested-note");
  const chatLog          = $("chat-log");
  const chatForm         = $("chat-form");
  const chatInput        = $("chat-input");
  const chatSend         = $("chat-send");
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

  const SUGGEST_CAP = 12;   // most pills to show at once

  // ---- State ------------------------------------------------------------
  const POOL = [];                   // every health subreddit (autocomplete)
  const poolByLower = new Map();     // lower -> canonical name
  const selected = new Map();        // lower -> display name (source of truth)

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
    scheduleSuggestions();
    updateWizardControls();
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

  // ---- Load the autocomplete pool ---------------------------------------
  function loadPool() {
    fetch("/api/subreddits", { headers: { Accept: "application/json" } })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status))))
      .then((data) => {
        (data.pool || []).forEach((n) => { POOL.push(n); poolByLower.set(n.toLowerCase(), n); });
      })
      .catch(() => { /* search-and-add still works without the pool */ });
  }

  // ---- Suggestions (Mercury 2, related to the current selection) --------
  let suggestSeq = 0;
  let suggestTimer = null;

  function scheduleSuggestions() {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(fetchSuggestions, 250);
  }

  function fetchSuggestions() {
    const seq = ++suggestSeq;
    suggestedEl.innerHTML = "";
    suggestedEl.appendChild(el("p", { class: "loading", text: "Finding related communities…" }));
    fetch("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ selected: Array.from(selected.values()) }),
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status))))
      .then((data) => { if (seq === suggestSeq) renderSuggested(data.suggestions || []); })
      .catch(() => {
        if (seq !== suggestSeq) return;
        suggestedEl.innerHTML = "";
        suggestedEl.appendChild(el("p", {
          class: "suggested-empty",
          text: "Couldn't load suggestions — search above to add any community.",
        }));
        if (suggestedNote) suggestedNote.hidden = true;
      });
  }

  function formatCount(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "m";
    if (n >= 1e3) return Math.round(n / 1e3) + "k";
    return String(n);
  }

  function renderSuggested(list) {
    const items = (list || []).filter(
      (s) => s && s.name && !selected.has(String(s.name).toLowerCase())
    );
    suggestedEl.innerHTML = "";

    if (!items.length) {
      suggestedEl.appendChild(el("p", {
        class: "suggested-empty",
        text: "No more suggestions right now — search above to add any community.",
      }));
      if (suggestedNote) suggestedNote.hidden = true;
      return;
    }

    let anyCount = false;
    items.slice(0, SUGGEST_CAP).forEach((s) => {
      const name = String(s.name);
      const pill = el("button", { class: "suggest-pill", attrs: { type: "button" } });
      pill.appendChild(el("span", { class: "plus", text: "+" }));
      pill.appendChild(el("span", { html: '<span class="r">r/</span>' + escapeHtml(name) }));
      if (typeof s.posts === "number" && s.posts > 0) {
        anyCount = true;
        pill.appendChild(el("span", { class: "count", text: "~" + formatCount(s.posts) }));
      }
      pill.addEventListener("click", () => addSel(name));
      suggestedEl.appendChild(pill);
    });
    if (suggestedNote) suggestedNote.hidden = !anyCount;
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

  // ---- Chat: describe a condition -> auto-select communities -----------
  function addBubble(role, text) {
    const msg = el("div", { class: "chat-msg " + role });
    const bubble = el("span", { class: "chat-bubble", text });
    msg.appendChild(bubble);
    chatLog.appendChild(msg);
    chatLog.scrollTop = chatLog.scrollHeight;
    return msg;
  }

  function sendChat(message) {
    message = (message || "").trim();
    if (!message) return;
    addBubble("user", message);
    chatInput.value = "";
    chatInput.disabled = true;
    chatSend.disabled = true;
    const thinking = addBubble("bot", "…");

    fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ message }),
    })
      .then((res) => (res.ok ? res.json() : Promise.reject(new Error("HTTP " + res.status))))
      .then((data) => {
        thinking.remove();
        const subs = (data.subreddits || []).filter(Boolean);
        subs.forEach(addSel);   // auto-select every relevant community
        const reply = data.reply ||
          (subs.length ? "Selected " + subs.length + " communities." : "I couldn't find matching communities.");
        addBubble("bot", reply + (subs.length ? "\n" + subs.map((s) => "r/" + s).join(", ") : ""));
      })
      .catch(() => {
        thinking.remove();
        addBubble("bot", "Sorry — I couldn't reach the assistant. Try the search box instead.");
      })
      .finally(() => {
        chatInput.disabled = false;
        chatSend.disabled = false;
        chatInput.focus();
      });
  }

  chatForm.addEventListener("submit", (e) => { e.preventDefault(); sendChat(chatInput.value); });

  // ---- Wizard navigation (one step per screen) -------------------------
  const STEPS = ["communities", "window", "export"];
  const wsteps = Array.from(document.querySelectorAll(".wstep"));
  const stepDots = Array.from(document.querySelectorAll(".step-dot"));
  const backBtn = $("wiz-back");
  const nextBtn = $("wiz-next");
  const wizMsg = $("wiz-msg");
  const exportRecap = $("export-recap");
  let stepIndex = 0;

  const clampStep = (i) => Math.max(0, Math.min(STEPS.length - 1, i));

  function showStep(i) {
    stepIndex = clampStep(i);
    wsteps.forEach((s) => { s.hidden = Number(s.dataset.step) !== stepIndex; });
    stepDots.forEach((d) => {
      const idx = Number(d.dataset.step);
      d.classList.toggle("active", idx === stepIndex);
      d.classList.toggle("done", idx < stepIndex);
      d.setAttribute("aria-current", idx === stepIndex ? "step" : "false");
    });
    hideWizMsg();
    updateWizardControls();
    if (STEPS[stepIndex] === "export") renderRecap();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function updateWizardControls() {
    backBtn.hidden = stepIndex === 0;
    if (stepIndex === STEPS.length - 1) {
      nextBtn.hidden = true;
    } else {
      nextBtn.hidden = false;
      nextBtn.textContent = stepIndex === 0 ? "Next: window →" : "Next: export →";
      nextBtn.disabled = stepIndex === 0 && selected.size === 0;
    }
  }

  function showWizMsg(text) { wizMsg.textContent = text; wizMsg.hidden = false; }
  function hideWizMsg() { wizMsg.hidden = true; wizMsg.textContent = ""; }

  function goStep(i) { location.hash = STEPS[clampStep(i)]; }

  function tryNext() {
    if (stepIndex === 0 && selected.size === 0) {
      showWizMsg("Pick at least one community to continue.");
      return;
    }
    if (stepIndex === 1) {
      if (!startDateEl.value || !endDateEl.value) { showWizMsg("Choose both a start and end date."); return; }
      if (startDateEl.value > endDateEl.value) { showWizMsg("The start date must be on or before the end date."); return; }
    }
    goStep(stepIndex + 1);
  }

  function renderRecap() {
    const subs = Array.from(selected.values());
    const dash = (d) => d || "—";
    const perSub = maxPostsEl.value + " posts" +
      (includeCommentsEl.checked ? ", " + maxCommentsEl.value + " comments" : ", no comments");
    exportRecap.innerHTML = "";
    [
      ["Communities", subs.length ? subs.length + " selected" : "none selected"],
      ["Window", dash(startDateEl.value) + "  →  " + dash(endDateEl.value)],
      ["Per subreddit", perSub],
      ["Usernames", excludeNamesEl.checked ? "excluded" : "included"],
    ].forEach(([k, v]) => {
      const row = el("div", { class: "recap-row" });
      row.appendChild(el("span", { class: "recap-k", text: k }));
      row.appendChild(el("span", { class: "recap-v", text: v }));
      exportRecap.appendChild(row);
    });
    if (subs.length) {
      const chips = el("div", { class: "recap-chips" });
      subs.slice().sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()))
        .forEach((n) => chips.appendChild(el("span", { class: "recap-chip", text: "r/" + n })));
      exportRecap.appendChild(chips);
    }
  }

  function syncStepFromHash() {
    const idx = STEPS.indexOf((location.hash || "").replace("#", ""));
    showStep(idx >= 0 ? idx : 0);
  }

  nextBtn.addEventListener("click", tryNext);
  backBtn.addEventListener("click", () => goStep(stepIndex - 1));
  stepDots.forEach((d) => d.addEventListener("click", () => goStep(Number(d.dataset.step))));
  window.addEventListener("hashchange", syncStepFromHash);

  // ---- Wire up events ---------------------------------------------------
  clearSelectionEl.addEventListener("click", clearSelection);
  generateBtn.addEventListener("click", onGenerate);
  includeCommentsEl.addEventListener("change", syncCommentsCap);

  // ---- Init -------------------------------------------------------------
  syncCommentsCap();
  renderTray();
  loadPool();
  fetchSuggestions();
  syncStepFromHash();
})();
