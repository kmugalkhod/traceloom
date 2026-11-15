/* Traceloom public feedback: thumbs and ratings from your own pages. No dependencies.
   <script src=".../static/feedback.js" defer></script>
   <button data-traceloom-feedback="thumbs" data-trace-id="TRACE_ID" data-value="true">+</button>
   <script>Traceloom.init({ publicKey: "pk-..." })</script>
   Optional attributes: data-observation-id, data-session-id, data-comment-from="#selector". */
(function (w, d) {
  'use strict';
  var base = (d.currentScript && d.currentScript.src || '').replace(/\/static\/.*$/, '');
  var cfg = { host: base, publicKey: '' };
  var T = w.Traceloom = w.Traceloom || {};

  function parse(v) {
    if (v === 'true') { return true; }
    if (v === 'false') { return false; }
    return v !== '' && !isNaN(v) ? Number(v) : v;
  }
  function uid() { return Math.random().toString(36).slice(2) + Date.now().toString(36); }

  /* Send one feedback. o: {name, value, traceId | sessionId, observationId, comment, id}. */
  T.send = function (o) {
    return fetch(cfg.host + '/api/public/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Traceloom-Public-Key': cfg.publicKey },
      body: JSON.stringify(o)
    }).then(function (r) {
      if (!r.ok) { throw new Error('Traceloom feedback failed: ' + r.status); }
      return r.json();
    });
  };

  T.init = function (o) {
    cfg.publicKey = o.publicKey || '';
    if (o.host) { cfg.host = o.host.replace(/\/$/, ''); }
  };

  d.addEventListener('click', function (e) {
    var el = e.target.closest('[data-traceloom-feedback]');
    if (!el) { return; }
    var from = el.getAttribute('data-comment-from');
    var box = from && d.querySelector(from);
    el.__id = el.__id || 'fb_' + uid(); /* clicking again replaces this feedback */
    var body = {
      id: el.__id,
      name: el.getAttribute('data-traceloom-feedback'),
      value: parse(el.getAttribute('data-value') || ''),
      traceId: el.getAttribute('data-trace-id') || undefined,
      observationId: el.getAttribute('data-observation-id') || undefined,
      sessionId: el.getAttribute('data-session-id') || undefined,
      comment: box ? box.value : undefined
    };
    el.setAttribute('aria-busy', 'true');
    T.send(body).then(function () {
      el.setAttribute('data-state', 'sent');
      el.setAttribute('aria-pressed', 'true');
    }, function () {
      el.setAttribute('data-state', 'error');
    }).then(function () { el.removeAttribute('aria-busy'); });
  });
})(window, document);
