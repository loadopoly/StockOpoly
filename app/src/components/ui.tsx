import type { ReactNode } from 'react';
import { AlertTriangle, Loader2 } from 'lucide-react';

export function Card({
  title,
  actions,
  children,
  className = '',
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="text-base font-semibold text-slate-100">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Spinner({ label = 'Loading…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-slate-400" role="status">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <div
      className="flex items-start gap-2 rounded-lg border border-error-600/40 bg-error-600/10 p-3 text-sm text-error-500"
      role="alert"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}

export function EmptyState({ icon, title, hint }: { icon?: ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-12 text-center">
      {icon && <div className="text-slate-600">{icon}</div>}
      <p className="text-sm font-medium text-slate-300">{title}</p>
      {hint && <p className="max-w-md text-xs text-slate-500">{hint}</p>}
    </div>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/60 p-3">
      <div className="label mb-0.5">{label}</div>
      <div className="text-xl font-semibold text-slate-100">{value}</div>
      {hint && <div className="mt-0.5 text-2xs text-slate-500">{hint}</div>}
    </div>
  );
}

export function Field({ label, children, htmlFor }: { label: string; children: ReactNode; htmlFor?: string }) {
  return (
    <label className="block" htmlFor={htmlFor}>
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

export function Table({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full border-collapse text-sm">
        <thead className="bg-slate-900/80 text-left text-2xs uppercase tracking-wide text-slate-400">
          {head}
        </thead>
        <tbody className="divide-y divide-slate-800">{children}</tbody>
      </table>
    </div>
  );
}
