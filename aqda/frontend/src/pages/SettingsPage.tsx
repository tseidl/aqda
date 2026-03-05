import { useState, useEffect } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { Link } from 'react-router-dom';
import { ArrowLeft, RefreshCw, Check, FolderOpen, Power } from 'lucide-react';
import { settings as settingsApi } from '../api';

export function SettingsPage() {
  const [form, setForm] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [saveError, setSaveError] = useState<Record<string, string> | null>(null);
  const [starting, setStarting] = useState(false);

  const { data: currentSettings } = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  });

  const { data: dataDirInfo } = useQuery({
    queryKey: ['data-dir'],
    queryFn: settingsApi.dataDir,
  });

  const { data: ollamaStatus, refetch: refreshModels } = useQuery({
    queryKey: ['ollama-models'],
    queryFn: settingsApi.ollamaModels,
    retry: false,
  });

  useEffect(() => {
    if (currentSettings) setForm(currentSettings);
  }, [currentSettings]);

  const saveMut = useMutation({
    mutationFn: () =>
      settingsApi.update(
        Object.entries(form).map(([key, value]) => ({ key, value }))
      ),
    onSuccess: () => {
      setSaved(true);
      setSaveError(null);
      setTimeout(() => setSaved(false), 2000);
    },
    onError: (err) => {
      try {
        const msg = err instanceof Error ? err.message : String(err);
        const jsonStr = msg.replace(/^\d+:\s*/, '');
        const parsed = JSON.parse(jsonStr);
        if (parsed.detail && typeof parsed.detail === 'object') {
          setSaveError(parsed.detail);
        }
      } catch {
        setSaveError(null);
      }
    },
  });

  const handleStartOllama = async () => {
    setStarting(true);
    try {
      await settingsApi.ollamaStart();
      setTimeout(() => refreshModels(), 2000);
    } catch { /* ignore */ }
    finally { setStarting(false); }
  };

  const update = (key: string, value: string) => {
    setForm((f) => ({ ...f, [key]: value }));
  };

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-3xl mx-auto px-6 py-4 flex items-center gap-3">
          <Link to="/" className="p-1.5 text-gray-500 hover:text-gray-700 rounded hover:bg-gray-100">
            <ArrowLeft size={18} />
          </Link>
          <h1 className="text-xl font-semibold text-gray-900">Settings</h1>
        </div>
      </header>

      <main className="max-w-3xl mx-auto px-6 py-8 space-y-8">
        {/* Data Storage */}
        <section>
          <h2 className="text-lg font-medium text-gray-800 mb-4">Data Storage</h2>
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Database Location</label>
              <div className="flex items-center gap-2 px-3 py-2 bg-gray-50 rounded-md border border-gray-200">
                <FolderOpen size={16} className="text-gray-400 shrink-0" />
                <code className="text-sm text-gray-600 break-all">{dataDirInfo?.db_file ?? '...'}</code>
              </div>
            </div>
            <p className="text-xs text-gray-400">
              All projects and data are stored in this single SQLite file. You can back it up, share it via cloud storage (Google Drive, Dropbox), or move it to another machine.
              To change the location, set the <code className="bg-gray-100 px-1 rounded">AQDA_DATA_DIR</code> environment variable before starting AQDA.
            </p>
            <div className="text-xs text-gray-500 bg-gray-50 rounded p-2 font-mono">
              AQDA_DATA_DIR=/path/to/folder aqda
            </div>
          </div>
        </section>

        {/* Ollama Connection */}
        <section>
          <h2 className="text-lg font-medium text-gray-800 mb-4">AI Setup (Ollama)</h2>
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
            <div className="text-sm text-gray-600 space-y-2 bg-indigo-50 rounded-lg p-3">
              <p className="font-medium text-gray-700">How AI features work</p>
              <p>
                AQDA uses <a href="https://ollama.com" target="_blank" rel="noopener noreferrer" className="text-indigo-600 underline">Ollama</a> to
                run AI models locally on your machine. No data leaves your computer.
              </p>
              <p>To get started:</p>
              <ol className="list-decimal ml-5 space-y-1 text-xs text-gray-500">
                <li>Install Ollama from <a href="https://ollama.com/download" target="_blank" rel="noopener noreferrer" className="text-indigo-600 underline">ollama.com/download</a></li>
                <li>Ollama runs as a background service automatically after installation</li>
                <li>Pull models you need, e.g.: <code className="bg-white px-1 rounded">ollama pull qwen3.5:9b</code> (LLM) and <code className="bg-white px-1 rounded">ollama pull nomic-embed-text</code> (embeddings)</li>
                <li>Select your models below and save</li>
              </ol>
              <p className="text-xs text-gray-400">
                If you see &ldquo;address already in use&rdquo; when running <code className="bg-white px-1 rounded">ollama serve</code>, that means Ollama is already running in the background &mdash; this is normal.
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Ollama URL</label>
              <input
                value={form.ollama_url ?? ''}
                onChange={(e) => update('ollama_url', e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
            </div>
            <div className="flex items-center gap-3">
              <span
                className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-medium ${
                  ollamaStatus?.available
                    ? 'bg-green-100 text-green-700'
                    : 'bg-red-100 text-red-700'
                }`}
              >
                <span className={`w-1.5 h-1.5 rounded-full ${ollamaStatus?.available ? 'bg-green-500' : 'bg-red-500'}`} />
                {ollamaStatus?.available ? 'Connected' : 'Not connected'}
              </span>
              <button
                onClick={() => refreshModels()}
                className="text-xs text-gray-500 hover:text-gray-700 flex items-center gap-1"
              >
                <RefreshCw size={12} /> Refresh
              </button>
              {!ollamaStatus?.available && (
                <button
                  onClick={handleStartOllama}
                  disabled={starting}
                  className="text-xs text-indigo-600 hover:text-indigo-700 flex items-center gap-1"
                >
                  <Power size={12} /> {starting ? 'Starting...' : 'Start Ollama'}
                </button>
              )}
              {ollamaStatus?.available && (
                <span className="text-xs text-gray-400">
                  {ollamaStatus.models.length} model{ollamaStatus.models.length !== 1 ? 's' : ''} available
                </span>
              )}
            </div>
          </div>
        </section>

        {/* Model Selection */}
        <section>
          <h2 className="text-lg font-medium text-gray-800 mb-4">Model Selection</h2>
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                LLM Model <span className="text-gray-400 font-normal">(for analysis & chat)</span>
              </label>
              {ollamaStatus?.available ? (
                <select
                  value={form.llm_model ?? ''}
                  onChange={(e) => update('llm_model', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option value="">Select a model...</option>
                  {ollamaStatus.models.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              ) : (
                <input
                  value={form.llm_model ?? ''}
                  onChange={(e) => update('llm_model', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  placeholder="e.g., qwen3.5:9b"
                />
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Embedding Model <span className="text-gray-400 font-normal">(for similarity search)</span>
              </label>
              {ollamaStatus?.available ? (
                <select
                  value={form.embedding_model ?? ''}
                  onChange={(e) => update('embedding_model', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  <option value="">Select a model...</option>
                  {ollamaStatus.models.map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              ) : (
                <input
                  value={form.embedding_model ?? ''}
                  onChange={(e) => update('embedding_model', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  placeholder="e.g., nomic-embed-text"
                />
              )}
            </div>
            <div>
              <label className="flex items-center gap-3 cursor-pointer">
                <input
                  type="checkbox"
                  checked={(form.think_mode ?? 'off') === 'on'}
                  onChange={(e) => update('think_mode', e.target.checked ? 'on' : 'off')}
                  className="w-4 h-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                />
                <span className="text-sm font-medium text-gray-700">Enable thinking mode</span>
              </label>
              <p className="text-xs text-gray-400 mt-1 ml-7">
                Some models (like qwen3) can "think" before answering, which may improve quality but takes much longer. Off by default.
              </p>
            </div>
          </div>
        </section>

        {/* Chunking */}
        <section>
          <h2 className="text-lg font-medium text-gray-800 mb-4">Text Chunking</h2>
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Chunk Size <span className="font-normal text-gray-400">(characters)</span></label>
                <input
                  type="number"
                  value={form.chunk_size ?? '500'}
                  onChange={(e) => update('chunk_size', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Chunk Overlap <span className="font-normal text-gray-400">(characters)</span></label>
                <input
                  type="number"
                  value={form.chunk_overlap ?? '50'}
                  onChange={(e) => update('chunk_overlap', e.target.value)}
                  className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>
            </div>
            <p className="text-xs text-gray-400">
              Documents are split into overlapping chunks for embedding search. 500 characters ≈ 100 words. Smaller chunks are more precise, larger chunks capture more context.
            </p>
          </div>
        </section>

        {/* Color Scheme */}
        <section>
          <h2 className="text-lg font-medium text-gray-800 mb-4">Color Scheme</h2>
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
            <p className="text-xs text-gray-400">
              Choose a color palette for new codes. Existing code colors are not changed.
            </p>
            <div className="space-y-3">
              {[
                { name: 'Default', colors: ['#6366f1','#ec4899','#f59e0b','#10b981','#3b82f6','#8b5cf6','#ef4444','#14b8a6','#f97316','#06b6d4'] },
                { name: 'Pastel', colors: ['#a5b4fc','#f9a8d4','#fcd34d','#6ee7b7','#93c5fd','#c4b5fd','#fca5a5','#5eead4','#fdba74','#67e8f9'] },
                { name: 'Earthy', colors: ['#92400e','#78350f','#365314','#1e3a5f','#4c1d95','#831843','#6b7280','#b45309','#166534','#1e40af'] },
                { name: 'Vivid', colors: ['#dc2626','#2563eb','#16a34a','#d97706','#9333ea','#0891b2','#e11d48','#4f46e5','#059669','#ea580c'] },
              ].map((scheme) => (
                <div
                  key={scheme.name}
                  className={`flex items-center gap-3 p-2 rounded-lg cursor-pointer hover:bg-gray-50 ${
                    (form.color_scheme ?? 'Default') === scheme.name ? 'ring-2 ring-indigo-500 bg-indigo-50' : ''
                  }`}
                  onClick={() => update('color_scheme', scheme.name)}
                >
                  <span className="text-sm font-medium text-gray-700 w-16">{scheme.name}</span>
                  <div className="flex gap-1">
                    {scheme.colors.map((c) => (
                      <span key={c} className="w-5 h-5 rounded-sm" style={{ backgroundColor: c }} />
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* Filename Variable Parsing */}
        <section>
          <h2 className="text-lg font-medium text-gray-800 mb-4">Filename Variable Parsing</h2>
          <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
            <div className="text-sm text-gray-600 space-y-2 bg-amber-50 rounded-lg p-3">
              <p className="font-medium text-gray-700">Auto-extract metadata from filenames</p>
              <p className="text-xs text-gray-500">
                Define a regex pattern with named groups to automatically extract variables when importing documents.
                Use Python-style named groups: <code className="bg-white px-1 rounded">{'(?P<name>pattern)'}</code>
              </p>
              <p className="text-xs text-gray-500">
                Example: for files like <code className="bg-white px-1 rounded">Interview_M35_NYC.txt</code>, use pattern:{' '}
                <code className="bg-white px-1 rounded">{'(?P<gender>[MF])(?P<age>\\d+)_(?P<city>\\w+)'}</code>
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Regex Pattern</label>
              <input
                value={form.filename_pattern ?? ''}
                onChange={(e) => update('filename_pattern', e.target.value)}
                className={`w-full px-3 py-2 border rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-indigo-500 ${
                  saveError?.filename_pattern ? 'border-red-400' : 'border-gray-300'
                }`}
                placeholder="e.g., (?P<id>\d+)_(?P<gender>[MF])(?P<age>\d+)_(?P<city>\w+)"
              />
              {saveError?.filename_pattern && (
                <p className="text-xs text-red-600 mt-1">{saveError.filename_pattern}</p>
              )}
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Variable Names <span className="text-gray-400 font-normal">(fallback for unnamed groups, comma-separated)</span>
              </label>
              <input
                value={form.filename_variables ?? ''}
                onChange={(e) => update('filename_variables', e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                placeholder="e.g., id, gender, age, city"
              />
            </div>
          </div>
        </section>

        {/* Save */}
        <button
          onClick={() => saveMut.mutate()}
          className="flex items-center gap-2 px-6 py-2.5 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm font-medium"
        >
          {saved ? <><Check size={16} /> Saved</> : 'Save Settings'}
        </button>
      </main>
    </div>
  );
}
