'use strict';

const $ = (sel) => document.querySelector(sel);
const state = { page: 1, query: '', hasNext: false, settings: {}, tick: 0, running: false };

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

function fmtDate(value) {
  if (!value) return '';
  const seconds = typeof value === 'number' ? value : (value._seconds ?? value.seconds);
  if (!seconds) return '';
  const d = new Date(seconds * 1000);
  return Number.isNaN(d.getTime()) ? '' : d.toISOString().slice(0, 10);
}

async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(message, kind = '') {
  const el = $('#toast');
  el.textContent = message;
  el.className = `toast show ${kind}`.trim();
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.className = 'toast'; }, 4200);
}

// ---------------------------------------------------------------- status
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    state.settings = s.settings;
    const pill = $('#lazer-pill');
    const l = s.lazer;
    const autoClose = s.settings.close_lazer_before_import !== false;
    const version = l.version ? ` ${l.version}` : '';
    if (l.exe && l.running) {
      pill.className = autoClose ? 'pill pill-warn' : 'pill pill-bad';
      pill.innerHTML = `<span class="dot ${autoClose ? 'warn' : 'bad'}"></span>lazer running`;
      pill.title = autoClose
        ? `osu!lazer${version} is open — it is closed automatically before an import`
        : `osu!lazer${version} is open — imports need it closed and auto-close is off (Settings)`;
    } else if (l.exe) {
      pill.className = 'pill pill-ok';
      pill.innerHTML = '<span class="dot ok"></span>lazer ready';
      pill.title = `osu!lazer${version} found, not running`;
    } else {
      pill.className = 'pill pill-bad';
      pill.innerHTML = '<span class="dot bad"></span>lazer not found';
      pill.title = 'osu!lazer was not found — set its path in Settings';
    }
    $('#lib-dir').textContent = s.download_dir;
  } catch (e) {
    const pill = $('#lazer-pill');
    pill.className = 'pill pill-bad';
    pill.innerHTML = '<span class="dot bad"></span>server unreachable';
    pill.title = e.message;
  }
}

// ---------------------------------------------------------------- search
function renderResults(data) {
  const box = $('#results');
  state.hasNext = !!data.has_next;
  if (!data.results.length) {
    box.innerHTML = `<p class="muted">No matches for “${esc(data.query)}”.</p>`;
    return;
  }
  box.innerHTML = data.results
    .map(
      (r) => `<div class="item">
        <div class="title">${esc(r.name)}</div>
        <div class="meta">id ${r.id}${r.favourites ? ` · ★ ${r.favourites}` : ''}</div>
        ${r.snippet ? `<div class="meta clamp2">${esc(r.snippet.slice(0, 130))}</div>` : ''}
        <div class="actions">
          <button class="small" data-fetch="${r.id}">Open</button>
          <button class="small ghost" data-download="${r.id}">Download</button>
          <a class="link" href="${esc(r.url)}" target="_blank" rel="noreferrer" title="open on osu!collector">↗</a>
        </div>
      </div>`
    )
    .join('');
}

async function doSearch(page = 1) {
  const q = $('#q').value.trim();
  const sort = $('#sort').value;
  if (!q) return;
  state.query = q;
  state.page = page;
  $('#results').innerHTML = '<p class="muted">searching…</p>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}&page=${page}${sort ? `&sort=${sort}` : ''}`);
    renderResults(data);
    const pager = $('#pager');
    pager.classList.remove('hidden');
    $('#page-label').textContent = `page ${page}`;
    $('#btn-prev').disabled = page <= 1;
    $('#btn-next').disabled = !data.has_next;
  } catch (e) {
    $('#results').innerHTML = `<div class="warn">search failed: ${esc(e.message)}</div>`;
  }
}

async function fetchRef(ev) {
  const value = typeof ev === 'string' ? ev : $('#ref').value.trim();
  if (!value) return;
  $('#detail').classList.remove('hidden');
  $('#detail').innerHTML = '<p class="muted">loading…</p>';
  try {
    const c = await api(`/api/collection?ref=${encodeURIComponent(value)}`);
    showDetail(c);
  } catch (e) {
    $('#detail').innerHTML = `<div class="warn">could not load: ${esc(e.message)}</div>`;
  }
}

