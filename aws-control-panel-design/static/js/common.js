/*
 * Shared helpers for the overview and client dashboard (vanilla JS).
 *
 * A client can have any number of databases (RDS) and applications (EC2).
 * The browser only ever sends the client slug and resource IDs; the backend checks
 * both against clients.json and rejects anything that is not configured.
 *
 *   GET  /api/overview                 -> { clients: [ { slug, name, region,
 *                                              rds: [ {state} ], ec2: [ {state} ], operation|null } ] }
 *   GET  /api/c/<slug>/status          -> { client: {slug, name}, region,
 *                                              rds: [ { id, name, state, error } ],   // [] = client has none
 *                                              ec2: [ { id, name, state, error } ],
 *                                              operation: null | { id, type: "start"|"stop",
 *                                                status: "running"|"done"|"failed",
 *                                                steps: [..], step: 0, message } }
 *   POST /api/c/<slug>/{ec2|rds}/<id>/{start|stop}    one resource
 *   POST /api/c/<slug>/{ec2|rds}/{start|stop}         every resource of that kind
 *   POST /api/c/<slug>/environment/{start|stop}       whole client: databases first, applications last
 *        -> 200/202 { ok:true, message } | 4xx/5xx { ok:false, error }
 *
 * States are raw AWS states. Every POST sends the CSRF token in X-CSRF-Token.
 * A 401 anywhere sends the browser back to /login.
 */
