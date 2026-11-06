/* Traceloom UI behaviour. Loaded as an external script (CSP script-src 'self'): no inline JS. */
(function () {
  'use strict';
  var root = document.documentElement;

  // --- theme: system default, a toggle, persisted in localStorage and a cookie -----------------
  function readTheme() { try { return localStorage.getItem('tl-theme'); } catch (e) { return null; } }
  function applyTheme(mode) {
    // mode: 'light' | 'dark' | 'auto'. Auto removes the attribute so prefers-color-scheme rules.
    if (mode === 'light' || mode === 'dark') { root.setAttribute('data-theme', mode); }
    else { root.removeAttribute('data-theme'); mode = 'auto'; }
    try { if (mode === 'auto') { localStorage.removeItem('tl-theme'); } else { localStorage.setItem('tl-theme', mode); } } catch (e) { /* storage blocked */ }
    document.cookie = 'tl_theme=' + mode + '; path=/; max-age=31536000; samesite=lax';
    syncThemeControls(mode);
  }
  function effectiveTheme() {
    return root.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  }
  function syncThemeControls(mode) {
    mode = mode || readTheme() || 'auto';
    document.querySelectorAll('[data-themebtn]').forEach(function (b) {
      b.setAttribute('aria-pressed', String(b.getAttribute('data-themebtn') === mode));
    });
    document.querySelectorAll('[data-theme-toggle] use').forEach(function (u) {
      u.setAttribute('href', effectiveTheme() === 'dark' ? '#i-moon' : '#i-sun');
    });
  }
  var saved = readTheme();
  if (saved === 'light' || saved === 'dark') { root.setAttribute('data-theme', saved); } // before first paint

  // --- helpers -----------------------------------------------------------------------------------
  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function typing(el) {
    return el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable);
  }
  function closeMenus(except) {
    document.querySelectorAll('[data-menu]:not([hidden])').forEach(function (m) {
      if (m !== except) { m.hidden = true; var t = document.querySelector('[data-menu-toggle="' + m.id + '"]'); if (t) { t.setAttribute('aria-expanded', 'false'); } }
    });
  }
  function helpDialog() { return document.getElementById('help-dialog'); }
  function toggleHelp(force) {
    var d = helpDialog(); if (!d || typeof d.showModal !== 'function') { return; }
    var open = typeof force === 'boolean' ? force : !d.open;
    if (open && !d.open) { d.showModal(); } else if (!open && d.open) { d.close(); }
  }

  // --- click delegation: theme, menus, dialogs, copy ---------------------------------------------
  document.addEventListener('click', function (e) {
    var t = e.target;
    var themeBtn = t.closest('[data-themebtn]');
    if (themeBtn) { applyTheme(themeBtn.getAttribute('data-themebtn')); return; }
    var toggle = t.closest('[data-theme-toggle]');
    if (toggle) { applyTheme(effectiveTheme() === 'dark' ? 'light' : 'dark'); return; }

    var mt = t.closest('[data-menu-toggle]');
    if (mt) {
      var menu = document.getElementById(mt.getAttribute('data-menu-toggle'));
      if (menu) {
        var show = menu.hidden; closeMenus(menu); menu.hidden = !show; mt.setAttribute('aria-expanded', String(show));
        if (show && e.detail === 0) { var first = menu.querySelector('.item, a[href], button, input'); if (first) { first.focus(); } } // opened by keyboard
      }
      return;
    }
    if (!t.closest('[data-menu]')) { closeMenus(); }

    if (t.closest('[data-help-open]')) { toggleHelp(true); return; }
    var open = t.closest('[data-dialog-open]');
    if (open) {
      var dlg = document.getElementById(open.getAttribute('data-dialog-open'));
      if (dlg && typeof dlg.showModal === 'function') {
        var action = open.getAttribute('data-form-action');
        var form = dlg.querySelector('form[data-dialog-form]');
        if (action && form) { form.setAttribute('action', action); }
        dlg.querySelectorAll('[data-slot]').forEach(function (s) {
          var v = open.getAttribute('data-fill-' + s.getAttribute('data-slot'));
          if (v !== null) { s.textContent = v; }
        });
        var prefill = open.getAttribute('data-prefill');
        if (prefill && form) { try { var vals = JSON.parse(prefill); Object.keys(vals).forEach(function (k) { var f = form.elements[k]; if (f) { f.value = vals[k]; } }); } catch (err) { /* ignore */ } }
        dlg.showModal();
      }
      return;
    }
    var close = t.closest('[data-dialog-close]');
    if (close) { var d = close.closest('dialog'); if (d) { d.close(); } return; }
    if (t.tagName === 'DIALOG' && t.open) { t.close(); return; } // click on the backdrop

    var copy = t.closest('[data-copy]');
    if (copy) {
      var src = copy.getAttribute('data-copy');
      var text = src.charAt(0) === '#' ? (document.querySelector(src) || {}).textContent : src;
      copyText(text || '', copy);
    }
  });

  function copyText(text, button) {
    function done() {
      var label = button.querySelector('[data-copy-label]');
      var old = label ? label.textContent : null;
      if (label) { label.textContent = 'Copied'; }
      button.setAttribute('data-copied', 'true');
      setTimeout(function () { if (label && old !== null) { label.textContent = old; } button.removeAttribute('data-copied'); }, 1600);
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text); done(); });
    } else { legacyCopy(text); done(); }
  }
  function legacyCopy(text) {
    var ta = document.createElement('textarea'); ta.value = text; ta.className = 'sr';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e) { /* no clipboard */ }
    document.body.removeChild(ta);
  }

  // Selects and checkboxes that save themselves (role pickers, column chooser), and confirmations.
  document.addEventListener('change', function (e) {
    var s = e.target.closest('[data-autosubmit]');
    if (s && s.form) { s.form.requestSubmit(); }
  });
  document.addEventListener('submit', function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute('data-confirm');
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  });

  // --- HTMX: send the CSRF token from the cookie on every request ---------------------------------
  document.addEventListener('htmx:configRequest', function (e) {
    var token = cookie('tl_csrf');
    if (token) { e.detail.headers['X-CSRF-Token'] = token; }
  });

  // --- keyboard shortcuts: / search, j/k move, enter open, ? help, g d dashboard, esc ---------------
  var cursor = -1, pendingG = false, gTimer = null;
  function rows() { return Array.prototype.slice.call(document.querySelectorAll('[data-row]')); }
  function moveCursor(delta) {
    var list = rows(); if (!list.length) { return; }
    list.forEach(function (r) { r.classList.remove('is-cursor'); });
    cursor = Math.max(0, Math.min(list.length - 1, (cursor < 0 ? (delta > 0 ? -1 : list.length) : cursor) + delta));
    var row = list[cursor]; row.classList.add('is-cursor'); row.scrollIntoView({ block: 'nearest' });
  }
  document.addEventListener('keydown', function (e) {
    var openMenu = document.querySelector('[data-menu]:not([hidden])');
    if (openMenu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      var items = Array.prototype.slice.call(openMenu.querySelectorAll('.item:not([aria-disabled=true]):not([disabled])'));
      if (items.length) {
        e.preventDefault();
        var at = items.indexOf(document.activeElement);
        var next = e.key === 'ArrowDown' ? (at + 1) % items.length : (at <= 0 ? items.length - 1 : at - 1);
        items[next].focus();
      }
      return;
    }
    if (e.key === 'Escape') {
      var toggler = openMenu && document.querySelector('[data-menu-toggle="' + openMenu.id + '"]');
      closeMenus(); toggleHelp(false);
      if (toggler) { toggler.focus(); } // give focus back to the button that opened the menu
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) { return; }
    var openDialog = document.querySelector('dialog[open]');
    if (e.key === '?') { e.preventDefault(); toggleHelp(); return; }
    if (openDialog) { return; }
    if (pendingG) {
      pendingG = false; clearTimeout(gTimer);
      if (e.key === 'd') { var dash = document.querySelector('[data-nav="dashboard"]'); if (dash) { location.href = dash.getAttribute('href'); } return; }
    }
    switch (e.key) {
      case '/': var s = document.querySelector('[data-search]'); if (s) { e.preventDefault(); s.focus(); s.select(); } break;
      case 'j': moveCursor(1); break;
      case 'k': moveCursor(-1); break;
      case 'Enter':
        var list = rows();
        if (cursor >= 0 && list[cursor]) {
          var link = list[cursor].getAttribute('data-href') ? list[cursor] : list[cursor].querySelector('a[href]');
          var href = link && (link.getAttribute('data-href') || link.getAttribute('href'));
          if (href) { location.href = href; }
        }
        break;
      case 'g': pendingG = true; gTimer = setTimeout(function () { pendingG = false; }, 900); break;
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    syncThemeControls();
    document.querySelectorAll('dialog[data-autoopen]').forEach(function (d) { if (typeof d.showModal === 'function' && !d.open) { d.showModal(); } }); // server asked for it (an error to fix, a secret to copy)
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { syncThemeControls(); });
    var focusMe = document.querySelector('[autofocus-field]'); if (focusMe) { focusMe.focus(); }
  });
})();
