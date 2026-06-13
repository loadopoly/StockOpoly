import { useEffect, useState } from 'react';
import { Cloud, RefreshCw, Save, Send, UploadCloud } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { num, relativeTime } from '../../lib/format';
import { Card, ErrorNote, Spinner } from '../ui';
import type { EngineEvent, Settings } from '../../lib/types';

const BOOL_KEYS = ['share_supabase', 'auto_sync', 'crawl_live', 'scb_vision', 'llm_vision'];
const HIDE_KEYS = ['supplier_sites', 'golden_zone_levels']; // structured — left as-is

export default function SettingsPanel({ onActivity }: { onActivity: () => void }) {
  const settings = useAsync(() => api.settings(), []);
  const scb = useAsync(() => api.scbStatus(), []);
  const sync = useAsync(() => api.syncStatus(), []);
  const events = useAsync(() => api.events(20), []);
  const [draft, setDraft] = useState<Settings>({});

  useEffect(() => {
    if (settings.data) setDraft(settings.data);
  }, [settings.data]);

  const save = useAction(async () => {
    const updated = await api.updateSettings(draft);
    setDraft(updated);
    announce('Settings saved');
    onActivity();
  });

  const flush = useAction(async () => {
    const r = await api.scbFlush();
    announce(`Flushed ${r.flushed} queued events to the Brain`);
    scb.reload();
  });

  const syncNow = useAction(async () => {
    const r = await api.syncNow();
    const msg =
      r.mode === 'ready'
        ? `Synced ${r.pushed} rows and ${r.photos ?? 0} photos to Supabase`
        : `Sync ${r.mode}`;
    announce(msg);
    sync.reload();
  });

  const set = (key: string, value: unknown) => setDraft((d) => ({ ...d, [key]: value }));

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <div className="space-y-4 lg:col-span-2">
        <Card
          title="Engine settings"
          actions={
            <button className="btn-primary" onClick={() => save.run()} disabled={save.pending}>
              <Save className="h-4 w-4" aria-hidden="true" />
              {save.pending ? 'Saving…' : 'Save'}
            </button>
          }
        >
          {settings.loading && <Spinner />}
          {settings.error && <ErrorNote message={settings.error} />}
          {save.error && <ErrorNote message={save.error} />}

          {settings.data && (
            <div className="space-y-5">
              <fieldset>
                <legend className="label mb-2">Toggles</legend>
                <div className="grid gap-2 sm:grid-cols-2">
                  {BOOL_KEYS.map((key) => (
                    <label
                      key={key}
                      className="flex items-center justify-between gap-3 rounded-lg border border-slate-800 bg-slate-900/50 px-3 py-2.5"
                    >
                      <span className="text-sm text-slate-300">{key}</span>
                      <input
                        type="checkbox"
                        className="h-5 w-5 accent-primary-500"
                        checked={!!draft[key]}
                        onChange={(e) => set(key, e.target.checked)}
                        aria-label={key}
                      />
                    </label>
                  ))}
                </div>
              </fieldset>

              <fieldset>
                <legend className="label mb-2">Values</legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  {Object.entries(draft)
                    .filter(
                      ([k, v]) =>
                        !BOOL_KEYS.includes(k) &&
                        !HIDE_KEYS.includes(k) &&
                        (typeof v === 'number' || typeof v === 'string'),
                    )
                    .map(([key, value]) => (
                      <label key={key} className="block">
                        <span className="label">{key}</span>
                        <input
                          className="input"
                          type={typeof value === 'number' ? 'number' : 'text'}
                          value={String(value ?? '')}
                          onChange={(e) =>
                            set(
                              key,
                              typeof value === 'number' ? Number(e.target.value) : e.target.value,
                            )
                          }
                          aria-label={key}
                        />
                      </label>
                    ))}
                </div>
              </fieldset>
            </div>
          )}
        </Card>

        <Card title="Recent activity">
          {events.loading && <Spinner />}
          <ul className="space-y-1.5">
            {events.data?.events.map((e: EngineEvent, i) => (
              <li key={i} className="flex items-center justify-between gap-3 text-sm">
                <span className="flex items-center gap-2">
                  <span className="badge bg-slate-700/50 text-slate-300">{e.kind}</span>
                  <span className="truncate text-slate-400">
                    {typeof e.payload.title === 'string' ? e.payload.title : ''}
                  </span>
                </span>
                <span className="shrink-0 text-2xs text-slate-500">{relativeTime(e.at)}</span>
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <div className="space-y-4">
      <Card
        title="Supply-Chain-Brain link"
        actions={
          <button className="btn-ghost px-2.5" onClick={() => scb.reload()} aria-label="Refresh Brain status">
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
          </button>
        }
      >
        {scb.loading && <Spinner />}
        {scb.data && (
          <dl className="space-y-2 text-sm">
            <div className="flex justify-between gap-2">
              <dt className="text-slate-400">Repo</dt>
              <dd className="truncate text-right font-mono text-2xs text-slate-300">
                {scb.data.scb_repo ?? 'not found'}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">DB present</dt>
              <dd className={scb.data.scb_db_exists ? 'text-success-500' : 'text-slate-500'}>
                {scb.data.scb_db_exists ? 'yes' : 'no'}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Queued events</dt>
              <dd className="tabular-nums text-slate-200">{num(scb.data.outbox_pending)}</dd>
            </div>
          </dl>
        )}
        <button
          className="btn-primary mt-4 w-full"
          onClick={() => flush.run()}
          disabled={flush.pending}
        >
          <Send className="h-4 w-4" aria-hidden="true" />
          {flush.pending ? 'Flushing…' : 'Flush queued learning'}
        </button>
        {flush.error && <div className="mt-2"><ErrorNote message={flush.error} /></div>}
        <p className="mt-3 text-2xs text-slate-500">
          Learning writes to the Brain are always on and independent of the Supabase sharing toggle.
        </p>
      </Card>

      <Card
        title="Cloud sync (Supabase)"
        actions={
          <button
            className="btn-ghost px-2.5"
            onClick={() => sync.reload()}
            aria-label="Refresh sync status"
          >
            <RefreshCw className="h-4 w-4" aria-hidden="true" />
          </button>
        }
      >
        {sync.loading && <Spinner />}
        {sync.data && (
          <dl className="space-y-2 text-sm">
            <div className="flex items-center justify-between gap-2">
              <dt className="text-slate-400">Status</dt>
              <dd className="flex items-center gap-1.5">
                <Cloud className="h-3.5 w-3.5 text-primary-300" aria-hidden="true" />
                <span
                  className={
                    sync.data.mode === 'ready'
                      ? 'text-success-500'
                      : sync.data.mode === 'off'
                        ? 'text-slate-500'
                        : 'text-warning-500'
                  }
                >
                  {sync.data.mode}
                </span>
              </dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-slate-400">Project</dt>
              <dd className="truncate text-right font-mono text-2xs text-slate-300">
                {sync.data.url ?? 'not configured'}
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Bucket</dt>
              <dd className="font-mono text-2xs text-slate-300">{sync.data.bucket}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Photos to upload</dt>
              <dd className="tabular-nums text-slate-200">{num(sync.data.unsynced_photos)}</dd>
            </div>
          </dl>
        )}
        <button
          className="btn-primary mt-4 w-full"
          onClick={() => syncNow.run()}
          disabled={syncNow.pending || sync.data?.mode !== 'ready'}
        >
          <UploadCloud className="h-4 w-4" aria-hidden="true" />
          {syncNow.pending ? 'Syncing…' : 'Sync now'}
        </button>
        {syncNow.error && (
          <div className="mt-2">
            <ErrorNote message={syncNow.error} />
          </div>
        )}
        <p className="mt-3 text-2xs text-slate-500">
          When sharing is on and configured, structured data and photos upload automatically after
          every change. The hosted (GitHub Pages) app reads from here.
        </p>
      </Card>
      </div>
    </div>
  );
}
