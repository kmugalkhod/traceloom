// Filter tray, Score section: add and remove condition rows, and hide the value box for the
// operators that take none (exists, missing). The server still validates every condition.
(function () {
  'use strict';
  var NO_VALUE = { exists: true, missing: true };

  function rows() { return document.querySelector('[data-sf-rows]'); }

  function sync(row) {
    var op = row.querySelector('[data-sf-op]');
    if (op && NO_VALUE[op.value]) { row.setAttribute('data-op-none', ''); } else { row.removeAttribute('data-op-none'); }
  }

  document.addEventListener('click', function (e) {
    var add = e.target.closest('[data-sf-add]');
    if (add) {
      var tpl = document.getElementById('sf-template'), box = rows();
      if (!tpl || !box) { return; }
      box.appendChild(tpl.content.cloneNode(true));
      var last = box.querySelectorAll('[data-sf-row]');
      var row = last[last.length - 1];
      if (row) { var first = row.querySelector('select'); if (first) { first.focus(); } }
      return;
    }
    var rm = e.target.closest('[data-sf-remove]');
    if (rm) {
      var r = rm.closest('[data-sf-row]');
      if (!r) { return; }
      var err = r.nextElementSibling;
      if (err && err.classList.contains('field-error')) { err.remove(); }
      r.remove();
      var addBtn = document.querySelector('[data-sf-add]');
      if (addBtn) { addBtn.focus(); }
    }
  });

  document.addEventListener('change', function (e) {
    if (e.target.matches('[data-sf-op]')) { sync(e.target.closest('[data-sf-row]')); }
  });
})();
