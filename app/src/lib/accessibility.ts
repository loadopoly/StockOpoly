// Screen-reader announcements via a polite live region (mirrors the
// Loadopoly-OCR announce() pattern). A single shared region is created lazily.
let region: HTMLElement | null = null;

function ensureRegion(): HTMLElement {
  if (region) return region;
  const el = document.createElement('div');
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.setAttribute('aria-atomic', 'true');
  el.className = 'sr-only';
  document.body.appendChild(el);
  region = el;
  return el;
}

export function announce(message: string): void {
  const el = ensureRegion();
  // Clear first so repeated identical messages are still announced.
  el.textContent = '';
  window.setTimeout(() => {
    el.textContent = message;
  }, 30);
}
