import { useState } from 'react';
import { Power } from 'lucide-react';
import { system } from '../api';

export function CloseAqdaButton() {
  const [closing, setClosing] = useState(false);
  const [closed, setClosed] = useState(false);

  const close = async () => {
    if (!confirm('Close AQDA? All local changes are already saved. Shared projects will be synced before shutdown.')) return;
    setClosing(true);
    try {
      const result = await system.shutdown();
      if (!result.closing) throw new Error(result.message);
      setClosed(true);
    } catch (error) {
      setClosing(false);
      alert(error instanceof Error ? error.message : 'AQDA could not close automatically. Press Ctrl+C once in the terminal.');
    }
  };

  return (
    <>
      <button
        onClick={close}
        disabled={closing}
        className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 hover:text-red-700 hover:bg-red-50 rounded-md disabled:opacity-50"
        title="Save shared projects and close AQDA safely"
      >
        <Power size={16} /> {closing ? 'Closing…' : 'Close AQDA'}
      </button>
      {closed && (
        <div className="fixed inset-0 z-[100] bg-gray-50 flex items-center justify-center p-6">
          <div className="max-w-md text-center bg-white border border-gray-200 rounded-xl shadow-sm p-8">
            <div className="w-12 h-12 mx-auto mb-4 rounded-full bg-green-100 text-green-700 flex items-center justify-center">
              <Power size={22} />
            </div>
            <h1 className="text-xl font-semibold text-gray-900 mb-2">AQDA is closed safely</h1>
            <p className="text-sm text-gray-600">
              Your local changes were saved and shared projects were given a final sync. You can close this browser tab.
            </p>
          </div>
        </div>
      )}
    </>
  );
}
