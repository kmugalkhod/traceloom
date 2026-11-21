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

  // Horizontal gridlines only, no tick marks, as in the design.
  function axes() {
    var axis = { stroke: css('--ink-3'), font: '11px ' + css('--font-ui'), ticks: { show: false } };
    return [
      Object.assign({ grid: { show: false }, gap: 8, size: 28 }, axis),
      Object.assign({ grid: { stroke: css('--line'), width: 1 }, size: 44, gap: 8 }, axis)
    ];
  }

  function base(el) {
    return {
      width: Math.max(el.clientWidth, 200), height: el.clientHeight || 200,
      legend: { show: false }, cursor: { points: { show: false }, drag: { x: false, y: false } },
      axes: axes()
    };
  }

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var DAY = 86400;
  var HOUR = 3600;

  function tickLabel(ts, hourly) {
    var d = new Date(ts * 1e3);
    if (hourly) { return String(d.getUTCHours()).padStart(2, '0') + ':00'; }
    return MONTHS[d.getUTCMonth()] + ' ' + d.getUTCDate();
  }

  // Show a dot only where a value has no neighbour to join to, so a lone bucket stays visible.
  function isolated(u, si) {
    var ys = u.data[si];
    var out = [];
    for (var i = 0; i < ys.length; i++) {
      if (ys[i] != null && ys[i - 1] == null && ys[i + 1] == null) { out.push(i); }
    }
    return out;
  }

  function trend(el, spec) {
    var xs = spec.labels.map(function (l) { return Date.parse(l) / 1000; });
    var data = [xs].concat(spec.series.map(function (s) { return s.values; }));
    var hourly = xs.length > 1 && xs[1] - xs[0] < DAY;
    var opts = base(el);
    opts.scales = {
      x: { time: false, range: function () { var a = xs[0], b = xs[xs.length - 1]; return a === b ? [a - DAY / 2, b + DAY / 2] : [a, b]; } },
      y: { range: function (u, lo, hi) { return [Math.min(0, lo), hi <= 1 ? 1 : hi * 1.1]; } }
    };
    // Whole-day (or hour) ticks, labelled "Nov 3" (or "14:00").
    opts.axes[0].incrs = hourly ? [HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR] : [DAY, 2 * DAY, 7 * DAY, 14 * DAY, 28 * DAY];
    opts.axes[0].space = 64;
    opts.axes[0].values = function (u, splits) { return splits.map(function (v) { return tickLabel(v, hourly); }); };
    opts.axes[1].incrs = [0.05, 0.1, 0.25, 0.5, 1, 2, 2.5, 5, 10, 25, 50, 100];
    opts.axes[1].values = function (u, splits) { return splits.map(function (v) { return String(+v.toFixed(2)); }); };
    opts.series = [{}].concat(spec.series.map(function (s) {
      var look = SOURCE[s.source] || SOURCE.API;
      return {
        label: s.name, stroke: css(look.colour), width: 2.5, dash: look.dash, spanGaps: true,
        points: { show: true, size: 6, fill: css(look.colour), stroke: css(look.colour), filter: isolated }
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
          height: c.root.parentNode.clientHeight || 200
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
