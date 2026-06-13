import { useState } from 'react';
import { Calculator, Plus, Ruler } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { inches, num, pct } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Stat } from '../ui';
import type { DimEntity, SolveReport } from '../../lib/types';

const ENTITY_KINDS = ['object', 'rack_member', 'pattern_unit', 'span', 'distance'];
const AXES = ['L', 'W', 'H', 'SPAN'];

export default function MeasurePanel({ onActivity }: { onActivity: () => void }) {
  const entities = useAsync(() => api.entities(), []);
  const references = useAsync(() => api.references(), []);
  const [report, setReport] = useState<SolveReport | null>(null);

  const solve = useAction(async () => {
    const r = await api.solve();
    setReport(r);
    announce(
      `Solved ${r.variables} variables, ${r.outliers.length} outliers, ${r.ungrounded_components} ungrounded`,
    );
    entities.reload();
    onActivity();
  });

  return (
    <div className="space-y-4">
      <Card
        title="Relational dimension solver"
        actions={
          <button className="btn-primary" onClick={() => solve.run()} disabled={solve.pending}>
            <Calculator className="h-4 w-4" aria-hidden="true" />
            {solve.pending ? 'Solving…' : 'Solve graph'}
          </button>
        }
      >
        <p className="text-sm text-slate-400">
          Build a graph of measurements — absolute lengths, pixel extents tied to a photo scale,
          ratios, pattern counts, sum-of-parts — and the solver recovers real-world dimensions in
          inches with confidence intervals. Ungrounded components need a reference shot.
        </p>
        {solve.error && <div className="mt-3"><ErrorNote message={solve.error} /></div>}
        {report && (
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Variables" value={num(report.variables)} />
            <Stat label="Measurements" value={num(report.measurements)} />
            <Stat label="RMS" value={report.rms.toFixed(3)} hint={`${report.iterations} iters`} />
            <Stat
              label="Ungrounded"
              value={num(report.ungrounded_components)}
              hint={report.outliers.length > 0 ? `${report.outliers.length} outliers` : 'no outliers'}
            />
          </div>
        )}
        {report && report.ungrounded_components > 0 && (
          <div className="mt-3 rounded-lg border border-warning-600/40 bg-warning-600/10 p-3 text-xs text-warning-500">
            {report.components
              .filter((c) => !c.grounded)
              .map((c, i) => (
                <div key={i}>Ungrounded: {c.members.join(', ')} — add a reference or absolute measurement.</div>
              ))}
          </div>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <EntitiesCard entities={entities} report={report} />
        </div>
        <div className="space-y-4">
          <NewEntityCard onCreated={entities.reload} />
          <NewMeasurementCard entities={entities.data?.entities ?? []} onAdded={entities.reload} />
          <ReferencesCard references={references} onChanged={references.reload} />
        </div>
      </div>
    </div>
  );
}

function EntitiesCard({
  entities,
  report,
}: {
  entities: ReturnType<typeof useAsync<{ entities: DimEntity[] }>>;
  report: SolveReport | null;
}) {
  return (
    <Card title="Entities & solved dimensions">
      {entities.loading && <Spinner />}
      {entities.error && <ErrorNote message={entities.error} />}
      {entities.data && entities.data.entities.length === 0 && (
        <EmptyState icon={<Ruler className="h-8 w-8" />} title="No entities" hint="Create one to start measuring." />
      )}
      <div className="space-y-3">
        {entities.data?.entities.map((e) => (
          <div key={e.entity_id} className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-medium text-slate-200">{e.label}</span>
              <span className="badge bg-slate-700/50 text-slate-300">{e.kind}</span>
            </div>
            {e.variables.length === 0 ? (
              <p className="text-2xs text-slate-500">No axes yet — add a measurement.</p>
            ) : (
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {e.variables.map((v) => (
                  <div key={v.var_id} className="rounded bg-slate-950/60 p-2">
                    <div className="label mb-0.5">{v.axis}</div>
                    <div className="text-sm font-semibold text-slate-100">{inches(v.value)}</div>
                    <div className="text-2xs text-slate-500">
                      {v.confidence ? `conf ${pct(v.confidence)}` : 'unsolved'}
                      {v.ci_low != null && v.ci_high != null && (
                        <> · {inches(v.ci_low)}–{inches(v.ci_high)}</>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      {report && report.outliers.length > 0 && (
        <p className="mt-3 text-2xs text-error-500">
          Flagged outlier measurements: {report.outliers.join(', ')}
        </p>
      )}
    </Card>
  );
}

function NewEntityCard({ onCreated }: { onCreated: () => void }) {
  const [kind, setKind] = useState('object');
  const [label, setLabel] = useState('');
  const create = useAction(async () => {
    await api.createEntity(kind, label || 'Entity');
    setLabel('');
    announce('Entity created');
    onCreated();
  });
  return (
    <Card title="New entity">
      <div className="space-y-2">
        <select className="input" value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Entity kind">
          {ENTITY_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <input
          className="input"
          placeholder="Label (e.g. Rack bay, Crate A)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          aria-label="Entity label"
        />
        <button className="btn-primary w-full" onClick={() => create.run()} disabled={create.pending}>
          <Plus className="h-4 w-4" aria-hidden="true" />
          Add entity
        </button>
        {create.error && <ErrorNote message={create.error} />}
      </div>
    </Card>
  );
}

function NewMeasurementCard({ entities, onAdded }: { entities: DimEntity[]; onAdded: () => void }) {
  const [entityId, setEntityId] = useState('');
  const [axis, setAxis] = useState('L');
  const [value, setValue] = useState('');
  const [unit, setUnit] = useState('in');

  const add = useAction(async () => {
    const eid = Number(entityId);
    if (!eid || !value) return;
    const v = await api.ensureVariable(eid, axis);
    await api.addMeasurement({
      kind: 'absolute',
      a_var: v.var_id,
      value: Number(value),
      unit,
      source: 'manual',
    });
    setValue('');
    announce('Measurement added');
    onAdded();
  });

  return (
    <Card title="Absolute measurement">
      <div className="space-y-2">
        <select
          className="input"
          value={entityId}
          onChange={(e) => setEntityId(e.target.value)}
          aria-label="Entity"
        >
          <option value="">Select entity…</option>
          {entities.map((e) => (
            <option key={e.entity_id} value={e.entity_id}>
              {e.label}
            </option>
          ))}
        </select>
        <div className="grid grid-cols-3 gap-2">
          <select className="input" value={axis} onChange={(e) => setAxis(e.target.value)} aria-label="Axis">
            {AXES.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
          <input
            className="input"
            type="number"
            placeholder="Value"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label="Measurement value"
          />
          <select className="input" value={unit} onChange={(e) => setUnit(e.target.value)} aria-label="Unit">
            {['in', 'ft', 'cm', 'mm', 'm'].map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
        </div>
        <button
          className="btn-primary w-full"
          onClick={() => add.run()}
          disabled={add.pending || !entityId || !value}
        >
          <Plus className="h-4 w-4" aria-hidden="true" />
          Add measurement
        </button>
        {add.error && <ErrorNote message={add.error} />}
      </div>
    </Card>
  );
}

function ReferencesCard({
  references,
  onChanged,
}: {
  references: ReturnType<typeof useAsync<{ references: import('../../lib/types').ReferenceObject[] }>>;
  onChanged: () => void;
}) {
  const [name, setName] = useState('');
  const [dimL, setDimL] = useState('');
  const add = useAction(async () => {
    await api.addReference({ name: name || 'Reference', kind: 'ruler', dim_l: Number(dimL) || null });
    setName('');
    setDimL('');
    announce('Reference object added');
    onChanged();
  });
  return (
    <Card title="Reference objects">
      <p className="mb-2 text-2xs text-slate-500">
        A known object (ruler, standard carton) seen in a photo grounds that photo's scale.
      </p>
      <div className="space-y-2">
        <input
          className="input"
          placeholder="Name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Reference name"
        />
        <input
          className="input"
          type="number"
          placeholder="Length (in)"
          value={dimL}
          onChange={(e) => setDimL(e.target.value)}
          aria-label="Reference length in inches"
        />
        <button className="btn-ghost w-full" onClick={() => add.run()} disabled={add.pending}>
          <Plus className="h-4 w-4" aria-hidden="true" />
          Add reference
        </button>
      </div>
      <ul className="mt-3 space-y-1 text-xs text-slate-400">
        {references.data?.references.map((r) => (
          <li key={r.ref_id} className="flex justify-between">
            <span>{r.name}</span>
            <span className="text-slate-500">{inches(r.dim_l)}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}
