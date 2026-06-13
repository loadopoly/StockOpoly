import { lazy, Suspense, useEffect, useState } from 'react';
import {
  Boxes,
  Camera,
  Cloud,
  Group as GroupIcon,
  Layers,
  MapPin,
  Move3d,
  Ruler,
  Settings as SettingsIcon,
  Sparkles,
  Warehouse,
} from 'lucide-react';
import type { ComponentType } from 'react';
import { useAsync } from './hooks/useApi';
import { api, engineReachable } from './lib/api';
import { isSupabaseConfigured } from './lib/supabase';
import { Card, Spinner } from './components/ui';
import { StatusBar } from './components/StatusBar';
import IntakePanel from './components/intake/IntakePanel';
import GroupsPanel from './components/groups/GroupsPanel';
import MeasurePanel from './components/measure/MeasurePanel';
import LocationsPanel from './components/locations/LocationsPanel';
import InventoryPanel from './components/inventory/InventoryPanel';
import SlottingPanel from './components/slotting/SlottingPanel';
import OptimizePanel from './components/optimize/OptimizePanel';
import SettingsPanel from './components/settings/SettingsPanel';

const Map3DPanel = lazy(() => import('./components/map3d/Map3DPanel'));
const CloudView = lazy(() => import('./components/cloud/CloudView'));

interface TabDef {
  id: string;
  label: string;
  icon: ComponentType<{ className?: string; 'aria-hidden'?: boolean }>;
  Panel: ComponentType<{ onActivity: () => void }>;
}

const TABS: TabDef[] = [
  { id: 'intake', label: 'Intake', icon: Camera, Panel: IntakePanel },
  { id: 'groups', label: 'Groups', icon: GroupIcon, Panel: GroupsPanel },
  { id: 'measure', label: 'Measure', icon: Ruler, Panel: MeasurePanel },
  { id: 'locations', label: 'Locations', icon: MapPin, Panel: LocationsPanel },
  { id: 'inventory', label: 'Inventory', icon: Boxes, Panel: InventoryPanel },
  { id: 'slotting', label: 'Slotting', icon: Layers, Panel: SlottingPanel },
  { id: 'optimize', label: 'Optimize', icon: Sparkles, Panel: OptimizePanel },
  { id: 'map3d', label: 'Map 3D', icon: Move3d, Panel: Map3DPanel },
  { id: 'settings', label: 'Settings', icon: SettingsIcon, Panel: SettingsPanel },
];

// 'engine' — a local/remote engine answers /api; full workspace.
// 'cloud'  — no engine but Supabase is configured (the GitHub Pages case).
// 'offline'— neither; show how to get started.
type Mode = 'probing' | 'engine' | 'cloud' | 'offline';

export default function App() {
  const [active, setActive] = useState('intake');
  const [mode, setMode] = useState<Mode>('probing');
  const status = useAsync(() => api.status(), []);

  useEffect(() => {
    let alive = true;
    engineReachable().then((ok) => {
      if (alive) setMode(ok ? 'engine' : isSupabaseConfigured() ? 'cloud' : 'offline');
    });
    return () => {
      alive = false;
    };
  }, []);

  const current = TABS.find((t) => t.id === active) ?? TABS[0];
  const Panel = current.Panel;

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center gap-3 px-4 py-3">
          <Warehouse className="h-6 w-6 text-primary-400" aria-hidden="true" />
          <div className="mr-auto">
            <h1 className="text-lg font-bold leading-tight text-slate-100">StockOpoly</h1>
            <p className="text-2xs text-slate-500">Warehouse mapping &amp; slotting</p>
          </div>
          {mode === 'engine' && <StatusBar status={status.data} onReload={status.reload} />}
          {mode === 'cloud' && (
            <span
              className="badge gap-1 bg-primary-500/20 text-primary-300"
              title="Reading from Supabase — no local engine"
            >
              <Cloud className="h-3 w-3" aria-hidden="true" />
              Cloud
            </span>
          )}
        </div>
        {mode === 'engine' && (
          <nav aria-label="Workspace sections" className="mx-auto max-w-7xl px-2">
            <ul className="flex gap-1 overflow-x-auto pb-2" role="tablist">
              {TABS.map((tab) => {
                const Icon = tab.icon;
                const selected = tab.id === active;
                return (
                  <li key={tab.id} role="presentation">
                    <button
                      role="tab"
                      aria-selected={selected}
                      aria-controls={`panel-${tab.id}`}
                      id={`tab-${tab.id}`}
                      onClick={() => setActive(tab.id)}
                      className={`btn min-h-[44px] whitespace-nowrap px-3 ${
                        selected
                          ? 'bg-primary-600 text-white'
                          : 'bg-transparent text-slate-300 hover:bg-slate-800'
                      }`}
                    >
                      <Icon className="h-4 w-4" aria-hidden={true} />
                      {tab.label}
                    </button>
                  </li>
                );
              })}
            </ul>
          </nav>
        )}
      </header>

      <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6">
        {mode === 'probing' && <Spinner label="Connecting…" />}

        {mode === 'cloud' && (
          <Suspense fallback={<Spinner label="Loading cloud view…" />}>
            <CloudView />
          </Suspense>
        )}

        {mode === 'offline' && (
          <Card title="No engine and no cloud configured">
            <p className="text-sm text-slate-400">
              The StockOpoly engine isn&apos;t reachable and Supabase isn&apos;t configured. Start
              the engine with{' '}
              <code className="text-primary-300">python -m stockopoly serve</code> (it serves this UI
              on <code className="text-primary-300">:8181</code>), or set{' '}
              <code className="text-primary-300">VITE_SUPABASE_URL</code> and{' '}
              <code className="text-primary-300">VITE_SUPABASE_ANON_KEY</code> to read a shared
              project.
            </p>
          </Card>
        )}

        {mode === 'engine' && (
          <section
            id={`panel-${current.id}`}
            role="tabpanel"
            aria-labelledby={`tab-${current.id}`}
          >
            <Suspense fallback={<Spinner label="Loading view…" />}>
              <Panel onActivity={status.reload} />
            </Suspense>
          </section>
        )}
      </main>
    </div>
  );
}
