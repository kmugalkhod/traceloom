/* Playground compare: up to 4 variant columns, "Run all" streams every column on its own.
   Opens on the page's 'pg:action' event {action: 'compare'}, mounts into #pg-compare-mount and
   reads the shared editor through window.Playground (getState, setMode, runStream, config). Each column has
   its own AbortController and its own usage; nothing is shared between runs. Everything from the
   server or the model is written with textContent. */
(function () {
  'use strict';

  var MAX_DEFAULT = 4;
  var panel = null, data = [], seq = 0;
  var controllers = new WeakMap(); // variant element -> AbortController

  function $(root, sel) { return root.querySelector(sel); }
  function all(root, sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function max() { return parseInt(panel.getAttribute('data-max'), 10) || MAX_DEFAULT; }
  function variants() { return all(panel, '[data-variant]'); }

  // --- open / close ------------------------------------------------------------------------------

  function setOn(on) {
    if (window.Playground && window.Playground.setMode) { window.Playground.setMode(on ? 'compare' : 'single'); }
    var mount = document.getElementById('pg-compare-mount');
    if (mount) { mount.hidden = !on; }
    on ? document.body.setAttribute('data-compare', '') : document.body.removeAttribute('data-compare');
  }

  function toggle() {
    var mount = document.getElementById('pg-compare-mount');
    if (!mount || !window.Playground) { return; }
    if (panel) { setOn(mount.hidden); return; }
    var url = '/project/' + window.Playground.config.projectId + '/playground/compare-panel';
    fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
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

  // A message row edits role and content. Tool calls and tool call ids are not editable here; a row
  // copied from the shared messages keeps them and says so.
  function addMessage(v, message) {
    message = message || { role: 'user', content: '' };
    var row = document.createElement('div');
    row.className = 'v-msg';
    var sel = document.createElement('select');
    sel.className = 'select'; sel.setAttribute('data-m-role', ''); sel.setAttribute('aria-label', 'Role');
    ['system', 'user', 'assistant', 'tool'].forEach(function (r) {
      var o = document.createElement('option'); o.value = r; o.textContent = r; sel.appendChild(o);
    });
    sel.value = message.role || 'user';
    var ta = document.createElement('textarea');
    ta.className = 'input'; ta.rows = 2; ta.setAttribute('data-m-content', ''); ta.setAttribute('aria-label', 'Message');
    ta.value = message.content || '';
    var extra = {};
    if (message.tool_calls) { extra.tool_calls = message.tool_calls; }
    if (message.tool_call_id) { extra.tool_call_id = message.tool_call_id; }
    row.__extra = extra;
    var rm = document.createElement('button');
    rm.type = 'button'; rm.className = 'btn btn-icon btn-ghost btn-sm'; rm.setAttribute('data-m-remove', ''); rm.setAttribute('aria-label', 'Remove message'); rm.textContent = '×';
    row.appendChild(sel); row.appendChild(ta); row.appendChild(rm);
    if (Object.keys(extra).length) {
      var note = document.createElement('p');
      note.className = 'hint v-note';
      note.textContent = extra.tool_calls ? 'Keeps its tool calls (not editable here).' : 'Keeps its tool call id (not editable here).';
      row.appendChild(note);
    }
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
    updateStopAll();
    renumber();
    return v;
  }

  function removeVariant(v) {
    stop(v);
    v.remove();
    renumber();
  }

  // --- running -----------------------------------------------------------------------------------

  var LABELS = { ready: 'Ready', waiting: 'Waiting', streaming: 'Streaming', done: 'Done', early: 'Ended early', error: 'Error', stopped: 'Stopped' };
  function setStatus(v, state) {
    var chip = $(v, '[data-v-status]');
    chip.setAttribute('data-state', state);
    chip.textContent = LABELS[state];
  }

  function updateStopAll() {
    var any = variants().some(function (v) { return controllers.has(v); });
    var btn = $(panel, '[data-stop-all]');
    if (btn) { btn.disabled = !any; }
  }

  function stop(v) {
    var c = controllers.get(v);
    if (c) { c.abort(); controllers.delete(v); setStatus(v, 'stopped'); }
    $(v, '[data-variant-stop]').hidden = true;
    updateStopAll();
  }

  function stopAll() { variants().forEach(stop); }

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
    if ((name === 'delta' || name === 'tool_call') && $(v, '[data-v-status]').getAttribute('data-state') === 'waiting') {
      setStatus(v, 'streaming');
    }
    if (name === 'error') { v.__errored = true; }
    if (name === 'delta') { textNode.appendData(d.text); }
    else if (name === 'tool_call') {
      var li = document.createElement('li');
      li.textContent = d.name + '(' + JSON.stringify(d.arguments) + ')';
      $(v, '[data-v-calls]').appendChild(li);
    } else if (name === 'usage') { showUsage(v, d); }
    else if (name === 'error') { showError(v, d.message); }
  }

  function ownMessages(v) {
    return all(v, '.v-msg').map(function (row) {
      var m = { role: $(row, '[data-m-role]').value, content: $(row, '[data-m-content]').value };
      var extra = row.__extra || {};
      if (m.role === 'assistant' && extra.tool_calls) { m.tool_calls = extra.tool_calls; }
      if (m.role === 'tool' && extra.tool_call_id) { m.tool_call_id = extra.tool_call_id; }
      return m;
    }).filter(function (m) { return m.content.trim() !== '' || m.tool_calls; });
  }

  function stateFor(v, shared) {
    var params = {};
    Object.keys(shared.params || {}).forEach(function (k) { params[k] = shared.params[k]; });
    var t = $(v, '[data-v-temperature]').value, mt = $(v, '[data-v-max-tokens]').value;
    if (t !== '') { params.temperature = parseFloat(t); }
    if (mt !== '') { params.max_tokens = parseInt(mt, 10); }
    return {
      connectionId: $(v, '[data-v-connection]').value || shared.connectionId,
      model: $(v, '[data-v-model]').value.trim() || shared.model,
      messages: $(v, '[data-v-follow]').checked ? shared.messages : ownMessages(v),
      variables: shared.variables || {},
      params: params,
      tools: shared.tools || [],
      responseSchema: shared.responseSchema || null,
      trace: false,
      promptName: null,
      promptVersion: null
    };
  }

  function run(v, shared) {
    stop(v);
    var textEl = $(v, '[data-v-text]');
    clear(textEl); clear($(v, '[data-v-calls]'));
    $(v, '[data-v-error]').hidden = true; $(v, '[data-v-usage]').hidden = true;
    var textNode = document.createTextNode(''); textEl.appendChild(textNode);
    var state = stateFor(v, shared);
    if (!state.connectionId || !state.model || !state.messages.length) {
      showError(v, 'Pick a connection and a model, and give this variant at least one message.');
      return Promise.resolve();
    }
    var controller = new AbortController();
    controllers.set(v, controller);
    v.__errored = false;
    setStatus(v, 'waiting');
    $(v, '[data-variant-stop]').hidden = false;
    updateStopAll();
    return window.Playground.runStream(state, function (ev) {
      handle(v, textNode, ev.name, ev.data);
    }, controller.signal).then(function (result) {
      if (!result.ok && !result.aborted) { showError(v, result.message || 'The run failed.'); }
      else if (result.endedEarly && !v.__errored) { showError(v, 'The stream ended early. The text above is what arrived before the connection closed.'); }
      if (controllers.get(v) !== controller) { return; } // stopped or replaced meanwhile
      controllers.delete(v);
      $(v, '[data-variant-stop]').hidden = true;
      setStatus(v, result.aborted ? 'stopped' : (!result.ok || v.__errored) ? 'error' : result.endedEarly ? 'early' : 'done');
      updateStopAll();
    });
  }

  function runAll() {
    var shared = window.Playground ? window.Playground.getState() : null;
    if (!shared) { return; }
    // every column starts at once; each has its own request, controller and usage
    variants().forEach(function (v) { run(v, shared); });
  }

  // --- wiring ------------------------------------------------------------------------------------

  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target : null;
    if (!t) { return; }
    if (!panel || !panel.contains(t)) { return; }
    var v = t.closest('[data-variant]');
    if (t.closest('[data-add-variant]')) { addVariant(); }
    else if (t.closest('[data-run-all]')) { runAll(); }
    else if (v && t.closest('[data-variant-remove]')) { removeVariant(v); }
    else if (v && t.closest('[data-variant-stop]')) { stop(v); }
    else if (t.closest('[data-stop-all]')) { stopAll(); }
    else if (v && t.closest('[data-v-add-message]')) { addMessage(v); }
    else if (v && t.closest('[data-m-remove]')) { t.closest('.v-msg').remove(); }
  });

  // In compare mode ⌘/Ctrl+Enter runs every column instead of the (hidden) single response.
  document.addEventListener('keydown', function (e) {
    if (!panel || e.key !== 'Enter' || !(e.metaKey || e.ctrlKey)) { return; }
    var root = document.getElementById('pg-root');
    if (!root || root.getAttribute('data-mode') !== 'compare') { return; }
    e.preventDefault(); e.stopImmediatePropagation();
    runAll();
  }, true);

  document.addEventListener('pg:action', function (e) {
    if (e.detail && e.detail.action === 'compare') { toggle(); }
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
        var shared = window.Playground ? window.Playground.getState() : { messages: [] };
        (shared.messages || []).forEach(function (m) { addMessage(v, m); }); // start from a copy of the shared messages
        if (!all(v, '.v-msg').length) { addMessage(v); }
      }
    }
  });
})();
