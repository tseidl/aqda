import { useEffect, useState } from 'react';
import { ai, type GenerationProgress as Progress, type GenerationTiming } from '../api';

// "1:05" for 65 seconds.
const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

/** What the model is doing right now, in words. How long an answer will take is unknown,
 *  so this reports the stage, the time so far, and the words written. */
function describe(progress: Progress | null, seconds: number): string {
  const time = clock(progress?.active ? Math.max(seconds, progress.seconds) : seconds);
  if (progress?.active && progress.phase === 'writing') {
    return `Writing the answer: ${progress.words} word${progress.words === 1 ? '' : 's'} so far · ${time}`;
  }
  if (progress?.active && progress.phase === 'thinking') return `The model is reasoning… ${time}`;
  return seconds < 20
    ? `Waiting for the model… ${time}`
    : `Waiting for the model… ${time}. It is loading or reading the passages; this can take a few minutes.`;
}

/** Progress of a running model answer: a moving bar and a line saying what is happening.
 *  Render it only while the answer is awaited. */
export function GenerationProgress({ className = '' }: { className?: string }) {
  const [progress, setProgress] = useState<Progress | null>(null);
  const [seconds, setSeconds] = useState(0);

  useEffect(() => {
    const started = Date.now();
    const timer = setInterval(async () => {
      setSeconds(Math.floor((Date.now() - started) / 1000));
      try {
        setProgress(await ai.generationProgress());
      } catch {
        // keep the last known state
      }
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className={className}>
      <div className="h-1.5 overflow-hidden rounded-full bg-purple-100">
        <div className="aqda-working-bar h-full w-1/3 rounded-full bg-purple-500" />
      </div>
      <p className="mt-1 text-[10px] leading-snug text-gray-500">{describe(progress, seconds)}</p>
    </div>
  );
}

/** One line saying how long a finished answer took and where the time went, so a researcher
 *  can tell a slow start (loading the model) from long reading or long writing. */
export function AnswerTiming({ timing, className = '' }: { timing?: GenerationTiming; className?: string }) {
  if (!timing || !timing.total_seconds) return null;
  const part = (label: string, seconds?: number) =>
    seconds && seconds >= 1 ? `${label} ${clock(Math.round(seconds))}` : null;
  const parts = [
    part('loading the model', timing.load_seconds),
    part('reading the passages', timing.prompt_seconds),
    part(timing.reasoned ? 'reasoning and writing' : 'writing', timing.answer_seconds),
  ].filter(Boolean);
  return (
    <p className={`text-[10px] text-gray-400 ${className}`}>
      Took {clock(Math.round(timing.total_seconds))}{parts.length ? `: ${parts.join(', ')}` : ''}
    </p>
  );
}
