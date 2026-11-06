/* LLM connections dialog: show only the chosen provider's fields, and turn the custom-model input
   into chips. User text is only written with textContent/createTextNode; no inline handlers. */
(function () {
  'use strict';

  function showProvider(form) {
    var select = form.querySelector('[data-provider-select]');
    if (!select) { return; }
    form.querySelectorAll('[data-provider-fields]').forEach(function (box) {
      var on = box.getAttribute('data-provider-fields') === select.value;
      box.hidden = !on;
      box.querySelectorAll('input, select, textarea').forEach(function (el) { el.disabled = !on; }); // hidden fields are not submitted
    });
  }

  function chips(input) {
    if (input.getAttribute('data-chips-ready')) { return; }
    input.setAttribute('data-chips-ready', '1');
    var wrap = document.createElement('div');
    wrap.className = 'chips input';
    var list = document.createElement('span');
    list.className = 'chip-list';
    var typed = document.createElement('input');
    typed.type = 'text'; typed.className = 'chip-in mono'; typed.autocomplete = 'off';
    typed.setAttribute('aria-label', input.id ? 'Add a model name' : 'Add');
    typed.placeholder = input.getAttribute('placeholder') || '';
    wrap.appendChild(list); wrap.appendChild(typed);
    input.parentNode.insertBefore(wrap, input);
    input.hidden = true;

    function names() { return input.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean); }
    function save(arr) { input.value = arr.join(', '); render(); }
    function render() {
      while (list.firstChild) { list.removeChild(list.firstChild); }
      names().forEach(function (name) {
        var chip = document.createElement('span');
        chip.className = 'chip';
        chip.appendChild(document.createTextNode(name));
        var x = document.createElement('button');
        x.type = 'button'; x.setAttribute('aria-label', 'Remove ' + name); x.textContent = '×';
        x.addEventListener('click', function () { save(names().filter(function (n) { return n !== name; })); typed.focus(); });
        chip.appendChild(x);
        list.appendChild(chip);
      });
      typed.placeholder = names().length ? '' : (input.getAttribute('placeholder') || '');
    }
    function commit() {
      var add = typed.value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      if (add.length) { var cur = names(); add.forEach(function (n) { if (cur.indexOf(n) < 0) { cur.push(n); } }); save(cur); }
      typed.value = '';
    }
    typed.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); commit(); }
      else if (e.key === 'Backspace' && !typed.value && names().length) { save(names().slice(0, -1)); }
    });
    typed.addEventListener('blur', commit);
    typed.addEventListener('paste', function () { setTimeout(commit, 0); });
    wrap.addEventListener('click', function () { typed.focus(); });
    render();
  }

  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-provider-form]').forEach(function (form) {
      showProvider(form);
      var select = form.querySelector('[data-provider-select]');
      if (select) { select.addEventListener('change', function () { showProvider(form); }); }
    });
    document.querySelectorAll('input[data-chips]').forEach(chips);
  });
})();
