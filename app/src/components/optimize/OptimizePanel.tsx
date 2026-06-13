import { useEffect, useState } from 'react';
import { CheckCircle2, ListChecks, Sparkles, TrendingDown } from 'lucide-react';
import { api } from '../../lib/api';
import { announce } from '../../lib/accessibility';
import { useAction, useAsync } from '../../hooks/useApi';
import { num, pct, relativeTime } from '../../lib/format';
import { Card, EmptyState, ErrorNote, Spinner, Stat, Table } from '../ui';
import type { MoveTask } from '../../lib/types';

export default function OptimizePanel({ onActivity }: { onActivity: () => void }) {
  const plans = useAsync(() => api.plans(), []);
  const [planId, setPlanId] = useState('');

  useEffect(() => {
    if (!planId && plans.data && plans.data.plans.length > 0) {
      setPlanId(plans.data.plans[0].plan_id);
    }
  }, [plans.data, planId]);

  const tasks = useAsync(
    () => (planId ? api.tasks(planId) : Promise.resolve({ tasks: [] })),
    [planId],
  );

  const optimize = useAction(async () => {
    const r = await api.optimize();
    announce(`Plan ${r.plan_id}: ${r.improvement_pct}% travel reduction across ${r.parts_assigned} parts`);
    await plans.reload();
    setPlanId(r.plan_id);
    onActivity();
  });

  const build = useAction(async () => {
    if (!planId) return;
    const r = await api.buildTasks(planId);
    announce(`Built ${r.moves} moves over ${r.days} days`);
    tasks.reload();
    plans.reload();
  });

  const complete = useAction(async (t: MoveTask) => {
    await api.completeTask(t.task_id);
    announce(`Completed move of ${t.part_number}`);
    tasks.reload();
  });

  const activePlan = plans.data?.plans.find((p) => p.plan_id === planId);
  const taskRows = tasks.data?.tasks ?? [];
  const days = [...new Set(taskRows.map((t) => t.day))].sort((a, b) => a - b);

  return (
    <div className="space-y-4">
      <Card
        title="Future-state optimizer"
        actions={
          <button className="btn-primary" onClick={() => optimize.run()} disabled={optimize.pending}>
            <Sparkles className="h-4 w-4" aria-hidden="true" />
            {optimize.pending ? 'Optimizing…' : 'Run optimizer'}
          </button>
        }
      >
        <p className="text-sm text-slate-400">
          Greedy assignment by velocity over Dijkstra travel costs, with golden-zone reservation for
          fast movers and pairwise-swap refinement. Minimizes total picks × travel distance.
        </p>
        {optimize.error && <div className="mt-3"><ErrorNote message={optimize.error} /></div>}
        {activePlan && (
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Objective before" value={num(activePlan.objective_before)} />
            <Stat label="Objective after" value={num(activePlan.objective_after)} />
            <Stat
              label="Reduction"
              value={
                <span className="flex items-center gap-1 text-success-500">
                  <TrendingDown className="h-4 w-4" aria-hidden="true" />
                  {activePlan.objective_before > 0
                    ? pct(1 - activePlan.objective_after / activePlan.objective_before)
                    : '—'}
                </span>
              }
            />
            <Stat label="Moves / days" value={`${num(activePlan.total_moves)} / ${num(activePlan.total_days)}`} />
          </div>
        )}
      </Card>

      <Card
        title="Plan & migration"
        actions={
          <div className="flex items-center gap-2">
            <select
              className="input max-w-[260px]"
              value={planId}
              onChange={(e) => setPlanId(e.target.value)}
              aria-label="Select plan"
            >
              {plans.data?.plans.map((p) => (
                <option key={p.plan_id} value={p.plan_id}>
                  {p.plan_id} · {relativeTime(p.created_at)}
                </option>
              ))}
            </select>
            <button className="btn-ghost" onClick={() => build.run()} disabled={build.pending || !planId}>
              <ListChecks className="h-4 w-4" aria-hidden="true" />
              {build.pending ? 'Building…' : 'Build tasks'}
            </button>
          </div>
        }
      >
        {plans.data && plans.data.plans.length === 0 && (
          <EmptyState
            icon={<Sparkles className="h-8 w-8" />}
            title="No plans yet"
            hint="Run the optimizer (needs inventory, located locations, and velocity)."
          />
        )}
        {build.error && <ErrorNote message={build.error} />}
        {tasks.loading && <Spinner />}
        {planId && taskRows.length === 0 && !tasks.loading && (
          <EmptyState icon={<ListChecks className="h-8 w-8" />} title="No migration tasks" hint="Build tasks for this plan." />
        )}

        <div className="space-y-4">
          {days.map((day) => (
            <div key={day}>
              <h3 className="mb-2 text-sm font-semibold text-slate-300">Day {day}</h3>
              <Table
                head={
                  <tr>
                    <th className="px-3 py-2">Part</th>
                    <th className="px-3 py-2">From → To</th>
                    <th className="px-3 py-2 text-right">Qty</th>
                    <th className="px-3 py-2 text-right">Min</th>
                    <th className="px-3 py-2">Status</th>
                  </tr>
                }
              >
                {taskRows
                  .filter((t) => t.day === day)
                  .map((t) => (
                    <tr key={t.task_id} className="hover:bg-slate-900/60">
                      <td className="px-3 py-2 font-mono text-slate-200">{t.part_number}</td>
                      <td className="px-3 py-2 font-mono text-2xs text-slate-400">
                        {t.from_location} → {t.to_location}
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums text-slate-300">{num(t.qty)}</td>
                      <td className="px-3 py-2 text-right tabular-nums text-slate-400">{num(t.est_minutes, 1)}</td>
                      <td className="px-3 py-2">
                        {t.status === 'done' ? (
                          <span className="badge bg-success-500/20 text-success-500">done</span>
                        ) : (
                          <button
                            className="btn-ghost px-2 py-1 text-xs"
                            onClick={() => complete.run(t)}
                            disabled={complete.pending}
                          >
                            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                            Complete
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
              </Table>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
