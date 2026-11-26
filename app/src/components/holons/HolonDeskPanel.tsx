import { Network } from 'lucide-react';
import { useAsync } from '../../hooks/useApi';
import { api } from '../../lib/api';
import { Card, EmptyState, ErrorNote, Spinner, Table } from '../ui';

// Holon Desk — read-only view of the holons registered in holons/registry.json.
// Holons hosted in another repo (e.g. x_deepmap in Perceptopoly) are listed as
// external so the whole Holon Hub is visible from one place.
export default function HolonDeskPanel() {
  const holons = useAsync(() => api.holons(), []);

  return (
    <Card title="Holon Desk">
      {holons.loading && <Spinner label="Loading holons…" />}
      {holons.error && <ErrorNote message={holons.error} />}
      {holons.data && holons.data.holons.length === 0 && (
        <EmptyState icon={<Network className="h-6 w-6" aria-hidden="true" />} title="No holons registered" hint="Add entries to holons/registry.json." />
      )}
      {holons.data && holons.data.holons.length > 0 && (
        <Table
          head={
            <tr>
              <th className="px-3 py-2">Holon</th>
              <th className="px-3 py-2">Version</th>
              <th className="px-3 py-2">Axis</th>
              <th className="px-3 py-2">Host repo</th>
              <th className="px-3 py-2">Capabilities</th>
            </tr>
          }
        >
          {holons.data.holons.map((h) => (
            <tr key={h.holon_id}>
              <td className="px-3 py-2">
                <div className="font-medium text-slate-100">{h.name}</div>
                <div className="text-2xs text-slate-500">{h.holon_id}</div>
              </td>
              <td className="px-3 py-2">{h.version ?? '—'}</td>
              <td className="px-3 py-2">
                {h.axis}
                {h.expansion ? <span className="ml-1 text-2xs text-slate-500">({h.expansion})</span> : null}
              </td>
              <td className="px-3 py-2">
                {h.hostRepo}
                {h.external && <span className="badge ml-2 bg-slate-800 text-slate-300">external</span>}
              </td>
              <td className="px-3 py-2 text-2xs text-slate-400">{h.capabilities.join(', ') || '—'}</td>
            </tr>
          ))}
        </Table>
      )}
    </Card>
  );
}
