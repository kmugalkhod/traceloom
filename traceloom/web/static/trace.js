/* Trace detail page: view switching (tree / timeline / graph), selection and shortcuts. */
(function () {
  'use strict';
  var root = document.querySelector('[data-trace-detail]');
  if (!root) { return; }

  function tabs() { return Array.prototype.slice.call(root.querySelectorAll('[role=tab][data-view]')); }
  function show(view) {
    tabs().forEach(function (t) { t.setAttribute('aria-selected', String(t.getAttribute('data-view') === view)); });
    root.querySelectorAll('[data-body]').forEach(function (b) { b.hidden = b.getAttribute('data-body') !== view; });
  }
  function current() {
    var t = tabs().filter(function (x) { return x.getAttribute('aria-selected') === 'true'; })[0];
    return t ? t.getAttribute('data-view') : 'tree';
  }
  function select(id) {
    root.querySelectorAll('[data-obs]').forEach(function (el) {
      var on = el.getAttribute('data-obs') === id;
      if (el.matches('[role=treeitem],.tl-row')) { el.setAttribute('aria-selected', String(on)); }
      var g = el.querySelector('.gn');
      if (g) { g.classList.toggle('sel', on); }
    });
  }

  root.addEventListener('click', function (e) {
    var tab = e.target.closest('[role=tab][data-view]');
    if (tab) { show(tab.getAttribute('data-view')); return; }
    var row = e.target.closest('[role=treeitem],.tl-row');
    if (row && !e.target.closest('a')) {  // a click anywhere on the row follows its link
      var link = row.querySelector('[data-obs-link]');
      if (link) { link.click(); }
      return;
    }
    var link2 = e.target.closest('[data-obs-link]');
    if (link2) { select(link2.getAttribute('data-obs') || (link2.closest('[data-obs]') || {}).getAttribute && link2.closest('[data-obs]').getAttribute('data-obs')); }
  });

  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    var tag = e.target && e.target.tagName;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable)) { return; }
    if (document.querySelector('dialog[open]')) { return; }
    if (e.key === 't') {
      var view = current();
      show(view === 'tree' ? 'timeline' : 'tree');
    } else if (e.key === 'e') {
      var jump = root.querySelector('[data-jump]');
      if (jump) { jump.click(); }
    } else if (e.key === 'j' || e.key === 'k') {
      var items = Array.prototype.slice.call(root.querySelectorAll('[data-body]:not([hidden]) [data-obs]'));
      if (!items.length) { return; }
      var i = items.findIndex(function (el) { return el.getAttribute('aria-selected') === 'true'; });
      i = Math.max(0, Math.min(items.length - 1, i + (e.key === 'j' ? 1 : -1)));
      var l = items[i].querySelector('[data-obs-link]') || items[i];
      l.click();
      items[i].scrollIntoView({ block: 'nearest' });
    }
  });

  // After HTMX swaps the IO pane, keep the selected row in sync with the URL it pushed.
  document.body.addEventListener('htmx:pushedIntoHistory', function (e) {
    var m = /[?&]obs=([^&]+)/.exec(e.detail.path || '');
    select(m ? decodeURIComponent(m[1]) : '');
  });
})();
