// Same theme as the app: its saved toggle (ff-dark) wins; with no saved choice,
// follow the OS like the app does (launch audit 2026-10-06 U-6).
// Loaded synchronously in <head> so the page never flashes the wrong theme;
// external (not inline) so the pages' CSP can stay script-src 'self'.
(function(){var s=null;try{s=localStorage.getItem('ff-dark')}catch(_){}
if(s==='1'||(s===null&&window.matchMedia&&matchMedia('(prefers-color-scheme: dark)').matches))document.documentElement.classList.add('dark')})();