function showDetail(c) {
  const modes = Object.entries(c.modes || {})
    .filter(([, v]) => v)
    .map(([k, v]) => `<span class="chip">${esc(k)} ${v}</span>`)
    .join('');
  const updated = fmtDate(c.date_modified);
  $('#detail').innerHTML = `
    <h3>${esc(c.name)}</h3>
    <div class="muted">by ${esc(c.uploader || 'unknown')} · id ${c.id}${updated ? ` · updated ${updated}` : ''}</div>
    <div class="kv">
      <div><b>${c.set_count}</b><span>beatmapsets</span></div>
      <div><b>${c.checksum_count}</b><span>difficulties</span></div>
      <div><b>${c.favourites ?? 0}</b><span>favourites</span></div>
      ${c.unsubmitted ? `<div><b>${c.unsubmitted}</b><span>not on osu! servers</span></div>` : ''}
      ${c.unknown ? `<div><b>${c.unknown}</b><span>unknown checksums</span></div>` : ''}
    </div>
    ${modes ? `<div class="chips">${modes}</div>` : ''}
    ${c.description ? `<p class="muted clamp3" style="margin:11px 0 0">${esc(c.description.slice(0, 400))}</p>` : ''}
    <div class="row" style="margin-top:13px">
      <button id="btn-dl">Download ${c.set_count} sets${state.settings.no_video ? ' (no video)' : ''}</button>
      <button id="btn-site" class="ghost">osu!collector ↗</button>
    </div>
    <div class="chips" style="margin-top:11px">
      <span class="chip ghosty">direct import</span>
      <span class="chip ghosty">in parallel</span>
      <span class="chip ghosty">archives deleted as they land</span>
      <span class="chip ghosty">game closed if open</span>
    </div>`;
  $('#btn-site').onclick = () => window.open(`https://osucollector.com/collections/${c.id}`, '_blank', 'noreferrer');
  $('#btn-dl').onclick = async () => {
    $('#btn-dl').disabled = true;
    $('#btn-dl').textContent = 'starting…';
    try {
      await api('/api/download', { ref: String(c.id) });
      refreshJobs();
    } catch (e) {
      toast(e.message, 'bad');
      $('#btn-dl').disabled = false;
      $('#btn-dl').textContent = 'retry download';
    }
  };
}

