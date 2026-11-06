/* Traceloom charts: draws [data-chart] with uPlot and applies bar widths from data attributes.
   External file driven by data attributes, so it works under script-src 'self' with no inline JS. */
(function () {
  'use strict';
  var charts = [];

  function css(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  // Widths and colours arrive as data attributes (the CSP forbids style="" in the HTML itself;
  // setting them through the DOM is allowed).
  function applyBars(root) {
    root.querySelectorAll('[data-w]').forEach(function (el) {
      var w = parseFloat(el.getAttribute('data-w'));
      if (isFinite(w)) { el.style.width = Math.max(0, Math.min(100, w)) + '%'; }
    });
    root.querySelectorAll('[data-px]').forEach(function (el) {
      var px = parseFloat(el.getAttribute('data-px'));
      if (isFinite(px)) { el.style.width = Math.max(2, px) + 'px'; }
    });
    root.querySelectorAll('[data-series]').forEach(function (el) {
      el.style.setProperty('--c', 'var(--s' + el.getAttribute('data-series') + ')');
    });
    root.querySelectorAll('[data-bg]').forEach(function (el) {
      el.style.background = 'var(' + el.getAttribute('data-bg') + ')';
    });
  }

  function parseSpec(el) {
    try { return JSON.parse(el.getAttribute('data-chart')); } catch (e) { return null; }
  }

  function draw(el) {
    var spec = parseSpec(el);
    if (!spec || !spec.labels || !spec.labels.length || typeof window.uPlot !== 'function') { return null; }
    var xs = spec.labels.map(function (l) { return Date.parse(l) / 1000; });
    var data = [xs].concat(spec.series.map(function (s) { return s.values; }));
    var ink = css('--ink-3', '#62666b'), line = css('--line', '#e3e5e8');
    var font = '11px "JetBrains Mono", monospace';
    var axis = { stroke: ink, font: font, grid: { stroke: line, width: 1 }, ticks: { stroke: line, width: 1 } };
    var palette = ['--s1', '--s2', '--s3', '--s4', '--s5', '--s6'];
    var series = [{}].concat(spec.series.map(function (s, i) {
      var colour = css(palette[i % palette.length], '#2a55d8');
      return {
        label: s.name, stroke: colour, fill: colour, width: 1,
        paths: window.uPlot.paths.bars({ size: [0.7, 48] }), points: { show: false }
      };
    }));
    var opts = {
      width: Math.max(el.clientWidth, 200), height: el.clientHeight || 220,
      tzDate: function (ts) { return window.uPlot.tzDate(new Date(ts * 1e3), 'Etc/UTC'); },
      legend: { show: false }, cursor: { points: { show: false }, drag: { x: false, y: false } },
      scales: { x: { time: true } },
      axes: [axis, Object.assign({ size: 48 }, axis)],
      series: series
    };
    el.textContent = '';
    return new window.uPlot(opts, data, el);
  }

  function init() {
    applyBars(document);
    charts.forEach(function (c) { c.destroy(); });
    charts = [];
    document.querySelectorAll('[data-chart]').forEach(function (el) {
      var chart = draw(el);
      if (chart) { charts.push(chart); }
    });
  }

  var timer = null;
  function later() { clearTimeout(timer); timer = setTimeout(init, 60); }

  document.addEventListener('DOMContentLoaded', function () {
    init();
    window.addEventListener('resize', function () {
      charts.forEach(function (c) { c.setSize({ width: Math.max(c.root.parentNode.clientWidth, 200), height: c.root.parentNode.clientHeight || 220 }); });
    });
    // Redraw with the new palette when the theme changes.
    new MutationObserver(later).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', later);
  });
  document.addEventListener('htmx:afterSwap', function (e) { applyBars(e.target); });
})();
