/* Theme: dark by default, light on request. Loaded synchronously in <head> (before first paint) so there is no flash.
   The choice is remembered in localStorage and shared across tabs. Without JS the CSS default (dark) applies. */
(function () {
  'use strict';
  var KEY = 'l180-theme', root = document.documentElement;
  var COLORS = { dark: '#0B1220', light: '#FCFCFA' };      // browser UI colour (mobile address bar)

  function stored() {
    try { var v = localStorage.getItem(KEY); return v === 'light' || v === 'dark' ? v : null; } catch (e) { return null; }
  }
  function apply(theme) {
    root.setAttribute('data-theme', theme);
    root.setAttribute('data-bs-theme', theme);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) { meta = document.createElement('meta'); meta.name = 'theme-color'; document.head.appendChild(meta); }
    meta.content = COLORS[theme];
    var label = theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';
    var buttons = document.querySelectorAll('.theme-toggle');
    for (var i = 0; i < buttons.length; i++) { buttons[i].setAttribute('aria-label', label); buttons[i].title = label; }
  }
  function set(theme) {
    try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode: still works for this page */ }
    apply(theme);
  }

  apply(stored() || 'dark');
  window.L180Theme = { get: function () { return root.getAttribute('data-theme'); }, set: set };

  document.addEventListener('DOMContentLoaded', function () {
    apply(root.getAttribute('data-theme') || 'dark');      // buttons now exist: set their labels
    var buttons = document.querySelectorAll('.theme-toggle');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].addEventListener('click', function () { set(root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark'); });
    }
  });
  window.addEventListener('storage', function (e) {          // another tab changed it
    if (e.key === KEY && (e.newValue === 'light' || e.newValue === 'dark')) apply(e.newValue);
  });
})();
