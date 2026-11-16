/* Datasets UI helpers: the import drop zone, the "Add to dataset" dialog loaded by HTMX, and the
   trace-list selection bar for bulk add. No dependencies; the pages also work without it. */
(function () {
  'use strict';

  // Import step 1: highlight the drop zone, accept dropped files, show the chosen name.
  var zone = document.querySelector('[data-drop-zone]');
  if (zone) {
    var input = zone.querySelector('input[type=file]') || document.getElementById('import-file');
    var label = zone.querySelector('[data-file-name]');
    var show = function () { if (label && input.files.length) { label.textContent = input.files[0].name; } };
    ['dragenter', 'dragover'].forEach(function (name) {
      zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.add('hot'); });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      zone.addEventListener(name, function (e) { e.preventDefault(); zone.classList.remove('hot'); });
    });
    zone.addEventListener('drop', function (e) {
      if (e.dataTransfer && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; show(); }
    });
    input.addEventListener('change', show);
  }

  // The CSRF middleware reads the token from a header or a urlencoded body, not from multipart,
  // so the upload is sent with fetch and the X-CSRF-Token header (like HTMX requests are).
  var upForm = document.querySelector('form[data-import-drop]');
  if (upForm && window.fetch && window.FormData) {
    upForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var match = /(?:^|; )tl_csrf=([^;]*)/.exec(document.cookie);
      fetch(upForm.getAttribute('action'), {
        method: 'POST', body: new FormData(upForm), credentials: 'same-origin',
        headers: { 'X-CSRF-Token': match ? decodeURIComponent(match[1]) : '', 'Accept': 'text/html' }
      }).then(function (res) {
        if (res.redirected) { window.location.href = res.url; return null; }
        return res.text();
      }).then(function (html) {
        if (html === null || html === undefined) { return; }
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById('main'), cur = document.getElementById('main');
        if (fresh && cur) { cur.innerHTML = fresh.innerHTML; } else { window.location.reload(); }
      }).catch(function () { upForm.submit(); });
    });
  }

  // The Add to dataset dialog arrives as an HTMX fragment: open it once it is in the page.
  function openDialogs(root) {
    (root || document).querySelectorAll('dialog[data-autoopen]').forEach(function (d) {
      if (typeof d.showModal === 'function' && !d.open) { d.showModal(); }
    });
  }
  document.addEventListener('htmx:afterSwap', function (e) { openDialogs(e.detail.target); });

  // Trace list: a checkbox per row and a bar that sends the selection to Add to dataset.
  var bar = document.querySelector('[data-bulk-add]');
  if (!bar) { return; }
  var count = bar.querySelector('[data-bulk-count]');
  var go = bar.querySelector('[data-bulk-go]');
  var selected = {};

  function traceId(row) {
    var m = /\/traces\/([^/?#]+)/.exec(row.getAttribute('data-href') || '');
    return m ? decodeURIComponent(m[1]) : '';
  }
  function refresh() {
    var ids = Object.keys(selected);
    bar.hidden = ids.length === 0;
    count.textContent = ids.length + ' selected';
    go.href = bar.getAttribute('data-bulk-add') + '?' + ids.map(function (id) { return 'trace_id=' + encodeURIComponent(id); }).join('&');
  }
  function decorate() {
    document.querySelectorAll('tr[data-row]').forEach(function (row) {
      if (row.querySelector('[data-bulk-box]')) { return; }
      var id = traceId(row);
      if (!id) { return; }
      var cell = document.createElement('td');
      cell.className = 'bulk-cell';
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.setAttribute('data-bulk-box', id);
      box.setAttribute('aria-label', 'Select trace ' + id);
      box.checked = !!selected[id];
      cell.appendChild(box);
      row.insertBefore(cell, row.firstChild);
    });
    document.querySelectorAll('table thead tr').forEach(function (head) {
      if (head.closest('table').querySelector('tr[data-row]') && !head.querySelector('.bulk-head')) {
        var th = document.createElement('th');
        th.className = 'bulk-head';
        th.innerHTML = '<span class="sr">Select</span>';
        head.insertBefore(th, head.firstChild);
      }
    });
  }
  document.addEventListener('click', function (e) {
    var box = e.target.closest('[data-bulk-box]');
    if (box) {
      e.stopPropagation(); // a click on the checkbox must not open the row
      var id = box.getAttribute('data-bulk-box');
      if (box.checked) { selected[id] = true; } else { delete selected[id]; }
      box.closest('tr').classList.toggle('rowsel', box.checked);
      refresh();
    } else if (e.target.closest('[data-bulk-clear]')) {
      selected = {};
      document.querySelectorAll('[data-bulk-box]').forEach(function (b) { b.checked = false; b.closest('tr').classList.remove('rowsel'); });
      refresh();
    }
  }, true);
  document.addEventListener('htmx:afterSwap', decorate);
  decorate();
})();