// ---------------------------------------------------------------- jobs
function renderJobs(jobs) {
  const box = $('#jobs');
  const active = jobs.filter((j) => j.status === 'running' || j.status === 'pending').length;
  state.running = active > 0;
  $('#job-count').textContent = active ? `· ${active} running` : jobs.length ? '' : '· nothing yet';
  if (!jobs.length) {
    box.innerHTML = '<p class="muted">No downloads yet.</p>';
    return;
  }
  box.innerHTML = jobs
    .map((j) => {
      const total = j.total || 1;
      const done = j.done || 0;
      const pct = Math.min(100, Math.round((done / total) * 100));
      const dot = { done: 'ok', partial: 'warn', failed: 'bad' }[j.status] || '';
      const word = { done: 'done', partial: 'partial', failed: 'failed', running: 'running', pending: 'queued', cancelled: 'cancelled' }[j.status] || j.status;
      const chips = [];
      if (j.kind === 'download') {
        chips.push(`<span class="chip">${done}/${total} sets</span>`);
        if (j.ok) chips.push(`<span class="chip ok">${j.ok} ok</span>`);
        if (j.skipped) chips.push(`<span class="chip ghosty">${j.skipped} cached</span>`);
        if (j.failed) chips.push(`<span class="chip bad">${j.failed} failed</span>`);
        if (j.bytes) chips.push(`<span class="chip ghosty">${fmtBytes(j.bytes)}</span>`);
        if (j.speed) chips.push(`<span class="chip ghosty">${fmtBytes(j.speed)}/s</span>`);
      } else {
        if (j.imported) chips.push(`<span class="chip ok">${j.imported} imported</span>`);
        if (j.deleted) chips.push(`<span class="chip ghosty">${j.deleted} archives deleted</span>`);
        if (j.freed) chips.push(`<span class="chip ghosty">${fmtBytes(j.freed)} freed</span>`);
        if (j.failed) chips.push(`<span class="chip bad">${j.failed} failed</span>`);
      }
      const logLines = (j.log || []).map(esc);
      const errors = j.errors && Object.keys(j.errors).length
        ? Object.entries(j.errors).map(([k, v]) => `${esc(k)}: ${esc(v)}`)
        : [];
      const detail = errors.length || logLines.length
        ? `<details class="more">
             <summary>${errors.length ? `error log (${errors.length})` : `log (${logLines.length})`}</summary>
             ${errors.length ? `<div class="log">${errors.join('\n')}</div>` : ''}
             ${logLines.length ? `<div class="log">${logLines.slice(-40).join('\n')}</div>` : ''}
           </details>`
        : '';
      const actions = j.status === 'running' ? `<button class="small ghost" data-cancel="${j.id}">cancel</button>` : '';
      const footer = j.status !== 'running' && j.folder
        ? `<div class="row" style="margin:10px 0 0;gap:6px">
             <button class="small" data-finalize="${esc(j.folder)}" title="import the maps into lazer in parallel, delete each archive as it lands, then write the collection">import now</button>
             <button class="small ghost" data-writecoll="${esc(j.folder)}" title="write the collection entry straight into lazer's database">collection</button>
             <button class="small ghost" data-open="${esc(j.folder)}">folder</button>
           </div>`
        : '';
      return `<div class="job">
        <div class="job-head">
          <div class="job-title">
            <span class="dot ${dot}" title="${esc(j.status)}"></span>
            <b>${esc(j.name || j.folder)}</b>
            <span class="muted">${j.kind} · ${esc(word)} · ${j.elapsed ?? 0}s</span>
          </div>
          <div>${actions}</div>
        </div>
        <div class="bar${j.kind === 'import' ? ' push' : ''}"><i style="width:${pct}%"></i></div>
        <div class="chips">${chips.join('')}</div>
        ${j.message ? `<div class="muted" style="margin-top:7px">${esc(j.message)}</div>` : ''}
        ${detail}
        ${footer}
      </div>`;
    })
    .join('');
}

async function refreshJobs() {
  try {
    const data = await api('/api/jobs');
    renderJobs(data.jobs || []);
  } catch (e) { /* server restarting */ }
}

// ---------------------------------------------------------------- library
function renderLibrary(cols) {
  const box = $('#library');
  state.library = cols || [];
  if (!cols.length) {
    box.innerHTML = '<p class="muted">Nothing downloaded yet.</p>';
    return;
  }
  box.innerHTML = `<div class="table-wrap"><table><thead><tr>
      <th>collection</th><th>maps</th><th>collection entry</th><th></th>
    </tr></thead><tbody>
    ${cols
      .map((c) => {
        const maps = c.maps_imported ? `${c.maps_imported} imported` : 'not imported';
        const deleted = c.maps_deleted
          ? `<div class="muted">${c.maps_deleted} deleted · ${fmtBytes(c.maps_freed)}</div>`
          : '';
        const coll = c.collection_in_lazer
          ? `<span class="chip ok"><span class="dot ok"></span>in lazer</span><div class="muted">${esc(Object.keys(c.collection_in_lazer).join(', '))}</div>`
          : c.collection_imported ? '<span class="chip ok">imported</span>'
            : c.has_collection_db ? '<span class="chip warn">ready</span>' : '<span class="muted">—</span>';
        return `<tr>
      <td><b>${esc(c.name)}</b><div class="muted">${c.collection_id ? `id ${c.collection_id} · ` : ''}${fmtBytes(c.size_bytes)}</div></td>
      <td>${c.beatmapsets}${c.expected_sets ? ` / ${c.expected_sets}` : ''}<div class="muted">${maps}</div>${deleted}</td>
      <td>${coll}</td>
      <td><div class="row">
        <button class="small" data-finalize="${esc(c.folder)}" title="import the maps into lazer in parallel, delete each archive as it lands, then write the collection">import</button>
        <button class="small ghost" data-writecoll="${esc(c.folder)}" title="write the collection entry straight into lazer's database">collection</button>
        <button class="small ghost" data-open="${esc(c.folder)}">folder</button>
      </div></td>
    </tr>`;
      })
      .join('')}
    </tbody></table></div>`;
}

