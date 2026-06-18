import { Activity, Database, RefreshCw, Wifi, WifiOff } from 'lucide-react';
import type { StatusResponse } from '../lib/types';

// Engine connection + Brain learning-link pills. The Brain link is "always
// on" by design; here it shows reachability and any queued (outbox) events.
export function StatusBar({
  status,
  onReload,
}: {
  status: StatusResponse | null;
  onReload: () => void;
}) {
  const online = !!status;
  const scbReachable = !!status?.scb.scb_db_exists;
  const pending = status?.scb.outbox_pending ?? 0;

  return (
    <div className="flex items-center gap-2">
      <span
        className={`badge gap-1 ${
          online ? 'bg-success-500/20 text-success-500' : 'bg-error-500/20 text-error-500'
        }`}
        title={online ? `Engine ${status?.version}` : 'Engine unreachable'}
      >
        {online ? <Wifi className="h-3 w-3" aria-hidden="true" /> : <WifiOff className="h-3 w-3" aria-hidden="true" />}
        {online ? 'Engine' : 'Offline'}
      </span>

      <span
        className={`badge gap-1 ${
          scbReachable ? 'bg-primary-500/20 text-primary-300' : 'bg-slate-700/40 text-slate-400'
        }`}
        title={
          status?.scb.scb_repo
            ? `Brain: ${status.scb.scb_repo}`
            : 'VS-Code workspace not found — set SCB_REPO_DIR; learning queued locally'
        }
      >
        <Activity className="h-3 w-3" aria-hidden="true" />
        Brain{pending > 0 ? ` (${pending})` : ''}
      </span>

      {status?.erp.enabled && (
        <span className="badge gap-1 bg-primary-500/20 text-primary-300" title="ERP bridge enabled">
          <Database className="h-3 w-3" aria-hidden="true" />
          ERP
        </span>
      )}

      <button className="btn-ghost px-2 py-2" onClick={onReload} aria-label="Refresh engine status">
        <RefreshCw className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  );
}
