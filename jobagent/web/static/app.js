/* JobAgent progressive enhancement. Vanilla JS, no inline handlers (CSP: script-src 'self').
   Every page works without this file; it only adds motion, loaders and conveniences. */
(function () {
  "use strict";

  var doc = document;
  // reuse the sprite URL the server rendered (keeps working behind a path prefix)
  var SPRITE = (function () {
    var u = doc.querySelector("use[href*='icons.svg']");
    var h = u ? u.getAttribute("href") : "";
    return h ? h.split("#")[0] : "/static/icons.svg";
  })();
  var root = doc.documentElement;

  function $all(sel, scope) { return Array.prototype.slice.call((scope || doc).querySelectorAll(sel)); }
  function ready(fn) {
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", fn);
    else fn();
  }
  function mq(q) { return window.matchMedia ? window.matchMedia(q) : { matches: false, addEventListener: function () {} }; }
  var reducedMQ = mq("(prefers-reduced-motion: reduce)");
  function reduced() { return reducedMQ.matches; }
  function onMQ(m, fn) {
    if (m.addEventListener) m.addEventListener("change", fn);
    else if (m.addListener) m.addListener(fn);
  }
  function icon(id, cls) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = doc.createElementNS(ns, "svg");
    svg.setAttribute("class", "ico" + (cls ? " " + cls : ""));
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    var use = doc.createElementNS(ns, "use");
    use.setAttribute("href", SPRITE + "#" + id);
    svg.appendChild(use);
    return svg;
  }
  function el(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  var lowPower = (navigator.hardwareConcurrency || 8) <= 4;

  // ================================================================= top progress bar
  var progress = (function () {
    var bar = null, value = 0, timer = 0, hideTimer = 0;
    function ensure() {
      if (!bar) {
        bar = el("div", "ja-progress");
        bar.setAttribute("aria-hidden", "true");
        doc.body.appendChild(bar);
      }
      return bar;
    }
    function set(v) { value = v; ensure().style.transform = "scaleX(" + v.toFixed(3) + ")"; }
    function start() {
      ensure();
      clearTimeout(hideTimer);
      clearInterval(timer);
      bar.classList.add("active");
      set(Math.max(value, 0.08));
      timer = setInterval(function () { set(value + (0.92 - value) * 0.06); }, 250);
      // a navigation that never unloads (e.g. a download) should not leave the bar hanging
      hideTimer = setTimeout(done, 20000);
    }
    function done() {
      if (!bar) return;
      clearInterval(timer);
      clearTimeout(hideTimer);
      set(1);
      hideTimer = setTimeout(function () {
        bar.classList.remove("active");
        setTimeout(function () { set(0); }, 400);
      }, 250);
    }
    return { start: start, done: done };
  })();

  // ================================================================= "agent at work" overlay
  var OVERLAY = {
    apply: {
      title: "Your agent is preparing this application",
      sub: "Nothing is sent without your explicit approval.",
      stages: ["Re-checking the listing…", "Reading the job description…", "Matching your skills…",
               "Tailoring a one-page resume…", "Optimising for ATS…", "Drafting your cover letter…",
               "Preparing screening answers…", "Packaging everything for your review…"]
    },
    run: {
      title: "Agent run in progress",
      sub: "Runs can take a few minutes. Keep this tab open.",
      stages: ["Scanning MNC career sites…", "Querying official job-board APIs…", "Deduplicating listings…",
               "Verifying listings are still open…", "Checking sponsorship evidence…", "Scoring matches…",
               "Preparing drafts for your review…"]
    },
    score: {
      title: "Scoring your jobs",
      sub: "This can take a minute. Keep this tab open.",
      stages: ["Loading your canonical resume…", "Reading job requirements…", "Matching your skills…",
               "Checking sponsorship evidence…", "Scoring matches…", "Ranking recommendations…"]
    },
    resume: {
      title: "Resume Studio at work",
      sub: "Facts are only reordered and retargeted, never invented.",
      stages: ["Reading your resume…", "Extracting experience and skills…", "Analysing bullets and metrics…",
               "Retargeting for the role and country…", "Checking ATS keywords…", "Laying out one clean page…"]
    },
    verify: {
      title: "Re-checking the listing",
      sub: "Making sure the role is still open at its source.",
      stages: ["Contacting the job board…", "Confirming the listing is still open…", "Refreshing the details…"]
    },
    start: {
      title: "Starting your application",
      sub: "Your approval is checked against the review fingerprint.",
      stages: ["Validating your approval…", "Checking the review fingerprint…", "Opening the employer checklist…"]
    }
  };
  var overlay = (function () {
    var box = null, stageEl, titleEl, subEl, dots, timer = 0;
    function build() {
      box = el("div", "agent-overlay");
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      box.setAttribute("aria-busy", "true");
      var inner = el("div", "agent-box");
      inner.tabIndex = -1;
      var radar = el("div", "radar");
      radar.setAttribute("aria-hidden", "true");
      radar.appendChild(el("div", "radar-orbit"));
      radar.appendChild(el("div", "radar-sweep"));
      ["b1", "b2", "b3", "b4"].forEach(function (b) { radar.appendChild(el("span", "blip " + b)); });
      var core = el("div", "radar-core");
      core.appendChild(icon("logo"));
      radar.appendChild(core);
      titleEl = el("h2", "agent-title");
      stageEl = el("p", "agent-stage mb0");
      subEl = el("p", "agent-sub mb0");
      var barEl = el("div", "agent-bar");
      barEl.setAttribute("aria-hidden", "true");
      dots = el("ol", "agent-dots");
      dots.setAttribute("aria-hidden", "true");
      inner.appendChild(radar);
      inner.appendChild(titleEl);
      inner.appendChild(stageEl);
      inner.appendChild(barEl);
      inner.appendChild(dots);
      inner.appendChild(subEl);
      box.appendChild(inner);
      doc.body.appendChild(box);
    }
    function setStage(cfg, i) {
      var span = el("span", null, cfg.stages[i % cfg.stages.length]);
      stageEl.textContent = "";
      stageEl.appendChild(span);
      $all("li", dots).forEach(function (d, j) {
        var k = i % cfg.stages.length;
        d.className = j === k ? "on" : (j < k ? "done" : "");
      });
    }
    function show(kind) {
      var cfg = OVERLAY[kind];
      if (!cfg) return;
      if (!box) build();
      titleEl.textContent = cfg.title;
      subEl.textContent = cfg.sub;
      dots.textContent = "";
      cfg.stages.forEach(function () { dots.appendChild(doc.createElement("li")); });
      var i = 0;
      setStage(cfg, i);
      clearInterval(timer);
      timer = setInterval(function () { i += 1; setStage(cfg, i); }, 2300);
      // show on the next frame so the fade-in transition runs
      requestAnimationFrame(function () {
        box.classList.add("show");
        var inner = box.querySelector(".agent-box");
        if (inner) inner.focus({ preventScroll: true });
      });
    }
    function hide() {
      if (!box) return;
      clearInterval(timer);
      box.classList.remove("show");
    }
    return { show: show, hide: hide };
  })();

  // ================================================================= button loading states
  var loadingButtons = [];
  function setLoading(btn) {
    if (!btn || btn.classList.contains("is-loading")) return;
    var w = btn.getBoundingClientRect().width;
    btn.setAttribute("data-orig-html", btn.innerHTML);
    btn.style.minWidth = Math.ceil(w) + "px";
    btn.classList.add("is-loading");
    btn.setAttribute("aria-busy", "true");
    btn.disabled = true;
    btn.textContent = "";
    var sp = el("span", "spinner");
    sp.setAttribute("aria-hidden", "true");
    btn.appendChild(sp);
    btn.appendChild(doc.createTextNode(" " + (btn.getAttribute("data-loading-text") || "Working…")));
    loadingButtons.push(btn);
  }
  function resetButtons() {
    loadingButtons.forEach(function (btn) {
      var html = btn.getAttribute("data-orig-html");
      if (html != null) btn.innerHTML = html;
      btn.removeAttribute("data-orig-html");
      btn.classList.remove("is-loading");
      btn.removeAttribute("aria-busy");
      btn.style.minWidth = "";
      btn.disabled = false;
    });
    loadingButtons = [];
  }

  // ================================================================= colour theme
  function effectiveTheme() {
    var t = root.getAttribute("data-theme");
    if (t) return t;
    return mq("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  function syncThemeButtons() {
    var t = effectiveTheme();
    $all("[data-theme-toggle]").forEach(function (b) {
      b.setAttribute("aria-label", t === "dark" ? "Switch to light theme" : "Switch to dark theme");
      b.setAttribute("title", t === "dark" ? "Light theme" : "Dark theme");
    });
  }

  ready(function () {
    var body = doc.body;

    // ---------------------------------------------------------------- filter pill dropdowns
    var pills = $all("details.pill");
    pills.forEach(function (d) {
      d.addEventListener("toggle", function () {
        if (d.open) pills.forEach(function (o) { if (o !== d) o.open = false; });
      });
    });
    doc.addEventListener("click", function (e) {
      pills.forEach(function (d) { if (d.open && !d.contains(e.target)) d.open = false; });
    });

    // ---------------------------------------------------------------- mobile navigation drawer
    var sidebar = doc.getElementById("sidebar");
    var backdrop = null;
    function setNav(open, returnFocus) {
      body.classList.toggle("nav-open", open);
      $all("[data-nav-toggle]").forEach(function (b) { b.setAttribute("aria-expanded", open ? "true" : "false"); });
      if (open && sidebar) {
        var first = sidebar.querySelector(".nav a[aria-current], .nav a");
        if (first) setTimeout(function () { first.focus(); }, 60);
      } else if (returnFocus) {
        var t = doc.querySelector(".topbar [data-nav-toggle]");
        if (t) t.focus();
      }
    }
    if (sidebar) {
      backdrop = el("div", "nav-backdrop");
      backdrop.setAttribute("aria-hidden", "true");
      sidebar.parentNode.insertBefore(backdrop, sidebar.nextSibling);
      backdrop.addEventListener("click", function () { setNav(false, true); });
    }
    $all("[data-nav-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () { setNav(!body.classList.contains("nav-open"), true); });
    });

    doc.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      pills.forEach(function (d) {
        if (d.open) {
          d.open = false;
          var s = d.querySelector("summary");
          if (s) s.focus();
        }
      });
      if (body.classList.contains("nav-open")) setNav(false, true);
    });

    // ---------------------------------------------------------------- theme toggle
    $all("[data-theme-toggle]").forEach(function (b) {
      b.hidden = false;
      b.addEventListener("click", function () {
        var next = effectiveTheme() === "dark" ? "light" : "dark";
        root.setAttribute("data-theme", next);
        try { window.localStorage.setItem("ja-theme", next); } catch (err) { /* private mode */ }
        syncThemeButtons();
        doc.dispatchEvent(new CustomEvent("ja:theme"));
      });
    });
    syncThemeButtons();

    // ---------------------------------------------------------------- toast flash messages
    $all(".toast").forEach(function (t) {
      var close = t.querySelector("[data-dismiss]");
      var ms = t.classList.contains("alert-error") ? 10000 : 6000;
      var remaining = ms, started = Date.now(), timer = 0;
      t.style.setProperty("--toast-ms", ms + "ms");
      function dismiss() {
        clearTimeout(timer);
        t.classList.add("leaving");
        setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 380);
      }
      function run() { started = Date.now(); timer = setTimeout(dismiss, remaining); }
      function pause() { clearTimeout(timer); remaining -= Date.now() - started; t.classList.add("paused"); }
      function resume() { t.classList.remove("paused"); run(); }
      if (close) {
        close.hidden = false;
        close.addEventListener("click", dismiss);
      }
      t.addEventListener("mouseenter", pause);
      t.addEventListener("mouseleave", resume);
      t.addEventListener("focusin", pause);
      t.addEventListener("focusout", resume);
      run();
    });

    // ---------------------------------------------------------------- copy buttons
    function fallbackCopy(text) {
      var ta = doc.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.className = "visually-hidden";
      body.appendChild(ta);
      ta.select();
      var ok = false;
      try { ok = doc.execCommand("copy"); } catch (err) { ok = false; }
      body.removeChild(ta);
      return ok;
    }
    function feedback(btn, ok) {
      var orig = btn.getAttribute("data-label") || btn.textContent;
      btn.setAttribute("data-label", orig);
      btn.textContent = ok ? "Copied" : "Copy failed";
      btn.classList.add("copied");
      setTimeout(function () { btn.textContent = orig; btn.classList.remove("copied"); }, 1600);
    }
    $all("[data-copy], [data-copy-target]").forEach(function (btn) {
      btn.hidden = false;
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        var text = btn.getAttribute("data-copy");
        var target = btn.getAttribute("data-copy-target");
        if (target) {
          var t = doc.querySelector(target);
          if (!t) return;
          text = ("value" in t && t.tagName !== "PRE" && t.tagName !== "DIV") ? t.value : t.textContent;
        }
        text = text || "";
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(text).then(function () { feedback(btn, true); },
            function () { feedback(btn, fallbackCopy(text)); });
        } else {
          feedback(btn, fallbackCopy(text));
        }
      });
    });

    // ---------------------------------------------------------------- confirm prompts (registered before the
    // document-level submit handler so a cancelled confirm never shows a loader)
    $all("form[data-confirm]").forEach(function (f) {
      f.addEventListener("submit", function (e) {
        if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
      });
    });
    $all("button[data-confirm], input[type=submit][data-confirm], a[data-confirm]").forEach(function (b) {
      b.addEventListener("click", function (e) {
        if (!window.confirm(b.getAttribute("data-confirm"))) { e.preventDefault(); e.stopPropagation(); }
      });
    });

    // ---------------------------------------------------------------- submit: progress bar, button state, overlay
    doc.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      var form = e.target;
      if (!form || form.tagName !== "FORM") return;
      var btn = e.submitter || form.querySelector("button[type=submit], button:not([type])");
      progress.start();
      var kind = form.getAttribute("data-overlay");
      if (kind) overlay.show(kind);
      // disable after the browser has captured the form data (a disabled submitter would drop its name/value)
      if (btn && btn.tagName === "BUTTON" && !form.hasAttribute("data-no-loading")) {
        setTimeout(function () { setLoading(btn); }, 0);
      }
    });

    // ---------------------------------------------------------------- link clicks: progress bar
    doc.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest ? e.target.closest("a[href]") : null;
      if (!a || (a.target && a.target !== "_self") || a.hasAttribute("download")) return;
      var url;
      try { url = new URL(a.href, location.href); } catch (err) { return; }
      if (url.origin !== location.origin) return;
      if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
      if (/\/download\.|\.csv$|^\/files\//.test(url.pathname)) return;
      progress.start();
    });

    // ---------------------------------------------------------------- password visibility + strength
    $all("input[type=password]").forEach(function (input) {
      var wrap = input.parentNode;
      if (!wrap.classList || !wrap.classList.contains("input-wrap")) {
        wrap = el("div", "input-wrap");
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
      }
      wrap.classList.add("has-toggle");
      var b = el("button", "pw-toggle");
      b.type = "button";
      b.setAttribute("aria-pressed", "false");
      b.setAttribute("aria-label", "Show password");
      b.setAttribute("aria-controls", input.id || "");
      b.appendChild(icon("eye", "i-on"));
      b.appendChild(icon("eye-off", "i-off"));
      b.addEventListener("click", function () {
        var show = input.type === "password";
        input.type = show ? "text" : "password";
        b.setAttribute("aria-pressed", show ? "true" : "false");
        b.setAttribute("aria-label", show ? "Hide password" : "Show password");
        input.focus();
      });
      wrap.appendChild(b);
    });
    // reset to hidden before submitting so browsers never store the value as plain text in history
    doc.addEventListener("submit", function (e) {
      $all(".pw-toggle[aria-pressed=true]", e.target).forEach(function (b) { b.click(); });
    }, true);

    $all("input[data-strength]").forEach(function (input) {
      var wrap = input.closest(".input-wrap") || input;
      var box = el("div", "strength");
      box.setAttribute("data-level", "0");
      var bar = el("div", "strength-bar");
      bar.setAttribute("aria-hidden", "true");
      for (var i = 0; i < 4; i++) bar.appendChild(doc.createElement("span"));
      var txt = el("p", "strength-text mb0");
      txt.id = (input.id || "pw") + "-strength";
      txt.setAttribute("aria-live", "polite");
      box.appendChild(bar);
      box.appendChild(txt);
      wrap.parentNode.insertBefore(box, wrap.nextSibling);
      var LABELS = ["", "Weak", "Fair", "Good", "Strong"];
      function update() {
        var v = input.value;
        var need = [];
        if (v.length < 12) need.push("12+ characters");
        if (!(/[a-z]/.test(v) && /[A-Z]/.test(v))) need.push("upper- and lower-case letters");
        if (!/\d/.test(v)) need.push("a digit");
        var s = 0;
        if (v.length >= 12) s++;
        if (/[a-z]/.test(v) && /[A-Z]/.test(v)) s++;
        if (/\d/.test(v)) s++;
        if (/[^A-Za-z0-9]/.test(v) || v.length >= 16) s++;
        if (v.length < 8) s = Math.min(s, 1);
        if (need.length && s > 3) s = 3;
        var level = v ? Math.max(1, s) : 0;
        box.setAttribute("data-level", String(level));
        if (!v) txt.textContent = "";
        else txt.textContent = "Strength: " + LABELS[level] + (need.length ? " — still needs " + need.join(", ") : " — meets the requirements");
      }
      input.addEventListener("input", update);
      update();
    });
    $all("input[data-match]").forEach(function (input) {
      var other = doc.querySelector(input.getAttribute("data-match"));
      if (!other) return;
      var hint = el("p", "match-hint mb0");
      hint.setAttribute("aria-live", "polite");
      var wrap = input.closest(".input-wrap") || input;
      wrap.parentNode.insertBefore(hint, wrap.nextSibling);
      function update() {
        if (!input.value) { hint.textContent = ""; hint.className = "match-hint mb0"; return; }
        var ok = input.value === other.value;
        hint.textContent = ok ? "Passwords match" : "Passwords do not match yet";
        hint.className = "match-hint mb0 " + (ok ? "ok" : "bad");
      }
      input.addEventListener("input", update);
      other.addEventListener("input", update);
    });

    // ---------------------------------------------------------------- table text filter
    $all("input[data-filter-table]").forEach(function (input) {
      var table = doc.querySelector(input.getAttribute("data-filter-table"));
      if (!table) return;
      var status = input.getAttribute("data-filter-status") ? doc.querySelector(input.getAttribute("data-filter-status")) : null;
      function apply() {
        var q = input.value.trim().toLowerCase();
        var shown = 0, rows = $all("tbody tr", table);
        rows.forEach(function (tr) {
          var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
          tr.classList.toggle("hidden-row", !hit);
          if (hit) shown++;
        });
        if (status) status.textContent = shown + " of " + rows.length + " shown";
      }
      input.addEventListener("input", apply);
      apply();
    });

    // ---------------------------------------------------------------- auto-submit (toggles, sort)
    $all("[data-autosubmit]").forEach(function (c) {
      c.addEventListener("change", function () {
        var f = c.form;
        if (!f) return;
        if (typeof f.requestSubmit === "function") f.requestSubmit(); else { progress.start(); f.submit(); }
      });
    });

    // ---------------------------------------------------------------- select all
    $all("input[data-select-all]").forEach(function (master) {
      master.addEventListener("change", function () {
        $all(master.getAttribute("data-select-all")).forEach(function (cb) { cb.checked = master.checked; });
      });
    });

    // ---------------------------------------------------------------- compare counter
    var cmpBtn = doc.querySelector("[data-compare-button]");
    if (cmpBtn) {
      var boxes = $all("input[data-compare]");
      var counter = doc.querySelector("[data-compare-count]");
      var update = function () {
        var n = boxes.filter(function (b) { return b.checked; }).length;
        cmpBtn.disabled = n < 2;
        if (counter) counter.textContent = n ? n + " selected (max 4)" : "Tick 2–4 jobs to compare";
        boxes.forEach(function (b) { if (!b.checked) b.disabled = n >= 4; });
      };
      boxes.forEach(function (b) { b.addEventListener("change", update); });
      update();
    }

    // ---------------------------------------------------------------- reveal: score rings + count-up numbers
    enhance(doc);

    // ---------------------------------------------------------------- hover tilt on cards
    initTilt();

    // ---------------------------------------------------------------- jobs: in-page detail pane with skeleton
    initJobPane();

    // ---------------------------------------------------------------- constellation background
    initStarfield();

    progress.done();
  });

  // back/forward cache: undo loaders when the page is shown again
  window.addEventListener("pageshow", function (e) {
    progress.done();
    overlay.hide();
    if (e.persisted) resetButtons();
  });

  // ================================================================= reveal-on-view helpers
  var io = ("IntersectionObserver" in window) ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      io.unobserve(en.target);
      reveal(en.target);
    });
  }, { rootMargin: "0px 0px -6% 0px", threshold: 0.15 }) : null;

  function countUp(node) {
    var target = parseInt(node.getAttribute("data-count"), 10);
    if (!isFinite(target) || target <= 0 || reduced()) return;
    var final = node.textContent;
    var dur = Math.min(1400, 600 + target * 6), t0 = 0;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min(1, (ts - t0) / dur);
      var eased = 1 - Math.pow(1 - p, 3);
      node.textContent = String(Math.round(target * eased));
      if (p < 1) requestAnimationFrame(step);
      else node.textContent = final;
    }
    node.textContent = "0";
    requestAnimationFrame(step);
  }
  function reveal(node) {
    if (node.classList.contains("score")) {
      node.classList.add("in-view");
      var num = node.querySelector("[data-count]");
      if (num) countUp(num);
    } else if (node.hasAttribute("data-count")) {
      countUp(node);
    }
  }
  function enhance(scope) {
    $all(".score, .kpi-value[data-count]", scope).forEach(function (n) {
      if (io && !reduced()) io.observe(n);
      else n.classList.add("in-view");
    });
  }

  // ================================================================= tilt
  function initTilt() {
    if (reduced() || !mq("(hover: hover) and (pointer: fine)").matches) return;
    $all(".job-card, .mnc-card").forEach(function (card) {
      var raf = 0, px = 0, py = 0;
      function apply() {
        raf = 0;
        var r = card.getBoundingClientRect();
        var x = (px - r.left) / r.width - 0.5;
        var y = (py - r.top) / r.height - 0.5;
        card.style.setProperty("--rx", (-y * 4).toFixed(2) + "deg");
        card.style.setProperty("--ry", (x * 5).toFixed(2) + "deg");
      }
      card.addEventListener("pointermove", function (e) {
        px = e.clientX; py = e.clientY;
        if (!raf) raf = requestAnimationFrame(apply);
      });
      card.addEventListener("pointerleave", function () {
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        card.style.removeProperty("--rx");
        card.style.removeProperty("--ry");
      });
    });
  }

  // ================================================================= jobs split view
  function initJobPane() {
    var list = doc.querySelector(".job-list");
    var pane = doc.querySelector(".detail-pane");
    if (!list || !pane || !window.fetch || !window.DOMParser || !history.pushState) return;
    var live = el("p", "visually-hidden");
    live.setAttribute("aria-live", "polite");
    doc.body.appendChild(live);
    var pending = null;

    function skeleton() {
      var s = el("div", "skeleton-detail");
      s.setAttribute("aria-hidden", "true");
      var top = el("div", "sk-row");
      var col = el("div", "grow stack-sm");
      col.appendChild(el("div", "skeleton sk-title"));
      col.appendChild(el("div", "skeleton sk-line sk-w40"));
      col.appendChild(el("div", "skeleton sk-line sk-w55"));
      top.appendChild(col);
      top.appendChild(el("div", "skeleton sk-ring"));
      s.appendChild(top);
      var facts = el("div", "sk-facts");
      for (var i = 0; i < 6; i++) facts.appendChild(el("div", "skeleton"));
      s.appendChild(facts);
      s.appendChild(el("div", "skeleton sk-block"));
      ["sk-w90", "sk-w80", "sk-w90", "sk-w55"].forEach(function (w) { s.appendChild(el("div", "skeleton sk-line " + w)); });
      return s;
    }
    function markSelected(href) {
      $all(".job-card", list).forEach(function (li) {
        var a = li.querySelector("a.job-title");
        var on = !!(a && href && a.href === href);
        li.classList.toggle("selected", on);
        if (a) { if (on) a.setAttribute("aria-current", "true"); else a.removeAttribute("aria-current"); }
      });
    }
    function load(url, push) {
      if (pending && pending.abort) pending.abort();
      pending = window.AbortController ? new AbortController() : null;
      progress.start();
      markSelected(url);
      var sk = skeleton();
      pane.insertBefore(sk, pane.firstChild);
      pane.classList.add("is-loading");
      pane.setAttribute("aria-busy", "true");
      pane.scrollTop = 0;
      fetch(url, { credentials: "same-origin", signal: pending ? pending.signal : undefined })
        .then(function (r) {
          var p = new URL(r.url, location.href).pathname;
          if (!r.ok || p !== "/jobs") throw new Error("fallback");
          return r.text();
        })
        .then(function (html) {
          var d = new DOMParser().parseFromString(html, "text/html");
          var np = d.querySelector(".detail-pane");
          if (!np) throw new Error("fallback");
          pane.innerHTML = np.innerHTML;
          pane.classList.remove("is-loading");
          pane.removeAttribute("aria-busy");
          pane.classList.remove("swap-in");
          void pane.offsetWidth;
          pane.classList.add("swap-in");
          if (push) history.pushState({ jaJob: true }, "", url);
          if (d.title) doc.title = d.title;
          enhance(pane);
          var h = pane.querySelector("h2");
          live.textContent = h ? "Showing " + h.textContent.trim() : "Job details loaded";
          progress.done();
        })
        .catch(function (err) {
          if (err && err.name === "AbortError") return;
          location.href = url;
        });
    }
    list.addEventListener("click", function (e) {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target.closest ? e.target.closest("a.job-title") : null;
      if (!a || window.getComputedStyle(pane).display === "none") return;
      e.preventDefault();
      if (a.getAttribute("aria-current") === "true") return;
      load(a.href, true);
    });
    window.addEventListener("popstate", function () {
      if (location.pathname === "/jobs") load(location.href, false);
    });
  }

  // ================================================================= constellation canvas
  function initStarfield() {
    if (reduced() || !window.requestAnimationFrame) return;
    var canvas = el("canvas", "bg-canvas");
    canvas.setAttribute("aria-hidden", "true");
    var ctx = canvas.getContext && canvas.getContext("2d");
    if (!ctx) return;
    doc.body.insertBefore(canvas, doc.body.firstChild);

    var dpr = Math.min(window.devicePixelRatio || 1, lowPower ? 1 : 1.5);
    var W = 0, H = 0, nodes = [], raf = 0, last = 0, running = false;
    var FRAME = 1000 / (lowPower ? 24 : 30);
    var LINK = 140, LINK2 = LINK * LINK, MOUSE = 190, MOUSE2 = MOUSE * MOUSE;
    var mouse = { x: -1e5, y: -1e5 };
    var nodeRGB = "120,220,255", linkRGB = "129,140,248", sprite = null;

    function readColors() {
      var cs = getComputedStyle(root);
      nodeRGB = (cs.getPropertyValue("--canvas-node") || "120,220,255").trim();
      linkRGB = (cs.getPropertyValue("--canvas-link") || "129,140,248").trim();
      // pre-rendered glow sprite: one drawImage per node instead of a gradient per frame
      sprite = doc.createElement("canvas");
      sprite.width = sprite.height = 32;
      var s = sprite.getContext("2d");
      var g = s.createRadialGradient(16, 16, 0, 16, 16, 16);
      g.addColorStop(0, "rgba(" + nodeRGB + ",1)");
      g.addColorStop(0.25, "rgba(" + nodeRGB + ",.55)");
      g.addColorStop(1, "rgba(" + nodeRGB + ",0)");
      s.fillStyle = g;
      s.fillRect(0, 0, 32, 32);
    }
    function makeNode() {
      return {
        x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() - 0.5) * 0.28, vy: (Math.random() - 0.5) * 0.28,
        r: Math.random() * 1.3 + 0.6, p: Math.random() * Math.PI * 2
      };
    }
    function resize() {
      W = window.innerWidth; H = window.innerHeight;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var target = Math.round((W * H) / (lowPower ? 30000 : 15000));
      target = Math.max(16, Math.min(target, lowPower ? 38 : 90));
      while (nodes.length < target) nodes.push(makeNode());
      if (nodes.length > target) nodes.length = target;
    }
    function draw(ts) {
      var i, j, a, b, dx, dy, d2, n = nodes.length;
      ctx.clearRect(0, 0, W, H);
      for (i = 0; i < n; i++) {
        a = nodes[i];
        a.x += a.vx; a.y += a.vy;
        if (a.x < -20) a.x = W + 20; else if (a.x > W + 20) a.x = -20;
        if (a.y < -20) a.y = H + 20; else if (a.y > H + 20) a.y = -20;
      }
      ctx.lineWidth = 1;
      for (i = 0; i < n; i++) {
        a = nodes[i];
        for (j = i + 1; j < n; j++) {
          b = nodes[j];
          dx = a.x - b.x; dy = a.y - b.y; d2 = dx * dx + dy * dy;
          if (d2 < LINK2) {
            ctx.strokeStyle = "rgba(" + linkRGB + "," + ((1 - d2 / LINK2) * 0.32).toFixed(3) + ")";
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          }
        }
        dx = a.x - mouse.x; dy = a.y - mouse.y; d2 = dx * dx + dy * dy;
        if (d2 < MOUSE2) {
          ctx.strokeStyle = "rgba(" + nodeRGB + "," + ((1 - d2 / MOUSE2) * 0.55).toFixed(3) + ")";
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(mouse.x, mouse.y); ctx.stroke();
        }
      }
      for (i = 0; i < n; i++) {
        a = nodes[i];
        var tw = 0.65 + 0.35 * Math.sin(ts / 900 + a.p);
        var size = a.r * 7 * tw;
        ctx.globalAlpha = 0.55 + 0.45 * tw;
        ctx.drawImage(sprite, a.x - size / 2, a.y - size / 2, size, size);
      }
      ctx.globalAlpha = 1;
    }
    function loop(ts) {
      raf = requestAnimationFrame(loop);
      if (ts - last < FRAME) return;
      last = ts;
      draw(ts);
    }
    function start() {
      if (running || doc.hidden || reduced()) return;
      running = true; last = 0;
      raf = requestAnimationFrame(loop);
    }
    function stop() {
      running = false;
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
    }

    readColors();
    resize();
    start();

    var rt = 0;
    window.addEventListener("resize", function () {
      clearTimeout(rt);
      rt = setTimeout(function () { resize(); if (!running) draw(performance.now()); }, 150);
    }, { passive: true });
    window.addEventListener("pointermove", function (e) { mouse.x = e.clientX; mouse.y = e.clientY; }, { passive: true });
    doc.addEventListener("pointerleave", function () { mouse.x = mouse.y = -1e5; });
    // pause completely while the tab is hidden
    doc.addEventListener("visibilitychange", function () { if (doc.hidden) stop(); else start(); });
    onMQ(reducedMQ, function () {
      if (reduced()) { stop(); canvas.style.display = "none"; }
      else { canvas.style.display = ""; start(); }
    });
    doc.addEventListener("ja:theme", readColors);
    onMQ(mq("(prefers-color-scheme: light)"), function () { readColors(); syncThemeButtons(); });
  }
})();
