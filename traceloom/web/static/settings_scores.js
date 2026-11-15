/* Score config dialog: "Add category" reveals the next spare row; remove clears a row. */
(function () {
  'use strict';
  document.addEventListener('click', function (e) {
    var add = e.target.closest('[data-add-category]');
    if (add) {
      var rows = add.closest('.cfg-cat').querySelectorAll('.cat-row[hidden]');
      if (rows.length) { rows[0].hidden = false; rows[0].querySelector('input').focus(); }
      if (rows.length <= 1) { add.hidden = true; }
      return;
    }
    var rm = e.target.closest('[data-remove-category]');
    if (rm) {
      var row = rm.closest('.cat-row');
      row.querySelectorAll('input').forEach(function (i) { i.value = ''; });
      var visible = row.parentNode.querySelectorAll('.cat-row:not([hidden])');
      if (visible.length > 2) { row.hidden = true; var more = row.closest('.cfg-cat').querySelector('[data-add-category]'); if (more) { more.hidden = false; } }
    }
  });
})();
