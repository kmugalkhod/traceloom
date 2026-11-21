/* Dataset item editors: keep each JSON editor's status ("valid JSON" or the parse error) live
   while the user types. Progressive enhancement; the server validates on save either way. */
(function () {
  'use strict';
  function check(box) {
    var area = box.querySelector('textarea');
    var status = box.querySelector('[data-json-status]');
    if (!area || area.readOnly) { return; }
    var text = area.value.trim();
    var ok = true, msg = '';
    if (text) {
      try { JSON.parse(text); msg = 'valid JSON'; } catch (e) { ok = false; msg = 'Not valid JSON'; }
    } else if (!box.hasAttribute('data-optional')) {
      ok = false; msg = 'Required';
    }
    box.classList.toggle('bad', !ok);
    if (ok) { area.removeAttribute('aria-invalid'); } else { area.setAttribute('aria-invalid', 'true'); }
    if (status) { status.textContent = msg; }
  }
  document.addEventListener('input', function (e) {
    var box = e.target.closest && e.target.closest('[data-json]');
    if (box) { check(box); }
  });
})();
