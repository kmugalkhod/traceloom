/* Evaluators: the rule editor (filter rows, target, sampling, mapping rows, preview), the model
   picker and the backfill estimate. Progressive: every field also submits without this script. */
(function () {
  'use strict';

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  // --- model picker: offer the chosen connection's models ---------------------------------------
  function bindModelPicker(root) {
    var conn = $('[data-conn]', root), model = $('[data-model]', root);
    if (!conn || !model) { return; }
    var list = document.getElementById(model.getAttribute('list'));
    conn.addEventListener('change', function () {
      var opt = conn.options[conn.selectedIndex], names = [];
      try { names = JSON.parse(opt.getAttribute('data-models') || '[]'); } catch (e) { names = []; }
      if (!list || !names.length) { return; }
      list.textContent = '';
      names.forEach(function (n) { var o = document.createElement('option'); o.value = n; list.appendChild(o); });
    });
  }

  // --- template editor: variables found, score type fields --------------------------------------
  function bindTemplateEditor(form) {
    var prompt = $('[data-prompt]', form), vars = $('[data-vars]', form);
    if (prompt && vars) {
      var render = function () {
        var seen = [], re = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g, m;
        while ((m = re.exec(prompt.value)) !== null) { if (seen.indexOf(m[1]) < 0) { seen.push(m[1]); } }
        vars.textContent = '';
        if (!seen.length) { var h = document.createElement('span'); h.className = 'hint'; h.textContent = 'none yet'; vars.appendChild(h); return; }
        seen.forEach(function (n) {
          var c = document.createElement('span'); c.className = 'chip var'; c.textContent = '{{' + n + '}}';
          vars.appendChild(c); vars.appendChild(document.createTextNode(' '));
        });
      };
      prompt.addEventListener('input', render);
    }
    function showType() {
      var checked = $('[data-score-type]:checked', form), kind = checked ? checked.value : '';
      $$('[data-for-type]', form).forEach(function (el) { el.hidden = el.getAttribute('data-for-type') !== kind; });
    }
    $$('[data-score-type]', form).forEach(function (r) { r.addEventListener('change', showType); });
    bindModelPicker(form);
  }

  // --- rule editor ------------------------------------------------------------------------------
  function bindRuleForm(form) {
    var rows = $('[data-filter-rows]', form), tmpl = $('[data-filter-template]', form);
    var add = $('[data-add-row]', form);
    if (add && tmpl && rows) {
      add.addEventListener('click', function () { rows.appendChild(tmpl.content.cloneNode(true)); schedule(); });
    }
    form.addEventListener('click', function (e) {
      var rm = e.target.closest('[data-remove-row]');
      if (rm) { var row = rm.closest('.frow'); if (row) { row.remove(); schedule(); } }
    });
    form.addEventListener('change', function (e) {
      var f = e.target;
      if (f.name === 'f_field') {
        var value = f.closest('.frow').querySelector('[name=f_value]');
        value.placeholder = f.value === 'metadata' ? 'key=value' : (f.value === 'tags' ? 'comma separated' : 'value');
      }
    });

    // Target cards show the matching fields.
    function showTarget() {
      var checked = $('[data-target]:checked', form), kind = checked ? checked.value : 'trace';
      $$('[data-for-target]', form).forEach(function (el) { el.hidden = el.getAttribute('data-for-target') !== kind; });
    }
    $$('[data-target]', form).forEach(function (r) { r.addEventListener('change', function () { showTarget(); schedule(); }); });

    // Sampling shows its value.
    var range = $('[data-sampling]', form), out = $('[data-sampling-out]', form);
    if (range && out) { range.addEventListener('input', function () { out.textContent = range.value + '%'; }); }

    // Mapping rows reveal the inputs their source needs.
    $$('[data-map-row]', form).forEach(function (row) {
      var sel = $('[data-source]', row);
      function sync() { $$('[data-show-for]', row).forEach(function (el) { el.hidden = el.getAttribute('data-show-for') !== sel.value; }); }
      sel.addEventListener('change', function () { sync(); schedule(); });
    });

    // Choosing another template or version reloads the editor so the mapping rows match it.
    function reload() {
      var url = form.getAttribute('data-new-url'), t = $('[data-template-select]', form);
      var v = $('[data-version-select]', form);
      if (!url || !t || form.action.split('/').pop() !== 'rules') { return; }
      var q = '?template_id=' + encodeURIComponent(t.value);
      window.location.href = url + q + (v && v.value ? '&template_version=' + encodeURIComponent(v.value) : '');
    }
    var ts = $('[data-template-select]', form);
    if (ts) { ts.addEventListener('change', reload); }
    bindModelPicker(form);

    // Preview: what the judge would receive for a recent matching trace (no model call).
    var box = $('[data-preview]', form), skip = 0, timer = null, ctl = null;
    function query() {
      var params = new URLSearchParams();
      new FormData(form).forEach(function (v, k) { if (k !== 'csrf_token') { params.append(k, v); } });
      params.set('skip', String(skip));
      return params.toString();
    }
    function load() {
      if (!box) { return; }
      var url = form.getAttribute('data-preview-url');
      if (ctl) { ctl.abort(); }
      ctl = typeof AbortController === 'function' ? new AbortController() : null;
      fetch(url + '?' + query(), { credentials: 'same-origin', headers: { 'X-Requested-With': 'fetch' }, signal: ctl ? ctl.signal : undefined })
        .then(function (r) { return r.text(); })
        .then(function (html) { box.innerHTML = html; })  // server-rendered and escaped
        .catch(function (err) { if (err && err.name === 'AbortError') { return; } box.textContent = 'The preview could not be loaded.'; });
    }
    function schedule() { skip = 0; clearTimeout(timer); timer = setTimeout(load, 400); }
    form.addEventListener('input', function (e) { if (e.target.name && e.target.name !== 'name' && e.target.name !== 'score_name') { schedule(); } });
    var refresh = $('[data-preview-refresh]', form);
    if (refresh) { refresh.addEventListener('click', function () { skip = 0; load(); }); }
    if (box) {
      box.addEventListener('click', function (e) {
        var next = e.target.closest('[data-preview-next]');
        if (next) { skip = parseInt(next.getAttribute('data-preview-next'), 10) || 0; load(); }
      });
      load();
    }
  }

  // --- backfill dialog: matches in the range against the limit and the cap ----------------------
  function bindBackfill(dlg) {
    var url = dlg.getAttribute('data-estimate-url'), note = $('[data-bf-note]', dlg);
    var from = $('[data-bf-from]', dlg), to = $('[data-bf-to]', dlg);
    if (!url || !note) { return; }
    var text = note.querySelector('div');
    function money(x) { return '$' + x.toFixed(x < 0.1 ? 4 : 2); }
    function update() {
      fetch(url + '?from=' + encodeURIComponent(from.value) + '&to=' + encodeURIComponent(to.value), { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var parts = [];
          if (d.matches !== null) {
            var judged = Math.min(Math.round(d.matches * d.sampling_pct / 100), d.limit);
            parts.push('About ' + d.matches.toLocaleString() + ' traces match' + (d.matches > d.limit ? ', the limit is ' + d.limit.toLocaleString() + '. The newest ' + d.limit.toLocaleString() + ' will be judged' : '') + '.');
            if (d.priced) { parts.push('Estimated spend about ' + money(judged * d.per_call) + ', within ' + money(d.left) + ' left under today’s cap.'); }
          }
          if (!d.priced) { parts.push('Unpriced model, the cost cap does not apply.'); }
          if (parts.length) { text.textContent = parts.join(' '); }
        })
        .catch(function () { /* keep the static note */ });
    }
    [from, to].forEach(function (el) { if (el) { el.addEventListener('change', update); } });
    new MutationObserver(function () { if (dlg.open) { update(); } }).observe(dlg, { attributes: true, attributeFilter: ['open'] });
  }

  document.addEventListener('DOMContentLoaded', function () {
    $$('[data-template-editor]').forEach(bindTemplateEditor);
    $$('[data-rule-form]').forEach(bindRuleForm);
    $$('[data-backfill]').forEach(bindBackfill);
  });
}());
