/* Left navigation: expandable "All Clients" list, slide-in drawer on small screens, live status dots.
   Needs common.js (window.L180) for the status dots; everything else works without it. */
(() => {
  'use strict';
  const $ = (sel, root = document) => root.querySelector(sel);
  const body = document.body;

  /* ---------- expand / collapse "All Clients" (remembered) ---------- */
  const KEY = 'l180-nav-open';
  const toggle = $('.nav-toggle'), list = $('#nav-clients');
  function setOpen(open, remember) {
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? 'Collapse client list' : 'Expand client list');
    list.hidden = !open;
    if (remember) { try { localStorage.setItem(KEY, open ? '1' : '0'); } catch (_) { /* private mode */ } }
  }
  let saved = null;
  try { saved = localStorage.getItem(KEY); } catch (_) { /* ignore */ }
  setOpen(saved !== '0', false);                       // open unless the user collapsed it
  toggle.addEventListener('click', () => setOpen(toggle.getAttribute('aria-expanded') !== 'true', true));

  /* ---------- drawer (below 992px the sidebar slides in over the page) ---------- */
  const burger = $('.nav-burger'), closeBtn = $('.nav-close'), scrim = $('.scrim'), sidebar = $('#sidebar');
  const behind = [$('.topbar'), $('.app-main > main')].filter(Boolean);   // made inert while the drawer is open
  const wide = window.matchMedia('(min-width: 992px)');

  function openNav() {
    body.classList.add('nav-open');
    burger.setAttribute('aria-expanded', 'true');
    behind.forEach((el) => { el.inert = true; });
    closeBtn.focus();
  }
  function closeNav(returnFocus) {
    if (!body.classList.contains('nav-open')) return;
    body.classList.remove('nav-open');
    burger.setAttribute('aria-expanded', 'false');
    behind.forEach((el) => { el.inert = false; });
    if (returnFocus) burger.focus();
  }
  burger.addEventListener('click', openNav);
  closeBtn.addEventListener('click', () => closeNav(true));
  scrim.addEventListener('click', () => closeNav(true));
  sidebar.addEventListener('click', (e) => { if (e.target.closest('a')) closeNav(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeNav(true); });
  wide.addEventListener('change', (e) => { if (e.matches) closeNav(false); });

  /* ---------- live status dot per client ---------- */
  const L = window.L180;
  if (!L) return;
  const links = new Map([...document.querySelectorAll('.nav-client')].map((a) => [a.dataset.slug, a]));
  let timer = null;

  async function refresh() {
    let fast = false;
    try {
      if (!document.hidden) {
        const data = await L.api('/api/overview');
        for (const c of data.clients) {
          const a = links.get(c.slug);
          if (!a) continue;
          const keys = [...(c.rds || []), ...(c.ec2 || [])].map(L.classify);
          const sum = L.summarize(keys, c.operation, true);
          $('.nav-dot', a).dataset.state = sum.state;
          $('.js-nav-status', a).textContent = ', ' + sum.title;
          a.title = sum.title;
          if (sum.state === 'starting' || sum.state === 'stopping') fast = true;
        }
      }
    } catch (_) { /* keep the last known dots; the page itself reports connection problems */ }
    finally { timer = setTimeout(refresh, fast ? L.POLL.fast : L.POLL.normal); }
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(timer); refresh(); } });
  refresh();
})();
