/* Playground compare: up to 4 variant columns, "Run all" streams every column on its own.
   Needs window.traceloomPlayground.snapshot() from the playground page (shared messages, variables,
   params, tools). Each column has its own AbortController and its own usage; nothing is shared
   between runs. Everything from the server or the model is written with textContent. */
(function () {
  'use strict';

  var MAX_DEFAULT = 4;
  var panel = null, data = [], seq = 0;
  var controllers = new WeakMap(); // variant element -> AbortController

  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function $(root, sel) { return root.querySelector(sel); }
  function all(root, sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function max() { return parseInt(panel.getAttribute('data-max'), 10) || MAX_DEFAULT; }
  function variants() { return all(panel, '[data-variant]'); }

  // --- open / close ------------------------------------------------------------------------------

  function setOn(on) {
    var mount = document.getElementById('compare-mount');
    if (mount) { mount.hidden = !on; }
    document.querySelectorAll('[data-compare-toggle]').forEach(function (b) { b.setAttribute('aria-pressed', on ? 'true' : 'false'); });
    document.dispatchEvent(new CustomEvent('playground:compare', { detail: { on: on } }));
  }

  function toggle() {
    var mount = document.getElementById('compare-mount');
    if (!mount) { return; }
    if (panel) { setOn(mount.hidden); return; }
    fetch(mount.getAttribute('data-compare-url'), { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
      .then(function (r) { if (!r.ok) { throw new Error('status ' + r.status); } return r.text(); })
      .then(function (html) {
        mount.innerHTML = html; // our own template; every value in it is escaped server-side
        panel = document.getElementById('compare-panel');
        try { data = JSON.parse(document.getElementById('compare-data').textContent); } catch (e) { data = []; }
        addVariant(); addVariant();
        setOn(true);
      })
      .catch(function () { window.alert('Could not open compare.'); });
  }

  // --- variants ----------------------------------------------------------------------------------

  function modelsFor(connectionId) {
    for (var i = 0; i < data.length; i++) { if (data[i].id === connectionId) { return data[i].models; } }
    return [];
  }

  function refreshModels(v) {
    var list = $(v, 'datalist');
    clear(list);
    modelsFor($(v, '[data-v-connection]').value).forEach(function (name) {
      var o = document.createElement('option'); o.value = name; list.appendChild(o);
    });
  }

  function addMessage(v, role, content) {
    var row = document.createElement('div');
    row.className = 'v-msg';
    var sel = document.createElement('select');
    sel.className = 'select'; sel.setAttribute('data-m-role', ''); sel.setAttribute('aria-label', 'Role');
    ['system', 'user', 'assistant'].forEach(function (r) {
      var o = document.createElement('option'); o.value = r; o.textContent = r; sel.appendChild(o);
    });
    sel.value = role || 'user';
    var ta = document.createElement('textarea');
    ta.className = 'input'; ta.rows = 2; ta.setAttribute('data-m-content', ''); ta.setAttribute('aria-label', 'Message');
    ta.value = content || '';
    var rm = document.createElement('button');
    rm.type = 'button'; rm.className = 'btn btn-icon btn-ghost btn-sm'; rm.setAttribute('data-m-remove', ''); rm.setAttribute('aria-label', 'Remove message'); rm.textContent = '×';
    row.appendChild(sel); row.appendChild(ta); row.appendChild(rm);
    $(v, '[data-v-messages]').appendChild(row);
  }

  function renumber() {
    var list = variants();
    list.forEach(function (v, i) {
      $(v, '[data-variant-title]').textContent = 'Variant ' + (i + 1);
      $(v, '[data-variant-remove]').disabled = list.length === 1;
    });
    $(panel, '[data-grid]').setAttribute('data-count', String(list.length));
    var add = $(panel, '[data-add-variant]');
    add.disabled = list.length >= max();
    add.title = add.disabled ? 'You can compare up to ' + max() + ' variants.' : '';
  }

  function addVariant() {
    if (variants().length >= max()) { return null; }
    var tpl = document.getElementById('variant-tpl');
    var v = tpl.content.firstElementChild.cloneNode(true);
    seq += 1;
    var listId = 'cmp-models-' + seq;
    var model = $(v, '[data-v-model]');
    model.setAttribute('list', listId);
    var dl = document.createElement('datalist'); dl.id = listId; v.appendChild(dl);
    var select = $(v, '[data-v-connection]');
    var blank = document.createElement('option'); blank.value = ''; blank.textContent = 'Same as the editor'; select.appendChild(blank);
    data.forEach(function (c) {
      var o = document.createElement('option'); o.value = c.id; o.textContent = c.name + ' (' + c.provider + ')'; select.appendChild(o);
    });
    $(panel, '[data-grid]').appendChild(v);
    refreshModels(v);
    renumber();
    return v;
  }

  function removeVariant(v) {
    stop(v);
    v.remove();
    renumber();
  }

  // --- running -----------------------------------------------------------------------------------

  function stop(v) {
    var c = controllers.get(v);
    if (c) { c.abort(); controllers.delete(v); }
    $(v, '[data-variant-stop]').hidden = true;
  }

  function showError(v, text) {
    var box = $(v, '[data-v-error]');
    $(box, 'div').textContent = text;
    box.hidden = false;
  }

  function number(n) { return typeof n === 'number' ? n.toLocaleString() : '–'; }
  function seconds(ms) { return typeof ms === 'number' ? (ms / 1000).toFixed(2) + ' s' : '–'; }
  function money(c) { return typeof c === 'number' ? '$' + c.toFixed(c < 0.01 ? 6 : 4) : '–'; }

  function showUsage(v, u) {
    $(v, '[data-u-input]').textContent = number(u.input_tokens);
    $(v, '[data-u-output]').textContent = number(u.output_tokens);
    $(v, '[data-u-cost]').textContent = money(u.cost);
    $(v, '[data-u-latency]').textContent = seconds(u.latency_ms);
    $(v, '[data-u-ttft]').textContent = seconds(u.ttft_ms);
    $(v, '[data-v-usage]').hidden = false;
  }

  function handle(v, textNode, name, d) {
    if (name === 'delta') { textNode.appendData(d.text); }
    else if (name === 'tool_call') {
      var li = document.createElement('li');
      li.textContent = d.name + '(' + JSON.stringify(d.arguments) + ')';
      $(v, '[data-v-calls]').appendChild(li);
    } else if (name === 'usage') { showUsage(v, d); }
    else if (name === 'error') { showError(v, d.message); }
  }

  function readStream(v, response, textNode) {
    var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = '';
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) { return; }
        buffer += decoder.decode(r.value, { stream: true });
        var parts = buffer.split('\n\n'); buffer = parts.pop();
        parts.forEach(function (block) {
          var name = '', payload = '';
          block.split('\n').forEach(function (line) {
            if (line.indexOf('event:') === 0) { name = line.slice(6).trim(); }
            else if (line.indexOf('data:') === 0) { payload += line.slice(5).trim(); }
          });
          if (!name) { return; }
          try { handle(v, textNode, name, JSON.parse(payload)); } catch (e) { /* ignore a malformed event */ }
        });
        return pump();
      });
    }
    return pump();
  }

  function ownMessages(v) {
    return all(v, '.v-msg').map(function (row) {
      return { role: $(row, '[data-m-role]').value, content: $(row, '[data-m-content]').value };
    }).filter(function (m) { return m.content.trim() !== ''; });
  }

  function requestFor(v, shared) {
    var params = {};
    Object.keys(shared.params || {}).forEach(function (k) { params[k] = shared.params[k]; });
    var t = $(v, '[data-v-temperature]').value, mt = $(v, '[data-v-max-tokens]').value;
    if (t !== '') { params.temperature = parseFloat(t); }
    if (mt !== '') { params.max_tokens = parseInt(mt, 10); }
    return {
      connection_id: $(v, '[data-v-connection]').value || shared.connection_id,
      model: $(v, '[data-v-model]').value.trim() || shared.model,
      messages: $(v, '[data-v-follow]').checked ? shared.messages : ownMessages(v),
      variables: shared.variables || {},
      params: params,
      tools: shared.tools || [],
      response_schema: shared.response_schema || null
    };
  }

  function run(v, shared) {
    stop(v);
    var textEl = $(v, '[data-v-text]');
    clear(textEl); clear($(v, '[data-v-calls]'));
    $(v, '[data-v-error]').hidden = true; $(v, '[data-v-usage]').hidden = true;
    var textNode = document.createTextNode(''); textEl.appendChild(textNode);
    var body = requestFor(v, shared);
    if (!body.connection_id || !body.model || !body.messages.length) {
      showError(v, 'Pick a connection and a model, and give this variant at least one message.');
      return Promise.resolve();
    }
    var controller = new AbortController();
    controllers.set(v, controller);
    $(v, '[data-variant-stop]').hidden = false;
    return fetch(panel.getAttribute('data-run-url'), {
      method: 'POST', credentials: 'same-origin', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': cookie('tl_csrf') },
      body: JSON.stringify(body)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showError(v, (d && d.error && d.error.message) || 'The run failed (' + r.status + ').');
        });
      }
      return readStream(v, r, textNode);
    }).catch(function (e) {
      if (e && e.name === 'AbortError') { return; }
      showError(v, 'The connection to the server was lost.');
    }).then(function () {
      if (controllers.get(v) === controller) { controllers.delete(v); $(v, '[data-variant-stop]').hidden = true; }
    });
  }

  function runAll() {
    var page = window.traceloomPlayground;
    var shared = page && page.snapshot ? page.snapshot() : null;
    if (!shared) { return; }
    // every column starts at once; each has its own request, controller and usage
    variants().forEach(function (v) { run(v, shared); });
  }

  // --- wiring ------------------------------------------------------------------------------------

  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target : null;
    if (!t) { return; }
    if (t.closest('[data-compare-toggle]')) { e.preventDefault(); toggle(); return; }
    if (!panel || !panel.contains(t)) { return; }
    var v = t.closest('[data-variant]');
    if (t.closest('[data-add-variant]')) { addVariant(); }
    else if (t.closest('[data-run-all]')) { runAll(); }
    else if (v && t.closest('[data-variant-remove]')) { removeVariant(v); }
    else if (v && t.closest('[data-variant-stop]')) { stop(v); }
    else if (v && t.closest('[data-v-add-message]')) { addMessage(v, 'user', ''); }
    else if (v && t.closest('[data-m-remove]')) { t.closest('.v-msg').remove(); }
  });

  document.addEventListener('change', function (e) {
    if (!panel || !panel.contains(e.target)) { return; }
    var v = e.target.closest('[data-variant]');
    if (!v) { return; }
    if (e.target.matches('[data-v-connection]')) { refreshModels(v); }
    if (e.target.matches('[data-v-follow]')) {
      var own = $(v, '[data-v-own]');
      own.hidden = e.target.checked;
      if (!e.target.checked && !all(v, '.v-msg').length) {
        var shared = window.traceloomPlayground && window.traceloomPlayground.snapshot ? window.traceloomPlayground.snapshot() : { messages: [] };
        (shared.messages || []).forEach(function (m) { addMessage(v, m.role, m.content); }); // start from a copy of the shared messages
        if (!all(v, '.v-msg').length) { addMessage(v, 'user', ''); }
      }
    }
  });
})();
