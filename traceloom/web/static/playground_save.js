/* Save as prompt dialog. Any button with [data-save-as-prompt] opens it; the dialog fragment is
   fetched on first use. Needs window.traceloomPlayground.snapshot() from the playground page:
   { messages: [{role, content}], model, params, tools }. Text from the server or the user is only
   written with textContent. */
(function () {
  'use strict';

  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  function banner(box, text, href, linkText) {
    var body = box.querySelector('.banner > div');
    while (body.firstChild) { body.removeChild(body.firstChild); }
    body.appendChild(document.createTextNode(text));
    if (href) {
      body.appendChild(document.createTextNode(' '));
      var a = document.createElement('a');
      a.href = href; a.textContent = linkText;
      body.appendChild(a);
    }
    box.hidden = false;
  }

  function wire(dlg) {
    var form = dlg.querySelector('[data-save-form]');
    var errorBox = dlg.querySelector('[data-save-error]');
    var doneBox = dlg.querySelector('[data-save-done]');
    var fresh = dlg.querySelector('[data-save-new]');
    var existing = dlg.querySelector('[data-save-existing]');
    var nameInput = form.elements.name;
    var select = form.elements.existing;

    function mode() { return form.elements.mode.value; }
    function sync() {
      var isNew = mode() === 'new';
      fresh.hidden = !isNew; existing.hidden = isNew;
      nameInput.disabled = !isNew; select.disabled = isNew;
    }
    form.addEventListener('change', sync);
    sync();

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      errorBox.hidden = true; doneBox.hidden = true;
      var page = window.traceloomPlayground;
      var snap = page && page.snapshot ? page.snapshot() : null;
      var name = (mode() === 'new' ? nameInput.value : select.value).trim();
      var message = form.elements.commit_message.value.trim();
      if (!snap || !snap.messages || !snap.messages.length) { banner(errorBox, 'There are no messages to save.'); return; }
      if (!name) { banner(errorBox, 'Enter a prompt name.'); return; }
      if (!message) { banner(errorBox, 'A commit message is required.'); return; }
      var submit = form.querySelector('[data-save-submit]');
      submit.disabled = true;
      fetch(dlg.getAttribute('data-save-url'), {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': cookie('tl_csrf') },
        body: JSON.stringify({
          name: name, mode: mode(), commit_message: message,
          messages: snap.messages.map(function (m) { return { role: m.role, content: m.content || '' }; }),
          model: snap.model || null, params: snap.params || {}, tools: snap.tools || []
        })
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (data) { return { ok: r.ok, data: data }; });
      }).then(function (res) {
        submit.disabled = false;
        if (!res.ok) {
          var err = res.data && res.data.error && res.data.error.message;
          banner(errorBox, err || 'Could not save the prompt.');
          return;
        }
        banner(doneBox, 'Saved ' + res.data.name + ' as version ' + res.data.version + '.', res.data.url, 'View version');
        form.elements.commit_message.value = '';
      }).catch(function () {
        submit.disabled = false;
        banner(errorBox, 'Could not reach the server. Try again.');
      });
    });
  }

  function open() {
    var dlg = document.getElementById('save-prompt');
    if (dlg) { dlg.showModal(); return; }
    var base = document.body.getAttribute('data-playground-base') || window.location.pathname.replace(/\/playground.*$/, '/playground');
    fetch(base + '/save-dialog', { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) { throw new Error('status ' + r.status); } return r.text(); })
      .then(function (html) {
        var holder = document.createElement('div');
        holder.innerHTML = html; // markup from our own template (all values escaped server-side)
        var fresh = holder.querySelector('dialog');
        document.body.appendChild(fresh);
        wire(fresh);
        fresh.showModal();
      })
      .catch(function () { window.alert('Could not open the save dialog.'); });
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('[data-save-as-prompt]') : null;
    if (t) { e.preventDefault(); open(); }
  });
})();
