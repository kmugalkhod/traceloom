/* Add to annotation queue: opens the dialog as an HTMX fragment from a selection bar, closes
   the result toast, and drives the session-list selection bar. No dependencies beyond HTMX;
   the links also work as plain pages without JS. */
(function () {
  'use strict';

  function open(url) {
    if (window.htmx && document.getElementById('queue-dialog-slot')) {
      window.htmx.ajax('GET', url, { target: '#queue-dialog-slot', swap: 'innerHTML' });
    } else { window.location.href = url; }
  }
  function checked(attr) {
    return Array.prototype.map.call(document.querySelectorAll('[' + attr + ']:checked'), function (b) { return b.getAttribute(attr); });
  }
  function pairs(name, values) {
    return values.map(function (v) { return name + '=' + encodeURIComponent(v); });
  }

  document.addEventListener('htmx:afterSwap', function (e) {
    var root = e.detail.target;
    root.querySelectorAll('dialog[data-autoopen]').forEach(function (d) {
      if (typeof d.showModal === 'function' && !d.open) { d.showModal(); }
    });
    var toast = root.querySelector('[data-queue-toast]');
    if (toast) { setTimeout(function () { toast.hidden = true; }, 12000); }
  });

  document.addEventListener('click', function (e) {
    var close = e.target.closest('[data-toast-close]');
    if (close) { var t = close.closest('.toast'); if (t) { t.hidden = true; } return; }

    var go = e.target.closest('[data-bulk-queue]');
    if (go) {
      var parts = pairs('trace_id', checked('data-bulk-box')).concat(pairs('session_id', checked('data-session-box')));
      if (!parts.length) { return; }
      e.preventDefault();
      open(go.getAttribute('data-bulk-queue') + '?' + parts.join('&'));
      return;
    }

    var bar = document.querySelector('[data-session-bar]');
    if (!bar) { return; }
    var box = e.target.closest('[data-session-box]');
    if (box) {
      box.closest('tr').classList.toggle('rowsel', box.checked);
    } else if (e.target.closest('[data-session-clear]')) {
      document.querySelectorAll('[data-session-box]').forEach(function (b) { b.checked = false; b.closest('tr').classList.remove('rowsel'); });
    } else {
      return;
    }
    var n = checked('data-session-box').length;
    bar.hidden = n === 0;
    bar.querySelector('[data-session-count]').textContent = n + ' selected';
  });
})();
