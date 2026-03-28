import { useState, useCallback, useRef, useEffect } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import {
  ShieldCheck,
  Play,
  Eraser,
  Copy,
  Check,
  Scan,
  Github,
  Star,
  AlertTriangle,
  ChevronDown,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import Footer from '../components/Footer';

const GITHUB_URL = 'https://github.com/HikaruEgashira/pleno-anonymize';
const API_BASE = 'https://anonymize.plenoai.com';

interface AnalyzeResult {
  entity_type: string;
  start: number;
  end: number;
  score: number;
  text: string;
}

interface RedactResult {
  text: string;
}

const ENTITY_COLORS: Record<string, { bg: string; text: string; border: string; glow: string }> = {
  PERSON: {
    bg: 'rgba(59, 130, 246, 0.12)',
    text: '#60a5fa',
    border: 'rgba(59, 130, 246, 0.3)',
    glow: 'rgba(59, 130, 246, 0.15)',
  },
  EMAIL_ADDRESS: {
    bg: 'rgba(168, 85, 247, 0.12)',
    text: '#c084fc',
    border: 'rgba(168, 85, 247, 0.3)',
    glow: 'rgba(168, 85, 247, 0.15)',
  },
  PHONE_NUMBER: {
    bg: 'rgba(34, 197, 94, 0.12)',
    text: '#4ade80',
    border: 'rgba(34, 197, 94, 0.3)',
    glow: 'rgba(34, 197, 94, 0.15)',
  },
  LOCATION: {
    bg: 'rgba(251, 146, 60, 0.12)',
    text: '#fb923c',
    border: 'rgba(251, 146, 60, 0.3)',
    glow: 'rgba(251, 146, 60, 0.15)',
  },
  DATE_TIME: {
    bg: 'rgba(244, 114, 182, 0.12)',
    text: '#f472b6',
    border: 'rgba(244, 114, 182, 0.3)',
    glow: 'rgba(244, 114, 182, 0.15)',
  },
  URL: {
    bg: 'rgba(56, 189, 248, 0.12)',
    text: '#38bdf8',
    border: 'rgba(56, 189, 248, 0.3)',
    glow: 'rgba(56, 189, 248, 0.15)',
  },
  DEFAULT: {
    bg: 'rgba(148, 163, 184, 0.12)',
    text: '#94a3b8',
    border: 'rgba(148, 163, 184, 0.3)',
    glow: 'rgba(148, 163, 184, 0.15)',
  },
};

const getEntityColor = (type: string) => ENTITY_COLORS[type] || ENTITY_COLORS.DEFAULT;

const SAMPLE_TEXTS = [
  '山田太郎さんの電話番号は090-1234-5678です。メールはtaro@example.comまでお願いします。',
  'John Doe lives at 123 Main Street, New York. His email is john.doe@company.com and phone is 555-0123.',
  '田中花子（hanako.tanaka@gmail.com）に連絡してください。電話は03-1234-5678です。',
];

type Mode = 'analyze' | 'redact';

const Header = () => {
  const [starCount, setStarCount] = useState<number | null>(null);

  useEffect(() => {
    fetch('https://api.github.com/repos/HikaruEgashira/pleno-anonymize')
      .then((res) => res.json())
      .then((data) => {
        if (data.stargazers_count !== undefined) setStarCount(data.stargazers_count);
      })
      .catch(() => {});
  }, []);

  return (
    <header className="fixed top-0 left-0 right-0 z-50 bg-[#0a0a0a]/90 backdrop-blur-xl border-b border-[#1f1f1f]">
      <div className="mx-auto max-w-7xl px-4 md:px-6">
        <div className="flex items-center justify-between h-14">
          <div className="flex items-center gap-6">
            <Link to="/" className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-[#ededed]" />
              <span className="font-medium text-[#ededed]">Pleno Anonymize</span>
            </Link>
            <nav className="hidden md:flex items-center gap-1">
              <Link
                to="/docs"
                className="px-3 py-1.5 text-sm text-[#8f8f8f] hover:text-[#ededed] transition-colors rounded-md hover:bg-[#1a1a1a]"
              >
                Docs
              </Link>
              <span className="px-3 py-1.5 text-sm text-[#ededed] bg-[#1a1a1a] rounded-md">
                Playground
              </span>
            </nav>
          </div>
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg border border-[#2a2a2a] bg-[#141414] hover:bg-[#1a1a1a] transition-colors"
          >
            <Github className="h-4 w-4 text-[#ededed]" />
            {starCount !== null && (
              <span className="flex items-center gap-1 text-sm text-[#8f8f8f]">
                <Star className="h-3 w-3" />
                {starCount}
              </span>
            )}
          </a>
        </div>
      </div>
    </header>
  );
};

function buildHighlightedText(text: string, entities: AnalyzeResult[]) {
  if (entities.length === 0) return [{ text, type: null as string | null, score: 0 }];

  const sorted = [...entities].sort((a, b) => a.start - b.start);
  const segments: { text: string; type: string | null; score: number }[] = [];
  let cursor = 0;

  for (const entity of sorted) {
    if (entity.start > cursor) {
      segments.push({ text: text.slice(cursor, entity.start), type: null, score: 0 });
    }
    segments.push({ text: text.slice(entity.start, entity.end), type: entity.entity_type, score: entity.score });
    cursor = entity.end;
  }
  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), type: null, score: 0 });
  }
  return segments;
}

