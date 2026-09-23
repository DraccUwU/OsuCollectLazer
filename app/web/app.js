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

async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ---------------------------------------------------------------- status
async function refreshStatus() {
  try {
    const s = await api('/api/status');
    state.settings = s.settings;
    const pill = $('#lazer-pill');
    const l = s.lazer;
    if (l.exe && l.running) {
      pill.className = 'pill pill-ok';
      pill.textContent = `lazer ${l.version || ''} running — imports land live`;
    } else if (l.exe) {
      pill.className = 'pill pill-bad';
      pill.textContent = `lazer ${l.version || ''} not running`;
    } else {
      pill.className = 'pill pill-bad';
      pill.textContent = 'lazer not found';
    }
    $('#lib-dir').textContent = s.download_dir;
  } catch (e) {
    $('#lazer-pill').className = 'pill pill-bad';
    $('#lazer-pill').textContent = 'server unreachable';
  }
}

// ---------------------------------------------------------------- search
function renderResults(data) {
  const box = $('#results');
  state.hasNext = !!data.has_next;
  if (!data.results.length) {
    box.innerHTML = `<p class="muted">No collections matched “${esc(data.query)}”.</p>`;
    return;
  }
  box.innerHTML = data.results
    .map(
      (r) => `<div class="item">
        <div class="title">${esc(r.name)}</div>
        <div class="meta">id ${r.id}${r.favourites ? ` · ★ ${r.favourites}` : ''}</div>
        <div class="meta">${esc((r.snippet || '').slice(0, 130))}</div>
        <div class="actions">
          <button class="small" data-fetch="${r.id}">Open</button>
          <button class="small ghost" data-download="${r.id}">Download</button>
          <a class="small" style="color:var(--muted);align-self:center" href="${esc(r.url)}" target="_blank" rel="noreferrer">site ↗</a>
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
  $('#detail').innerHTML = '<p class="muted">loading collection…</p>';
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
    .map(([k, v]) => `${k}: ${v}`)
    .join(' · ');
  $('#detail').innerHTML = `
    <h3>${esc(c.name)}</h3>
    <div class="muted">by ${esc(c.uploader || 'unknown')} · id ${c.id}
      ${c.date_modified ? ` · updated ${esc(String(c.date_modified).slice(0, 10))}` : ''}</div>
    <div class="kv">
      <div><b>${c.set_count}</b><span>beatmapsets</span></div>
      <div><b>${c.checksum_count}</b><span>difficulties</span></div>
      <div><b>${c.favourites ?? 0}</b><span>favourites</span></div>
      ${c.unsubmitted ? `<div><b>${c.unsubmitted}</b><span>not on osu! servers</span></div>` : ''}
      ${c.unknown ? `<div><b>${c.unknown}</b><span>unknown checksums</span></div>` : ''}
    </div>
    ${modes ? `<div class="muted">${esc(modes)}</div>` : ''}
    ${c.description ? `<p class="muted">${esc(c.description.slice(0, 400))}</p>` : ''}
    <div class="row" style="margin-top:12px">
      <button id="btn-dl">Download ${c.set_count} sets${state.settings.no_video ? ' (no video)' : ''}</button>
      <a href="https://osucollector.com/collections/${c.id}" target="_blank" rel="noreferrer"><button class="ghost">Open on osu!collector</button></a>
    </div>
    <div class="note">Maps are pushed into your running osu!lazer automatically when the download finishes. The collection entry then needs one trip through lazer's import screen — the app shows you exactly what to click.</div>`;
  $('#btn-dl').onclick = async () => {
    $('#btn-dl').disabled = true;
    $('#btn-dl').textContent = 'starting…';
    try {
      await api('/api/download', { ref: String(c.id) });
      refreshJobs();
    } catch (e) {
      alert(`download failed to start: ${e.message}`);
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
    box.innerHTML = '<p class="muted">No downloads yet. Search above, then hit Download.</p>';
    return;
  }
  box.innerHTML = jobs
    .map((j) => {
      const total = j.total || 1;
      const done = j.done || 0;
      const pct = Math.min(100, Math.round((done / total) * 100));
      const speed = j.speed ? ` · ${fmtBytes(j.speed)}/s` : '';
      const counts = j.kind === 'download'
        ? `${done}/${total} sets · ${j.ok || 0} ok · ${j.skipped || 0} cached · ${j.failed || 0} failed · ${fmtBytes(j.bytes)}${speed}`
        : j.kind === 'prepare'
          ? `${done}/${total} maps unpacked · ${fmtBytes(j.bytes)}`
          : j.kind === 'import'
            ? `${j.imported || 0} imported · ${j.deleted || 0} files deleted · ${fmtBytes(j.freed || 0)} freed${j.failed ? ` · ${j.failed} failed` : ''}`
            : `${done}/${total} files · ${j.pushed || 0} pushed · ${j.failed || 0} failed`;
      const errors = j.errors && Object.keys(j.errors).length
        ? `<div class="log">${Object.entries(j.errors).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join('\n')}</div>`
        : '';
      const statusColour = { done: 'var(--green)', partial: 'var(--yellow)', failed: 'var(--red)', running: 'var(--muted)', pending: 'var(--muted)', cancelled: 'var(--muted)' }[j.status] || 'var(--muted)';
      const actions = j.status === 'running' ? `<button class="small ghost" data-cancel="${j.id}">cancel</button>` : '';
      const footer = j.status !== 'running' && j.folder
        ? `<div class="row" style="margin:8px 0 0">
             <button class="small" data-finalize="${esc(j.folder)}" title="import the maps, delete them once lazer confirms, and write the collection — no in-game steps">finish in lazer</button>
             <button class="small ghost" data-writecoll="${esc(j.folder)}" title="write the collection entry straight into lazer's database">write collection</button>
             <button class="small ghost" data-steps="${esc(j.import_folder || j.folder)}">import steps</button>
             ${j.kind === 'download' ? `
               <button class="small ghost" data-push="${esc(j.folder)}" title="hand the .osz files to the running game now">push maps</button>
               <button class="small ghost" data-batch="${esc(j.folder)}" title="unpack the maps so lazer's import screen takes them all in ONE task">stage</button>` : ''}
             ${j.kind === 'prepare' || j.kind === 'import' ? `<button class="small ghost" data-cleanup="${esc(j.folder)}">free space</button>` : ''}
           </div>`
        : '';
      return `<div class="job">
        <div class="job-head">
          <div><b>${esc(j.name || j.folder)}</b> <span class="muted">${j.kind} · <span style="color:${statusColour}">${esc(j.status)}</span> · ${j.elapsed ?? 0}s</span></div>
          <div>${actions}</div>
        </div>
        <div class="bar ${j.kind === 'push' ? 'push' : ''}"><i style="width:${pct}%"></i></div>
        <div class="muted">${esc(counts)}${j.message ? ` — ${esc(j.message)}` : ''}</div>
        ${j.log && j.log.length ? `<div class="log">${j.log.slice(-6).map(esc).join('\n')}</div>` : ''}
        ${errors}
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
  box.innerHTML = `<table><thead><tr>
      <th>collection</th><th>maps</th><th>lazer</th><th>collection</th><th></th>
    </tr></thead><tbody>
    ${cols
      .map((c) => {
        const staged = c.batch && c.batch.maps
          ? `<div class="muted">${c.batch.maps} staged</div>`
          : '';
        const maps = c.maps_imported
          ? `${c.maps_imported} imported${c.maps_deleted ? `<div class="muted">${c.maps_deleted} deleted · ${fmtBytes(c.maps_freed)} freed</div>` : ''}`
          : `${c.maps_pushed} pushed${c.maps_push_failed ? ` <span style="color:var(--red)">(+${c.maps_push_failed} failed)</span>` : ''}`;
        const coll = c.collection_in_lazer
          ? `<span style="color:var(--green)">in lazer</span><div class="muted">${esc(Object.keys(c.collection_in_lazer).join(', '))}</div>`
          : c.collection_imported ? '<span style="color:var(--green)">imported</span>'
            : c.has_collection_db ? '<span style="color:var(--yellow)">ready</span>' : '<span class="muted">—</span>';
        return `<tr>
      <td><b>${esc(c.name)}</b><div class="muted">${c.collection_id ? `id ${c.collection_id} · ` : ''}${fmtBytes(c.size_bytes)}</div></td>
      <td>${c.beatmapsets}${c.expected_sets ? ` / ${c.expected_sets}` : ''} .osz<div class="muted">${maps}</div></td>
      <td>${staged || '<span class="muted">—</span>'}</td>
      <td>${coll}</td>
      <td><div class="row" style="margin:0">
        <button class="small" data-finalize="${esc(c.folder)}" title="import the maps, delete them once lazer confirms, write the collection — no in-game steps">finish in lazer</button>
        <button class="small ghost" data-writecoll="${esc(c.folder)}" title="write the collection entry straight into lazer's database">write collection</button>
        <button class="small ghost" data-push="${esc(c.folder)}">push maps</button>
        <button class="small ghost" data-batch="${esc(c.folder)}" title="unpack for lazer's import screen (one task, needs the import screen)">stage</button>
        <button class="small ghost" data-steps="${esc(c.import_folder || c.folder)}">import steps</button>
        ${c.batch && c.batch.maps ? `<button class="small ghost" data-cleanup="${esc(c.folder)}" title="delete the unpacked copies (keep the .osz library)">free space</button>` : ''}
        <button class="small ghost" data-open="${esc(c.folder)}">folder</button>
      </div></td>
    </tr>`;
      })
      .join('')}
    </tbody></table>`;
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
  if (p.pipeline_mode !== 'auto') {
    box.textContent = p.pipeline_mode === 'wizard'
      ? 'pipeline: stage for lazer\'s import screen (manual) — maps are unpacked, you click through the wizard'
      : 'pipeline: download only — use the buttons below when you want maps in the game';
    return;
  }
  const parts = ['one click: maps imported and deleted as lazer confirms, collections written into lazer\'s database'];
  if (p.collection_mode !== 'database') parts.push(`collection mode: ${p.collection_mode}`);
  if (!p.delete_maps_after_import) parts.push('keeping the .osz files after import');
  if (p.lazerdb && p.lazerdb.available === false) parts.push(`⚠ collection write unavailable (${p.lazerdb.reason}) — collections will wait for the import screen`);
  box.textContent = parts.join(' · ');
}

// ---------------------------------------------------------------- delete all
async function runPurge(body) {
  const out = $('#purge-out');
  out.textContent = 'deleting…';
  try {
    const res = await api('/api/library/delete', body);
    out.textContent = `deleted ${res.removed} item(s), freed ${fmtBytes(res.freed)}`
      + (res.leftover && res.leftover.length ? ` — ${res.leftover.length} still in use (close lazer and retry)` : '');
    ['purge-staged', 'purge-all'].forEach((id) => { const el = document.getElementById(id); if (el) el.disabled = true; });
    refreshLibrary();
    refreshStatus();
  } catch (e) {
    out.textContent = e.message;
  }
}

async function purgeModal() {
  let dry, dryStaged;
  try {
    [dry, dryStaged] = await Promise.all([
      api('/api/library/delete', { scope: 'all', dry_run: true }),
      api('/api/library/delete', { scope: 'staged', dry_run: true }),
    ]);
  } catch (e) {
    return alert(e.message);
  }
  const osz = Math.max(0, dry.freed - dryStaged.freed);
  showModal(
    'Delete downloaded data',
    `<p class="muted">Everything the app downloaded into<br><code>${esc(dry.download_dir)}</code></p>
     <div class="kv">
       <div><b>${dry.removed}</b><span>collections</span></div>
       <div><b>${fmtBytes(osz)}</b><span>map archives</span></div>
       <div><b>${fmtBytes(dryStaged.freed)}</b><span>staged copies</span></div>
       <div><b>${fmtBytes(dry.freed)}</b><span>total on disk</span></div>
     </div>
     <div class="note">Beatmaps already imported into lazer stay in lazer — this only clears the app's own files
       (maps, staged copies, collection.db, bookkeeping). Your settings are kept.</div>
     <div class="warn">Deleting is not reversible. The .osz files would have to be downloaded again.</div>
     <div class="row right">
       <button id="purge-staged" class="ghost small">staged copies only (${fmtBytes(dryStaged.freed)})</button>
       <button id="purge-all" class="danger-solid small">delete everything (${fmtBytes(dry.freed)})</button>
     </div>
     <div id="purge-out" class="muted"></div>`
  );
  $('#purge-staged').onclick = () => runPurge({ scope: 'staged' });
  $('#purge-all').onclick = () => runPurge({ scope: 'all' });
}

// ---------------------------------------------------------------- modals
function showModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
function hideModal() { $('#modal').classList.add('hidden'); }

function stepsModal(folder) {
  const staged = /\.lazer-import$/.test(folder);
  showModal(
    'Add this collection to osu!lazer',
    `<p class="muted">Point lazer's import screen at:<br><code>${esc(folder)}</code>
       <button class="small ghost" onclick="navigator.clipboard.writeText('${esc(folder).replace(/\\/g, '\\\\')}')">copy path</button></p>
     <ol class="steps">
       <li>In lazer press <code>Ctrl+O</code> for Settings → <b>General</b> → click <b>Run setup wizard</b>.</li>
       <li>Click <b>Next</b> until the <b>Import</b> page.</li>
       <li>Set <b>previous osu! install</b> to the folder above.</li>
       <li>Click <b>Import content from previous version</b>.</li>
     </ol>
     ${staged
       ? `<div class="note">This folder contains the unpacked maps <b>and</b> the collection entry, so that single pass imports
            <b>every map as one task</b> ("Imported X of Y beatmaps") plus the collection — no per-map tasks.</div>`
       : `<div class="note">This folder only carries the collection entry, so the import adds just the collection.
            To get the maps in as one task too, press <b>stage (single task)</b> first.</div>`}
     <div class="note">Lazer merges collections by name, so re-running this never duplicates anything.</div>
     <div class="row right"><button id="btn-mark" class="ghost small">mark as imported</button></div>
     <div id="mark-result" class="muted"></div>`
  );
  const collectionFolder = staged ? folder.replace(/\.lazer-import$/, '') : folder;
  $('#btn-mark').onclick = async () => {
    try {
      await api('/api/mark-imported', { folder: collectionFolder, value: true });
      $('#mark-result').textContent = 'marked — thanks!';
      refreshLibrary();
    } catch (e) { $('#mark-result').textContent = e.message; }
  };
}

function settingsModal() {
  const s = state.settings;
  const pipeline = s.pipeline_mode || 'auto';
  const collMode = s.collection_mode || 'database';
  const mirrors = ['nerinyan', 'beatconnect', 'catboy', 'osu.direct', 'sayobot', 'nekoha', 'osudl', 'hinamizawa', 'nzbasic'];
  showModal(
    'Settings',
    `<label class="field"><span>download folder</span><input id="s-dir" type="text" value="${esc(s.download_dir)}"></label>
     <label class="field"><span>concurrent downloads (mirrors throttle above ~12)</span><input id="s-conc" type="text" value="${esc(s.concurrency)}"></label>
     <label class="field"><span>after a download finishes</span>
       <select id="s-pipeline">
         <option value="auto" ${pipeline === 'auto' ? 'selected' : ''}>import maps into the game and add the collection — one click, nothing to do in-game</option>
         <option value="wizard" ${pipeline === 'wizard' ? 'selected' : ''}>stage everything for lazer's own import screen (manual)</option>
         <option value="manual" ${pipeline === 'manual' ? 'selected' : ''}>download only</option>
       </select></label>
     <label class="field"><span>where the collection entry goes</span>
       <select id="s-collmode">
         <option value="database" ${collMode === 'database' ? 'selected' : ''}>straight into lazer's database (no in-game steps)</option>
         <option value="wizard" ${collMode === 'wizard' ? 'selected' : ''}>leave it for lazer's import screen</option>
         <option value="off" ${collMode === 'off' ? 'selected' : ''}>don't add collections</option>
       </select></label>
     <div class="check"><input id="s-delete" type="checkbox" ${s.delete_maps_after_import ? 'checked' : ''}><label for="s-delete">delete the .osz files once lazer confirms the import (frees the space as it goes)</label></div>
     <label class="field"><span>import batch size (paths per game launch)</span><input id="s-chunk" type="text" value="${esc(s.import_chunk ?? 250)}"></label>
     <label class="field"><span>maps → game via</span>
       <select id="s-transport">
         <option value="auto" ${(s.import_transport || 'auto') === 'auto' ? 'selected' : ''}>IPC pipe (fast), fall back to osu!.exe</option>
         <option value="pipe" ${s.import_transport === 'pipe' ? 'selected' : ''}>only the IPC pipe</option>
         <option value="launcher" ${s.import_transport === 'launcher' ? 'selected' : ''}>only osu!.exe forwarders (old way)</option>
         <option value="direct" ${s.import_transport === 'direct' ? 'selected' : ''}>direct into lazer's files, in parallel (~8× faster, needs the game closed)</option>
       </select></label>
     <div class="check"><input id="s-stream" type="checkbox" ${s.stream_import ? 'checked' : ''}><label for="s-stream">hand maps to the game while they download (lazer imports ~1 map/s, so this runs it during the transfer)</label></div>
     <label class="field"><span>launches at once / paths per launch</span><input id="s-pushpar" type="text" value="${esc(s.push_parallel ?? 8)}" style="width:60px"> <input id="s-pushbatch" type="text" value="${esc(s.push_batch_size ?? 20)}" style="width:60px"></label>
     <div class="check"><input id="s-autostart" type="checkbox" ${s.auto_start_lazer ? 'checked' : ''}><label for="s-autostart">start osu!lazer automatically when it isn't running (it takes over the screen)</label></div>
     <label class="field"><span>lazer executable (empty = auto-detect)</span><input id="s-exe" type="text" value="${esc(s.lazer_exe || '')}"></label>
     <label class="field"><span>collection name prefix (e.g. “o!c - ”)</span><input id="s-prefix" type="text" value="${esc(s.collection_prefix || '')}"></label>
     <div class="check"><input id="s-novideo" type="checkbox" ${s.no_video ? 'checked' : ''}><label for="s-novideo">download without video (smaller files)</label></div>
     <div class="check"><input id="s-verify" type="checkbox" ${s.verify_zips ? 'checked' : ''}><label for="s-verify">verify every archive (slower, catches bad mirrors early)</label></div>
     <label class="field" style="margin-top:10px"><span>enabled mirrors</span>
       ${mirrors.map((m) => `<label class="check"><input type="checkbox" data-mirror="${m}" ${(s.mirrors || []).includes(m) ? 'checked' : ''}><span style="margin:0">${m}</span></label>`).join('')}
       <button id="s-probe" class="small ghost" style="margin-top:6px">probe mirrors now</button>
     </label>
     <div id="s-probe-out" class="muted"></div>
     <div class="row right"><button id="s-save">Save</button></div>`
  );
  $('#s-probe').onclick = async () => {
    $('#s-probe-out').textContent = 'probing…';
    try {
      const res = await api('/api/mirrors/probe', {});
      $('#s-probe-out').textContent = 'fastest first: ' + res.order.map((o) => `${o.name} ${o.ms ?? 'n/a'}${o.ms ? 'ms' : ''}`).join(' · ');
    } catch (e) { $('#s-probe-out').textContent = e.message; }
  };
  $('#s-save').onclick = async () => {
    const enabled = [...document.querySelectorAll('[data-mirror]')].filter((el) => el.checked).map((el) => el.dataset.mirror);
    const patch = {
      download_dir: $('#s-dir').value.trim(),
      concurrency: Math.max(1, parseInt($('#s-conc').value, 10) || 10),
      pipeline_mode: $('#s-pipeline').value,
      collection_mode: $('#s-collmode').value,
      delete_maps_after_import: $('#s-delete').checked,
      import_chunk: Math.max(1, parseInt($('#s-chunk').value, 10) || 250),
      import_transport: $('#s-transport').value,
      stream_import: $('#s-stream').checked,
      push_parallel: Math.max(1, parseInt($('#s-pushpar').value, 10) || 8),
      push_batch_size: Math.max(1, parseInt($('#s-pushbatch').value, 10) || 20),
      auto_start_lazer: $('#s-autostart').checked,
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
      refreshStatus();
      refreshLibrary();
    } catch (e) { alert(e.message); }
  };
}

// ---------------------------------------------------------------- wiring
document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-fetch],[data-download],[data-push],[data-batch],[data-cleanup],[data-steps],[data-open],[data-cancel],[data-finalize],[data-writecoll]');
  if (!t) return;
  const btn = t;
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
      t.textContent = 'written ✔';
      alert('collection in lazer: ' + JSON.stringify(res.changed));
      return void refreshLibrary();
    }
    if (t.dataset.push || t.dataset.force) {
      const folder = t.dataset.push || t.dataset.force;
      t.disabled = true; t.textContent = 'pushing…';
      await api('/api/push', { folder, force: !!t.dataset.force });
      return void refreshJobs();
    }
    if (t.dataset.batch) {
      t.disabled = true; t.textContent = 'staging…';
      await api('/api/batch/prepare', { folder: t.dataset.batch });
      return void refreshJobs();
    }
    if (t.dataset.cleanup) {
      t.disabled = true; t.textContent = 'cleaning…';
      const res = await api('/api/batch/cleanup', { folder: t.dataset.cleanup });
      alert(`freed ${fmtBytes(res.freed)}`);
      return void refreshLibrary();
    }
    if (t.dataset.steps) return void stepsModal(t.dataset.steps);
    if (t.dataset.open) return void api('/api/open', { folder: t.dataset.open });
    if (t.dataset.cancel) {
      await api('/api/jobs/cancel', { id: t.dataset.cancel });
      return void refreshJobs();
    }
  } catch (e) {
    alert(e.message);
  }
});

$('#btn-search').onclick = () => doSearch(1);
$('#q').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(1); });
$('#btn-next').onclick = () => doSearch(state.page + 1);
$('#btn-prev').onclick = () => doSearch(Math.max(1, state.page - 1));
$('#btn-fetch').onclick = () => fetchRef();
$('#ref').addEventListener('keydown', (e) => { if (e.key === 'Enter') fetchRef(); });
$('#btn-settings').onclick = settingsModal;
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
