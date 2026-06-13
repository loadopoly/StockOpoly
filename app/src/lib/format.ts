export function inches(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined) return '—';
  return `${v.toFixed(digits)}″`;
}

export function num(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return '—';
  return v.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return '—';
  return `${(v * 100).toFixed(digits)}%`;
}

export function money(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return v.toLocaleString(undefined, { style: 'currency', currency: 'USD' });
}

export function shortId(id: string, n = 8): string {
  return id.length > n ? `${id.slice(0, n)}…` : id;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

// ABC×XYZ class → tailwind badge classes.
export function abcColor(cls: string | null): string {
  switch (cls) {
    case 'A':
      return 'bg-success-500/20 text-success-500';
    case 'B':
      return 'bg-primary-500/20 text-primary-300';
    case 'C':
      return 'bg-slate-600/40 text-slate-300';
    default:
      return 'bg-slate-700/40 text-slate-400';
  }
}

export function occColor(status: string | null | undefined): string {
  switch (status) {
    case 'over':
      return 'bg-error-500/20 text-error-500';
    case 'tight':
      return 'bg-warning-500/20 text-warning-500';
    case 'ok':
      return 'bg-success-500/20 text-success-500';
    case 'empty':
      return 'bg-slate-700/40 text-slate-400';
    default:
      return 'bg-slate-700/40 text-slate-400';
  }
}
