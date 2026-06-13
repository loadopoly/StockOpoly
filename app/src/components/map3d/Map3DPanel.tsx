import { useEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { Move3d } from 'lucide-react';
import { api } from '../../lib/api';
import { useAsync } from '../../hooks/useApi';
import { Card, EmptyState, ErrorNote, Spinner } from '../ui';
import type { LocationRow, MapData } from '../../lib/types';

type Mode = 'current' | 'future' | 'overlay';

// Inches → scene units. Aisle pitches and bays are hundreds of inches, so a
// small factor keeps the whole warehouse inside a comfortable view volume.
const S = 0.02;

const COLORS = {
  over: new THREE.Color('#ef4444'),
  tight: new THREE.Color('#f59e0b'),
  ok: new THREE.Color('#10b981'),
  empty: new THREE.Color('#334155'),
  future: new THREE.Color('#3b82f6'),
  futureHot: new THREE.Color('#60a5fa'),
  none: new THREE.Color('#1e293b'),
};

function colorFor(mode: Mode, loc: LocationRow, assigned: boolean): THREE.Color {
  if (mode === 'current') {
    return COLORS[(loc.occ_status as keyof typeof COLORS) ?? 'none'] ?? COLORS.none;
  }
  if (mode === 'future') {
    return assigned ? COLORS.future : COLORS.empty;
  }
  // overlay: occupancy as base, future-assigned bins lifted to blue
  if (assigned) return COLORS.futureHot;
  return COLORS[(loc.occ_status as keyof typeof COLORS) ?? 'none'] ?? COLORS.none;
}

function Controls() {
  const { camera, gl } = useThree();
  const ref = useRef<OrbitControls | null>(null);
  useEffect(() => {
    const c = new OrbitControls(camera, gl.domElement);
    c.enableDamping = true;
    c.dampingFactor = 0.1;
    ref.current = c;
    return () => c.dispose();
  }, [camera, gl]);
  useFrame(() => ref.current?.update());
  return null;
}

function Bins({
  locations,
  colors,
}: {
  locations: LocationRow[];
  colors: THREE.Color[];
}) {
  const ref = useRef<THREE.InstancedMesh>(null);
  const geometry = useMemo(() => new THREE.BoxGeometry(1, 1, 1), []);
  const material = useMemo(
    () => new THREE.MeshStandardMaterial({ roughness: 0.55, metalness: 0.05 }),
    [],
  );

  useEffect(() => {
    const mesh = ref.current;
    if (!mesh) return;
    const dummy = new THREE.Object3D();
    locations.forEach((l, i) => {
      // three is y-up; map warehouse z (level) → y, warehouse y (bay) → z.
      dummy.position.set((l.x ?? 0) * S, (l.z ?? 0) * S + (l.h ?? 10) * S * 0.5, (l.y ?? 0) * S);
      dummy.scale.set(
        Math.max((l.w ?? 10) * S, 0.05),
        Math.max((l.h ?? 10) * S, 0.05),
        Math.max((l.d ?? 10) * S, 0.05),
      );
      dummy.updateMatrix();
      mesh.setMatrixAt(i, dummy.matrix);
      mesh.setColorAt(i, colors[i] ?? COLORS.none);
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  }, [locations, colors]);

  if (locations.length === 0) return null;
  return (
    <instancedMesh
      ref={ref}
      args={[geometry, material, locations.length]}
      castShadow
      receiveShadow
    />
  );
}

function Scene({ data, mode }: { data: MapData; mode: Mode }) {
  const located = useMemo(() => data.locations.filter((l) => l.x !== null), [data.locations]);
  const colors = useMemo(
    () => located.map((l) => colorFor(mode, l, !!data.future[l.location_code])),
    [located, mode, data.future],
  );

  // Center the camera target on the bounding box of all bins.
  const center = useMemo(() => {
    if (located.length === 0) return new THREE.Vector3();
    const xs = located.map((l) => (l.x ?? 0) * S);
    const ys = located.map((l) => (l.y ?? 0) * S);
    const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
    const cz = (Math.min(...ys) + Math.max(...ys)) / 2;
    return new THREE.Vector3(cx, 0, cz);
  }, [located]);

  const span = useMemo(() => {
    if (located.length === 0) return 10;
    const ys = located.map((l) => (l.y ?? 0) * S);
    const xs = located.map((l) => (l.x ?? 0) * S);
    return Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 5);
  }, [located]);

  return (
    <>
      <ambientLight intensity={0.6} />
      <directionalLight position={[20, 40, 20]} intensity={1.1} castShadow />
      <group position={[-center.x, 0, -center.z]}>
        <Bins locations={located} colors={colors} />
        <gridHelper args={[span * 2.2, 24, '#1e293b', '#0f172a']} position={[center.x, 0, center.z]} />
      </group>
    </>
  );
}

export default function Map3DPanel() {
  const [mode, setMode] = useState<Mode>('current');
  const plans = useAsync(() => api.plans(), []);
  const [planId, setPlanId] = useState<string>('');

  useEffect(() => {
    if (!planId && plans.data && plans.data.plans.length > 0) {
      setPlanId(plans.data.plans[0].plan_id);
    }
  }, [plans.data, planId]);

  const map = useAsync(
    () => api.map(mode === 'current' ? undefined : planId || undefined),
    [mode, planId],
  );

  const located = (map.data?.locations ?? []).filter((l) => l.x !== null);

  return (
    <div className="space-y-4">
      <Card
        title="3D warehouse map"
        actions={
          <div className="flex items-center gap-2">
            {(['current', 'future', 'overlay'] as Mode[]).map((m) => (
              <button
                key={m}
                className={`btn px-3 ${
                  mode === m ? 'bg-primary-600 text-white' : 'bg-slate-800 text-slate-300'
                }`}
                onClick={() => setMode(m)}
                aria-pressed={mode === m}
              >
                {m}
              </button>
            ))}
            {mode !== 'current' && plans.data && plans.data.plans.length > 0 && (
              <select
                className="input max-w-[220px]"
                value={planId}
                onChange={(e) => setPlanId(e.target.value)}
                aria-label="Plan for future view"
              >
                {plans.data.plans.map((p) => (
                  <option key={p.plan_id} value={p.plan_id}>
                    {p.plan_id}
                  </option>
                ))}
              </select>
            )}
          </div>
        }
      >
        {map.loading && <Spinner />}
        {map.error && <ErrorNote message={map.error} />}
        {!map.loading && located.length === 0 && (
          <EmptyState
            icon={<Move3d className="h-8 w-8" />}
            title="Nothing to render yet"
            hint="Register locations and compute coordinates (Locations tab), then compute occupancy (Slotting) for colors."
          />
        )}
        {located.length > 0 && map.data && (
          <>
            <div className="h-[520px] w-full overflow-hidden rounded-lg border border-slate-800 bg-slate-950">
              <Canvas
                shadows
                camera={{ position: [8, 9, 12], fov: 50 }}
                gl={{ antialias: true }}
              >
                <color attach="background" args={['#020617']} />
                <Scene data={map.data} mode={mode} />
                <Controls />
              </Canvas>
            </div>
            <Legend mode={mode} />
          </>
        )}
      </Card>
    </div>
  );
}

function Legend({ mode }: { mode: Mode }) {
  const items =
    mode === 'future'
      ? [
          ['#3b82f6', 'Assigned (plan)'],
          ['#334155', 'Unassigned'],
        ]
      : mode === 'overlay'
        ? [
            ['#60a5fa', 'Future-assigned'],
            ['#ef4444', 'Over'],
            ['#f59e0b', 'Tight'],
            ['#10b981', 'OK'],
          ]
        : [
            ['#ef4444', 'Over'],
            ['#f59e0b', 'Tight'],
            ['#10b981', 'OK'],
            ['#334155', 'Empty'],
          ];
  return (
    <div className="mt-3 flex flex-wrap items-center gap-4 text-2xs text-slate-400">
      {items.map(([c, label]) => (
        <span key={label} className="flex items-center gap-1.5">
          <span className="h-3 w-3 rounded-sm" style={{ backgroundColor: c }} aria-hidden="true" />
          {label}
        </span>
      ))}
      <span className="ml-auto text-slate-600">Drag to orbit · scroll to zoom</span>
    </div>
  );
}
