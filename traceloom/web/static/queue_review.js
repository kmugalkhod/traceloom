/* Annotation queue review: keyboard shortcuts, score buttons and lock renewal.
   Every action is a normal form post, so the screen also works without this script. */
(function () {
  'use strict';

  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  var form = document.querySelector('[data-review]');
  if (!form) { return; }
  var mine = form.getAttribute('data-mine') === 'true';
  var started = Date.now();
  var duration = form.querySelector('[data-duration]');

  function submit(act) {
    var button = form.querySelector('[data-act="' + act + '"]');
    if (!button) { return; }
    if (duration) { duration.value = String(Date.now() - started); }
    if (typeof form.requestSubmit === 'function') { form.requestSubmit(button); } else { button.click(); }
  }

  // Pressed-state buttons for boolean and categorical scores.
  function choose(group, value) {
    var hidden = group.parentNode.querySelector('[data-value]');
    if (!hidden) { return; }
    hidden.value = value;
    Array.prototype.forEach.call(group.querySelectorAll('button[data-v]'), function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-v') === value ? 'true' : 'false');
    });
  }
  form.addEventListener('click', function (e) {
    var b = e.target.closest('button[data-v]');
    if (!b || !mine) { return; }
    choose(b.parentNode, b.getAttribute('data-v'));
  });

  function inField(t) {
    return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable);
  }
  function groupFor(t, selector) {
    var own = t && t.closest ? t.closest('[data-score]') : null;
    var scope = own || form;
    return scope.querySelector(selector);
  }

  document.addEventListener('keydown', function (e) {
    var openDialog = document.querySelector('dialog[open]');
    if (e.key === '?' && !inField(e.target) && !e.metaKey && !e.ctrlKey) {
      var help = document.getElementById('review-help');
      if (help && typeof help.showModal === 'function' && !help.open) { help.showModal(); e.preventDefault(); }
      return;
    }
    if (openDialog || !mine) { return; }
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); submit('complete'); return; }
    if (e.metaKey || e.ctrlKey || e.altKey || inField(e.target)) { return; }
    var key = e.key;
    if (key === 's') { e.preventDefault(); submit('skip'); }
    else if (key === 'n' || key === 'ArrowRight') { e.preventDefault(); submit('next'); }
    else if (/^[1-9]$/.test(key)) {
      var cats = groupFor(document.activeElement, '[data-cats]');
      var btn = cats && cats.querySelectorAll('button[data-v]')[parseInt(key, 10) - 1];
      if (btn) { e.preventDefault(); choose(cats, btn.getAttribute('data-v')); }
    } else if (key === 't' || key === 'f') {
      var bool = groupFor(document.activeElement, '[data-bool]');
      if (bool) { e.preventDefault(); choose(bool, key === 't' ? 'true' : 'false'); }
    }
  });

  // Keep the lock while the page is open.
  var seconds = parseInt(form.getAttribute('data-renew-seconds'), 10) || 180;
  var label = document.querySelector('[data-lock-label]');
  if (mine) {
    setInterval(function () {
      fetch(form.getAttribute('data-base') + '/renew', {
        method: 'POST', credentials: 'same-origin', headers: { 'X-CSRF-Token': cookie('tl_csrf'), 'Accept': 'application/json' }
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (!label) { return; }
        if (d.renewed) { label.lastChild.textContent = 'Yours until ' + d.until + ' UTC'; }
        else { label.lastChild.textContent = 'Lock lost: reload to continue'; }
      }).catch(function () { /* offline: the next tick tries again */ });
    }, seconds * 1000);
  }
}());
