import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from '../lib/api';

interface AsyncState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}

// Runs an async loader on mount and whenever a dep changes; exposes reload().
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);

  // `deps` is the caller's dependency list for the loader; intentional.
  const run = useCallback(loader, deps);

  const reload = useCallback(() => {
    setLoading(true);
    setError(null);
    run()
      .then((d) => {
        if (alive.current) setData(d);
      })
      .catch((e: unknown) => {
        if (alive.current) {
          setError(e instanceof ApiError ? e.message : String(e));
        }
      })
      .finally(() => {
        if (alive.current) setLoading(false);
      });
  }, [run]);

  useEffect(() => {
    alive.current = true;
    reload();
    return () => {
      alive.current = false;
    };
  }, [reload]);

  return { data, loading, error, reload };
}

// One-shot mutation runner with pending + error state, for buttons.
export function useAction<TArgs extends unknown[], TResult>(
  fn: (...args: TArgs) => Promise<TResult>,
) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(
    async (...args: TArgs): Promise<TResult | undefined> => {
      setPending(true);
      setError(null);
      try {
        return await fn(...args);
      } catch (e: unknown) {
        setError(e instanceof ApiError ? e.message : String(e));
        return undefined;
      } finally {
        setPending(false);
      }
    },
    [fn],
  );
  return { run, pending, error };
}
