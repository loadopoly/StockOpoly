import { lazy, Suspense, useState } from 'react';
import {
  Boxes,
  Camera,
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
import { api } from './lib/api';
import { Spinner } from './components/ui';
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

export default function App() {
  const [active, setActive] = useState('intake');
  const status = useAsync(() => api.status(), []);
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
          <StatusBar status={status.data} onReload={status.reload} />
        </div>
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
      </header>

      <main
        id={`panel-${current.id}`}
        role="tabpanel"
        aria-labelledby={`tab-${current.id}`}
        className="mx-auto w-full max-w-7xl flex-1 px-4 py-6"
      >
        <Suspense fallback={<Spinner label="Loading view…" />}>
          <Panel onActivity={status.reload} />
        </Suspense>
      </main>
    </div>
  );
}
