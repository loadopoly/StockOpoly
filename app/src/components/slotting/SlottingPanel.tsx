import { useState } from 'react';
import { Gauge, Layers, ShieldCheck } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { abcColor, num, occColor, pct } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Stat, Table } from '../ui';

const SCENARIOS = ['conservative', 'baseline', 'aggressive'];

export default function SlottingPanel({ onActivity }: { onActivity: () => void }) {
  const velocity = useAsync(() => api.velocity(), []);
  const occupancy = useAsync(() => api.currentState(), []);
  const ss = useAsync(() => api.safetyStock(), []);
  const [scenario, setScenario] = useState('baseline');

  const computeVel = useAction(async () => {
    const r = await api.computeVelocity();
    announce(`Velocity computed for ${r.parts} parts`);
    velocity.reload();
    onActivity();
  });
  const computeOcc = useAction(async () => {
    const r = await api.computeOccupancy();
    announce(`Occupancy: ${r.over} over, ${r.tight} tight, ${r.ok} ok`);
    occupancy.reload();
  });
  const computeSs = useAction(async () => {
    const r = await api.computeSafetyStock(scenario);
    announce(`Safety stock computed (${r.scenario})`);
    ss.reload();
  });

  const occRows = occupancy.data?.locations ?? [];
  const occCounts = occRows.reduce<Record<string, number>>((acc, r) => {
    acc[r.status] = (acc[r.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="space-y-4">
      <Card
        title="Velocity (ABC × XYZ)"
        actions={
          <button className="btn-primary" onClick={() => computeVel.run()} disabled={computeVel.pending}>
            <Gauge className="h-4 w-4" aria-hidden="true" />
            {computeVel.pending ? 'Computing…' : 'Compute velocity'}
          </button>
        }
      >
        {velocity.loading && <Spinner />}
        {computeVel.error && <ErrorNote message={computeVel.error} />}
        {velocity.data && velocity.data.velocity.length === 0 && (
          <EmptyState icon={<Gauge className="h-8 w-8" />} title="No velocity yet" hint="Import usage data, then compute." />
        )}
        {velocity.data && velocity.data.velocity.length > 0 && (
          <Table
            head={
              <tr>
                <th className="px-3 py-2">Part</th>
                <th className="px-3 py-2">Class</th>
                <th className="px-3 py-2 text-right">12-mo qty</th>
                <th className="px-3 py-2 text-right">12-mo value</th>
                <th className="px-3 py-2 text-right">Hits</th>
                <th className="px-3 py-2 text-right">Score</th>
              </tr>
            }
          >
            {velocity.data.velocity.slice(0, 100).map((v) => (
              <tr key={v.part_number} className="hover:bg-slate-900/60">
                <td className="px-3 py-2 font-mono text-slate-200">{v.part_number}</td>
                <td className="px-3 py-2">
                  <span className={`badge ${abcColor(v.abc_class)}`}>
                    {v.abc_class}
                    {v.xyz_class}
                  </span>
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">{num(v.usage_12m_qty)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">{num(v.usage_12m_value)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">{num(v.monthly_hits)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-primary-300">
                  {v.velocity_score.toFixed(3)}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card
        title="Current-state occupancy"
        actions={
          <button className="btn-primary" onClick={() => computeOcc.run()} disabled={computeOcc.pending}>
            <Layers className="h-4 w-4" aria-hidden="true" />
            {computeOcc.pending ? 'Computing…' : 'Compute occupancy'}
          </button>
        }
      >
        {occRows.length > 0 && (
          <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Over" value={num(occCounts.over ?? 0)} />
            <Stat label="Tight" value={num(occCounts.tight ?? 0)} />
            <Stat label="OK" value={num(occCounts.ok ?? 0)} />
            <Stat label="Empty" value={num(occCounts.empty ?? 0)} />
          </div>
        )}
        {occupancy.loading && <Spinner />}
        {computeOcc.error && <ErrorNote message={computeOcc.error} />}
        {occRows.length === 0 && !occupancy.loading && (
          <EmptyState icon={<Layers className="h-8 w-8" />} title="No occupancy yet" hint="Import inventory + compute coordinates, then compute occupancy." />
        )}
        {occRows.length > 0 && (
          <Table
            head={
              <tr>
                <th className="px-3 py-2">Location</th>
                <th className="px-3 py-2 text-right">Fill</th>
                <th className="px-3 py-2 text-right">Parts</th>
                <th className="px-3 py-2">Status</th>
                <th className="px-3 py-2">Top contents</th>
              </tr>
            }
          >
            {occRows.slice(0, 100).map((o) => (
              <tr key={o.location_code} className="hover:bg-slate-900/60">
                <td className="px-3 py-2 font-mono text-slate-200">{o.location_code}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{pct(o.occupancy_pct)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">{num(o.part_count)}</td>
                <td className="px-3 py-2">
                  <span className={`badge ${occColor(o.status)}`}>{o.status}</span>
                </td>
                <td className="px-3 py-2 text-2xs text-slate-500">
                  {o.inventory
                    .slice(0, 3)
                    .map((i) => `${i.part_number}×${num(i.qty_oh)}`)
                    .join(', ') || '—'}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card
        title="Safety stock"
        actions={
          <div className="flex items-center gap-2">
            <select
              className="input max-w-[150px]"
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
              aria-label="Safety stock scenario"
            >
              {SCENARIOS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
            <button className="btn-primary" onClick={() => computeSs.run()} disabled={computeSs.pending}>
              <ShieldCheck className="h-4 w-4" aria-hidden="true" />
              Compute
            </button>
          </div>
        }
      >
        {ss.loading && <Spinner />}
        {computeSs.error && <ErrorNote message={computeSs.error} />}
        {ss.data && ss.data.ss.length === 0 && (
          <EmptyState icon={<ShieldCheck className="h-8 w-8" />} title="No safety stock yet" hint="Import usage + PO history, then compute." />
        )}
        {ss.data && ss.data.ss.length > 0 && (
          <Table
            head={
              <tr>
                <th className="px-3 py-2">Part</th>
                <th className="px-3 py-2 text-right">Demand μ/σ (mo)</th>
                <th className="px-3 py-2 text-right">Lead (d)</th>
                <th className="px-3 py-2 text-right">SS</th>
                <th className="px-3 py-2 text-right">Min</th>
                <th className="px-3 py-2 text-right">Max</th>
              </tr>
            }
          >
            {ss.data.ss.slice(0, 100).map((s) => (
              <tr key={s.part_number} className="hover:bg-slate-900/60">
                <td className="px-3 py-2 font-mono text-slate-200">{s.part_number}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {num(s.demand_mean_m, 1)} / {num(s.demand_std_m, 1)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">{num(s.lead_time_days, 1)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{num(s.ss_qty, 1)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{num(s.min_qty, 1)}</td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">{num(s.max_qty, 1)}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}