window.L180 = (() => {
  'use strict';
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  const $ = (sel, root = document) => root.querySelector(sel);
  const capitalise = (s) => s.charAt(0).toUpperCase() + s.slice(1);

  const WORDS = {
    rds: { one: 'database', many: 'databases', title: 'Databases', okLabel: 'Available', kindLabel: 'RDS' },
    ec2: { one: 'application', many: 'applications', title: 'Applications', okLabel: 'Running', kindLabel: 'EC2' },
  };

  /** One resource ({state, error}) -> ok | stopped | starting | stopping | error | unknown */
  function classify(res) {
    if (res && res.error) return 'error';
    const s = String((res && res.state) || '').toLowerCase();
    if (s === 'running' || s === 'available') return 'ok';
    if (s === 'stopped') return 'stopped';
    if (s === 'pending' || s === 'starting') return 'starting';
    if (s === 'stopping' || s === 'shutting-down') return 'stopping';
    if (!s || s === 'unknown') return 'unknown';
    if (/fail|incompatible|inaccessible|storage-full|terminated|error/.test(s)) return 'error';
    return 'starting'; // other transitional DB states (rebooting, modifying, backing-up, ...)
  }

  function labelFor(key, res, kind) {
    const raw = String((res && res.state) || '').toLowerCase();
    switch (key) {
      case 'ok': return WORDS[kind].okLabel;
      case 'stopped': return 'Stopped';
      case 'starting': return (!raw || raw === 'pending' || raw === 'starting') ? 'Starting' : capitalise(raw.replace(/-/g, ' '));
      case 'stopping': return 'Stopping';
      case 'error': return 'Error';
      default: return 'Unknown';
    }
  }

  const isTransition = (k) => k === 'starting' || k === 'stopping';

  /** Overall state of one client from the keys of all its resources and its current operation. */
  function summarize(keys, op, known) {
    if (!known) return { state: 'unknown', title: 'Checking status' };
    if (op && op.status === 'running') {
      return op.type === 'start' ? { state: 'starting', title: 'Starting up' } : { state: 'stopping', title: 'Shutting down' };
    }
    if (!keys.length) return { state: 'unknown', title: 'No resources' };
    if (keys.includes('error')) return { state: 'error', title: 'Needs attention' };
    if (keys.some(isTransition)) {
      return keys.includes('stopping') ? { state: 'stopping', title: 'Shutting down' } : { state: 'starting', title: 'Starting up' };
    }
    if (keys.every((v) => v === 'ok')) return { state: 'ok', title: 'Running' };
    if (keys.every((v) => v === 'stopped')) return { state: 'stopped', title: 'Stopped' };
    if (keys.includes('unknown')) return { state: 'unknown', title: 'Status unavailable' };
    return { state: 'stopped', title: 'Partly running' };
  }

  /** Which "everything" buttons make sense right now. */
  function envButtons(keys, op, busy) {
    const moving = keys.some(isTransition) || !!(op && op.status === 'running');
    return {
      startDisabled: busy || moving || !keys.length || keys.every((v) => v === 'ok'),
      stopDisabled: busy || moving || !keys.length || keys.every((v) => v === 'stopped'),
    };
  }

  /**
   * Plain-language state of a group of resources of one kind.
   * One: "database available". Several: "2 databases available", "1 of 3 applications running".
   */
  function describeGroup(kind, keys) {
    const w = WORDS[kind], n = keys.length;
    if (!n) return '';
    const count = (k) => keys.filter((x) => x === k).length;
    const label = (k) => labelFor(k, {}, kind).toLowerCase();
    if (n === 1) return `${w.one} ${label(keys[0])}`;
    if (keys.every((k) => k === keys[0])) return `${n} ${w.many} ${label(keys[0])}`;
    for (const k of ['error', 'starting', 'stopping']) if (count(k)) return `${count(k)} of ${n} ${w.many} ${label(k)}`;
    return `${count('ok')} of ${n} ${w.many} ${label('ok')}`;
  }

  /** Short value for an overview card: "Available", "All 3 stopped", "2 of 3 running". */
  function groupValue(kind, keys) {
    const n = keys.length;
    if (!n) return '';
    const count = (k) => keys.filter((x) => x === k).length;
    const label = (k) => labelFor(k, {}, kind).toLowerCase();
    if (n === 1) return labelFor(keys[0], {}, kind);
    if (keys.every((k) => k === keys[0])) return `All ${n} ${label(keys[0])}`;
    for (const k of ['error', 'starting', 'stopping']) if (count(k)) return `${count(k)} of ${n} ${label(k)}`;
    return `${count('ok')} of ${n} ${label('ok')}`;
  }

  async function api(path, method = 'GET') {
    const res = await fetch(path, {
      method,
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', ...(method !== 'GET' ? { 'X-CSRF-Token': csrf } : {}) },
    });
    if (res.status === 401) { window.location.href = '/login'; throw new Error('Session expired.'); }
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON error body */ }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && (data.error || data.message)) || `Request failed (${res.status}).`);
    }
    return data || {};
  }

  function toast(kind, text) {
    const box = $('#toasts');
    const t = document.createElement('div');
    t.className = 'toast'; t.dataset.kind = kind;
    t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    const p = document.createElement('p'); p.textContent = text;
    const x = document.createElement('button'); x.type = 'button'; x.setAttribute('aria-label', 'Dismiss'); x.textContent = '\u00d7';
    const close = () => t.remove();
    x.addEventListener('click', close);
    t.append(p, x);
    box.append(t);
    while (box.children.length > 3) box.firstElementChild.remove();
    setTimeout(close, kind === 'error' ? 9000 : 5000);
  }

  function confirmStop({ title, body, ok }) {
    const dlg = $('#confirm');
    $('#confirm-title').textContent = title;
    $('#confirm-body').textContent = body;
    $('#confirm-ok').textContent = ok;
    return new Promise((resolve) => {
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'), { once: true });
      dlg.returnValue = 'cancel';
      dlg.showModal();
    });
  }

  const POLL = { normal: 15000, fast: 4000 };
  /** Plain-language length of time: "less than a minute", "42m", "3h 12m", "2d 5h". */
  const durationLabel = (secs) => {
    const s = Math.max(0, Math.floor(secs));
    if (s < 60) return 'less than a minute';
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  };
  const uptimeLabel = (secs) => `Uptime - ${durationLabel(secs)}`;
  const secondsAgo = (t) => { const s = Math.max(0, Math.round((Date.now() - t) / 1000)); return s < 5 ? 'Updated just now' : `Updated ${s}s ago`; };

  return { $, capitalise, classify, labelFor, isTransition, summarize, envButtons, describeGroup, groupValue, api, toast, confirmStop, POLL, secondsAgo, WORDS, durationLabel, uptimeLabel };
})();
