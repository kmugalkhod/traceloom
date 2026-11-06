/* Playground: edit messages, run one prompt and stream the answer.
 *
 * Everything the model or the user wrote reaches the page through textContent / createTextNode;
 * nothing here uses innerHTML. No inline handlers (CSP). Other scripts (compare, save) use the
 * public `window.Playground` object: config, getState, setState, runStream, setMode,
 * supportedParams, modelsFor, renderUsage, compile; plus the 'pg:state' and 'pg:action' events.
 */
(function () {
  'use strict';

  var configEl = document.getElementById('pg-config');
  var rootEl = document.getElementById('pg-root');
  if (!configEl || !rootEl) { return; }
  var config = JSON.parse(configEl.textContent);

  function $(id) { return document.getElementById(id); }
  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  function text(el, value) { el.textContent = value; return el; }
  function node(tag, cls, content) {
    var el = document.createElement(tag);
    if (cls) { el.className = cls; }
    if (content !== undefined && content !== null) { el.textContent = content; }
    return el;
  }
  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }

  // ---- state ------------------------------------------------------------------------------------

  var state = {
    connectionId: '', model: '', messages: [], variables: {}, params: {}, tools: [],
    responseSchema: null, trace: false, promptName: null, promptVersion: null
  };

  function connection(id) {
    for (var i = 0; i < config.connections.length; i++) { if (config.connections[i].id === id) { return config.connections[i]; } }
    return null;
  }
  function modelsFor(id) { var c = connection(id); return c ? c.models.slice() : []; }
  // The rules come from the server (traceloom.llm.models.PARAM_RULES), so the page and the server
  // can never disagree about which parameters a model takes.
  var ruleCache = {};
  function supportedParams(model, id) {
    var c = connection(id === undefined ? state.connectionId : id);
    var provider = c ? c.provider : '';
    var rules = config.paramRules || [];
    for (var i = 0; i < rules.length; i++) {
      var rule = rules[i];
      if (rule.providers.indexOf(provider) < 0) { continue; }
      var re = ruleCache[rule.pattern] || (ruleCache[rule.pattern] = new RegExp(rule.pattern));
      if (re.test(model || '')) { return rule.params.slice(); }
    }
    return (config.defaultParams || ['temperature', 'top_p', 'max_tokens', 'stop']).slice();
  }

  var VAR = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}/g;
  function variablesIn(messages) {
    var names = [];
    messages.forEach(function (m) {
      if (m.role === 'tool') { return; }
      var re = new RegExp(VAR.source, 'g'), hit;
      while ((hit = re.exec(m.content)) !== null) { if (names.indexOf(hit[1]) < 0) { names.push(hit[1]); } }
    });
    return names;
  }
  function compile(messages, variables) {
    return messages.map(function (m) {
      var out = clone(m);
      if (m.role !== 'tool') {
        out.content = m.content.replace(new RegExp(VAR.source, 'g'), function (whole, name) {
          return Object.prototype.hasOwnProperty.call(variables, name) && variables[name] !== '' ? String(variables[name]) : whole;
        });
      }
      return out;
    });
  }

  function emitState() { document.dispatchEvent(new CustomEvent('pg:state', { detail: clone(state) })); }

  // ---- run (no DOM) -----------------------------------------------------------------------------

  function body(s) {
    return {
      connection_id: s.connectionId, model: s.model,
      messages: s.messages.map(function (m) {
        var out = { role: m.role, content: m.content };
        if (m.role === 'assistant' && m.tool_calls && m.tool_calls.length) { out.tool_calls = m.tool_calls; }
        if (m.role === 'tool') { out.tool_call_id = m.tool_call_id || ''; }
        return out;
      }),
      variables: s.variables || {}, params: s.params || {}, tools: s.tools || [],
      response_schema: s.responseSchema || null, trace: !!s.trace,
      prompt_name: s.promptName || null, prompt_version: s.promptVersion || null
    };
  }

  function parseBlock(block) {
    var name = 'message', data = [];
    block.split('\n').forEach(function (line) {
      if (line.indexOf('event:') === 0) { name = line.slice(6).trim(); }
      else if (line.indexOf('data:') === 0) { data.push(line.slice(5).replace(/^ /, '')); }
    });
    if (!data.length) { return null; }
    try { return { name: name, data: JSON.parse(data.join('\n')) }; } catch (e) { return null; }
  }

  function runStream(s, onEvent, signal) {
    var headers = { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': cookie('tl_csrf') };
    return fetch(config.runUrl, { method: 'POST', headers: headers, body: JSON.stringify(body(s)), signal: signal, credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) {
          return response.json().catch(function () { return {}; }).then(function (data) {
            var message = (data && data.error && data.error.message) || ('The run failed (HTTP ' + response.status + ').');
            return { ok: false, status: response.status, message: message, retryAfter: Number(response.headers.get('Retry-After')) || null };
          });
        }
        // The server always ends a run with `done`; a stream that closes without one was cut off.
        var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = '', sawDone = false;
        function emit(ev) { if (ev.name === 'done') { sawDone = true; } onEvent(ev); }
        function pump() {
          return reader.read().then(function (chunk) {
            if (chunk.done) {
              buffer += decoder.decode();
              var last = parseBlock(buffer.replace(/\r/g, ''));
              if (last) { emit(last); }
              return { ok: true, status: response.status, endedEarly: !sawDone };
            }
            buffer += decoder.decode(chunk.value, { stream: true }).replace(/\r/g, '');
            var parts = buffer.split('\n\n');
            buffer = parts.pop();
            parts.forEach(function (part) { var ev = parseBlock(part); if (ev) { emit(ev); } });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') { return { ok: false, aborted: true, message: 'Stopped.' }; }
        return { ok: false, message: 'The connection to the server was lost.' };
      });
  }

  // ---- formatting -------------------------------------------------------------------------------

  function fmtCount(n) { return n === null || n === undefined ? '-' : Number(n).toLocaleString('en-US'); }
  function fmtCost(c) {
    if (c === null || c === undefined) { return '-'; }
    if (c === 0) { return '$0'; }
    return '$' + (c < 0.01 ? c.toFixed(6).replace(/0+$/, '') : c.toFixed(4));
  }
  function fmtMs(ms) {
    if (ms === null || ms === undefined) { return '-'; }
    return ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(2) + ' s';
  }
  function renderUsage(el, usage) {
    var format = { input_tokens: fmtCount, output_tokens: fmtCount, total_tokens: fmtCount, cost: fmtCost, latency_ms: fmtMs, ttft_ms: fmtMs };
    el.querySelectorAll('[data-u]').forEach(function (dd) {
      var key = dd.getAttribute('data-u');
      var has = usage && usage[key] !== undefined;
      text(dd, has ? format[key](usage[key]) : '-');
      dd.classList.toggle('pend', !has);
    });
    var src = el.querySelector('[data-u-src]');
    if (src) { src.hidden = !(usage && usage.cost_source === 'calculated'); }
  }

  // ---- editor rendering ---------------------------------------------------------------------------

  var TOKEN = /\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}/g;
  function highlight(pre, value) {
    while (pre.firstChild) { pre.removeChild(pre.firstChild); }
    var last = 0, hit;
    TOKEN.lastIndex = 0;
    while ((hit = TOKEN.exec(value)) !== null) {
      if (hit.index > last) { pre.appendChild(document.createTextNode(value.slice(last, hit.index))); }
      pre.appendChild(node('span', 'var', hit[0]));
      last = hit.index + hit[0].length;
    }
    pre.appendChild(document.createTextNode(value.slice(last) + '\n '));
  }

  var msgsEl = $('pg-msgs'), template = $('pg-msg-tpl');

  function renderMessages() {
    while (msgsEl.firstChild) { msgsEl.removeChild(msgsEl.firstChild); }
    state.messages.forEach(function (message, i) {
      var card = template.content.firstElementChild.cloneNode(true);
      card.classList.remove('system', 'user', 'assistant', 'tool');
      card.classList.add(message.role);
      var select = card.querySelector('[data-msg-role]'), area = card.querySelector('[data-msg-text]'), pre = card.querySelector('.ed-hl');
      select.value = message.role;
      showExtras(card, message);
      area.value = message.content;
      area.setAttribute('aria-label', message.role + ' message ' + (i + 1));
      highlight(pre, message.content);
      card.querySelector('.ed-wrap').setAttribute('data-ready', '');
      card.setAttribute('data-index', String(i));
      msgsEl.appendChild(card);
    });
    renderCounts();
  }

  // Tool call id (tool messages) and tool calls (assistant messages) are editable, never hidden.
  function showExtras(card, message) {
    var idWrap = card.querySelector('[data-msg-idwrap]'), callsWrap = card.querySelector('[data-msg-callswrap]');
    idWrap.hidden = message.role !== 'tool';
    idWrap.querySelector('[data-msg-callid]').value = message.tool_call_id || '';
    var has = message.role === 'assistant' && message.tool_calls && message.tool_calls.length;
    callsWrap.hidden = message.role !== 'assistant';
    callsWrap.open = !!has;
    callsWrap.querySelector('[data-msg-calls]').value = has ? JSON.stringify(message.tool_calls, null, 2) : '';
  }

  function renderCounts() {
    var n = state.messages.length, v = Object.keys(state.variables).length;
    text($('pg-counts'), n + (n === 1 ? ' message' : ' messages') + ' · ' + v + (v === 1 ? ' input' : ' inputs'));
  }

  function syncVariables() {
    var names = variablesIn(state.messages), next = {}, changed = names.length !== Object.keys(state.variables).length;
    names.forEach(function (name) {
      next[name] = Object.prototype.hasOwnProperty.call(state.variables, name) ? state.variables[name] : '';
      if (!Object.prototype.hasOwnProperty.call(state.variables, name)) { changed = true; }
    });
    state.variables = next;
    if (changed) { renderVariables(); }
    renderCounts();
  }

  function renderVariables() {
    var box = $('pg-vars');
    while (box.firstChild) { box.removeChild(box.firstChild); }
    Object.keys(state.variables).forEach(function (name, i) {
      var field = node('div', 'field'), id = 'pg-var-' + i;
      var label = node('label', null, name); label.setAttribute('for', id);
      var input = node('input', 'input'); input.id = id; input.value = state.variables[name]; input.autocomplete = 'off';
      input.setAttribute('data-var', name);
      field.appendChild(label); field.appendChild(input); box.appendChild(field);
    });
  }

  function renderConnection() {
    var select = $('pg-connection');
    while (select.firstChild) { select.removeChild(select.firstChild); }
    if (!state.connectionId) {
      var none = node('option', null, 'Choose a connection'); none.value = ''; select.appendChild(none);
    }
    config.connections.forEach(function (c) {
      var o = node('option', null, c.name); o.value = c.id; select.appendChild(o);
    });
    select.value = state.connectionId;
    var list = $('pg-models');
    while (list.firstChild) { list.removeChild(list.firstChild); }
    modelsFor(state.connectionId).forEach(function (m) { var o = node('option'); o.value = m; list.appendChild(o); });
    $('pg-model').value = state.model;
  }

  var PARAM_LABEL = { temperature: 'Temperature', top_p: 'Top p', max_tokens: 'Max tokens', stop: 'Stop sequences' };
  var paramsShown = '';
  function renderParams(force) {
    var box = $('pg-params');
    var supported = supportedParams(state.model);
    // Rebuild the fields only when the supported set changed (or on a full re-render), so moving
    // focus from the model box into a parameter field never swaps the field away under the cursor.
    var rebuild = force === true || paramsShown !== supported.join(',');
    paramsShown = supported.join(',');
    if (rebuild) { while (box.firstChild) { box.removeChild(box.firstChild); } }
    (rebuild ? supported : []).forEach(function (key) {
      var field = node('div', 'field'), id = 'pg-param-' + key;
      var label = node('label', null, PARAM_LABEL[key] || key); label.setAttribute('for', id);
      var input = node('input', 'input mono'); input.id = id; input.setAttribute('data-param', key); input.autocomplete = 'off';
      var value = state.params[key];
      input.value = Array.isArray(value) ? value.join(', ') : (value === undefined ? '' : String(value));
      if (key === 'stop') { input.placeholder = 'comma separated'; }
      field.appendChild(label); field.appendChild(input); box.appendChild(field);
    });
    var hidden = ['temperature', 'top_p', 'max_tokens', 'stop'].filter(function (k) { return supported.indexOf(k) < 0; });
    text($('pg-params-hint'), hidden.length ? hidden.join(', ') + (hidden.length === 1 ? ' is' : ' are') + ' hidden because ' + (state.model || 'this model') + ' does not support ' + (hidden.length === 1 ? 'it.' : 'them.') : '');
    // Drop values the model no longer supports so they are never sent.
    Object.keys(state.params).forEach(function (k) { if (supported.indexOf(k) < 0) { delete state.params[k]; } });
  }

  function renderTools() {
    $('pg-tools').value = state.tools.length ? JSON.stringify(state.tools, null, 2) : '';
    $('pg-schema-on').setAttribute('aria-checked', state.responseSchema ? 'true' : 'false');
    $('pg-schema-field').hidden = !state.responseSchema;
    $('pg-schema').value = state.responseSchema ? JSON.stringify(state.responseSchema, null, 2) : '';
  }

  function renderAll() {
    renderConnection(); renderMessages(); renderVariables(); renderParams(true); renderTools();
    $('pg-trace').setAttribute('aria-checked', state.trace ? 'true' : 'false');
    updateRunButton();
  }

  // ---- editing events -----------------------------------------------------------------------------

  function setState(partial) {
    Object.keys(partial || {}).forEach(function (k) { state[k] = clone(partial[k]); });
    if (partial && partial.messages) { syncVariables(); }
    renderAll();
    emitState();
  }

  msgsEl.addEventListener('input', function (e) {
    var idInput = e.target.closest('[data-msg-callid]'), callsArea = e.target.closest('[data-msg-calls]');
    if (idInput || callsArea) {
      var owner = e.target.closest('[data-msg]'), idx = Number(owner.getAttribute('data-index')), msg = state.messages[idx];
      if (idInput) { msg.tool_call_id = idInput.value; emitState(); return; }
      var raw = callsArea.value.trim(), box = owner.querySelector('[data-msg-callserr]');
      if (!raw) { delete msg.tool_calls; box.hidden = true; callsArea.removeAttribute('aria-invalid'); emitState(); return; }
      var parsed = jsonProblem(raw), problem = parsed.error || '';
      if (!problem && !(Array.isArray(parsed.value) && parsed.value.every(function (c) { return c && typeof c === 'object' && typeof c.name === 'string'; }))) { problem = 'Tool calls must be a JSON array of objects with a name.'; }
      box.hidden = !problem; text(box.querySelector('[data-errtext]') || box, problem ? problem + ' The tool calls were not applied.' : '');
      if (problem) { callsArea.setAttribute('aria-invalid', 'true'); } else { callsArea.removeAttribute('aria-invalid'); msg.tool_calls = parsed.value; emitState(); }
      return;
    }
    var area = e.target.closest('[data-msg-text]');
    if (!area) { return; }
    var card = area.closest('[data-msg]'), i = Number(card.getAttribute('data-index'));
    state.messages[i].content = area.value;
    highlight(card.querySelector('.ed-hl'), area.value);
    syncVariables();
    emitState();
  });
  msgsEl.addEventListener('change', function (e) {
    var sel = e.target.closest('[data-msg-role]');
    if (!sel) { return; }
    var card = sel.closest('[data-msg]'), i = Number(card.getAttribute('data-index'));
    state.messages[i].role = sel.value;
    card.classList.remove('system', 'user', 'assistant', 'tool'); card.classList.add(sel.value);
    showExtras(card, state.messages[i]);
    syncVariables();
    emitState();
  });
  msgsEl.addEventListener('click', function (e) {
    var btn = e.target.closest('button'), card = btn && btn.closest('[data-msg]');
    if (!card) { return; }
    var i = Number(card.getAttribute('data-index')), list = state.messages;
    if (btn.hasAttribute('data-msg-remove') && list.length > 1) { list.splice(i, 1); }
    else if (btn.hasAttribute('data-msg-up') && i > 0) { list.splice(i - 1, 0, list.splice(i, 1)[0]); }
    else if (btn.hasAttribute('data-msg-down') && i < list.length - 1) { list.splice(i + 1, 0, list.splice(i, 1)[0]); }
    else { return; }
    renderMessages(); syncVariables(); emitState();
  });
  $('pg-add').addEventListener('click', function () {
    var last = state.messages[state.messages.length - 1];
    state.messages.push({ role: last && last.role === 'user' ? 'assistant' : 'user', content: '' });
    renderMessages(); emitState();
    var areas = msgsEl.querySelectorAll('[data-msg-text]'); areas[areas.length - 1].focus();
  });
  $('pg-vars').addEventListener('input', function (e) {
    var input = e.target.closest('[data-var]');
    if (input) { state.variables[input.getAttribute('data-var')] = input.value; emitState(); }
  });

  function rememberConnection(id) {
    if (!id) { return; }
    fetch(config.rememberUrl, { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF-Token': cookie('tl_csrf') },
      body: JSON.stringify({ connection_id: id }) }).catch(function () { /* a preference, never fatal */ });
  }
  $('pg-connection').addEventListener('change', function (e) {
    state.connectionId = e.target.value;
    rememberConnection(state.connectionId);
    var models = modelsFor(state.connectionId);
    if (models.indexOf(state.model) < 0 && models.length) { state.model = models[0]; }
    renderConnection(); renderParams(); updateRunButton(); emitState();
  });
  $('pg-model').addEventListener('input', function (e) { state.model = e.target.value.trim(); updateRunButton(); emitState(); });
  $('pg-model').addEventListener('change', function () { renderParams(); emitState(); });

  $('pg-params').addEventListener('input', function (e) {
    var input = e.target.closest('[data-param]');
    if (!input) { return; }
    var key = input.getAttribute('data-param'), raw = input.value.trim();
    if (raw === '') { delete state.params[key]; }
    else if (key === 'stop') { state.params[key] = raw.split(',').map(function (s) { return s.trim(); }).filter(Boolean); }
    else if (!isNaN(Number(raw))) { state.params[key] = key === 'max_tokens' ? Math.round(Number(raw)) : Number(raw); }
    emitState();
  });

  function jsonProblem(source) {
    try { return { value: JSON.parse(source) }; }
    catch (err) { return { error: err.message.replace(/^JSON\.parse: /, '') }; }
  }
  function showError(id, message) {
    var box = $(id); box.hidden = !message; text(box.querySelector('[data-msg]'), message || '');
  }
  $('pg-tools').addEventListener('input', function (e) {
    var raw = e.target.value.trim();
    if (!raw) { state.tools = []; showError('pg-tools-err', ''); e.target.removeAttribute('aria-invalid'); emitState(); return; }
    var parsed = jsonProblem(raw), problem = parsed.error || '';
    if (!problem) {
      if (!Array.isArray(parsed.value)) { problem = 'Tools must be a JSON array.'; }
      else if (!parsed.value.every(function (t) { return t && typeof t === 'object' && typeof t.name === 'string' && t.name; })) { problem = 'Each tool needs a name.'; }
    }
    showError('pg-tools-err', problem ? problem + ' The tools were not applied.' : '');
    if (problem) { e.target.setAttribute('aria-invalid', 'true'); } else { e.target.removeAttribute('aria-invalid'); state.tools = parsed.value; emitState(); }
  });
  $('pg-add-tool').addEventListener('click', function () {
    var area = $('pg-tools'), parsed = area.value.trim() ? jsonProblem(area.value) : { value: [] };
    if (parsed.error || !Array.isArray(parsed.value)) { area.focus(); return; }
    parsed.value.push({ name: 'new_tool', description: '', parameters: { type: 'object', properties: {} } });
    area.value = JSON.stringify(parsed.value, null, 2);
    area.dispatchEvent(new Event('input', { bubbles: true }));
    area.focus();
  });
  $('pg-schema-on').addEventListener('click', function (e) {
    var on = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(on));
    $('pg-schema-field').hidden = !on;
    if (!on) { state.responseSchema = null; showError('pg-schema-err', ''); }
    else { $('pg-schema').dispatchEvent(new Event('input', { bubbles: true })); }
    emitState();
  });
  $('pg-schema').addEventListener('input', function (e) {
    var raw = e.target.value.trim();
    if (!raw) { state.responseSchema = null; showError('pg-schema-err', ''); emitState(); return; }
    var parsed = jsonProblem(raw), problem = parsed.error || (parsed.value && typeof parsed.value === 'object' && !Array.isArray(parsed.value) ? '' : 'The schema must be a JSON object.');
    showError('pg-schema-err', problem ? problem + ' The schema was not applied.' : '');
    if (problem) { e.target.setAttribute('aria-invalid', 'true'); } else { e.target.removeAttribute('aria-invalid'); state.responseSchema = parsed.value; emitState(); }
  });
  $('pg-trace').addEventListener('click', function (e) {
    state.trace = e.currentTarget.getAttribute('aria-checked') !== 'true';
    e.currentTarget.setAttribute('aria-checked', String(state.trace));
    emitState();
  });

  // ---- settings drawer ------------------------------------------------------------------------------

  var drawer = $('pg-drawer'), settingsBtn = $('pg-settings-btn');
  function toggleDrawer(open) {
    var show = typeof open === 'boolean' ? open : drawer.hidden;
    drawer.hidden = !show;
    settingsBtn.setAttribute('aria-pressed', String(show));
    $('pg-grid').classList.toggle('with-drawer', show);
  }
  settingsBtn.addEventListener('click', function () { toggleDrawer(); });
  $('pg-drawer-close').addEventListener('click', function () { toggleDrawer(false); settingsBtn.focus(); });
  drawer.querySelector('.seg').addEventListener('click', function (e) {
    var tab = e.target.closest('[data-tab]');
    if (!tab) { return; }
    drawer.querySelectorAll('.seg [data-tab]').forEach(function (b) { b.setAttribute('aria-pressed', String(b === tab)); });
    drawer.querySelectorAll('[data-pane]').forEach(function (p) { p.hidden = p.getAttribute('data-pane') !== tab.getAttribute('data-tab'); });
  });

  // ---- running -----------------------------------------------------------------------------------

  var outEl = $('pg-out'), statusEl = $('pg-status'), runBtn = $('pg-run'), stopBtn = $('pg-stop');
  var controller = null, textNode = null, caret = null, limitTimer = null;

  function canRun() { return !!(state.connectionId && state.model && !controller && !limitTimer); }
  function updateRunButton() {
    runBtn.hidden = !!controller; stopBtn.hidden = !controller;
    runBtn.disabled = false;
    runBtn.setAttribute('aria-disabled', canRun() ? 'false' : 'true');
  }
  function setStatus(kind, label) {
    statusEl.hidden = !kind;
    statusEl.className = 'stat-chip' + (kind ? ' ' + kind : '');
    while (statusEl.firstChild) { statusEl.removeChild(statusEl.firstChild); }
    if (kind === 'run') { statusEl.appendChild(node('span', 'spin')); }
    if (label) { statusEl.appendChild(document.createTextNode(label)); }
  }
  function banner(kind, strong, message) {
    var box = node('div', 'banner ' + kind); box.setAttribute('role', kind === 'err' || kind === 'warn' ? 'alert' : 'status');
    var inner = node('div');
    if (strong) { inner.appendChild(node('b', null, strong)); inner.appendChild(document.createTextNode(' ')); }
    inner.appendChild(document.createTextNode(message));
    box.appendChild(inner);
    return box;
  }
  function clearOut() {
    while (outEl.firstChild) { outEl.removeChild(outEl.firstChild); }
    outEl.classList.remove('idle'); textNode = null;
  }
  function ensureText() {
    if (!textNode) {
      textNode = document.createTextNode('');
      outEl.appendChild(textNode);
      if (caret) { outEl.appendChild(caret); }
    }
    return textNode;
  }
  function removeCaret() { if (caret && caret.parentNode) { caret.parentNode.removeChild(caret); } caret = null; }

  function toolCallBlock(call) {
    var box = node('div', 'toolcall'), head = node('header', null, 'Tool call · ' + (call.name || '?'));
    var pre = node('pre', null, JSON.stringify(call.arguments, null, 2));
    box.appendChild(head); box.appendChild(pre);
    return box;
  }

  function onEvent(ev, session) {
    var data = ev.data || {};
    if (ev.name === 'delta') {
      ensureText().appendData(String(data.text || ''));
    } else if (ev.name === 'tool_call') {
      removeCaret(); outEl.appendChild(toolCallBlock(data)); textNode = null;
    } else if (ev.name === 'usage') {
      renderUsage($('pg-usage'), data);
    } else if (ev.name === 'error') {
      session.error = String(data.message || 'The provider returned an error.');
      session.refusal = /declined to answer/i.test(session.error);
    } else if (ev.name === 'done') {
      session.finish = data.finish_reason || null; session.traceId = data.trace_id || null;
    }
  }

  function finishRun(session, result) {
    removeCaret();
    outEl.setAttribute('aria-busy', 'false');
    controller = null;
    if (result.aborted || session.stopped) {
      setStatus('', ''); setStatus('warn', 'Stopped');
      outEl.appendChild(banner('info', null, 'Stopped. The text above is what arrived before you stopped the run.'));
    } else if (!result.ok) {
      if (result.status === 429) {
        setStatus('err', 'Limited');
        outEl.appendChild(banner('warn', 'You have reached the run limit.', result.message));
        startLimitCountdown(result.retryAfter);
      } else {
        setStatus('err', 'Failed');
        outEl.appendChild(banner('err', 'The run failed.', result.message));
      }
    } else if (session.refusal) {
      setStatus('', ''); setStatus('warn', 'Declined');
      outEl.appendChild(banner('warn', 'The model declined to answer.', session.error));
    } else if (session.error) {
      setStatus('err', 'Failed');
      outEl.appendChild(banner('err', 'The provider returned an error.', session.error));
    } else if (result.endedEarly) {
      setStatus('', ''); setStatus('warn', 'Ended early');
      outEl.appendChild(banner('warn', 'The stream ended early.', 'The connection closed before the run finished. The text above is what arrived; run it again for the full response.'));
    } else {
      setStatus('ok', 'Done');
      if (session.traceId) {
        var link = node('a', null, 'Open the trace'); link.href = '/project/' + config.projectId + '/traces/' + encodeURIComponent(session.traceId);
        var note = node('div', 'hint'); note.appendChild(link); outEl.appendChild(note);
      }
    }
    updateRunButton();
  }

  function startLimitCountdown(seconds) {
    var left = Math.max(1, seconds || 10);
    limitTimer = setInterval(function () {
      left -= 1;
      if (left <= 0) { clearInterval(limitTimer); limitTimer = null; updateRunButton(); }
    }, 1000);
    updateRunButton();
  }

  function run() {
    if (controller || limitTimer) { return; }
    clearOut();
    if (!state.connectionId) { outEl.appendChild(banner('warn', 'Pick a connection first.', 'Choose one in the “Run with” row to run this prompt.')); return; }
    if (!state.model) { outEl.appendChild(banner('warn', 'Pick a model first.', 'Enter or choose a model name.')); return; }
    renderUsage($('pg-usage'), null);
    controller = new AbortController();
    var session = { error: '', refusal: false, finish: null, stopped: false, traceId: null };
    caret = node('span', 'caret'); caret.setAttribute('aria-hidden', 'true'); outEl.appendChild(caret);
    outEl.setAttribute('aria-busy', 'true');
    setStatus('run', 'Streaming');
    updateRunButton();
    var mine = controller;
    session.abort = function () { session.stopped = true; mine.abort(); };
    currentSession = session;
    runStream(clone(state), function (ev) { onEvent(ev, session); }, mine.signal).then(function (result) {
      finishRun(session, result);
      currentSession = null;
    });
  }
  var currentSession = null;
  function stop() { if (currentSession) { currentSession.abort(); } }

  runBtn.addEventListener('click', run);
  stopBtn.addEventListener('click', stop);
  document.addEventListener('keydown', function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); run(); }
    else if (e.key === 'Escape') {
      if (controller) { e.preventDefault(); stop(); }
      else if (!drawer.hidden && !document.querySelector('[data-menu]:not([hidden])') && !document.querySelector('dialog[open]')) { toggleDrawer(false); settingsBtn.focus(); }
    }
  });

  // ---- menu actions, mode ---------------------------------------------------------------------------

  document.addEventListener('click', function (e) {
    var item = e.target.closest('[data-pg-action]');
    if (item) { document.dispatchEvent(new CustomEvent('pg:action', { detail: { action: item.getAttribute('data-pg-action') } })); }
  });
  function setMode(mode) {
    rootEl.setAttribute('data-mode', mode);
    // #pg-grid stays on screen in compare mode: the shared messages and inputs are what the columns
    // follow. CSS (playground_compare.css) hides the single Response panel instead.
    $('pg-compare-mount').hidden = mode !== 'compare';
    var label = document.querySelector('[data-compare-label]');
    if (label) { label.textContent = mode === 'compare' ? 'Exit compare' : 'Compare variants'; }
  }

  // ---- from a trace ---------------------------------------------------------------------------------

  var ROLES = ['system', 'user', 'assistant', 'tool'];
  function fromTrace(traceId, obsId) {
    var note = $('pg-note');
    return fetch(config.observationUrl + encodeURIComponent(obsId) + '?trace_id=' + encodeURIComponent(traceId), { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function (fill) {
        var all = fill.messages || [], shown = [], unshown = [];
        all.forEach(function (m) {
          if (m && ROLES.indexOf(m.role) >= 0 && typeof m.content === 'string') {
            var kept = { role: m.role, content: m.content };
            if (m.role === 'assistant' && m.tool_calls && m.tool_calls.length) { kept.tool_calls = m.tool_calls; }
            if (m.role === 'tool') { kept.tool_call_id = m.tool_call_id || ''; }
            shown.push(kept);
          } else { unshown.push(m); }
        });
        var variables = {};
        variablesIn(shown).forEach(function (n) { variables[n] = ''; });
        var chosen = fill.connection_id || '';
        var models = modelsFor(chosen);
        var partial = {
          messages: shown.length ? shown : [{ role: 'user', content: '' }],
          variables: variables, params: fill.params || {}, tools: fill.tools || [],
          connectionId: chosen, model: fill.model || (models[0] || ''),
          promptName: fill.prompt_name || null, promptVersion: fill.prompt_version || null
        };
        setState(partial);
        while (note.firstChild) { note.removeChild(note.firstChild); }
        var results = shown.filter(function (m) { return m.role === 'tool'; }).length;
        var calls = shown.reduce(function (n, m) { return n + (m.tool_calls ? m.tool_calls.length : 0); }, 0);
        var detail = shown.length + (shown.length === 1 ? ' message was' : ' messages were') + ' copied' +
          (results ? ' (' + results + ' tool ' + (results === 1 ? 'result' : 'results') + (calls ? ', ' + calls + ' tool ' + (calls === 1 ? 'call' : 'calls') : '') + ')' : '') +
          ', with the model, parameters and tools of the ' + (fill.prompt_name || 'generation') + (chosen ? '.' : '. Pick a connection to run it.');
        note.appendChild(banner('info', 'Opened from trace ' + traceId + '.', detail));
        if (unshown.length) {
          var warn = banner('warn', unshown.length + (unshown.length === 1 ? ' message' : ' messages') + ' could not be shown.',
            'The editor has no card for these roles, so they are not part of this run: ');
          unshown.forEach(function (m) {
            var item = node('div', 'hint mono', '[' + (m && m.role !== undefined ? m.role : '?') + '] ' + (m && typeof m.content === 'string' ? m.content : JSON.stringify(m)));
            warn.firstChild.appendChild(item);
          });
          note.appendChild(warn);
        }
      })
      .catch(function () {
        note.appendChild(banner('warn', 'Could not open that generation.', 'It may have been deleted, or you may not have access.'));
      });
  }

  // ---- boot ------------------------------------------------------------------------------------------

  var initial = clone(config.initial || {});
  state.messages = initial.messages && initial.messages.length ? initial.messages : [{ role: 'system', content: '' }, { role: 'user', content: '' }];
  state.variables = initial.variables || {};
  state.params = initial.params || {};
  state.tools = initial.tools || [];
  state.promptName = initial.promptName || null;
  state.promptVersion = initial.promptVersion || null;
  state.connectionId = initial.connectionId || ''; // the server decides: the only one, the last used, or none
  state.model = initial.model || (modelsFor(state.connectionId)[0] || '');
  syncVariables();
  renderAll();

  window.Playground = {
    config: config,
    getState: function () { return clone(state); },
    setState: setState,
    runStream: runStream,
    setMode: setMode,
    supportedParams: function (model) { return supportedParams(model); },
    modelsFor: modelsFor,
    renderUsage: renderUsage,
    compile: compile
  };

  var query = new URLSearchParams(window.location.search);
  if (query.get('trace') && query.get('obs')) { fromTrace(query.get('trace'), query.get('obs')); }
})();
