'use strict';
// First-run setup. Shows itself only when the install is not set up yet; the header's
// "Setup" button can open it again (after a lazer update breaks the helper, say).
(() => {
  const box = document.getElementById('wizard');
  if (!box) return;

  const api = async (path, body) => {
    const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
    const res = await fetch(path, opts);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  };
  const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const note = (text, kind = '') => { if (typeof toast === 'function') toast(text, kind); };

  const STEPS = ['osu!lazer', 'helper', 'folder', 'options', 'ready'];
  const MIRRORS = ['nerinyan', 'beatconnect', 'catboy', 'osu.direct', 'sayobot', 'nekoha', 'osudl', 'hinamizawa', 'nzbasic'];
  const pick = {
    lazer_exe: '',
    download_dir: '',
    no_video: true,
    verify_zips: true,
    close_lazer_before_import: true,
    collection_prefix: '',
    mirrors: MIRRORS.slice(),
  };
  let state = null;
  let step = 0;
  let busy = '';
  let log = [];

  function hydrate() {
    const s = state.settings || {};
    pick.lazer_exe = s.lazer_exe || state.lazer.exe || '';
    pick.download_dir = s.download_dir || pick.download_dir;
    pick.no_video = s.no_video !== false;
    pick.verify_zips = s.verify_zips !== false;
    pick.close_lazer_before_import = s.close_lazer_before_import !== false;
    pick.collection_prefix = s.collection_prefix || '';
    if (Array.isArray(s.mirrors) && s.mirrors.length) pick.mirrors = s.mirrors.slice();
  }

  // ------------------------------------------------------------- pieces
  function rail() {
    return `<div class="wiz-rail">${STEPS.map((name, i) =>
      `<span class="chip${i === step ? ' ok' : i < step ? ' ghosty' : ''}">${i + 1} · ${name}</span>`).join('')}</div>`;
  }

  function lazerStep() {
    const l = state.lazer;
    const found = !!l.exe;
    return `
      <h3>osu!lazer</h3>
      <p class="muted">${found ? 'Found on this machine.' : 'Not found — point the app at osu!.exe.'}</p>
      <div class="row">
        <input id="w-exe" placeholder="path to osu!.exe" value="${esc(pick.lazer_exe)}" />
        <button class="ghost small" id="w-browse-file">Browse…</button>
        <button class="ghost small" id="w-redetect">Detect again</button>
      </div>
      <div class="chips">
        <span class="chip ${found ? 'ok' : 'bad'}">${found ? 'installed' : 'missing'}</span>
        ${l.version ? `<span class="chip ghosty">version ${esc(l.version)}</span>` : ''}
        <span class="chip ghosty">${l.running ? 'running now' : 'not running'}</span>
        ${l.data_dir ? `<span class="chip ghosty">data at ${esc(l.data_dir)}</span>` : ''}
      </div>`;
  }

  function helperStep() {
    const h = state.helper;
    const ready = !!h.available;
    const mismatch = !ready && /schema mismatch/i.test(h.reason || '');
    const actions = [];
    if (!ready) {
      actions.push(`<button class="small" id="w-download"${busy ? ' disabled' : ''}>${busy === 'download' ? 'downloading…' : 'Download helper'}</button>`);
      if (state.dotnet.can_build) {
        actions.push(`<button class="ghost small" id="w-build"${busy ? ' disabled' : ''}>${busy === 'build' ? 'building…' : 'Build with .NET'}</button>`);
      }
    }
    return `
      <h3>Import helper</h3>
      <p class="muted">${ready
        ? 'Installed and it matches the lazer build on this machine.'
        : 'The piece that writes maps and collections into lazer. About 70 MB, one time.'}</p>
      <div class="chips">
        <span class="chip ${ready ? 'ok' : 'bad'}">${ready ? 'ready' : 'not installed'}</span>
        ${h.path ? `<span class="chip ghosty">${esc(h.path)}</span>` : ''}
        ${mismatch ? '<span class="chip warn">built for a different lazer version</span>' : ''}
        ${state.dotnet.version ? `<span class="chip ghosty">.NET ${esc(state.dotnet.version)}</span>` : '<span class="chip ghosty">no .NET SDK</span>'}
      </div>
      ${mismatch ? `<p class="muted">The installed lazer is newer than this helper's database schema. Download the helper again (or update the app), or build it from source.</p>` : ''}
      ${!ready && !state.dotnet.can_build && !state.dotnet.version ? `<p class="muted">Downloading works without .NET. Building from source needs the .NET 10 SDK.</p>` : ''}
      ${actions.length ? `<div class="row" style="margin-top:12px">${actions}</div>` : ''}
      ${log.length ? `<div class="log">${log.map(esc).join('\n')}</div>` : ''}`;
  }

  function folderStep() {
    return `
      <h3>Download folder</h3>
      <p class="muted">Where collections land before they go into lazer. Archives are deleted as they import.</p>
      <div class="row">
        <input id="w-dir" value="${esc(pick.download_dir)}" />
        <button class="ghost small" id="w-browse-dir">Browse…</button>
      </div>
      <div class="chips"><span class="chip ghosty">created if it does not exist</span></div>`;
  }

  function optionsStep() {
    const check = (id, label, checked, hint) =>
      `<label class="check" title="${esc(hint)}"><input id="${id}" type="checkbox" ${checked ? 'checked' : ''}><span>${label}</span></label>`;
    return `
      <h3>Options</h3>
      <p class="muted">Sensible defaults; everything else is in Settings later.</p>
      ${check('w-novideo', 'skip video', pick.no_video, 'download without video — smaller files')}
      ${check('w-verify', 'verify every archive', pick.verify_zips, 'slower, catches bad mirrors early')}
      ${check('w-close', 'close osu!lazer automatically before an import', pick.close_lazer_before_import, 'imports write straight into its files, so the game must not be open')}
      <div class="row" style="margin-top:10px">
        <input id="w-prefix" placeholder="collection name prefix — optional" value="${esc(pick.collection_prefix)}" />
      </div>`;
  }

  function readyStep() {
    const l = state.lazer;
    const h = state.helper;
    const ok = (v) => `<span class="dot ${v ? 'ok' : 'bad'}"></span>`;
    return `
      <h3>Ready</h3>
      <p class="muted">${h.available ? 'Everything is in place.' : 'Almost — imports stay disabled until the helper is installed (Setup, or the Library card, can do it later).'}</p>
      <div class="kv">
        <div><b>${ok(!!l.exe)}</b><span>osu!lazer</span></div>
        <div><b>${ok(!!h.available)}</b><span>import helper</span></div>
        <div><b>${ok(!!pick.download_dir)}</b><span>download folder</span></div>
      </div>
      <div class="chips">
        <span class="chip ghosty">${esc(pick.download_dir || 'default folder')}</span>
        ${pick.no_video ? '<span class="chip ghosty">no video</span>' : ''}
        ${pick.verify_zips ? '<span class="chip ghosty">verify archives</span>' : ''}
        ${pick.close_lazer_before_import ? '<span class="chip ghosty">lazer closed automatically</span>' : ''}
      </div>
      <div class="row" style="margin-top:12px">
        <button class="ghost small" id="w-lnk-desktop" title="put a shortcut on the desktop">Desktop shortcut</button>
        <button class="ghost small" id="w-lnk-startmenu" title="add it to the Start menu">Start menu</button>
      </div>
      <div class="chips" id="w-shortcuts"></div>`;
  }

  function footer() {
    const last = step === STEPS.length - 1;
    return `<div class="row right">
      ${step > 0 ? '<button class="ghost small" id="w-back">Back</button>' : ''}
      ${step === 1 && !state.helper.available ? '<button class="ghost small" id="w-skip">Later</button>' : ''}
      ${last
        ? '<button id="w-finish">Finish</button>'
        : `<button id="w-next"${busy ? ' disabled' : ''}>Next</button>`}
    </div>`;
  }

  function render() {
    box.innerHTML = `<div class="wizard-box">
      <div class="wizard-head">
        <div>
          <span class="wizard-title">Setup</span>
          <span class="muted">OsuCollectLazer ${esc(state.version)}</span>
        </div>
        <button class="ghost small" id="w-close" title="close (Setup can be reopened from the header)">✕</button>
      </div>
      ${rail()}
      <div class="wizard-body">${[lazerStep, helperStep, folderStep, optionsStep, readyStep][step]()}</div>
      ${footer()}
    </div>`;
    bind();
  }

  function capture() {
    const read = (sel) => document.querySelector(sel);
    const exe = read('#w-exe');
    if (exe) pick.lazer_exe = exe.value.trim();
    const dir = read('#w-dir');
    if (dir) pick.download_dir = dir.value.trim();
    const nov = read('#w-novideo');
    if (nov) pick.no_video = nov.checked;
    const ver = read('#w-verify');
    if (ver) pick.verify_zips = ver.checked;
    const clo = read('#w-close');
    if (clo) pick.close_lazer_before_import = clo.checked;
    const pre = read('#w-prefix');
    if (pre) pick.collection_prefix = pre.value;
  }

  function bind() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
    on('w-close', () => box.classList.add('hidden'));
    on('w-back', () => { capture(); step = Math.max(0, step - 1); render(); });
    on('w-next', () => { capture(); step = Math.min(STEPS.length - 1, step + 1); render(); });
    on('w-skip', () => finish());
    on('w-finish', () => finish());
    on('w-redetect', async () => {
      capture();
      state = await api('/api/setup');
      pick.lazer_exe = state.lazer.exe || pick.lazer_exe;
      render();
    });
    on('w-browse-file', () => browse('file', '#w-exe'));
    on('w-browse-dir', () => browse('folder', '#w-dir'));
    const renderShortcutChips = async () => {
      const chips = document.getElementById('w-shortcuts');
      if (!chips) return;
      try {
        const s = await window.AppShortcuts.status();
        chips.innerHTML = s.available
          ? Object.values(s.places)
            .map((p) => `<span class="chip${p.exists ? ' ok' : ' ghosty'}">${esc(p.label)}${p.exists ? ' ✓' : ' —'}</span>`)
            .join('')
          : `<span class="chip ghosty">${esc(s.reason || 'shortcuts unavailable')}</span>`;
      } catch (e) {
        chips.innerHTML = `<span class="chip bad">${esc(e.message)}</span>`;
      }
    };
    const addShortcut = async (where) => {
      try {
        await window.AppShortcuts.create(where);
        note('shortcut created', 'ok');
      } catch (e) {
        note(e.message, 'bad');
      }
      renderShortcutChips();
    };
    on('w-lnk-desktop', () => addShortcut('desktop'));
    on('w-lnk-startmenu', () => addShortcut('startmenu'));
    renderShortcutChips();
    on('w-download', () => install('download'));
    on('w-build', () => install('build'));
  }

  async function browse(kind, target) {
    try {
      const res = await api('/api/setup/pick', { kind, start: kind === 'file' ? pick.lazer_exe : pick.download_dir });
      if (!res.path) return note('nothing picked');
      document.querySelector(target).value = res.path;
      if (kind === 'file') pick.lazer_exe = res.path;
      else pick.download_dir = res.path;
    } catch (e) {
      note(e.message, 'bad');
    }
  }

  async function install(method) {
    busy = method;
    log = method === 'download' ? ['asking GitHub for the latest release…'] : ['starting dotnet build…'];
    render();
    try {
      const res = await api('/api/setup/helper', { method });
      log = res.log || [];
      state = await api('/api/setup');
      busy = '';
      note(res.status && res.status.available ? 'helper ready' : 'helper installed, but it does not match this lazer build', res.status && res.status.available ? 'ok' : 'bad');
      render();
    } catch (e) {
      busy = '';
      log = [...log, e.message];
      note(e.message, 'bad');
      render();
    }
  }

  async function finish() {
    capture();
    try {
      await api('/api/setup/finish', {
        settings: {
          lazer_exe: pick.lazer_exe,
          download_dir: pick.download_dir,
          no_video: pick.no_video,
          verify_zips: pick.verify_zips,
          close_lazer_before_import: pick.close_lazer_before_import,
          collection_prefix: pick.collection_prefix,
          mirrors: pick.mirrors,
        },
      });
      box.classList.add('hidden');
      note('setup saved', 'ok');
      setTimeout(() => location.reload(), 600);
    } catch (e) {
      note(e.message, 'bad');
    }
  }

  window.Wizard = { open: () => open(true) };

  async function open(force) {
    try {
      state = await api('/api/setup');
    } catch (e) {
      return;
    }
    hydrate();
    if (!force && state.setup_complete) return;
    step = force ? 0 : (state.helper.available && state.lazer.exe ? 3 : 0);
    log = [];
    busy = '';
    render();
    box.classList.remove('hidden');
  }

  document.addEventListener('DOMContentLoaded', () => open(false));
  // app.js boots immediately; if it is slower than this script, /api/setup is still fine
  if (document.readyState !== 'loading') open(false);
})();
