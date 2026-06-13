import { useEffect, useState } from 'react';
import { Check, Layers, Sparkles, Trash2 } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { num } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner } from '../ui';
import type { Group, Photo } from '../../lib/types';

const TIER_LABEL: Record<number, string> = {
  0: 'Manual',
  1: 'Brain',
  2: 'Heuristic',
  3: 'LLM',
};

export default function GroupsPanel({ onActivity }: { onActivity: () => void }) {
  const batches = useAsync(() => api.batches(), []);
  const [batchId, setBatchId] = useState('');

  useEffect(() => {
    if (!batchId && batches.data && batches.data.batches.length > 0) {
      setBatchId(batches.data.batches[0].batch_id);
    }
  }, [batches.data, batchId]);

  const groups = useAsync(() => (batchId ? api.groups(batchId) : Promise.resolve({ groups: [] })), [
    batchId,
  ]);
  const photos = useAsync(
    () => (batchId ? api.batchPhotos(batchId) : Promise.resolve({ photos: [] })),
    [batchId],
  );

  const cascade = useAction(async () => {
    const r = await api.cascade(batchId);
    announce(`Cascade ran tiers ${r.tiers_run.join(', ')}: ${r.created} new groups`);
    groups.reload();
    onActivity();
  });
  const confirm = useAction(async (g: Group) => {
    await api.confirmGroup(g.group_id, !g.confirmed);
    groups.reload();
  });
  const remove = useAction(async (g: Group) => {
    await api.deleteGroup(g.group_id);
    announce('Group deleted');
    groups.reload();
  });

  const photoMap = new Map((photos.data?.photos ?? []).map((p) => [p.photo_id, p]));

  if (batches.loading) return <Spinner />;
  if (batches.data && batches.data.batches.length === 0) {
    return (
      <EmptyState
        icon={<Layers className="h-8 w-8" />}
        title="No batches to group"
        hint="Ingest photos in the Intake tab first."
      />
    );
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex-1 min-w-[220px]">
            <span className="label">Batch</span>
            <select
              className="input"
              value={batchId}
              onChange={(e) => setBatchId(e.target.value)}
              aria-label="Select batch to group"
            >
              {batches.data?.batches.map((b) => (
                <option key={b.batch_id} value={b.batch_id}>
                  {b.source_name ?? b.batch_id} · {b.photo_count} photos
                </option>
              ))}
            </select>
          </label>
          <button className="btn-primary" onClick={() => cascade.run()} disabled={cascade.pending || !batchId}>
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            {cascade.pending ? 'Running cascade…' : 'Run grouping cascade'}
          </button>
        </div>
        <p className="mt-2 text-2xs text-slate-500">
          Cascade order: Brain knowledge → offline heuristics (time/GPS/visual) → optional LLM vision.
          Manual and confirmed groups are preserved.
        </p>
        {cascade.error && <div className="mt-3"><ErrorNote message={cascade.error} /></div>}
      </Card>

      {groups.loading && <Spinner />}
      {groups.error && <ErrorNote message={groups.error} />}
      {groups.data && groups.data.groups.length === 0 && (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title="No groups yet"
          hint="Run the cascade to propose photo groups."
        />
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {groups.data?.groups.map((g) => (
          <GroupCard
            key={g.group_id}
            group={g}
            photoMap={photoMap}
            batchId={batchId}
            onConfirm={() => confirm.run(g)}
            onDelete={() => remove.run(g)}
            busy={confirm.pending || remove.pending}
          />
        ))}
      </div>
    </div>
  );
}

function GroupCard({
  group,
  photoMap,
  batchId,
  onConfirm,
  onDelete,
  busy,
}: {
  group: Group;
  photoMap: Map<string, Photo>;
  batchId: string;
  onConfirm: () => void;
  onDelete: () => void;
  busy: boolean;
}) {
  return (
    <div className="card">
      <div className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold text-slate-100">{group.label}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <span className="badge bg-slate-700/50 text-slate-300">{group.kind}</span>
            <span className="badge bg-primary-500/20 text-primary-300">
              {TIER_LABEL[group.source_tier] ?? `T${group.source_tier}`}
            </span>
            <span className="badge bg-slate-700/50 text-slate-300">
              conf {(group.confidence * 100).toFixed(0)}%
            </span>
            {group.confirmed && (
              <span className="badge bg-success-500/20 text-success-500">confirmed</span>
            )}
          </div>
        </div>
        <div className="flex gap-1">
          <button
            className={group.confirmed ? 'btn-ghost px-2.5' : 'btn-primary px-2.5'}
            onClick={onConfirm}
            disabled={busy}
            aria-label={group.confirmed ? 'Unconfirm group' : 'Confirm group'}
          >
            <Check className="h-4 w-4" aria-hidden="true" />
          </button>
          <button className="btn-danger px-2.5" onClick={onDelete} disabled={busy} aria-label="Delete group">
            <Trash2 className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {group.photo_ids.slice(0, 8).map((pid) => {
          const photo = photoMap.get(pid);
          if (!photo) return null;
          return (
            <img
              key={pid}
              src={api.photoUrl(batchId, photo.file)}
              alt={photo.file}
              loading="lazy"
              className="h-14 w-14 rounded border border-slate-700 object-cover"
            />
          );
        })}
        {group.photo_ids.length > 8 && (
          <div className="flex h-14 w-14 items-center justify-center rounded border border-slate-700 bg-slate-900 text-2xs text-slate-400">
            +{num(group.photo_ids.length - 8)}
          </div>
        )}
      </div>
    </div>
  );
}
