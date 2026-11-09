/* All-clients overview: one card per client with its overall state and quick start/stop. */
(() => {
  'use strict';
  const { $, classify, isTransition, summarize, envButtons, groupValue, api, toast, confirmStop, POLL, secondsAgo } = window.L180;

  const cards = {};
  for (const card of document.querySelectorAll('.client')) {
    cards[card.dataset.slug] = {
      root: card, name: card.dataset.name,
      pill: $('.pill', card), label: $('.pill .js-label', card),
      rds: $('[data-res="rds"]', card), ec2: $('[data-res="ec2"]', card),
      rdsVal: $('.js-rds', card), ec2Val: $('.js-ec2', card), op: $('.js-op', card),
      start: $('.js-start', card), stop: $('.js-stop', card),
    };
  }
  const el = { tally: $('#tally'), conn: $('#ov-conn-alert'), updated: $('#ov-updated'), refresh: $('#ov-refresh') };

  let data = null, lastOkAt = 0, inflight = false, timer = null;
  const pendingSlugs = new Set();

  function render() {
    let running = 0, total = 0;
    for (const [slug, c] of Object.entries(cards)) {
      const d = data && data.clients.find((x) => x.slug === slug);
      const keys = { rds: d ? (d.rds || []).map(classify) : [], ec2: d ? (d.ec2 || []).map(classify) : [] };
      const flat = [...keys.rds, ...keys.ec2];
      const op = d && d.operation;
      const sum = summarize(flat, op, !!d);

      c.root.dataset.state = sum.state;
      c.pill.dataset.state = sum.state;
      c.label.textContent = sum.title;
      c.root.dataset.busy = String(sum.state === 'starting' || sum.state === 'stopping');

      for (const name of ['rds', 'ec2']) {
        const row = c[name], val = c[name + 'Val'];
        row.hidden = !!d && keys[name].length === 0;
        const group = summarize(keys[name], null, !!d);
        row.dataset.state = d ? group.state : 'unknown';
        val.textContent = d ? groupValue(name, keys[name]) : 'Checking';
      }

      const opText = op && op.status === 'running' ? op.message : '';
      c.op.hidden = !opText; c.op.textContent = opText || '';

      const b = envButtons(flat, op, pendingSlugs.has(slug));
      c.start.disabled = !d || b.startDisabled;
      c.stop.disabled = !d || b.stopDisabled;

      if (d) { total++; if (sum.state === 'ok') running++; }
    }
    el.tally.textContent = data
      ? (running === 0 ? `No environment${total === 1 ? '' : 's'} running, ${total} in total`
                       : `${running} of ${total} environment${total === 1 ? '' : 's'} running`)
      : 'Checking environments';
  }

  async function refresh({ spin = false } = {}) {
    if (inflight) return;
    inflight = true;
    if (spin) el.refresh.classList.add('is-loading');
    try {
      data = await api('/api/overview');
      lastOkAt = Date.now(); el.conn.hidden = true;
    } catch (err) {
      if (data) el.conn.hidden = false; else toast('error', err.message || "Couldn't load the status.");
    } finally {
      inflight = false; el.refresh.classList.remove('is-loading');
      render(); tick(); schedule();
    }
  }

  function schedule() {
    clearTimeout(timer);
    const fast = !!data && data.clients.some((c) =>
      (c.operation && c.operation.status === 'running') || [...(c.rds || []), ...(c.ec2 || [])].some((r) => isTransition(classify(r))));
    timer = setTimeout(() => refresh(), fast ? POLL.fast : POLL.normal);
  }

  function tick() { if (lastOkAt) el.updated.textContent = secondsAgo(lastOkAt); }

  async function run(slug, btn, action, fallback) {
    pendingSlugs.add(slug);
    btn.classList.add('is-loading'); render();
    try {
      const res = await api(`/api/c/${encodeURIComponent(slug)}/environment/${action}`, 'POST');
      toast('success', `${cards[slug].name}: ${res.message || fallback}`);
    } catch (err) {
      toast('error', `${cards[slug].name}: ${err.message || 'Something went wrong. Try again.'}`);
    } finally {
      pendingSlugs.delete(slug); btn.classList.remove('is-loading');
      await refresh();
    }
  }

  for (const [slug, c] of Object.entries(cards)) {
    c.start.addEventListener('click', (e) => run(slug, e.currentTarget, 'start', 'Starting the environment.'));
    c.stop.addEventListener('click', async (e) => {
      const btn = e.currentTarget;
      const yes = await confirmStop({
        title: `Stop everything for ${c.name}?`,
        body: 'The application stops first. Once it has fully stopped, the database stops too. Your team will lose access in the meantime.',
        ok: 'Stop everything',
      });
      if (yes) run(slug, btn, 'stop', 'Stopping the environment.');
    });
  }

  el.refresh.addEventListener('click', () => refresh({ spin: true }));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  setInterval(tick, 1000);
  render(); refresh();
})();