async function refreshLibrary() {
  try {
    const data = await api('/api/library');
    renderLibrary(data.collections || []);
    renderPipelineHint(data.pipeline || {});
  } catch (e) { /* ignore */ }
}

function renderPipelineHint(p) {
  const box = $('#lib-pipeline');
  if (!box) return;
  const chips = [];
  if (p.pipeline_mode !== 'auto') {
    chips.push('<span class="chip ghosty">download only</span>');
    chips.push('<span class="chip ghosty">press “import” to put a collection into lazer</span>');
    box.innerHTML = chips.join('');
    return;
  }
  chips.push('<span class="chip ghosty">one click: download → import → collection</span>');
  if (!p.stream_import) chips.push('<span class="chip ghosty">imports start after the download</span>');
  if (p.close_lazer_before_import) chips.push('<span class="chip ghosty">osu!lazer closed automatically when in the way</span>');
  if (p.collection_mode !== 'database') chips.push(`<span class="chip ghosty">collection entries: ${esc(p.collection_mode)}</span>`);
  if (p.lazerdb && p.lazerdb.available === false) {
    chips.push(`<span class="chip bad" title="${esc(p.lazerdb.reason || '')}">direct import unavailable — run tools\\build.bat</span>`);
  }
  box.innerHTML = chips.join('');
}

// ---------------------------------------------------------------- delete all
async function runPurge(body) {
  const out = $('#purge-out');
  out.textContent = 'deleting…';
  try {
    const res = await api('/api/library/delete', body);
    const msg = `deleted ${res.removed} item(s), freed ${fmtBytes(res.freed)}`;
    if (res.leftover && res.leftover.length) {
      out.textContent = '';
      toast(`${msg} — ${res.leftover.length} still in use (close lazer and retry)`, 'bad');
    } else {
      hideModal();
      toast(msg, 'ok');
    }
    const all = document.getElementById('purge-all');
    if (all) all.disabled = true;
    refreshLibrary();
    refreshStatus();
  } catch (e) {
    out.textContent = e.message;
  }
}

async function purgeModal() {
  let dry;
  try {
    dry = await api('/api/library/delete', { scope: 'all', dry_run: true });
  } catch (e) {
    return toast(e.message, 'bad');
  }
  showModal(
    'Delete downloads',
    `<div><code class="path">${esc(dry.download_dir)}</code></div>
     <div class="chips" style="margin:12px 0">
       <span class="chip">${dry.removed} item(s)</span>
       <span class="chip">${fmtBytes(dry.freed)} on disk</span>
     </div>
     <div class="muted">Beatmaps already in lazer stay; settings are kept. Not reversible — .osz files would have to be downloaded again.</div>
     <div class="row right" style="margin-top:13px">
       <button id="purge-all" class="danger-solid small">delete everything (${fmtBytes(dry.freed)})</button>
     </div>
     <div id="purge-out" class="muted"></div>`
  );
  $('#purge-all').onclick = () => runPurge({ scope: 'all' });
}

// ---------------------------------------------------------------- modals
function showModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
function hideModal() { $('#modal').classList.add('hidden'); }

