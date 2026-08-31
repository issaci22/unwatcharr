/* ===========================================================================
   Unwatcharr — front-end runtime
   ---------------------------------------------------------------------------
   Vanilla, dependency-free, no CDN: the NAS this runs on may have no outbound
   internet, and the whole point of the app is to stay light.

   Two layers:
     SHELL   theme, navigation drawer, toasts, busy buttons, run indicator.
             Loaded on every page; owns nothing a page needs to know about.
     DATA    a single fetch wrapper around the JSON API in app/web/api.py.
             There is no second data path and no server-rendered mutation.
   =========================================================================== */
(function () {
  "use strict";

  var THEME_KEY = "unwatcharr.theme";

  /* ==================================================================== shell */

  function readTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (err) { return null; }
  }

  function currentTheme() {
    var saved = readTheme();
    if (saved === "light" || saved === "dark") { return saved; }
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
      ? "light" : "dark";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (err) { /* private mode */ }
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      var label = btn.querySelector(".theme-label");
      if (label) { label.textContent = theme === "light" ? "Dark" : "Light"; }
      btn.setAttribute("title", theme === "light"
        ? "Switch to the dark theme" : "Switch to the light theme");
    });
  }

  function initTheme() {
    applyTheme(currentTheme());
    document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        applyTheme(currentTheme() === "light" ? "dark" : "light");
      });
    });
  }

  // --- navigation drawer (mobile) -------------------------------------------
  function initDrawer() {
    var drawer = document.getElementById("sidenav");
    var scrim = document.querySelector(".scrim");
    var opener = document.querySelector("[data-drawer-open]");
    if (!drawer || !opener) { return; }

    function setOpen(open) {
      drawer.setAttribute("data-open", open ? "true" : "false");
      opener.setAttribute("aria-expanded", open ? "true" : "false");
      document.body.classList.toggle("drawer-open", open);
      if (scrim) {
        if (open) { scrim.removeAttribute("hidden"); } else { scrim.setAttribute("hidden", ""); }
        scrim.setAttribute("data-open", open ? "true" : "false");
      }
      // Focus follows the drawer, so keyboard and screen-reader users are not
      // left behind the scrim.
      if (open) {
        var first = drawer.querySelector("[data-drawer-close], .nav__item");
        if (first) { first.focus(); }
      } else {
        opener.focus();
      }
    }

    opener.addEventListener("click", function () {
      setOpen(drawer.getAttribute("data-open") !== "true");
    });
    document.querySelectorAll("[data-drawer-close]").forEach(function (el) {
      el.addEventListener("click", function () { setOpen(false); });
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && drawer.getAttribute("data-open") === "true") {
        setOpen(false);
      }
    });
    // Following a link inside the drawer navigates; leave no open state behind.
    drawer.querySelectorAll(".nav__item").forEach(function (link) {
      link.addEventListener("click", function () { setOpen(false); });
    });
  }

  // --- toasts ---------------------------------------------------------------
  var TOAST_ICON = {
    ok: "M4.8 12.6 9.6 17.4 19.2 6.6",
    err: "M12 3.6 21 19.6H3Z M12 9.4v4.2 M12 16.9h.01",
    warn: "M12 3.6 21 19.6H3Z M12 9.4v4.2 M12 16.9h.01",
    info: "M12 11.2v5 M12 7.9h.01"
  };

  function toast(message, kind) {
    var host = document.getElementById("toasts");
    if (!host) { return; }
    kind = kind || "info";

    var el = document.createElement("div");
    // The legacy class is kept alongside the modifier while pages are rebuilt.
    el.className = "toast toast--" + kind + " " + kind;

    var icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("width", "16");
    icon.setAttribute("height", "16");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.8");
    icon.setAttribute("stroke-linecap", "round");
    icon.setAttribute("stroke-linejoin", "round");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("class", "icon " + (kind === "err" ? "text-danger"
      : kind === "ok" ? "text-ok" : kind === "warn" ? "text-warn" : "text-info"));
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", TOAST_ICON[kind] || TOAST_ICON.info);
    icon.appendChild(path);

    var text = document.createElement("span");
    text.className = "toast__text";
    text.textContent = message;

    var close = document.createElement("button");
    close.type = "button";
    close.className = "btn btn--ghost btn--sm toast__close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.addEventListener("click", function () { el.remove(); });

    el.appendChild(icon);
    el.appendChild(text);
    el.appendChild(close);
    host.appendChild(el);

    // Errors stay long enough to be read and copied; confirmations do not.
    setTimeout(function () { el.remove(); }, kind === "err" ? 9000 : 4500);
  }
  window.toast = toast;

  // --- busy buttons ---------------------------------------------------------
  // A button mid-request keeps its label and gains a spinner, so nothing ever
  // looks visually dead while a request is in flight.
  function busy(el, state) {
    if (!el) { return; }
    if (state) {
      el.setAttribute("data-busy", "true");
      el.setAttribute("aria-busy", "true");
    } else {
      el.removeAttribute("data-busy");
      el.removeAttribute("aria-busy");
    }
  }
  window.busy = busy;

  /* ===================================================================== data */

  // Every mutation goes through here, so error handling lives in one place.
  // The API always answers with a flat {"detail": "..."} sentence written for a
  // human — it is shown verbatim rather than mapped to our own copy.
  // A status code is not a sentence. Every endpoint in this app answers a
  // failure with a flat {"detail": "..."} written for a human, so that is what
  // gets shown -- but a request can still fail before any handler runs (the
  // session lapsed, the origin check rejected it, uvicorn fell over, the
  // browser lost the network). Those are the cases this maps, so nothing in the
  // UI ever prints a bare number at somebody.
  var HTTP_TEXT = {
    400: "That request was not valid. Reload the page and try again.",
    401: "Your session has ended. Sign in again to continue.",
    403: "That request was blocked as cross-site. Reach this page through the "
       + "same address you signed in on, rather than through a second proxy or hostname.",
    404: "That no longer exists — it was probably removed in another tab. Reload the page.",
    405: "That action is not available on this build.",
    409: "Something else changed first, so this was refused. Reload the page and look again.",
    413: "That was too large to send.",
    422: "Some of those values were not usable. Check the fields and try again.",
    429: "Too many attempts in a row. Wait a minute, then try again.",
    500: "Unwatcharr hit an internal error handling that. The Logs page will "
       + "have the traceback.",
    502: "No answer from the server behind this address.",
    503: "Unwatcharr is busy or still starting up. Try again in a moment.",
    504: "That took too long to answer. If Plex is slow or unreachable, the "
       + "Logs page will say so."
  };

  // The one place a thrown error becomes text a person reads.
  function errorText(err) {
    if (!err) { return "Something went wrong."; }
    if (err.detail) { return err.detail; }
    if (err.status && HTTP_TEXT[err.status]) { return HTTP_TEXT[err.status]; }
    if (err.status) {
      return "The server answered HTTP " + err.status + " and said nothing more. "
           + "The Logs page will have the detail.";
    }
    if (err.offline) {
      return "Could not reach Unwatcharr. Check that the container is still "
           + "running and that this browser still has a route to it.";
    }
    return err.message || "Something went wrong.";
  }
  window.errorText = errorText;

  async function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }

    var response;
    try {
      response = await fetch(path, init);
    } catch (networkError) {
      // fetch only rejects when the request never completed at all.
      var offline = new Error(
        "Could not reach Unwatcharr. Check that the container is still running "
        + "and that this browser still has a route to it.");
      offline.offline = true;
      throw offline;
    }

    var payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok) {
      // The API's own sentence wins; the map only covers a failure that never
      // reached a handler.
      var detail = (payload && payload.detail) || null;
      var error = new Error(detail || HTTP_TEXT[response.status] ||
        ("The server answered HTTP " + response.status + " and said nothing more. "
         + "The Logs page will have the detail."));
      error.status = response.status;
      error.detail = detail;
      error.payload = payload;
      throw error;
    }
    return payload;
  }
  window.api = api;

  // Render a failure inline, beside the thing that failed, rather than in a
  // toast that vanishes while it is still being read. Pass null to clear.
  window.showError = function (el, err) {
    if (!el) { return; }
    el.textContent = err ? errorText(err) : "";
    el.hidden = !err;
  };

  // Call an endpoint, report the outcome, and reload so the page shows truth
  // rather than an optimistic guess. `options.source` marks the button to busy.
  window.act = async function (path, options, message) {
    options = options || {};
    busy(options.source, true);
    try {
      var result = await api(path, options);
      toast(message || "Done.", "ok");
      if (options.reload !== false) {
        setTimeout(function () { window.location.reload(); }, 400);
      } else {
        busy(options.source, false);
      }
      return result;
    } catch (err) {
      busy(options.source, false);
      toast(err.message, "err");
      throw err;
    }
  };

  window.confirmAct = function (question, path, options, message) {
    if (!window.confirm(question)) { return; }
    window.act(path, options, message);
  };

  /* ========================================================== confirm dialog */

  // Deliberate actions get a real dialog, not window.confirm: the point is that
  // the consequence is readable before the button is pressed. Resolves to a
  // boolean and always cleans itself up.
  //
  //   confirmDialog({ title, body, detail, confirmLabel, tone })
  //     body    string or array of strings, rendered as paragraphs
  //     detail  a caveat rendered as a callout (the undo caveat, for instance)
  //     tone    "danger" | "warn" | undefined
  window.confirmDialog = function (opts) {
    opts = opts || {};
    return new Promise(function (resolve) {
      var dialog = document.createElement("dialog");
      dialog.className = "dialog--confirm";

      var head = document.createElement("div");
      head.className = "dialog__head";
      var title = document.createElement("h2");
      title.className = "dialog__title";
      title.textContent = opts.title || "Are you sure?";
      head.appendChild(title);

      var body = document.createElement("div");
      body.className = "dialog__body stack stack--sm";
      var lines = Array.isArray(opts.body) ? opts.body : [opts.body || ""];
      lines.forEach(function (line) {
        if (!line) { return; }
        var para = document.createElement("p");
        para.className = "mb-0";
        para.textContent = line;
        body.appendChild(para);
      });
      if (opts.detail) {
        var note = document.createElement("div");
        note.className = "callout callout--warn mb-0";
        var noteText = document.createElement("div");
        noteText.className = "callout__body small";
        noteText.textContent = opts.detail;
        note.appendChild(noteText);
        body.appendChild(note);
      }

      var foot = document.createElement("div");
      foot.className = "dialog__foot";
      var cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn";
      cancel.textContent = opts.cancelLabel || "Cancel";
      var confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = "btn " + (opts.tone === "danger" ? "btn--danger" : "btn--primary");
      confirm.textContent = opts.confirmLabel || "Continue";
      foot.appendChild(cancel);
      foot.appendChild(confirm);

      dialog.appendChild(head);
      dialog.appendChild(body);
      dialog.appendChild(foot);
      document.body.appendChild(dialog);

      var answer = false;
      function finish() { dialog.close(); }
      cancel.addEventListener("click", finish);
      confirm.addEventListener("click", function () { answer = true; finish(); });
      // Escape and the backdrop both count as "no".
      dialog.addEventListener("close", function () {
        dialog.remove();
        resolve(answer);
      });

      dialog.showModal();
      confirm.focus();
    });
  };

  /* ============================================================ run indicator */

  var polling = null;

  function paintRail(state) {
    var rail = document.getElementById("rail-run");
    var railText = document.getElementById("rail-run-text");
    if (!rail || !railText) { return; }
    if (state && state.busy) {
      rail.removeAttribute("hidden");
      railText.textContent =
        (state.mode === "dry" ? "Dry run" : "Run") + " — " + (state.percent || 0) + "%" +
        (state.current ? " · " + state.current : "");
    } else {
      rail.setAttribute("hidden", "");
    }
  }

  // The console on a page that owns run controls: a meter, a phase line and the
  // rule/user pass currently being worked on.
  function paintConsole(state) {
    var box = document.getElementById("run-progress");
    var meter = document.getElementById("run-meter");
    var panel = document.getElementById("run-live");
    var idle = document.getElementById("run-idle");

    if (!box) { return false; }

    if (!state || !state.busy) {
      if (panel) { panel.setAttribute("hidden", ""); }
      if (idle) { idle.removeAttribute("hidden"); }
      return false;
    }

    if (panel) { panel.removeAttribute("hidden"); }
    if (idle) { idle.setAttribute("hidden", ""); }
    if (meter) {
      meter.style.width = (state.percent || 0) + "%";
      var bar = meter.parentElement;
      if (bar && bar.hasAttribute("role")) {
        bar.setAttribute("aria-valuenow", String(state.percent || 0));
      }
    }
    box.textContent =
      (state.mode === "dry" ? "Dry run" : "Run") + " · " +
      (state.phase || "working") + " · pass " + state.done + " of " + state.total +
      " (" + state.percent + "%)" + (state.current ? " · " + state.current : "");
    return true;
  }

  async function pollRun() {
    try {
      var state = await api("/api/runs/current");
      paintRail(state);
      var running = paintConsole(state);

      if (!running && polling) {
        clearInterval(polling);
        polling = null;
        var box = document.getElementById("run-progress");
        if (box) { box.textContent = "Run finished. Reloading…"; }
        setTimeout(function () { window.location.reload(); }, 600);
      }
    } catch (err) {
      // A failed poll is not worth a toast; the next tick retries.
    }
  }

  window.startRunPolling = function () {
    if (polling) { return; }
    polling = setInterval(pollRun, 1000);
    pollRun();
  };

  // A run started from any page: read `effective_mode`, never the mode asked
  // for. Safe mode silently downgrades apply to dry, and saying otherwise here
  // would be the one lie the UI must never tell.
  window.startRun = async function (mode, ruleIds, source) {
    busy(source, true);
    try {
      var body = { mode: mode };
      if (ruleIds) { body.rule_ids = ruleIds; }
      var started = await api("/api/runs", { method: "POST", body: body });
      if (started.safe_mode && mode === "apply") {
        toast("Safe mode is on — this run was forced to a dry run. Nothing in Plex will change.", "warn");
      } else {
        toast((started.effective_mode === "dry" ? "Dry run" : "Run") + " started.", "ok");
      }
      window.startRunPolling();
      return started;
    } catch (err) {
      toast(err.message, "err");
      throw err;
    } finally {
      busy(source, false);
    }
  };

  /* ======================================================================= go */

  document.addEventListener("DOMContentLoaded", function () {
    initTheme();
    initDrawer();

    if (document.getElementById("run-progress") || document.getElementById("rail-run")) {
      api("/api/runs/current").then(function (state) {
        if (state && state.busy) { window.startRunPolling(); }
      }).catch(function () {});
    }

    // An avatar or poster that will not load degrades to nothing rather than a
    // broken-image icon.
    document.addEventListener("error", function (event) {
      var el = event.target;
      if (el && el.tagName === "IMG") { el.style.visibility = "hidden"; }
    }, true);
  });
})();