export default function PlaygroundPage() {
  const [inputText, setInputText] = useState('');
  const [mode, setMode] = useState<Mode>('analyze');
  const [entities, setEntities] = useState<AnalyzeResult[]>([]);
  const [redactedText, setRedactedText] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [hasResult, setHasResult] = useState(false);
  const [copied, setCopied] = useState(false);
  const [scanProgress, setScanProgress] = useState(0);
  const [sampleOpen, setSampleOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scanInterval = useRef<ReturnType<typeof setInterval>>();

  const resetResults = useCallback(() => {
    setEntities([]);
    setRedactedText('');
    setHasResult(false);
    setError('');
  }, []);

  const runAnalysis = useCallback(async () => {
    if (!inputText.trim()) return;
    setLoading(true);
    setError('');
    setScanProgress(0);

    scanInterval.current = setInterval(() => {
      setScanProgress((p) => Math.min(p + Math.random() * 15, 90));
    }, 100);

    try {
      if (mode === 'analyze') {
        const res = await fetch(`${API_BASE}/api/analyze`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: inputText }),
        });
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        const data: AnalyzeResult[] = await res.json();
        setEntities(data);
        setRedactedText('');
      } else {
        const res = await fetch(`${API_BASE}/api/redact`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: inputText }),
        });
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        const data: RedactResult = await res.json();
        setRedactedText(data.text);
        // Also run analyze to get entities
        const analyzeRes = await fetch(`${API_BASE}/api/analyze`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: inputText }),
        });
        if (analyzeRes.ok) {
          setEntities(await analyzeRes.json());
        }
      }
      setHasResult(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Unknown error');
    } finally {
      clearInterval(scanInterval.current);
      setScanProgress(100);
      setTimeout(() => setLoading(false), 300);
    }
  }, [inputText, mode]);

  const handleCopy = useCallback(() => {
    const text = mode === 'redact' && redactedText ? redactedText : JSON.stringify(entities, null, 2);
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }, [mode, redactedText, entities]);

  const segments = hasResult && mode === 'analyze' ? buildHighlightedText(inputText, entities) : [];

  const entityCounts = entities.reduce<Record<string, number>>((acc, e) => {
    acc[e.entity_type] = (acc[e.entity_type] || 0) + 1;
    return acc;
  }, {});

  return (
    <div className="min-h-screen flex flex-col bg-[#0a0a0a]">
      <Header />

      {/* Subtle grid background */}
      <div
        className="fixed inset-0 pointer-events-none opacity-[0.03]"
        style={{
          backgroundImage:
            'linear-gradient(rgba(255,255,255,0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.1) 1px, transparent 1px)',
          backgroundSize: '48px 48px',
        }}
      />

      <main className="flex-1 pt-14">
        <div className="mx-auto max-w-7xl px-4 md:px-6 py-8 md:py-12">
          {/* Title bar */}
          <motion.div
            className="mb-8"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
          >
            <div className="flex items-center gap-3 mb-2">
              <div className="flex items-center gap-2 px-3 py-1 rounded-full bg-emerald-500/10 border border-emerald-500/20">
                <Scan className="h-3.5 w-3.5 text-emerald-400" />
                <span className="text-xs font-mono text-emerald-400 tracking-wide uppercase">Live</span>
              </div>
            </div>
            <h1
              className="text-3xl md:text-4xl font-light tracking-tight text-[#ededed]"
              style={{ fontFamily: "'SF Pro Display', -apple-system, BlinkMacSystemFont, sans-serif" }}
            >
              Playground
            </h1>
            <p className="mt-2 text-[#666] text-sm">
              テキストを入力してPII検出・匿名化をリアルタイムで試す
            </p>
          </motion.div>

          <div className="grid grid-cols-1 lg:grid-cols-[1fr_380px] gap-6">
            {/* Main panel */}
            <motion.div
              className="space-y-4"
              initial={{ opacity: 0, y: 16 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5, delay: 0.1 }}
            >
              {/* Input */}
              <div className="rounded-xl border border-[#1f1f1f] bg-[#111] overflow-hidden">
                <div className="flex items-center justify-between px-4 py-3 border-b border-[#1f1f1f]">
                  <div className="flex items-center gap-3">
                    <span className="text-xs font-mono text-[#666] uppercase tracking-wider">Input</span>
                    <div className="relative">
                      <button
                        onClick={() => setSampleOpen(!sampleOpen)}
                        className="flex items-center gap-1.5 text-xs text-[#555] hover:text-[#999] transition-colors"
                      >
                        サンプル
                        <ChevronDown className={`h-3 w-3 transition-transform ${sampleOpen ? 'rotate-180' : ''}`} />
                      </button>
                      <AnimatePresence>
                        {sampleOpen && (
                          <motion.div
                            initial={{ opacity: 0, y: -4 }}
                            animate={{ opacity: 1, y: 0 }}
                            exit={{ opacity: 0, y: -4 }}
                            className="absolute top-full left-0 mt-2 w-80 z-20 rounded-lg border border-[#2a2a2a] bg-[#161616] shadow-2xl overflow-hidden"
                          >
                            {SAMPLE_TEXTS.map((sample, i) => (
                              <button
                                key={i}
                                onClick={() => {
                                  setInputText(sample);
                                  resetResults();
                                  setSampleOpen(false);
                                }}
                                className="w-full text-left px-4 py-3 text-sm text-[#999] hover:text-[#ededed] hover:bg-[#1a1a1a] transition-colors border-b border-[#1f1f1f] last:border-0 line-clamp-2"
                              >
                                {sample}
                              </button>
                            ))}
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </div>
                  </div>
                  <span className="text-xs font-mono text-[#444]">{inputText.length} chars</span>
                </div>
                <textarea
                  ref={textareaRef}
                  value={inputText}
                  onChange={(e) => {
                    setInputText(e.target.value);
                    resetResults();
                  }}
                  placeholder="個人情報を含むテキストを入力..."
                  className="w-full min-h-[180px] bg-transparent text-[#e5e5e5] text-[15px] leading-relaxed px-4 py-4 resize-y placeholder:text-[#333] focus:outline-none"
                  style={{ fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace" }}
                />
              </div>

              {/* Controls */}
              <div className="flex items-center gap-3">
                <div className="flex items-center rounded-lg border border-[#1f1f1f] bg-[#111] p-0.5">
                  {(['analyze', 'redact'] as Mode[]).map((m) => (
                    <button
                      key={m}
                      onClick={() => {
                        setMode(m);
                        resetResults();
                      }}
                      className={`px-4 py-2 text-sm font-medium rounded-md transition-all ${
                        mode === m
                          ? 'bg-[#1f1f1f] text-[#ededed]'
                          : 'text-[#666] hover:text-[#999]'
                      }`}
                    >
                      {m === 'analyze' ? 'Analyze' : 'Redact'}
                    </button>
                  ))}
                </div>

                <button
                  onClick={runAnalysis}
                  disabled={loading || !inputText.trim()}
                  className="flex items-center gap-2 px-5 py-2 rounded-lg bg-[#ededed] text-[#0a0a0a] text-sm font-medium hover:bg-white disabled:opacity-30 disabled:cursor-not-allowed transition-all"
                >
                  <Play className="h-3.5 w-3.5" />
                  {loading ? 'Scanning...' : 'Run'}
                </button>

                <button
                  onClick={() => {
                    setInputText('');
                    resetResults();
                  }}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg border border-[#1f1f1f] text-[#666] text-sm hover:text-[#999] hover:border-[#2a2a2a] transition-all"
                >
                  <Eraser className="h-3.5 w-3.5" />
                  Clear
                </button>
              </div>

              {/* Scan progress */}
              <AnimatePresence>
                {loading && (
                  <motion.div
                    initial={{ opacity: 0, scaleX: 0 }}
                    animate={{ opacity: 1, scaleX: 1 }}
                    exit={{ opacity: 0 }}
                    className="h-0.5 rounded-full bg-[#1a1a1a] overflow-hidden origin-left"
                  >
                    <motion.div
                      className="h-full bg-gradient-to-r from-emerald-500 to-cyan-400"
                      style={{ width: `${scanProgress}%` }}
                      transition={{ duration: 0.1 }}
                    />
                  </motion.div>
                )}
              </AnimatePresence>

              {/* Error */}
              <AnimatePresence>
                {error && (
                  <motion.div
                    initial={{ opacity: 0, y: -8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -8 }}
                    className="flex items-center gap-3 px-4 py-3 rounded-lg border border-red-500/20 bg-red-500/5 text-red-400 text-sm"
                  >
                    <AlertTriangle className="h-4 w-4 shrink-0" />
                    {error}
                  </motion.div>
                )}
              </AnimatePresence>

              {/* Output */}
              <AnimatePresence mode="wait">
                {hasResult && (
                  <motion.div
                    initial={{ opacity: 0, y: 12 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -8 }}
                    className="rounded-xl border border-[#1f1f1f] bg-[#111] overflow-hidden"
                  >
                    <div className="flex items-center justify-between px-4 py-3 border-b border-[#1f1f1f]">
                      <div className="flex items-center gap-3">
                        <span className="text-xs font-mono text-[#666] uppercase tracking-wider">Output</span>
                        <span className="text-xs font-mono text-emerald-500">
                          {entities.length} entit{entities.length === 1 ? 'y' : 'ies'} found
                        </span>
                      </div>
                      <button
                        onClick={handleCopy}
                        className="flex items-center gap-1.5 text-xs text-[#555] hover:text-[#999] transition-colors"
                      >
                        {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                        {copied ? 'Copied' : 'Copy'}
                      </button>
                    </div>

                    <div
                      className="px-4 py-4 text-[15px] leading-relaxed min-h-[120px]"
                      style={{ fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace" }}
                    >
                      {mode === 'analyze' ? (
                        <div className="flex flex-wrap">
                          {segments.map((seg, i) =>
                            seg.type ? (
                              <motion.span
                                key={i}
                                initial={{ opacity: 0, scale: 0.9 }}
                                animate={{ opacity: 1, scale: 1 }}
                                transition={{ delay: i * 0.03 }}
                                className="relative inline-block mx-0.5 group"
                              >
                                <span
                                  className="relative z-10 px-1.5 py-0.5 rounded-md border"
                                  style={{
                                    background: getEntityColor(seg.type).bg,
                                    borderColor: getEntityColor(seg.type).border,
                                    color: getEntityColor(seg.type).text,
                                    boxShadow: `0 0 12px ${getEntityColor(seg.type).glow}`,
                                  }}
                                >
                                  {seg.text}
                                </span>
                                <span
                                  className="absolute -top-5 left-1/2 -translate-x-1/2 px-1.5 py-0.5 rounded text-[10px] font-mono whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity z-20"
                                  style={{
                                    background: getEntityColor(seg.type).bg,
                                    color: getEntityColor(seg.type).text,
                                    border: `1px solid ${getEntityColor(seg.type).border}`,
                                  }}
                                >
                                  {seg.type} ({Math.round(seg.score * 100)}%)
                                </span>
                              </motion.span>
                            ) : (
                              <span key={i} className="text-[#999]">
                                {seg.text}
                              </span>
                            ),
                          )}
                        </div>
                      ) : (
                        <div className="text-[#e5e5e5] whitespace-pre-wrap">{redactedText}</div>
                      )}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.div>

            {/* Sidebar */}
            <motion.div
              className="space-y-4"
              initial={{ opacity: 0, x: 16 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.5, delay: 0.2 }}
            >
              {/* Entity legend */}
              <div className="rounded-xl border border-[#1f1f1f] bg-[#111] p-4">
                <h3 className="text-xs font-mono text-[#666] uppercase tracking-wider mb-4">Entity Types</h3>
                <div className="space-y-2">
                  {Object.entries(ENTITY_COLORS)
                    .filter(([k]) => k !== 'DEFAULT')
                    .map(([type, color]) => (
                      <div key={type} className="flex items-center justify-between">
                        <div className="flex items-center gap-2.5">
                          <div
                            className="w-2.5 h-2.5 rounded-full"
                            style={{ background: color.text, boxShadow: `0 0 8px ${color.glow}` }}
                          />
                          <span className="text-sm text-[#999] font-mono">{type}</span>
                        </div>
                        <AnimatePresence mode="wait">
                          {entityCounts[type] !== undefined && (
                            <motion.span
                              key={entityCounts[type]}
                              initial={{ opacity: 0, scale: 0.5 }}
                              animate={{ opacity: 1, scale: 1 }}
                              className="text-xs font-mono px-2 py-0.5 rounded-full"
                              style={{ background: color.bg, color: color.text, border: `1px solid ${color.border}` }}
                            >
                              {entityCounts[type]}
                            </motion.span>
                          )}
                        </AnimatePresence>
                      </div>
                    ))}
                </div>
              </div>

              {/* Detected entities detail */}
              <AnimatePresence>
                {hasResult && entities.length > 0 && (
                  <motion.div
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: -8 }}
                    className="rounded-xl border border-[#1f1f1f] bg-[#111] p-4"
                  >
                    <h3 className="text-xs font-mono text-[#666] uppercase tracking-wider mb-4">
                      Detected Entities
                    </h3>
                    <div className="space-y-2 max-h-[400px] overflow-y-auto">
                      {entities.map((entity, i) => {
                        const color = getEntityColor(entity.entity_type);
                        return (
                          <motion.div
                            key={i}
                            initial={{ opacity: 0, x: -8 }}
                            animate={{ opacity: 1, x: 0 }}
                            transition={{ delay: i * 0.05 }}
                            className="rounded-lg border p-3"
                            style={{ borderColor: color.border, background: color.bg }}
                          >
                            <div className="flex items-center justify-between mb-1.5">
                              <span
                                className="text-[11px] font-mono font-medium uppercase tracking-wider"
                                style={{ color: color.text }}
                              >
                                {entity.entity_type}
                              </span>
                              <span className="text-[11px] font-mono text-[#666]">
                                {Math.round(entity.score * 100)}%
                              </span>
                            </div>
                            <div className="text-sm font-mono text-[#e5e5e5] truncate">{entity.text}</div>
                            <div className="text-[11px] font-mono text-[#555] mt-1">
                              pos {entity.start}:{entity.end}
                            </div>
                          </motion.div>
                        );
                      })}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>

              {/* API info */}
              <div className="rounded-xl border border-[#1f1f1f] bg-[#111] p-4">
                <h3 className="text-xs font-mono text-[#666] uppercase tracking-wider mb-3">API Endpoint</h3>
                <div className="space-y-2">
                  <div className="flex items-center gap-2">
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                      POST
                    </span>
                    <code className="text-xs font-mono text-[#888] truncate">/api/analyze</code>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-mono font-bold bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                      POST
                    </span>
                    <code className="text-xs font-mono text-[#888] truncate">/api/redact</code>
                  </div>
                </div>
                <Link
                  to="/docs"
                  className="block mt-3 text-xs text-[#555] hover:text-[#999] transition-colors"
                >
                  Full documentation &rarr;
                </Link>
              </div>
            </motion.div>
          </div>
        </div>
      </main>

      <Footer />
    </div>
  );
}
