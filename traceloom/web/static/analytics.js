/* Traceloom score analytics: uPlot trend and scatter charts, matrix cell shading, auto-submitting
   pickers. Driven by data attributes so it works under script-src 'self' with no inline JS. */
(function () {
  'use strict';
  var charts = [];

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  // Colourblind-safe categorical palette from the theme (blue, violet, amber). Sources are also
  // told apart by line style: solid, dashed, dotted.
  var SOURCE = {
    ANNOTATION: { colour: '--s1', dash: [] },
    EVAL: { colour: '--s3', dash: [6, 4] },
    API: { colour: '--s4', dash: [2, 3] }
  };

  function parse(el) {
    try { return JSON.parse(el.getAttribute('data-an-chart')); } catch (e) { return null; }
  }

  function axes() {
    var axis = {
      stroke: css('--ink-3'), font: '11px ' + css('--font-ui'),
      grid: { stroke: css('--line'), width: 1 }, ticks: { stroke: css('--line'), width: 1 }
    };
    return [axis, Object.assign({ size: 48 }, axis)];
  }

  function base(el) {
    return {
      width: Math.max(el.clientWidth, 200), height: el.clientHeight || 220,
      legend: { show: false }, cursor: { points: { show: false }, drag: { x: false, y: false } },
      axes: axes()
    };
  }

  function trend(el, spec) {
    var xs = spec.labels.map(function (l) { return Date.parse(l) / 1000; });
    var data = [xs].concat(spec.series.map(function (s) { return s.values; }));
    var opts = base(el);
    opts.tzDate = function (ts) { return window.uPlot.tzDate(new Date(ts * 1e3), 'Etc/UTC'); };
    opts.scales = { x: { time: true } };
    opts.series = [{}].concat(spec.series.map(function (s) {
      var look = SOURCE[s.source] || SOURCE.API;
      return {
        label: s.name, stroke: css(look.colour), width: 2.5, dash: look.dash, spanGaps: true,
        points: { show: xs.length < 40, size: 6, fill: css(look.colour) }
      };
    }));
    return new window.uPlot(opts, data, el);
  }

  function scatter(el, spec) {
    var order = spec.x.map(function (_, i) { return i; })
      .sort(function (a, b) { return spec.x[a] - spec.x[b]; });
    var data = [
      order.map(function (i) { return spec.x[i]; }),
      order.map(function (i) { return spec.y[i]; })
    ];
    var colour = css('--s1');
    var opts = base(el);
    opts.scales = {
      x: { time: false },
      y: { range: function (u, lo, hi) { var pad = (hi - lo) * 0.08 || 0.1; return [lo - pad, hi + pad]; } }
    };
    opts.series = [
      { label: spec.xname },
      {
        label: spec.yname, stroke: colour, paths: function () { return null; },
        points: { show: true, size: 8, stroke: colour, fill: colour, width: 1 }
      }
    ];
    return new window.uPlot(opts, data, el);
  }

  function draw(el) {
    var spec = parse(el);
    if (!spec || typeof window.uPlot !== 'function') { return null; }
    el.textContent = '';
    return spec.kind === 'scatter' ? scatter(el, spec) : trend(el, spec);
  }

  // Grid columns and shading arrive as data attributes (the CSP forbids style="" in the HTML).
  function layout(root) {
    root.querySelectorAll('.an-cmat').forEach(function (el) {
      var n = parseInt(el.getAttribute('data-cols'), 10) || 2;
      el.style.gridTemplateColumns = 'auto repeat(' + n + ', minmax(0, 1fr))';
    });
    root.querySelectorAll('.an-cmat [data-w]').forEach(function (el) {
      var w = parseFloat(el.getAttribute('data-w'));
      if (isFinite(w)) { el.style.setProperty('--w', String(Math.max(0, Math.min(100, w)))); }
    });
  }

  function init() {
    layout(document);
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    document.querySelectorAll('[data-an-chart]').forEach(function (el) {
      var chart = draw(el);
      if (chart) { charts.push(chart); }
    });
  }

  var timer = null;
  function later() { clearTimeout(timer); timer = setTimeout(init, 60); }

  document.addEventListener('DOMContentLoaded', function () {
    init();
    window.addEventListener('resize', function () {
      charts.forEach(function (c) {
        c.setSize({
          width: Math.max(c.root.parentNode.clientWidth, 200),
          height: c.root.parentNode.clientHeight || 220
        });
      });
    });
    new MutationObserver(later).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', later);
    // Pickers and the feedback toggle apply as soon as they change.
    document.querySelectorAll('form[data-autosubmit]').forEach(function (form) {
      form.addEventListener('change', function () { form.submit(); });
    });
  });
})();
