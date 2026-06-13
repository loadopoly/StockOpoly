import { useRef, useState } from 'react';
import { Boxes, Check, Globe, Ruler, Upload, X } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { abcColor, inches, money, num } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Table } from '../ui';
import type { CrawlResult } from '../../lib/types';

const IMPORT_TARGETS: { id: string; label: string }[] = [
  { id: 'parts', label: 'Parts' },
  { id: 'inventory', label: 'Inventory' },
  { id: 'po', label: 'PO history' },
  { id: 'usage', label: 'Usage' },
];

export default function InventoryPanel({ onActivity }: { onActivity: () => void }) {
  const [query, setQuery] = useState('');
  const parts = useAsync(() => api.parts(query || undefined), [query]);
  const crawls = useAsync(() => api.crawlResults(false), []);
  const erp = useAsync(() => api.erpStatus(), []);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const fileInputs = useRef<Record<string, HTMLInputElement | null>>({});

  const importFile = useAction(async (target: string, file: File) => {
    const r = await api.importFile(target, file);
    setImportMsg(`${target}: imported ${r.imported}, skipped ${r.skipped}`);
    announce(`Imported ${r.imported} ${target} rows`);
    parts.reload();
    onActivity();
  });

  const normalize = useAction(async () => {
    const r = await api.normalize();
    setImportMsg(`Normalized ${r.normalized} parts (${r.missing_dims} missing dims)`);
    announce('Containers normalized');
    parts.reload();
  });

  const accept = useAction(async (c: CrawlResult) => {
    await api.acceptCrawl(c.id);
    announce('Crawl result accepted');
    crawls.reload();
    parts.reload();
  });
  const reject = useAction(async (c: CrawlResult) => {
    await api.rejectCrawl(c.id);
    crawls.reload();
  });

  return (
    <div className="space-y-4">
      <Card
        title="Import data"
        actions={
          <button className="btn-ghost" onClick={() => normalize.run()} disabled={normalize.pending}>
            <Ruler className="h-4 w-4" aria-hidden="true" />
            Normalize containers
          </button>
        }
      >
        <p className="mb-3 text-sm text-slate-400">
          Upload CSV or XLSX exports. Headers are matched by synonym, so typical ERP/WMS column names
          just work. Inventory auto-registers its locations.
        </p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {IMPORT_TARGETS.map((t) => (
            <div key={t.id}>
              <button
                className="btn-ghost w-full flex-col gap-1 py-4"
                onClick={() => fileInputs.current[t.id]?.click()}
                disabled={importFile.pending}
              >
                <Upload className="h-5 w-5" aria-hidden="true" />
                {t.label}
              </button>
              <input
                ref={(el) => {
                  fileInputs.current[t.id] = el;
                }}
                type="file"
                accept=".csv,.xlsx"
                className="sr-only"
                aria-label={`Import ${t.label} file`}
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) importFile.run(t.id, f);
                  e.target.value = '';
                }}
              />
            </div>
          ))}
        </div>
        {importMsg && <p className="mt-3 text-xs text-primary-300">{importMsg}</p>}
        {importFile.error && <div className="mt-3"><ErrorNote message={importFile.error} /></div>}
        {erp.data && (
          <p className="mt-3 text-2xs text-slate-500">
            ERP bridge: {erp.data.importable ? 'ready' : erp.data.reason ?? 'disabled'}
          </p>
        )}
      </Card>

      <Card
        title="Parts"
        actions={
          <input
            className="input max-w-[200px]"
            placeholder="Search…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search parts"
          />
        }
      >
        {parts.loading && <Spinner />}
        {parts.error && <ErrorNote message={parts.error} />}
        {parts.data && parts.data.parts.length === 0 && (
          <EmptyState icon={<Boxes className="h-8 w-8" />} title="No parts" hint="Import a parts or inventory file." />
        )}
        {parts.data && parts.data.parts.length > 0 && (
          <Table
            head={
              <tr>
                <th className="px-3 py-2">Part</th>
                <th className="px-3 py-2">Class</th>
                <th className="px-3 py-2 text-right">Cost</th>
                <th className="px-3 py-2 text-right">Dims (L×W×H)</th>
                <th className="px-3 py-2 text-right">MoS</th>
                <th className="px-3 py-2 text-right">SS / Min / Max</th>
              </tr>
            }
          >
            {parts.data.parts.slice(0, 150).map((p) => (
              <tr key={p.part_number} className="hover:bg-slate-900/60">
                <td className="px-3 py-2">
                  <div className="font-mono font-medium text-slate-200">{p.part_number}</div>
                  <div className="text-2xs text-slate-500">{p.description}</div>
                </td>
                <td className="px-3 py-2">
                  {p.abc_class ? (
                    <span className={`badge ${abcColor(p.abc_class)}`}>
                      {p.abc_class}
                      {p.xyz_class}
                    </span>
                  ) : (
                    <span className="text-2xs text-slate-600">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{money(p.unit_cost)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {p.dim_l ? `${inches(p.dim_l)}×${inches(p.dim_w)}×${inches(p.dim_h)}` : '—'}
                  {p.dim_source && <span className="ml-1 text-2xs text-slate-600">({p.dim_source})</span>}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {p.months_of_supply != null ? p.months_of_supply.toFixed(1) : '—'}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {p.ss_qty != null ? `${num(p.ss_qty)} / ${num(p.min_qty)} / ${num(p.max_qty)}` : '—'}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card
        title="Supplier dimension crawler"
        actions={<span className="text-2xs text-slate-500">{num(crawls.data?.results.length)} pending</span>}
      >
        <p className="mb-3 flex items-center gap-2 text-sm text-slate-400">
          <Globe className="h-4 w-4" aria-hidden="true" />
          Candidates are extracted from supplier pages but never touch a part until you accept them.
        </p>
        {crawls.loading && <Spinner />}
        {crawls.data && crawls.data.results.length === 0 && (
          <EmptyState title="No pending candidates" hint="Crawl runs are triggered per part with a supplier URL." />
        )}
        <div className="space-y-2">
          {crawls.data?.results.map((c) => (
            <div
              key={c.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-900/50 p-3"
            >
              <div className="min-w-0">
                <div className="font-mono text-sm text-slate-200">{c.part_number}</div>
                <div className="text-2xs text-slate-500">
                  {inches(c.dim_l)}×{inches(c.dim_w)}×{inches(c.dim_h)}
                  {c.weight ? ` · ${num(c.weight)} lb` : ''} · conf {(c.confidence * 100).toFixed(0)}%
                </div>
                {c.provenance_excerpt && (
                  <div className="mt-1 max-w-xl truncate text-2xs italic text-slate-600">
                    “{c.provenance_excerpt}”
                  </div>
                )}
              </div>
              <div className="flex gap-1">
                <button className="btn-primary px-2.5" onClick={() => accept.run(c)} aria-label="Accept candidate">
                  <Check className="h-4 w-4" aria-hidden="true" />
                </button>
                <button className="btn-ghost px-2.5" onClick={() => reject.run(c)} aria-label="Reject candidate">
                  <X className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
