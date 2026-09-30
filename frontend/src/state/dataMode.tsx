import { createContext, useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  BackendUnreachableError,
  apiList,
  apiGet,
} from "../api/client";

interface DataState {
  /** true → every screen reads synthetic SAMPLE data; false → live backend */
  sampleMode: boolean;
  toggleSampleMode: () => void;
  /** true while the backend failed to respond (only in live mode) */
  backendDown: boolean;
  /** Fetch a list endpoint, falling back to `fallback` in sample mode. */
  fetchList: <T>(path: string, fallback: T[]) => Promise<T[]>;
  /** Fetch a single-object endpoint, falling back to `fallback` in sample mode. */
  fetchOne: <T>(path: string, fallback: T) => Promise<T>;
}

const Ctx = createContext<DataState | null>(null);

export function DataProvider({ children }: { children: ReactNode }) {
  const [sampleMode, setSampleMode] = useState(false);
  const [backendDown, setBackendDown] = useState(false);

  const toggleSampleMode = useCallback(() => {
    setSampleMode((m) => !m);
    setBackendDown(false);
  }, []);

  const fetchList = useCallback(
    async <T,>(path: string, fallback: T[]): Promise<T[]> => {
      if (sampleMode) return fallback;
      try {
        const data = await apiList<T>(path);
        setBackendDown(false);
        return data;
      } catch (err) {
        if (err instanceof BackendUnreachableError) {
          setBackendDown(true);
          return [];
        }
        throw err;
      }
    },
    [sampleMode],
  );

  const fetchOne = useCallback(
    async <T,>(path: string, fallback: T): Promise<T> => {
      if (sampleMode) return fallback;
      try {
        const data = await apiGet<T>(path);
        setBackendDown(false);
        return data;
      } catch (err) {
        if (err instanceof BackendUnreachableError) {
          setBackendDown(true);
          return fallback;
        }
        throw err;
      }
    },
    [sampleMode],
  );

  const value = useMemo<DataState>(
    () => ({ sampleMode, toggleSampleMode, backendDown, fetchList, fetchOne }),
    [sampleMode, toggleSampleMode, backendDown, fetchList, fetchOne],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useData(): DataState {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useData must be used inside <DataProvider>");
  return ctx;
}
