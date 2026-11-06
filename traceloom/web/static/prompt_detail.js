/* Prompt detail: dismiss the "label moved" toast (it also fades on its own). No inline handlers. */
(function () {
  'use strict';
  document.addEventListener('click', function (e) {
    var close = e.target.closest('[data-toast-close]');
    if (close) { var toast = close.closest('.toast'); if (toast) { toast.hidden = true; } }
  });
  document.addEventListener('DOMContentLoaded', function () {
    var toast = document.getElementById('move-toast');
    if (toast) { setTimeout(function () { toast.hidden = true; }, 10000); }
  });
})();
