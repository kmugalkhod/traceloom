/* Save as prompt and Load prompt dialogs. They open on the page's 'pg:action' events
   ('save-as-prompt', 'load-prompt'); each fragment is fetched once into #pg-dialogs. The editor
   state comes from window.Playground.getState(). Text from the server or the user is only written
   with textContent. */
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
      var snap = window.Playground ? window.Playground.getState() : null;
      var name = (mode() === 'new' ? nameInput.value : select.value).trim();
      var message = form.elements.commit_message.value.trim();
      // Prompts hold system, user and assistant messages; tool results and tool calls are not saved.
      var kept = snap && snap.messages ? snap.messages.filter(function (m) { return m.role !== 'tool'; }) : [];
      var dropped = snap && snap.messages ? snap.messages.length - kept.length : 0;
      if (!kept.length) { banner(errorBox, 'There are no messages to save.'); return; }
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
          messages: kept.map(function (m) { return { role: m.role, content: m.content || '' }; }),
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
        var note = dropped ? ' ' + dropped + ' tool message' + (dropped === 1 ? ' was' : 's were') + ' not saved.' : '';
        banner(doneBox, 'Saved ' + res.data.name + ' as version ' + res.data.version + '.' + note, res.data.url, 'View version');
        form.elements.commit_message.value = '';
      }).catch(function () {
        submit.disabled = false;
        banner(errorBox, 'Could not reach the server. Try again.');
      });
    });
  }

  function wireLoad(dlg) {
    var form = dlg.querySelector('[data-load-form]');
    var select = form.elements.prompt, version = form.elements.version;
    if (select) {
      select.addEventListener('change', function () {
        var opt = select.options[select.selectedIndex];
        version.value = opt.getAttribute('data-latest');
      });
    }
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var n = parseInt(version.value, 10);
      if (!select || !n || n < 1) { version.focus(); return; }
      var q = '?prompt=' + encodeURIComponent(select.value) + '&version=' + n;
      window.location.assign(dlg.getAttribute('data-playground-url') + q);
    });
  }

  // Open a dialog the server renders: reuse it if it is already on the page, else fetch it once.
  function openDialog(id, path, wire) {
    var dlg = document.getElementById(id);
    if (dlg) { dlg.showModal(); return; }
    var base = '/project/' + window.Playground.config.projectId + '/playground';
    fetch(base + path, { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) { throw new Error('status ' + r.status); } return r.text(); })
      .then(function (html) {
        var holder = document.createElement('div');
        holder.innerHTML = html; // markup from our own template (all values escaped server-side)
        var fresh = holder.querySelector('dialog');
        (document.getElementById('pg-dialogs') || document.body).appendChild(fresh);
        wire(fresh);
        fresh.showModal();
      })
      .catch(function () { window.alert('Could not open the dialog.'); });
  }

  function open() { openDialog('save-prompt', '/save-dialog', wire); }

  document.addEventListener('pg:action', function (e) {
    var action = e.detail && e.detail.action;
    if (action === 'save-as-prompt') { open(); }
    else if (action === 'load-prompt') { openDialog('load-prompt', '/load-dialog', wireLoad); }
  });

  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('[data-save-as-prompt]') : null;
    if (t) { e.preventDefault(); open(); }
  });
})();
