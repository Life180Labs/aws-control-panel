/* Preview-only: fakes the Flask API in the browser so the UI can be tried without AWS.
   Client, instance and IDs other than Uniservice's name are placeholders. Not part of the app. */
(() => {
  const res = (id, name, state) => ({ id, name, state, error: null });
  const C = {
    uniservice: { slug: 'uniservice', name: 'Uniservice', region: 'ap-south-1', operation: null,
      rds: [res('uniservice-db', 'Primary database', 'available'), res('uniservice-reporting-db', 'Reporting database', 'available')],
      ec2: [res('i-0a4f81c2e9b7d3056', 'Web server', 'running'), res('i-0b7e22d4f1a96c835', 'API server', 'running'), res('i-03c91a5e7d2f48b60', 'Background worker', 'running')] },
    harbor: { slug: 'harbor', name: 'Harbor Logistics', region: 'eu-west-1', operation: null,
      rds: [res('harbor-db', 'Main database', 'stopped')],
      ec2: [res('i-05d1c7a39e84b2f10', 'Tracking app', 'stopped'), res('i-09e3b6c1a7f25d480', 'Dispatch app', 'stopped')] },
    northwind: { slug: 'northwind', name: 'Northwind Retail', region: 'us-east-1', operation: null,
      rds: [],
      ec2: [res('i-06a8d3f5c1b97e204', 'Storefront', 'running')] },
  };
  const mock = (window.__mock = { failNext: false });
  const later = (ms, fn) => setTimeout(fn, ms);
  const reply = (body, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const moving = (s) => ['pending', 'starting', 'stopping'].includes(s);
  const all = (c) => [...c.rds, ...c.ec2];
  const busy = (c) => (c.operation && c.operation.status === 'running');
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const kindOf = (c, r) => (c.rds.includes(r) ? 'rds' : 'ec2');
  const up = (kind) => (kind === 'rds' ? 'available' : 'running');
  const mid = (kind) => (kind === 'rds' ? 'starting' : 'pending');
  const ms = (kind) => (kind === 'rds' ? 9000 : 6000);

  function begin(c, r, action) {          // returns false when the resource is not eligible
    const kind = kindOf(c, r);
    if (action === 'start' && r.state === 'stopped') { r.state = mid(kind); later(ms(kind), () => { r.state = up(kind); }); return true; }
    if (action === 'stop' && r.state === up(kind)) { r.state = 'stopping'; later(ms(kind), () => { r.state = 'stopped'; }); return true; }
    return false;
  }

  function resources(c, kind, id, action) {
    const word = kind === 'rds' ? 'database' : 'application';
    const targets = id ? c[kind].filter((r) => r.id === id) : c[kind];
    if (!targets.length) return reply({ ok: false, error: 'That resource is not set up for this client.' }, 404);
    if (targets.some((r) => moving(r.state)) && id) return reply({ ok: false, error: `${targets[0].name} is already ${targets[0].state === 'stopping' ? 'stopping' : 'starting'}.` }, 409);
    const started = targets.filter((r) => begin(c, r, action));
    if (!started.length) return reply({ ok: false, error: id ? `${targets[0].name} is already ${action === 'start' ? up(kind) : 'stopped'}.` : `Every ${word} is already ${action === 'start' ? up(kind) : 'stopped'}.` }, 409);
    const verb = action === 'start' ? 'Starting' : 'Stopping';
    return reply({ ok: true, message: id ? `${verb} ${targets[0].name}.` : `${verb} ${started.length} ${started.length === 1 ? word : word + 's'}.` }, 202);
  }

  function environment(c, action) {
    const hasDb = c.rds.length > 0;
    const first = action === 'start' ? c.rds : c.ec2, second = action === 'start' ? c.ec2 : c.rds;
    const steps = action === 'start'
      ? (hasDb ? ['Start databases', 'Wait until available', 'Start applications'] : ['Start applications'])
      : (hasDb ? ['Stop applications', 'Wait until stopped', 'Stop databases'] : ['Stop applications']);
    const op = (c.operation = { id: String(Date.now()), type: action, status: 'running', step: 0, message: '', steps });
    const finish = () => { op.status = 'done'; op.step = steps.length; op.message = action === 'start' ? 'The environment is running.' : 'The environment is stopped.'; };
    const phase1 = hasDb ? first : c.ec2;
    phase1.forEach((r) => begin(c, r, action));
    op.message = (action === 'start' ? 'Starting ' : 'Stopping ') + (hasDb ? (action === 'start' ? 'the databases.' : 'the applications.') : 'the applications.');
    if (hasDb) {
      later(1500, () => { op.step = 1; op.message = action === 'start' ? 'Waiting for the databases to become available.' : 'Waiting for the applications to stop.'; });
      later(7500, () => {
        op.step = 2; op.message = action === 'start' ? 'Starting the applications.' : 'Stopping the databases.';
        second.forEach((r) => begin(c, r, action));
      });
      later(13500, () => finish());
    } else later(6500, () => finish());
    return reply({ ok: true, message: op.message }, 202);
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (url, opts = {}) => {
    const path = typeof url === 'string' ? url : url.url;
    if (!path.startsWith('/api/')) return realFetch(url, opts);
    await new Promise((r) => setTimeout(r, 250));
    const method = (opts.method || 'GET').toUpperCase();

    if (path === '/api/overview' && method === 'GET') {
      return reply({ clients: Object.values(C).map((c) => ({
        slug: c.slug, name: c.name, region: c.region, operation: clone(c.operation),
        rds: c.rds.map((r) => ({ state: r.state })), ec2: c.ec2.map((r) => ({ state: r.state })) })) });
    }
    const m = path.match(/^\/api\/c\/([a-z0-9-]+)\/(?:(status)|(environment)\/(start|stop)|(ec2|rds)(?:\/([A-Za-z0-9-]+))?\/(start|stop))$/);
    const c = m && C[m[1]];
    if (!c) return reply({ ok: false, error: 'Unknown client.' }, 404);
    if (m[2] === 'status' && method === 'GET') {
      return reply({ client: { slug: c.slug, name: c.name }, region: c.region, rds: clone(c.rds), ec2: clone(c.ec2), operation: clone(c.operation) });
    }
    if (method !== 'POST') return reply({ ok: false, error: 'Not found.' }, 404);
    if (mock.failNext) { mock.failNext = false; return reply({ ok: false, error: "The dashboard isn't allowed to perform this action in AWS. Check the IAM policy." }, 403); }
    if (busy(c)) return reply({ ok: false, error: 'Another operation is already in progress for this client.' }, 409);
    return m[3] ? environment(c, m[4]) : resources(c, m[5], m[6], m[7]);
  };
})();
