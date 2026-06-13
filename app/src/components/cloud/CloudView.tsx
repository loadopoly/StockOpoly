import { useRef, useState } from 'react';
import { Boxes, Cloud, Image as ImageIcon, MapPin, Upload } from 'lucide-react';
import { useAction, useAsync } from '../../hooks/useApi';
import {
  cloudCounts,
  cloudLocations,
  cloudParts,
  cloudPhotos,
  uploadCaptureToCloud,
} from '../../lib/cloud';
import { supabaseUrl } from '../../lib/supabase';
import { announce } from '../../lib/accessibility';
import { abcColor, money, num, occColor, relativeTime } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Stat, Table } from '../ui';

// Read-only Supabase dashboard for when there's no engine behind the app (the
// GitHub Pages deployment). The engine, run wherever capture/compute happens,
// mirrors everything here; this view reads it back and can upload new captures
// straight to Storage.
export default function CloudView() {
  const counts = useAsync(() => cloudCounts(), []);
  const parts = useAsync(() => cloudParts(), []);
  const locations = useAsync(() => cloudLocations(), []);
  const photos = useAsync(() => cloudPhotos(), []);
  const fileInput = useRef<HTMLInputElement>(null);
  const [message, setMessage] = useState<string | null>(null);

  const reloadAll = () => {
    counts.reload();
    photos.reload();
  };

  const upload = useAction(async (files: File[]) => {
    const r = await uploadCaptureToCloud(files);
    const msg = `Uploaded ${r.uploaded} photo${r.uploaded === 1 ? '' : 's'} to the cloud${
      r.failed ? ` (${r.failed} failed)` : ''
    }`;
    setMessage(msg);
    announce(msg);
    reloadAll();
  });

  const located = locations.data?.filter((l) => l.occ_status) ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 rounded-lg border border-primary-700/40 bg-primary-600/10 px-4 py-3 text-sm text-primary-200">
        <Cloud className="h-5 w-5 shrink-0" aria-hidden="true" />
        <p>
          Cloud mode — reading directly from Supabase
          {supabaseUrl() ? <span className="font-mono text-2xs"> ({supabaseUrl()})</span> : null}. No
          local engine is running; analytics and slotting run wherever the engine lives and sync
          here automatically.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Parts" value={num(counts.data?.parts)} />
        <Stat label="Locations" value={num(counts.data?.locations)} />
        <Stat label="Inventory" value={num(counts.data?.inventory)} />
        <Stat label="Plans" value={num(counts.data?.slotting_plans)} />
        <Stat label="Move tasks" value={num(counts.data?.move_tasks)} />
        <Stat label="Photos" value={num(counts.data?.photos)} />
      </div>

      <Card
        title="Capture upload"
        actions={
          <button
            className="btn-primary"
            onClick={() => fileInput.current?.click()}
            disabled={upload.pending}
          >
            <Upload className="h-4 w-4" aria-hidden="true" />
            {upload.pending ? 'Uploading…' : 'Upload photos'}
          </button>
        }
      >
        <p className="text-sm text-slate-400">
          Upload warehouse photos straight to the shared Supabase Storage bucket. The engine picks
          them up for grouping and dimensioning on its next pass.
        </p>
        {message && <p className="mt-2 text-sm text-success-500">{message}</p>}
        {upload.error && (
          <div className="mt-2">
            <ErrorNote message={upload.error} />
          </div>
        )}
        <input
          ref={fileInput}
          type="file"
          accept=".jpg,.jpeg,image/jpeg,image/png"
          multiple
          className="sr-only"
          aria-label="Photos to upload to the cloud"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            if (files.length) upload.run(files);
            e.target.value = '';
          }}
        />
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Parts">
          {parts.loading && <Spinner />}
          {parts.error && <ErrorNote message={parts.error} />}
          {parts.data && parts.data.length === 0 && (
            <EmptyState icon={<Boxes className="h-8 w-8" />} title="No parts synced yet" />
          )}
          {parts.data && parts.data.length > 0 && (
            <Table
              head={
                <tr>
                  <th className="px-3 py-2">Part</th>
                  <th className="px-3 py-2">Class</th>
                  <th className="px-3 py-2 text-right">Cost</th>
                </tr>
              }
            >
              {parts.data.slice(0, 100).map((p) => (
                <tr key={p.part_number}>
                  <td className="px-3 py-2">
                    <div className="font-medium text-slate-200">{p.part_number}</div>
                    <div className="truncate text-2xs text-slate-500">{p.description}</div>
                  </td>
                  <td className="px-3 py-2">
                    {p.abc_class ? (
                      <span className={`badge ${abcColor(p.abc_class)}`}>
                        {p.abc_class}
                        {p.xyz_class}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                    {money(p.unit_cost)}
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </Card>

        <Card title="Locations">
          {locations.loading && <Spinner />}
          {locations.error && <ErrorNote message={locations.error} />}
          {locations.data && locations.data.length === 0 && (
            <EmptyState icon={<MapPin className="h-8 w-8" />} title="No locations synced yet" />
          )}
          {locations.data && locations.data.length > 0 && (
            <Table
              head={
                <tr>
                  <th className="px-3 py-2">Location</th>
                  <th className="px-3 py-2">State</th>
                  <th className="px-3 py-2 text-right">Occupancy</th>
                </tr>
              }
            >
              {(located.length ? located : locations.data).slice(0, 100).map((l) => (
                <tr key={l.location_code}>
                  <td className="px-3 py-2 font-mono text-slate-200">{l.location_code}</td>
                  <td className="px-3 py-2">
                    {l.occ_status ? (
                      <span className={`badge ${occColor(l.occ_status)}`}>{l.occ_status}</span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                    {l.occupancy_pct == null ? '—' : `${Math.round(l.occupancy_pct * 100)}%`}
                  </td>
                </tr>
              ))}
            </Table>
          )}
        </Card>
      </div>

      <Card title="Recent photos">
        {photos.loading && <Spinner />}
        {photos.error && <ErrorNote message={photos.error} />}
        {photos.data && photos.data.length === 0 && (
          <EmptyState icon={<ImageIcon className="h-8 w-8" />} title="No photos uploaded yet" />
        )}
        {photos.data && photos.data.length > 0 && (
          <ul className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
            {photos.data.map((ph) => (
              <li
                key={ph.photo_id}
                className="overflow-hidden rounded-lg border border-slate-800 bg-slate-900/60"
              >
                {ph.remote_url ? (
                  <img
                    src={ph.remote_url}
                    alt={ph.file ?? ph.photo_id}
                    loading="lazy"
                    className="aspect-square w-full object-cover"
                  />
                ) : (
                  <div className="flex aspect-square items-center justify-center text-slate-600">
                    <ImageIcon className="h-6 w-6" aria-hidden="true" />
                  </div>
                )}
                <div className="truncate px-2 py-1 text-2xs text-slate-500">
                  {relativeTime(ph.captured_at)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
