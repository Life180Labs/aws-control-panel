// Shows a loading state while the browser is redirected to Google.
(() => {
  const btn = document.getElementById('google-btn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    btn.classList.add('is-loading');
    btn.setAttribute('aria-busy', 'true');
    btn.querySelector('span:last-child').textContent = 'Redirecting to Google';
  });
  // Coming back with the browser's back button restores the page from cache: reset the button.
  addEventListener('pageshow', (e) => {
    if (!e.persisted) return;
    btn.classList.remove('is-loading'); btn.removeAttribute('aria-busy');
    btn.querySelector('span:last-child').textContent = 'Sign in with Google';
  });
})();
