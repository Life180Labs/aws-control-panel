/* Client dashboard: all databases and applications of one client. Shared helpers and the API contract live in common.js. */
(() => {
  'use strict';
  const L = window.L180;
  const { $, capitalise, classify, labelFor, isTransition, summarize, envButtons, describeGroup, api, toast, confirmStop, POLL, secondsAgo, WORDS, durationLabel, uptimeLabel } = L;

  let slug = document.body.dataset.client;
  const clientName = () => document.body.dataset.clientName || 'this client';
  const url = (p) => `/api/c/${encodeURIComponent(slug)}/${p}`;
  const enc = encodeURIComponent;

  const el = {
    env: $('#env'), envState: $('#env-state'), envDetail: $('#env-detail'),
    envStart: $('#btn-env-start'), envStop: $('#btn-env-stop'),
    progress: $('#env-progress'), steps: $('#env-steps'), message: $('#env-message'),
    flow: $('#flow'), empty: $('#flow-empty'), connector: $('.connector'), region: $('#region'), conn: $('#conn-alert'),
    updated: $('#updated'), refresh: $('#btn-refresh'), rowTpl: $('#row-tpl'),
  };

  const KINDS = ['rds', 'ec2'];
  const G = {};
  for (const kind of KINDS) {
    const root = $(`#group-${kind}`);
    G[kind] = {
      root, rows: $('.js-rows', root), sub: $('.js-sub', root), note: $('.js-note', root),
      actions: $('.group-actions', root), startAll: $('.js-start-all', root), stopAll: $('.js-stop-all', root),
      map: new Map(),   // resource id -> <li>
    };
  }

  let status = null, lastOkAt = 0, pending = false, inflight = false, timer = null, lastSeenRunningOp = null;

  /* ---------- rendering ---------- */

  const listOf = (kind) => (status && Array.isArray(status[kind]) ? status[kind] : []);

  function render() {
    const op = status && status.operation;
    const opRunning = !!(op && op.status === 'running');
    const busy = pending || opRunning;

    const keys = { rds: listOf('rds').map(classify), ec2: listOf('ec2').map(classify) };
    const flat = [...keys.rds, ...keys.ec2];

    for (const kind of KINDS) renderGroup(kind, listOf(kind), keys[kind], busy);

    const both = keys.rds.length > 0 && keys.ec2.length > 0;
    el.connector.hidden = !both;
    G.rds.note.hidden = !both; G.ec2.note.hidden = !both;
    G.rds.note.textContent = 'Databases start first and stop last, so applications never lose their data.';
    G.ec2.note.textContent = 'Applications start once every database is available.';

    el.empty.hidden = !!status && flat.length > 0;
    el.empty.textContent = status ? 'No databases or applications are set up for this client yet.' : 'Loading resources';

    renderEnvironment(keys, flat, op, busy);
    if (status && status.region) { el.region.textContent = status.region; el.region.hidden = false; }

    el.flow.dataset.flow = opRunning ? op.type : flat.some(isTransition) ? (flat.includes('stopping') ? 'stop' : 'start') : 'idle';
  }

  function renderGroup(kind, items, keys, busy) {
    const g = G[kind], w = WORDS[kind];
    g.root.hidden = items.length === 0;
    if (!items.length) { g.map.clear(); g.rows.replaceChildren(); return; }

    g.root.dataset.busy = String(keys.some(isTransition));
    const okCount = keys.filter((k) => k === 'ok').length;
    g.sub.textContent = items.length === 1
      ? `${w.kindLabel} instance`
      : `${items.length} ${w.kindLabel} instances, ${okCount} ${w.okLabel.toLowerCase()}`;

    // keep existing rows (and their focus / loading state), add new ones, drop removed ones
    const seen = new Set();
    items.forEach((item, i) => {
      seen.add(item.id);
      let row = g.map.get(item.id);
      if (!row) { row = makeRow(kind, item.id); g.map.set(item.id, row); }
      updateRow(row, kind, item, keys[i], busy);
    });
    for (const [id, row] of g.map) if (!seen.has(id)) { row.remove(); g.map.delete(id); }
    const ordered = items.map((it) => g.map.get(it.id));
    if (ordered.some((r, i) => g.rows.children[i] !== r)) g.rows.replaceChildren(...ordered);

    // group-wide actions only make sense with more than one resource
    g.actions.hidden = items.length < 2;
    g.startAll.disabled = busy || !keys.includes('stopped');
    g.stopAll.disabled = busy || !keys.includes('ok');
  }

  function makeRow(kind, id) {
    const row = el.rowTpl.content.firstElementChild.cloneNode(true);
    row.dataset.id = id;
    const w = WORDS[kind];
    $('.js-start', row).addEventListener('click', (e) =>
      run(e.currentTarget, `${kind}/${enc(id)}/start`, `Starting ${row.dataset.name}.`));
    $('.js-stop', row).addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const yes = await confirmStop({
        title: `Stop ${row.dataset.name} for ${clientName()}?`,
        body: kind === 'rds'
          ? 'Applications that use this database will lose it while it is stopped.'
          : 'Anyone using this application will lose access until it is started again.',
        ok: `Stop ${w.one}`,
      });
      if (yes) run(btn, `${kind}/${enc(id)}/stop`, `Stopping ${row.dataset.name}.`);
    });
    return row;
  }

  function updateRow(row, kind, item, key, busy) {
    const name = item.name || item.id;
    row.dataset.name = name; row.dataset.state = key;
    $('.row-name', row).textContent = name; $('.row-name', row).title = name;
    const idEl = $('.row-id', row);
    idEl.textContent = item.name ? item.id : ''; idEl.title = item.id; idEl.hidden = !item.name;
    const pill = $('.pill', row); pill.dataset.state = key;
    $('.js-label', pill).textContent = labelFor(key, item, kind);
    const err = $('.row-error', row); err.hidden = !item.error; err.textContent = item.error || '';
    $('.js-start', row).disabled = busy || !(key === 'stopped' || key === 'error');
    $('.js-stop', row).disabled = busy || !(key === 'ok' || key === 'error');
    row._up = { base: item.uptime_seconds, over: item.uptime_over_seconds, startedAt: item.started_at, at: lastOkAt || Date.now() };
    paintUptime(row);
  }

  /** "Uptime - 3h 12m" under a running resource. The server sends seconds-up as of the last refresh; we add the time since. */
  function paintUptime(row) {
    const el = $('.row-up', row), u = row._up;
    let text = '', tip = '';
    if (u && u.base != null) {
      text = uptimeLabel(u.base + (Date.now() - u.at) / 1000);
      tip = 'Started ' + new Date(u.startedAt).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
    } else if (u && u.over != null) {
      text = `Uptime - over ${Math.round(u.over / 86400)} days`;
      tip = 'AWS only keeps 14 days of history, so the exact start time is not available.';
    }
    el.hidden = !text;
    if (el.textContent !== text) el.textContent = text;
    el.title = tip;
  }

  function renderEnvironment(keys, flat, op, busy) {
    const known = !!status;
    const sum = summarize(flat, op, known);
    let detail = '';
    if (known && flat.length) {
      detail = sum.state === 'error'
        ? 'A resource reported an error. Check the details below.'
        : capitalise(KINDS.filter((k) => keys[k].length).map((k) => describeGroup(k, keys[k])).join(', ')) + '.';
    }
    el.env.dataset.state = sum.state;
    el.envState.textContent = sum.title;
    if (el.envDetail.textContent !== detail) el.envDetail.textContent = detail;

    const b = envButtons(flat, op, busy);
    el.envStart.disabled = !known || b.startDisabled;
    el.envStop.disabled = !known || b.stopDisabled;

    const showSteps = !!(op && (op.status === 'running' || op.status === 'failed') && Array.isArray(op.steps));
    el.progress.hidden = !showSteps;
    if (showSteps) {
      el.steps.replaceChildren(...op.steps.map((label, i) => {
        const failed = op.status === 'failed' && i === op.step;
        const st = failed ? 'failed' : i < op.step ? 'done' : i === op.step ? 'current' : 'todo';
        const li = document.createElement('li');
        li.className = 'step'; li.dataset.s = st;
        li.innerHTML = `<span class="step-mark"><svg class="ic"><use href="#i-${failed ? 'x' : 'check'}"/></svg></span><span></span>`;
        li.lastChild.textContent = label;
        return li;
      }));
      el.message.textContent = op.message || '';
    }
  }

  /* ---------- polling ---------- */

  async function refresh({ spin = false } = {}) {
    if (inflight) return;
    inflight = true;
    const mySlug = slug;
    if (spin) el.refresh.classList.add('is-loading');
    try {
      const data = await api(url('status'));
      if (mySlug !== slug) return;               // client changed while the request was running
      status = data; lastOkAt = Date.now();
      el.conn.hidden = true;
      announceOperation(data.operation);
    } catch (err) {
      if (mySlug !== slug) return;
      if (status) el.conn.hidden = false;
      else toast('error', err.message || "Couldn't load the status.");
    } finally {
      inflight = false;
      el.refresh.classList.remove('is-loading');
      render(); tickUpdated(); schedule();
    }
  }

  function schedule() {
    clearTimeout(timer);
    const fast = !!status && ((status.operation && status.operation.status === 'running')
      || [...listOf('rds'), ...listOf('ec2')].some((r) => isTransition(classify(r))));
    timer = setTimeout(() => refresh(), fast ? POLL.fast : POLL.normal);
  }

  function announceOperation(op) {
    if (!op) { lastSeenRunningOp = null; return; }
    if (op.status === 'running') { lastSeenRunningOp = op.id; return; }
    if (lastSeenRunningOp === op.id) {
      toast(op.status === 'done' ? 'success' : 'error', op.message || (op.status === 'done' ? 'Done.' : 'The operation failed.'));
      lastSeenRunningOp = null;
    }
  }

  function tickUpdated() { if (lastOkAt) el.updated.textContent = secondsAgo(lastOkAt); }

  /* ---------- actions ---------- */

  async function run(btn, path, fallbackMessage) {
    pending = true;
    btn.classList.add('is-loading'); btn.setAttribute('aria-busy', 'true');
    render();
    try {
      const data = await api(url(path), 'POST');
      toast('success', data.message || fallbackMessage);
    } catch (err) {
      toast('error', err.message || 'Something went wrong. Try again.');
    } finally {
      pending = false;
      btn.classList.remove('is-loading'); btn.removeAttribute('aria-busy');
      await refresh();
    }
  }

  for (const kind of KINDS) {
    const g = G[kind], w = WORDS[kind];
    g.startAll.addEventListener('click', (e) => run(e.currentTarget, `${kind}/start`, `Starting all ${w.many}.`));
    g.stopAll.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const yes = await confirmStop({
        title: `Stop all ${listOf(kind).length} ${w.many} for ${clientName()}?`,
        body: kind === 'rds'
          ? 'Applications that use these databases will lose them while they are stopped.'
          : 'Anyone using these applications will lose access until they are started again.',
        ok: `Stop all ${w.many}`,
      });
      if (yes) run(btn, `${kind}/stop`, `Stopping all ${w.many}.`);
    });
  }

  el.envStart.addEventListener('click', (e) =>
    run(e.currentTarget, 'environment/start', 'Starting the environment. Databases start first, then applications.'));
  el.envStop.addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    const yes = await confirmStop({
      title: `Stop everything for ${clientName()}?`,
      body: 'Applications stop first. Once they have fully stopped, the databases stop too. Your team will lose access in the meantime.',
      ok: 'Stop everything',
    });
    if (yes) run(btn, 'environment/stop', 'Stopping the environment. Applications stop first, then databases.');
  });
  el.refresh.addEventListener('click', () => refresh({ spin: true }));

  /* ---------- switcher: close the menu on outside click / Escape ---------- */

  const switcher = $('#switcher');
  if (switcher) {
    document.addEventListener('click', (e) => { if (!switcher.contains(e.target)) switcher.open = false; });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') switcher.open = false; });
  }

  /* ---------- start ---------- */

  // Used only by the UI preview, which swaps clients without a page load.
  window.addEventListener('l180:client', (e) => {
    slug = e.detail.slug; status = null; lastOkAt = 0; lastSeenRunningOp = null;
    for (const kind of KINDS) { G[kind].map.clear(); G[kind].rows.replaceChildren(); }
    clearTimeout(timer); el.conn.hidden = true; el.updated.textContent = 'Waiting for the first update';
    render(); refresh();
  });

  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(tickUpdated, 1000);
  setInterval(() => { for (const kind of KINDS) for (const row of G[kind].rows.children) paintUptime(row); }, 10000);
  render();
  refresh();
})();
