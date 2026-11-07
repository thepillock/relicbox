// Confirmation prompts on dangerous forms
document.addEventListener('submit', e => {
  const msg = e.target.dataset && e.target.dataset.confirm;
  if (msg && !confirm(msg)) e.preventDefault();
});

// Auto-submit photo uploads
document.querySelectorAll('input[data-autosubmit]').forEach(inp =>
  inp.addEventListener('change', () => inp.files.length && inp.form.submit()));

// Item picker: fill the shared datalist from the search API as you type
(function () {
  const list = document.getElementById('item-options');
  if (!list) return;
  let t = null, lastQ = '';
  document.addEventListener('input', e => {
    const inp = e.target;
    if (!inp.classList || !inp.classList.contains('item-picker')) return;
    const qv = inp.value.trim();
    if (qv.length < 2 || qv === lastQ || qv.includes(' — ')) return;
    clearTimeout(t);
    t = setTimeout(async () => {
      lastQ = qv;
      try {
        const r = await fetch('/api/items?q=' + encodeURIComponent(qv));
        const rows = await r.json();
        list.innerHTML = '';
        rows.forEach(row => {
          const o = document.createElement('option');
          o.value = row.label;
          list.appendChild(o);
        });
      } catch (err) { /* ignore */ }
    }, 200);
  });
})();

// Breakdown form: dynamic part rows
(function () {
  const rows = document.getElementById('rows');
  if (!rows) return;
  const tpl = document.getElementById('row-template');
  const sync = row => {
    const sel = row.querySelector('select.outcome');
    row.dataset.outcome = sel.value;
  };
  rows.querySelectorAll('.part-row').forEach(sync);
  rows.addEventListener('change', e => {
    if (e.target.matches('select.outcome')) sync(e.target.closest('.part-row'));
  });
  rows.addEventListener('click', e => {
    if (e.target.matches('.remove-row')) e.target.closest('.part-row').remove();
  });
  document.getElementById('add-row').addEventListener('click', () => {
    const node = tpl.content.firstElementChild.cloneNode(true);
    rows.appendChild(node);
    sync(node);
    const name = node.querySelector('input[name=row_name]');
    if (name) name.focus();
  });
})();

// Dispose form: only show price for sales
(function () {
  const radios = document.querySelectorAll('input[name=kind]');
  if (!radios.length) return;
  const priceLbl = document.querySelector('.show-sold');
  const sync = () => {
    const v = document.querySelector('input[name=kind]:checked').value;
    if (priceLbl) priceLbl.style.display = v === 'sold' ? '' : 'none';
  };
  radios.forEach(r => r.addEventListener('change', sync));
  sync();
})();

// Inline table editing for work log, valuations and parts taken off
(function () {
  const showTable = sec => { const w = sec.querySelector('.tablewrap'); if (w) w.hidden = false; };
  const syncOutcome = row => {
    const sel = row.querySelector('select.outcome');
    if (!sel) return;
    row.dataset.outcome = sel.value;
    const target = row.querySelector('.d-target');
    if (target) target.required = sel.value === 'repair';
    const amt = row.querySelector('.d-amount');
    if (amt) {
      amt.placeholder = (document.body.dataset.currency || '') + ' price';
      amt.title = sel.value === 'repair' ? 'Internal price (optional): counts as income for the donor and a cost for the repaired item'
                                         : 'What it sold for';
    }
  };
  function openAdd(sec) {
    const row = sec.querySelector('tr.new-row');
    const btn = sec.querySelector('[data-add]');
    if (!row) return;
    showTable(sec);
    row.hidden = false;
    if (btn) btn.hidden = true;
    syncOutcome(row);
    const first = row.querySelector('input:not([type=date]), select');
    if (first) first.focus();
  }
  document.addEventListener('click', e => {
    const t = e.target;
    if (t.matches('[data-add]')) { openAdd(t.closest('.ledger-section')); }
    else if (t.matches('[data-add-for]')) {
      const sec = document.getElementById(t.dataset.addFor);
      if (sec) { e.preventDefault(); sec.scrollIntoView({ behavior: 'smooth', block: 'start' }); openAdd(sec); }
    } else if (t.matches('[data-edit]')) {
      const row = t.closest('tr');
      row.hidden = true;
      row.nextElementSibling.hidden = false;
      const first = row.nextElementSibling.querySelector('input:not([type=date])');
      if (first) first.focus();
    } else if (t.matches('[data-cancel]')) {
      const row = t.closest('tr'), sec = t.closest('.ledger-section');
      row.hidden = true;
      if (row.classList.contains('edit-row')) { row.previousElementSibling.hidden = false; return; }
      const btn = sec.querySelector('[data-add]');
      if (btn) btn.hidden = false;
      if (!sec.querySelector('tr.view-row')) sec.querySelector('.tablewrap').hidden = true;
    }
  });
  document.addEventListener('change', e => {
    if (e.target.matches('tr.new-row select.outcome')) syncOutcome(e.target.closest('tr'));
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      const row = e.target.closest && e.target.closest('tr.new-row, tr.edit-row');
      if (row) { const c = row.querySelector('[data-cancel]'); if (c) c.click(); }
    }
  });
  if (location.hash === '#add-part') { const s = document.getElementById('parts'); if (s) openAdd(s); }
})();