function howModal() {
  showModal(
    'How it works',
    `<ol class="steps">
       <li><b>Find</b> — search osu!collector, or paste a collection link.</li>
       <li><b>Download</b> — mirrors race, every archive is verified and cached.</li>
       <li><b>Import</b> — lazer's own importer takes the maps in parallel, and each archive is deleted as it lands.</li>
       <li><b>Collection</b> — the entry is written into lazer's database, so it is in the game when you next open it.</li>
     </ol>
     <div class="muted">osu!lazer is closed automatically when it is in the way, and nothing to do in-game either way.</div>`
  );
}

function settingsModal() {
  const s = state.settings;
  const pipeline = s.pipeline_mode || 'auto';
  const collMode = s.collection_mode || 'database';
  const mirrors = ['nerinyan', 'beatconnect', 'catboy', 'osu.direct', 'sayobot', 'nekoha', 'osudl', 'hinamizawa', 'nzbasic'];
  const check = (id, label, checked, hint) =>
    `<label class="check" title="${esc(hint)}"><input id="${id}" type="checkbox" ${checked ? 'checked' : ''}><span>${label}</span></label>`;
  showModal(
    'Settings',
    `<div class="group" style="border-top:0;padding-top:0;margin-top:0">
       <div class="group-title">Downloads</div>
       <label class="field" title="where collections are downloaded"><span>folder</span><input id="s-dir" type="text" value="${esc(s.download_dir)}"></label>
       <label class="field" title="mirrors throttle above ~12"><span>parallel downloads</span><input id="s-conc" type="text" value="${esc(s.concurrency)}"></label>
       ${check('s-novideo', 'skip video', s.no_video, 'download without video — smaller files')}
       ${check('s-verify', 'verify every archive', s.verify_zips, 'slower, catches bad mirrors early')}
     </div>
     <div class="group">
       <div class="group-title">Import</div>
       <label class="field" title="what happens when a download finishes"><span>after a download</span>
         <select id="s-pipeline">
           <option value="auto" ${pipeline === 'auto' ? 'selected' : ''}>import and add the collection</option>
           <option value="manual" ${pipeline === 'manual' ? 'selected' : ''}>download only</option>
         </select></label>
       <div class="hint">imports write straight into lazer's files — osu!lazer is closed automatically when it is open.</div>
       <label class="field" title="where the collection entry goes"><span>collection entry</span>
         <select id="s-collmode">
           <option value="database" ${collMode === 'database' ? 'selected' : ''}>into lazer's database</option>
           <option value="off" ${collMode === 'off' ? 'selected' : ''}>don't add collections</option>
         </select></label>
       <label class="field" title="how many maps one import wave hands to lazer"><span>maps per wave</span><input id="s-chunk" type="text" value="${esc(s.import_chunk ?? 250)}"></label>
       ${check('s-stream', 'import while downloading', s.stream_import, 'start importing waves while the download is still running — hides the import time inside the transfer')}
       ${check('s-close', 'close osu!lazer automatically', s.close_lazer_before_import, 'imports write straight into its files, so the game must not be open')}
       <label class="field" title="prefix for collection names in lazer"><span>collection name prefix</span><input id="s-prefix" type="text" value="${esc(s.collection_prefix || '')}" placeholder="none"></label>
     </div>
     <div class="group">
       <div class="group-title">osu!lazer</div>
       <label class="field" title="empty = find it automatically"><span>executable</span><input id="s-exe" type="text" value="${esc(s.lazer_exe || '')}" placeholder="auto-detect"></label>
     </div>
     <div class="group">
       <div class="group-title">Mirrors <button id="s-probe" class="small ghost">probe</button></div>
       <div class="mirrors">${mirrors.map((m) => `<label class="check" title="${m} — used for downloads when checked"><input type="checkbox" data-mirror="${m}" ${(s.mirrors || []).includes(m) ? 'checked' : ''}><span>${m}</span></label>`).join('')}</div>
       <div id="s-probe-out" class="chips"></div>
     </div>
     <div class="row right"><button id="s-save">Save</button></div>`
  );
  $('#s-probe').onclick = async () => {
    $('#s-probe-out').innerHTML = '<span class="chip ghosty">probing…</span>';
    try {
      const res = await api('/api/mirrors/probe', {});
      $('#s-probe-out').innerHTML = res.order
        .map((o) => `<span class="chip${o.ms ? '' : ' bad'}">${esc(o.name)} ${o.ms ? `${o.ms} ms` : 'n/a'}</span>`)
        .join('');
    } catch (e) { $('#s-probe-out').innerHTML = `<span class="chip bad">${esc(e.message)}</span>`; }
  };
  $('#s-save').onclick = async () => {
    const enabled = [...document.querySelectorAll('[data-mirror]')].filter((el) => el.checked).map((el) => el.dataset.mirror);
    const patch = {
      download_dir: $('#s-dir').value.trim(),
      concurrency: Math.max(1, parseInt($('#s-conc').value, 10) || 10),
      pipeline_mode: $('#s-pipeline').value,
      collection_mode: $('#s-collmode').value,
      import_chunk: Math.max(1, parseInt($('#s-chunk').value, 10) || 250),
      stream_import: $('#s-stream').checked,
      close_lazer_before_import: $('#s-close').checked,
      lazer_exe: $('#s-exe').value.trim(),
      collection_prefix: $('#s-prefix').value,
      no_video: $('#s-novideo').checked,
      verify_zips: $('#s-verify').checked,
      mirrors: enabled,
    };
    try {
      const res = await api('/api/settings', { settings: patch });
      state.settings = res.settings;
      hideModal();
      toast('settings saved', 'ok');
      refreshStatus();
      refreshLibrary();
    } catch (e) { toast(e.message, 'bad'); }
  };
}

