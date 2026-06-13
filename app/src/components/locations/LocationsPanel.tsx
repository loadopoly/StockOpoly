import { useState } from 'react';
import { Grid3x3, MapPin, Plus } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { num } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Stat, Table } from '../ui';

export default function LocationsPanel({ onActivity }: { onActivity: () => void }) {
  const locations = useAsync(() => api.locations(), []);
  const travel = useAsync(() => api.travelCosts(), []);
  const [codes, setCodes] = useState('');
  const [coordInfo, setCoordInfo] = useState<string | null>(null);

  const register = useAction(async () => {
    const list = codes
      .split(/[\s,]+/)
      .map((c) => c.trim())
      .filter(Boolean);
    if (!list.length) return;
    const r = await api.registerLocations(list);
    announce(`Registered ${r.added}, updated ${r.updated}, ${r.failed.length} unparsed`);
    setCodes('');
    locations.reload();
    onActivity();
  });

  const compute = useAction(async () => {
    const r = await api.computeCoordinates();
    setCoordInfo(`${r.located} located across ${r.aisles} aisles · bay width ${r.bay_width_in.toFixed(0)}″`);
    announce('Coordinates computed');
    locations.reload();
    travel.reload();
  });

  const costs = travel.data?.costs ?? {};
  const rows = locations.data?.locations ?? [];

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card title="Register locations" className="lg:col-span-2">
          <p className="mb-2 text-sm text-slate-400">
            Paste location codes (space, comma, or newline separated). The configurable grammar parses
            aisle/bay/level/bin — e.g. <code className="text-primary-300">A-01-2-B</code>.
          </p>
          <textarea
            className="input min-h-[96px] font-mono"
            placeholder="A-01-1-A A-01-1-B A-02-1 B-01-1 …"
            value={codes}
            onChange={(e) => setCodes(e.target.value)}
            aria-label="Location codes"
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <button className="btn-primary" onClick={() => register.run()} disabled={register.pending}>
              <Plus className="h-4 w-4" aria-hidden="true" />
              Register
            </button>
            <button className="btn-ghost" onClick={() => compute.run()} disabled={compute.pending}>
              <Grid3x3 className="h-4 w-4" aria-hidden="true" />
              {compute.pending ? 'Computing…' : 'Compute coordinates'}
            </button>
          </div>
          {coordInfo && <p className="mt-3 text-xs text-primary-300">{coordInfo}</p>}
          {(register.error || compute.error) && (
            <div className="mt-3">
              <ErrorNote message={register.error ?? compute.error ?? ''} />
            </div>
          )}
        </Card>

        <Card title="Summary">
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Locations" value={num(rows.length)} />
            <Stat label="With coords" value={num(rows.filter((r) => r.x !== null).length)} />
            <Stat label="Aisles" value={num(new Set(rows.map((r) => r.aisle)).size)} />
            <Stat label="Blocked" value={num(rows.filter((r) => r.status === 'blocked').length)} />
          </div>
        </Card>
      </div>

      <Card title="Location space">
        {locations.loading && <Spinner />}
        {locations.error && <ErrorNote message={locations.error} />}
        {rows.length === 0 && !locations.loading && (
          <EmptyState
            icon={<MapPin className="h-8 w-8" />}
            title="No locations"
            hint="Register codes above, or import inventory (which auto-registers locations)."
          />
        )}
        {rows.length > 0 && (
          <Table
            head={
              <tr>
                <th className="px-3 py-2">Code</th>
                <th className="px-3 py-2">Aisle/Bay/Lvl</th>
                <th className="px-3 py-2 text-right">x, y, z</th>
                <th className="px-3 py-2 text-right">Capacity</th>
                <th className="px-3 py-2 text-right">Travel</th>
                <th className="px-3 py-2">Status</th>
              </tr>
            }
          >
            {rows.slice(0, 200).map((l) => (
              <tr key={l.location_code} className="hover:bg-slate-900/60">
                <td className="px-3 py-2 font-mono text-slate-200">{l.location_code}</td>
                <td className="px-3 py-2 text-slate-400">
                  {l.aisle}/{l.bay}/{l.level}
                  {l.bin ? `/${l.bin}` : ''}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {l.x !== null ? `${num(l.x)}, ${num(l.y)}, ${num(l.z)}` : '—'}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                  {l.capacity_volume ? `${num(l.capacity_volume)} in³` : '—'}
                </td>
                <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                  {costs[l.location_code] != null ? `${num(costs[l.location_code])}″` : '—'}
                </td>
                <td className="px-3 py-2">
                  <span
                    className={`badge ${
                      l.status === 'active'
                        ? 'bg-success-500/20 text-success-500'
                        : 'bg-slate-600/40 text-slate-300'
                    }`}
                  >
                    {l.status}
                  </span>
                </td>
              </tr>
            ))}
          </Table>
        )}
        {rows.length > 200 && (
          <p className="mt-2 text-2xs text-slate-500">Showing first 200 of {num(rows.length)}.</p>
        )}
      </Card>
    </div>
  );
}
