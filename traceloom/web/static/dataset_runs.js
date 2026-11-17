/* New experiment dialog: version/label, model and variable-mapping choices follow the prompt and connection. */
(function () {
  'use strict';
  var form = document.getElementById('exp-form');
  if (!form) { return; }
  var spec = JSON.parse(form.getAttribute('data-spec') || '{}');
  var prompt = document.getElementById('xp'), ref = document.getElementById('xv');
  var conn = document.getElementById('xc'), model = document.getElementById('xm');
  var map = document.getElementById('xmap');
  var saved = {};
  try { saved = JSON.parse(map.getAttribute('data-values') || '{}'); } catch (e) { saved = {}; }

  function option(value, label, selected) {
    var o = document.createElement('option');
    o.value = value; o.textContent = label; o.selected = !!selected;
    return o;
  }
  function current() {
    return (spec.prompts || []).filter(function (p) { return p.name === prompt.value; })[0];
  }
  function fillRefs() {
    var p = current(), want = ref.getAttribute('data-selected') || '';
    ref.textContent = '';
    if (!p) { return; }
    for (var n = p.latest; n >= 1; n--) { ref.appendChild(option('v:' + n, 'v' + n, want === 'v:' + n)); }
    p.labels.forEach(function (l) { ref.appendChild(option('l:' + l, l, want === 'l:' + l)); });
    ref.removeAttribute('data-selected');
  }
  function fillMap() {
    var p = current();
    map.textContent = '';
    ((p && p.variables) || []).forEach(function (v) {
      var chip = document.createElement('span'); chip.className = 'chip var'; chip.textContent = '{{' + v + '}}';
      var arrow = document.createElement('span'); arrow.className = 'muted'; arrow.textContent = '←';
      var input = document.createElement('input');
      input.className = 'input'; input.name = 'map_' + v; input.value = saved[v] || 'input.' + v;
      input.setAttribute('aria-label', 'Item key for ' + v);
      map.appendChild(chip); map.appendChild(arrow); map.appendChild(input);
    });
    if (!map.children.length) { map.textContent = 'This prompt has no variables.'; map.classList.add('muted'); } else { map.classList.remove('muted'); }
  }
  function fillModels() {
    var c = (spec.connections || []).filter(function (x) { return x.id === conn.value; })[0];
    var want = model.getAttribute('data-selected') || '';
    model.textContent = '';
    ((c && c.models) || []).forEach(function (m) { model.appendChild(option(m, m, m === want)); });
    model.removeAttribute('data-selected');
  }
  prompt.addEventListener('change', function () { saved = {}; fillRefs(); fillMap(); });
  conn.addEventListener('change', fillModels);
  fillRefs(); fillMap(); fillModels();
})();