// ---------------------------------------------------------------- wiring
document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-fetch],[data-download],[data-open],[data-cancel],[data-finalize],[data-writecoll]');
  if (!t) return;
  try {
    if (t.dataset.fetch) return void fetchRef(t.dataset.fetch);
    if (t.dataset.download) {
      t.disabled = true; t.textContent = 'starting…';
      await api('/api/download', { ref: t.dataset.download });
      return void refreshJobs();
    }
    if (t.dataset.finalize) {
      t.disabled = true; t.textContent = 'working…';
      await api('/api/finalize', { folder: t.dataset.finalize });
      return void refreshJobs();
    }
    if (t.dataset.writecoll) {
      t.disabled = true; t.textContent = 'writing…';
      const res = await api('/api/collection/write', { folder: t.dataset.writecoll });
      t.textContent = 'collection ✔';
      const changed = res.changed ? Object.keys(res.changed).length : 0;
      toast(changed ? `collection added to lazer (${changed} mode(s))` : 'collection already up to date', 'ok');
      return void refreshLibrary();
    }
    if (t.dataset.open) return void api('/api/open', { folder: t.dataset.open });
    if (t.dataset.cancel) {
      await api('/api/jobs/cancel', { id: t.dataset.cancel });
      return void refreshJobs();
    }
  } catch (e) {
    toast(e.message, 'bad');
  }
});

$('#btn-search').onclick = () => doSearch(1);
$('#q').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(1); });
$('#btn-next').onclick = () => doSearch(state.page + 1);
$('#btn-prev').onclick = () => doSearch(Math.max(1, state.page - 1));
$('#btn-fetch').onclick = () => fetchRef();
$('#ref').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchRef(); });
$('#btn-settings').onclick = settingsModal;
$('#btn-how').onclick = howModal;
$('#btn-purge').onclick = purgeModal;
$('#modal-close').onclick = hideModal;
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') hideModal(); });

async function boot() {
  await refreshStatus();
  await refreshJobs();
  await refreshLibrary();
  const params = new URLSearchParams(location.search);
  if (params.get('collection')) fetchRef(params.get('collection'));
}
boot();
setInterval(async () => {
  state.tick++;
  await refreshJobs();
  if (state.tick % 4 === 0) { await refreshStatus(); await refreshLibrary(); }
}, 1500);
