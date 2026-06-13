import { useRef, useState } from 'react';
import { Camera, FileUp, Image, Package } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { num, relativeTime, shortId } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Table } from '../ui';
import type { Batch } from '../../lib/types';

export default function IntakePanel({ onActivity }: { onActivity: () => void }) {
  const batches = useAsync(() => api.batches(), []);
  const bundleInput = useRef<HTMLInputElement>(null);
  const looseInput = useRef<HTMLInputElement>(null);
  const [message, setMessage] = useState<string | null>(null);

  const uploadBundle = useAction(async (file: File) => {
    const r = await api.uploadBundle(file);
    const msg = r.duplicate
      ? `Bundle already ingested (${shortId(r.batch_id ?? '')})`
      : `Ingested bundle ${shortId(r.batch_id ?? '')}`;
    setMessage(msg);
    announce(msg);
    batches.reload();
    onActivity();
  });

  const uploadLoose = useAction(async (files: File[]) => {
    const r = await api.uploadLoose(files);
    setMessage(`Ingested ${r.photo_count} photos (${r.geotagged} geotagged)`);
    announce(`Ingested ${r.photo_count} photos`);
    batches.reload();
    onActivity();
  });

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Capture intake">
        <p className="mb-4 text-sm text-slate-400">
          Drop a <code className="text-primary-300">loadopoly.capture/1</code> bundle exported by the
          Operate Console, or a folder of loose JPEGs from any phone camera. Pose comes from the
          manifest or EXIF; fixity is verified on bundles.
        </p>

        <div className="grid gap-3 sm:grid-cols-2">
          <button
            className="btn-primary flex-col gap-2 py-6"
            onClick={() => bundleInput.current?.click()}
            disabled={uploadBundle.pending}
          >
            <Package className="h-6 w-6" aria-hidden="true" />
            {uploadBundle.pending ? 'Uploading…' : 'Upload capture bundle (.zip)'}
          </button>
          <button
            className="btn-ghost flex-col gap-2 py-6"
            onClick={() => looseInput.current?.click()}
            disabled={uploadLoose.pending}
          >
            <Image className="h-6 w-6" aria-hidden="true" />
            {uploadLoose.pending ? 'Uploading…' : 'Upload loose photos (.jpg)'}
          </button>
        </div>

        <input
          ref={bundleInput}
          type="file"
          accept=".zip,application/zip"
          className="sr-only"
          aria-label="Capture bundle ZIP file"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) uploadBundle.run(f);
            e.target.value = '';
          }}
        />
        <input
          ref={looseInput}
          type="file"
          accept=".jpg,.jpeg,image/jpeg"
          multiple
          className="sr-only"
          aria-label="Loose JPEG photos"
          onChange={(e) => {
            const files = Array.from(e.target.files ?? []);
            if (files.length) uploadLoose.run(files);
            e.target.value = '';
          }}
        />

        {message && (
          <div className="mt-4 rounded-lg border border-primary-600/40 bg-primary-600/10 p-3 text-sm text-primary-200">
            {message}
          </div>
        )}
        {(uploadBundle.error || uploadLoose.error) && (
          <div className="mt-4">
            <ErrorNote message={uploadBundle.error ?? uploadLoose.error ?? ''} />
          </div>
        )}

        <p className="mt-4 flex items-center gap-2 text-2xs text-slate-500">
          <FileUp className="h-3 w-3" aria-hidden="true" />
          The Operate Console can also uplink directly to this engine — point its intake URL at{' '}
          <code className="text-slate-400">/intake</code>.
        </p>
      </Card>

      <Card
        title="Batches"
        actions={
          <span className="text-2xs text-slate-500">{num(batches.data?.batches.length)} total</span>
        }
      >
        {batches.loading && <Spinner />}
        {batches.error && <ErrorNote message={batches.error} />}
        {batches.data && batches.data.batches.length === 0 && (
          <EmptyState
            icon={<Camera className="h-8 w-8" />}
            title="No batches yet"
            hint="Upload a bundle or loose photos to start mapping."
          />
        )}
        {batches.data && batches.data.batches.length > 0 && <BatchTable batches={batches.data.batches} />}
      </Card>
    </div>
  );
}

function BatchTable({ batches }: { batches: Batch[] }) {
  return (
    <Table
      head={
        <tr>
          <th className="px-3 py-2">Batch</th>
          <th className="px-3 py-2">Kind</th>
          <th className="px-3 py-2 text-right">Photos</th>
          <th className="px-3 py-2 text-right">Added</th>
        </tr>
      }
    >
      {batches.map((b) => (
        <tr key={b.batch_id} className="hover:bg-slate-900/60">
          <td className="px-3 py-2">
            <div className="font-medium text-slate-200">{b.source_name ?? shortId(b.batch_id)}</div>
            <div className="font-mono text-2xs text-slate-500">{shortId(b.batch_id, 16)}</div>
          </td>
          <td className="px-3 py-2">
            <span
              className={`badge ${
                b.kind === 'bundle'
                  ? 'bg-primary-500/20 text-primary-300'
                  : 'bg-slate-600/40 text-slate-300'
              }`}
            >
              {b.kind}
            </span>
          </td>
          <td className="px-3 py-2 text-right tabular-nums">{num(b.photo_count)}</td>
          <td className="px-3 py-2 text-right text-slate-400">{relativeTime(b.created_at)}</td>
        </tr>
      ))}
    </Table>
  );
}
