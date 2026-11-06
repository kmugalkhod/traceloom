/* Prompt editor: highlights {{variables}} and @@@prompt:...@@@ includes, lists variables, edits chat
   messages and checks config JSON. User text is only ever written with textContent/createTextNode;
   nothing here uses innerHTML. No inline handlers (CSP). */
(function () {
  'use strict';

  var TOKEN = /\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}|@@@prompt:[^\n@]*@@@/g;
  var VAR = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;

  function highlight(pre, text) {
    while (pre.firstChild) { pre.removeChild(pre.firstChild); }
    var last = 0, m;
    TOKEN.lastIndex = 0;
    while ((m = TOKEN.exec(text)) !== null) {
      if (m.index > last) { pre.appendChild(document.createTextNode(text.slice(last, m.index))); }
      var span = document.createElement('span');
      span.className = m[0].charAt(0) === '{' ? 'var' : 'inc';
      span.textContent = m[0];
      pre.appendChild(span);
      last = m.index + m[0].length;
    }
    // A trailing newline would collapse; the extra space keeps the mirror as tall as the textarea.
    pre.appendChild(document.createTextNode(text.slice(last) + '\n '));
  }

  function variablesIn(text) {
    var found = [], m;
    VAR.lastIndex = 0;
    while ((m = VAR.exec(text)) !== null) { if (found.indexOf(m[1]) < 0) { found.push(m[1]); } }
    return found;
  }

  function allText(form) {
    var type = (form.querySelector('input[name=type]:checked') || form.querySelector('input[name=type]') || {}).value;
    var out = [];
    if (type === 'chat') {
      form.querySelectorAll('[data-msg]').forEach(function (card) {
        if (card.querySelector('select[name=role]').value !== 'placeholder') {
          out.push(card.querySelector('textarea').value);
        }
      });
    } else {
      var t = form.querySelector('textarea[name=text]');
      if (t) { out.push(t.value); }
    }
    return out.join('\n');
  }

  function refreshVariables(form) {
    var list = form.querySelector('[data-vars]');
    if (!list) { return; }
    var names = variablesIn(allText(form));
    while (list.firstChild) { list.removeChild(list.firstChild); }
    names.forEach(function (name) {
      var li = document.createElement('li');
      var span = document.createElement('span');
      span.className = 'var';
      span.textContent = '{{' + name + '}}';
      li.appendChild(span);
      list.appendChild(li);
    });
    var empty = form.querySelector('[data-vars-empty]');
    if (empty) { empty.hidden = names.length > 0; }
    var total = form.querySelector('[data-var-total]');
    if (total) { total.textContent = String(names.length); }
    var count = form.querySelector('[data-var-count]');
    if (count) { count.textContent = names.length + (names.length === 1 ? ' variable' : ' variables'); }
  }

  function setupEditor(wrap, form) {
    if (wrap.hasAttribute('data-ready')) { return; }
    var ta = wrap.querySelector('textarea'), pre = wrap.querySelector('.ed-hl');
    if (!ta || !pre) { return; }
    function sync() { highlight(pre, ta.value); refreshVariables(form); }
    ta.addEventListener('input', sync);
    wrap.setAttribute('data-ready', '');
    sync();
  }

  function setupForm(form) {
    form.querySelectorAll('[data-ed]').forEach(function (wrap) { setupEditor(wrap, form); });

    // Text / chat switch.
    function showType() {
      var checked = form.querySelector('input[name=type]:checked');
      var type = checked ? checked.value : null;
      form.querySelectorAll('[data-type-section]').forEach(function (section) {
        if (type) { section.hidden = section.getAttribute('data-type-section') !== type; }
      });
      refreshVariables(form);
    }
    form.querySelectorAll('[data-type-toggle]').forEach(function (r) { r.addEventListener('change', showType); });

    // Chat messages: add, remove, move; a placeholder's content is a name, not text.
    var box = form.querySelector('[data-msgs]');
    if (box) {
      form.addEventListener('click', function (e) {
        var btn = e.target.closest('button');
        if (!btn) { return; }
        var card = btn.closest('[data-msg]');
        if (btn.hasAttribute('data-msg-add')) {
          var tpl = document.getElementById('msg-tpl');
          var clone = tpl.content.firstElementChild.cloneNode(true);
          box.appendChild(clone);
          setupEditor(clone.querySelector('[data-ed]'), form);
          clone.querySelector('textarea').focus();
        } else if (card && btn.hasAttribute('data-msg-remove')) {
          if (box.querySelectorAll('[data-msg]').length > 1) { card.remove(); refreshVariables(form); }
        } else if (card && btn.hasAttribute('data-msg-up') && card.previousElementSibling) {
          box.insertBefore(card, card.previousElementSibling);
        } else if (card && btn.hasAttribute('data-msg-down') && card.nextElementSibling) {
          box.insertBefore(card.nextElementSibling, card);
        }
      });
      form.addEventListener('change', function (e) {
        var sel = e.target.closest('select[name=role]');
        if (!sel) { return; }
        var card = sel.closest('[data-msg]');
        card.classList.remove('system', 'user', 'assistant', 'placeholder', 'ph');
        card.classList.add(sel.value);
        if (sel.value === 'placeholder') { card.classList.add('ph'); }
        refreshVariables(form);
      });
    }

    // Config JSON: say so early; the server checks again.
    var json = form.querySelector('[data-json-check]');
    if (json) {
      var message = form.querySelector('[data-json-error]');
      var holder = message ? message.parentNode : null;
      json.addEventListener('input', function () {
        var text = json.value.trim(), problem = '';
        if (text) {
          try {
            var parsed = JSON.parse(text);
            if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) { problem = 'Config must be a JSON object.'; }
          } catch (err) { problem = 'Config must be valid JSON.'; }
        }
        if (message) { message.textContent = problem; }
        if (holder) { holder.hidden = !problem; }
        if (problem) { json.setAttribute('aria-invalid', 'true'); } else { json.removeAttribute('aria-invalid'); }
      });
    }
    showType();
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-prompt-form]').forEach(setupForm);
  });
})();
