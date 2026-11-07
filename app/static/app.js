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
