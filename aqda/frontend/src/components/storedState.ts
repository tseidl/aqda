import { useCallback, useState } from 'react';

/** State that is remembered in this browser, so a display preference (font size, an open
 *  panel) survives opening another document and reloading the page. */
export function useStoredState<T>(key: string, initial: T): [T, (next: T | ((current: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored === null ? initial : (JSON.parse(stored) as T);
    } catch {
      return initial;
    }
  });

  const update = useCallback((next: T | ((current: T) => T)) => {
    setValue((current) => {
      const resolved = typeof next === 'function' ? (next as (current: T) => T)(current) : next;
      try {
        localStorage.setItem(key, JSON.stringify(resolved));
      } catch {
        // storage unavailable or full: the preference lasts until the page is closed
      }
      return resolved;
    });
  }, [key]);

  return [value, update];
}
