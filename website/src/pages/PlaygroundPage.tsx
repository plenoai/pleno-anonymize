import { useState, useCallback, useRef, useEffect, useMemo, memo, useReducer } from 'react';
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
  MEDICAL_HISTORY: {
    bg: 'rgba(239, 68, 68, 0.12)',
    text: '#f87171',
    border: 'rgba(239, 68, 68, 0.3)',
    glow: 'rgba(239, 68, 68, 0.15)',
  },
  HEALTH_CHECKUP: {
    bg: 'rgba(245, 158, 11, 0.12)',
    text: '#fbbf24',
    border: 'rgba(245, 158, 11, 0.3)',
    glow: 'rgba(245, 158, 11, 0.15)',
  },
  DISABILITY: {
    bg: 'rgba(217, 70, 239, 0.12)',
    text: '#e879f9',
    border: 'rgba(217, 70, 239, 0.3)',
    glow: 'rgba(217, 70, 239, 0.15)',
  },
  CRIMINAL_RECORD: {
    bg: 'rgba(220, 38, 38, 0.12)',
    text: '#dc2626',
    border: 'rgba(220, 38, 38, 0.3)',
    glow: 'rgba(220, 38, 38, 0.15)',
  },
  CRIME_VICTIM: {
    bg: 'rgba(190, 18, 60, 0.12)',
    text: '#fb7185',
    border: 'rgba(190, 18, 60, 0.3)',
    glow: 'rgba(190, 18, 60, 0.15)',
  },
  RACE: {
    bg: 'rgba(14, 165, 233, 0.12)',
    text: '#38bdf8',
    border: 'rgba(14, 165, 233, 0.3)',
    glow: 'rgba(14, 165, 233, 0.15)',
  },
  CREED: {
    bg: 'rgba(99, 102, 241, 0.12)',
    text: '#818cf8',
    border: 'rgba(99, 102, 241, 0.3)',
    glow: 'rgba(99, 102, 241, 0.15)',
  },
  SOCIAL_STATUS: {
    bg: 'rgba(139, 92, 246, 0.12)',
    text: '#a78bfa',
    border: 'rgba(139, 92, 246, 0.3)',
    glow: 'rgba(139, 92, 246, 0.15)',
  },
  DEFAULT: {
    bg: 'rgba(148, 163, 184, 0.12)',
    text: '#94a3b8',
    border: 'rgba(148, 163, 184, 0.3)',
    glow: 'rgba(148, 163, 184, 0.15)',
  },
};

const getEntityColor = (type: string) => ENTITY_COLORS[type] || ENTITY_COLORS.DEFAULT;

type Engine = 'default' | 'appi';

const ENGINE_LABELS: Record<Engine, string> = {
  default: 'Default',
  appi: 'APPI 要配慮',
};

const SAMPLE_TEXTS: Record<Engine, string[]> = {
  default: [
    '山田太郎さんの電話番号は090-1234-5678です。メールはtaro@example.comまでお願いします。',
    'John Doe lives at 123 Main Street, New York. His email is john.doe@company.com and phone is 555-0123.',
    '田中花子（hanako.tanaka@gmail.com）に連絡してください。電話は03-1234-5678です。',
  ],
  appi: [
    '患者 山田太郎はうつ病と診断され、2023年より通院中である。',
    '佐藤花子様の健康診断結果: HbA1c 7.2%、血圧 152/96mmHg。要精密検査。',
    '被告人 渡辺健は窃盗罪で懲役1年6月の判決を受けた。',
  ],
};

type Mode = 'analyze' | 'redact';

const Header = memo(function Header() {
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
});

// Build a mapping from Unicode codepoint index → UTF-16 code-unit index.
// The server returns Python (codepoint) offsets; JS String.slice uses UTF-16 units.
// Astral chars (emoji, some CJK) are 1 codepoint but 2 UTF-16 units, so a direct
// slice with server offsets would misalign highlights for any text containing them.
function buildCpToUtf16Map(text: string): number[] {
  const map: number[] = [0];
  let u = 0;
  for (const cp of text) {
    u += cp.length;
    map.push(u);
  }
  return map;
}

function buildHighlightedText(text: string, entities: AnalyzeResult[]) {
  if (entities.length === 0) return [{ text, type: null as string | null, score: 0 }];

  const cpMap = buildCpToUtf16Map(text);
  const toU16 = (cp: number) => cpMap[cp] ?? text.length;

  const sorted = [...entities].sort((a, b) => a.start - b.start);
  const segments: { text: string; type: string | null; score: number }[] = [];
  let cursor = 0;

  for (const entity of sorted) {
    const start = toU16(entity.start);
    const end = toU16(entity.end);
    if (end <= cursor) continue;
    if (start > cursor) {
      segments.push({ text: text.slice(cursor, start), type: null, score: 0 });
    }
    segments.push({ text: text.slice(start, end), type: entity.entity_type, score: entity.score });
    cursor = end;
  }
  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor), type: null, score: 0 });
  }
  return segments;
}

interface PlaygroundState {
  inputText: string;
  mode: Mode;
  engine: Engine;
  entities: AnalyzeResult[];
  redactedText: string;
  loading: boolean;
  error: string;
  hasResult: boolean;
  copied: boolean;
  scanProgress: number;
  sampleOpen: boolean;
}

type PlaygroundAction =
  | { type: 'SET_INPUT_TEXT'; payload: string }
  | { type: 'SET_MODE'; payload: Mode }
  | { type: 'SET_ENGINE'; payload: Engine }
  | { type: 'RESET_RESULTS' }
  | { type: 'START_SCAN' }
  | { type: 'ADVANCE_SCAN_PROGRESS'; payload: number }
  | { type: 'ANALYZE_SUCCESS'; payload: { entities: AnalyzeResult[] } }
  | { type: 'REDACT_SUCCESS'; payload: { redactedText: string; entities: AnalyzeResult[] } }
  | { type: 'SCAN_ERROR'; payload: string }
  | { type: 'SCAN_COMPLETE' }
  | { type: 'FINISH_LOADING' }
  | { type: 'SET_COPIED'; payload: boolean }
  | { type: 'SET_SAMPLE_OPEN'; payload: boolean };

const initialState: PlaygroundState = {
  inputText: '',
  mode: 'analyze',
  engine: 'default',
  entities: [],
  redactedText: '',
  loading: false,
  error: '',
  hasResult: false,
  copied: false,
  scanProgress: 0,
  sampleOpen: false,
};

function playgroundReducer(state: PlaygroundState, action: PlaygroundAction): PlaygroundState {
  switch (action.type) {
    case 'SET_INPUT_TEXT':
      return { ...state, inputText: action.payload };
    case 'SET_MODE':
      return { ...state, mode: action.payload };
    case 'SET_ENGINE':
      return { ...state, engine: action.payload };
    case 'RESET_RESULTS':
      return { ...state, entities: [], redactedText: '', hasResult: false, error: '' };
    case 'START_SCAN':
      return { ...state, loading: true, error: '', scanProgress: 0 };
    case 'ADVANCE_SCAN_PROGRESS':
      return { ...state, scanProgress: Math.min(state.scanProgress + action.payload, 90) };
    case 'ANALYZE_SUCCESS':
      return { ...state, entities: action.payload.entities, redactedText: '', hasResult: true };
    case 'REDACT_SUCCESS':
      return {
        ...state,
        redactedText: action.payload.redactedText,
        entities: action.payload.entities,
        hasResult: true,
      };
    case 'SCAN_ERROR':
      return { ...state, error: action.payload };
    case 'SCAN_COMPLETE':
      return { ...state, scanProgress: 100 };
    case 'FINISH_LOADING':
      return { ...state, loading: false };
    case 'SET_COPIED':
      return { ...state, copied: action.payload };
    case 'SET_SAMPLE_OPEN':
      return { ...state, sampleOpen: action.payload };
  }
}

export default function PlaygroundPage() {
  const [state, dispatch] = useReducer(playgroundReducer, initialState);
  const { inputText, mode, engine, entities, redactedText, loading, error, hasResult, copied, scanProgress, sampleOpen } = state;
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const scanInterval = useRef<ReturnType<typeof setInterval>>();

  useEffect(() => {
    return () => clearInterval(scanInterval.current);
  }, []);

  const resetResults = useCallback(() => {
    dispatch({ type: 'RESET_RESULTS' });
  }, []);

  const runAnalysis = useCallback(async () => {
    if (!inputText.trim()) return;
    dispatch({ type: 'START_SCAN' });

    scanInterval.current = setInterval(() => {
      dispatch({ type: 'ADVANCE_SCAN_PROGRESS', payload: Math.random() * 30 });
    }, 250);

    try {
      const body = JSON.stringify({ text: inputText, engine });
      const headers = { 'Content-Type': 'application/json' };

      if (mode === 'analyze') {
        const res = await fetch(`${API_BASE}/api/analyze`, { method: 'POST', headers, body });
        if (!res.ok) throw new Error(`API error: ${res.status}`);
        dispatch({ type: 'ANALYZE_SUCCESS', payload: { entities: await res.json() } });
      } else {
        const [redactResult, analyzeResult] = await Promise.allSettled([
          fetch(`${API_BASE}/api/redact`, { method: 'POST', headers, body }),
          fetch(`${API_BASE}/api/analyze`, { method: 'POST', headers, body }),
        ]);
        if (redactResult.status === 'rejected') throw new Error('Network error');
        if (!redactResult.value.ok) throw new Error(`API error: ${redactResult.value.status}`);
        const redacted = (await redactResult.value.json() as RedactResult).text;
        const analyzedEntities =
          analyzeResult.status === 'fulfilled' && analyzeResult.value.ok
            ? await analyzeResult.value.json()
            : [];
        dispatch({ type: 'REDACT_SUCCESS', payload: { redactedText: redacted, entities: analyzedEntities } });
      }
    } catch (e) {
      dispatch({ type: 'SCAN_ERROR', payload: e instanceof Error ? e.message : 'Unknown error' });
    } finally {
      clearInterval(scanInterval.current);
      dispatch({ type: 'SCAN_COMPLETE' });
      setTimeout(() => dispatch({ type: 'FINISH_LOADING' }), 300);
    }
  }, [inputText, mode, engine]);

  const handleCopy = useCallback(() => {
    const text = mode === 'redact' && redactedText ? redactedText : JSON.stringify(entities, null, 2);
    navigator.clipboard.writeText(text).then(() => {
      dispatch({ type: 'SET_COPIED', payload: true });
      setTimeout(() => dispatch({ type: 'SET_COPIED', payload: false }), 2000);
    }).catch(() => {});
  }, [mode, redactedText, entities]);

  const segments = useMemo(
    () => (hasResult && mode === 'analyze' ? buildHighlightedText(inputText, entities) : []),
    [hasResult, mode, inputText, entities],
  );

  const entityCounts = useMemo(
    () =>
      entities.reduce<Record<string, number>>((acc, e) => {
        acc[e.entity_type] = (acc[e.entity_type] || 0) + 1;
        return acc;
      }, {}),
    [entities],
  );

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
                        onClick={() => dispatch({ type: 'SET_SAMPLE_OPEN', payload: !sampleOpen })}
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') dispatch({ type: 'SET_SAMPLE_OPEN', payload: false });
                        }}
                        aria-expanded={sampleOpen}
                        aria-haspopup="listbox"
                        aria-label="サンプルテキストを選択"
                        className="flex items-center gap-1.5 text-xs text-[#555] hover:text-[#999] transition-colors focus-visible:ring-2 focus-visible:ring-emerald-500/50 focus-visible:rounded"
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
                            role="listbox"
                            aria-label="サンプルテキスト一覧"
                            className="absolute top-full left-0 mt-2 w-80 z-20 rounded-lg border border-[#2a2a2a] bg-[#161616] shadow-2xl overflow-hidden"
                          >
                            {SAMPLE_TEXTS[engine].map((sample, i) => (
                              <button
                                key={i}
                                role="option"
                                aria-selected={inputText === sample}
                                onClick={() => {
                                  dispatch({ type: 'SET_INPUT_TEXT', payload: sample });
                                  resetResults();
                                  dispatch({ type: 'SET_SAMPLE_OPEN', payload: false });
                                  textareaRef.current?.focus();
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
                <label htmlFor="playground-input" className="sr-only">
                個人情報を含むテキストを入力
              </label>
              <textarea
                  id="playground-input"
                  ref={textareaRef}
                  value={inputText}
                  onChange={(e) => {
                    dispatch({ type: 'SET_INPUT_TEXT', payload: e.target.value });
                    resetResults();
                  }}
                  onKeyDown={(e) => {
                    if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                      e.preventDefault();
                      runAnalysis();
                    }
                  }}
                  placeholder="個人情報を含むテキストを入力..."
                  aria-label="PII検出対象のテキスト入力"
                  className="w-full min-h-[180px] bg-transparent text-[#e5e5e5] text-[15px] leading-relaxed px-4 py-4 resize-y placeholder:text-[#333] focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500/50 focus-visible:ring-offset-0"
                  style={{ fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace" }}
                />
              </div>

              {/* Controls */}
              <div className="flex items-center gap-3 flex-wrap">
                <div className="flex items-center rounded-lg border border-[#1f1f1f] bg-[#111] p-0.5" role="radiogroup" aria-label="処理モード">
                  {(['analyze', 'redact'] as Mode[]).map((m) => (
                    <button
                      key={m}
                      role="radio"
                      aria-checked={mode === m}
                      onClick={() => {
                        dispatch({ type: 'SET_MODE', payload: m });
                        resetResults();
                      }}
                      className={`px-4 py-2 text-sm font-medium rounded-md transition-all focus-visible:ring-2 focus-visible:ring-emerald-500/50 ${
                        mode === m
                          ? 'bg-[#1f1f1f] text-[#ededed]'
                          : 'text-[#666] hover:text-[#999]'
                      }`}
                    >
                      {m === 'analyze' ? 'Analyze' : 'Redact'}
                    </button>
                  ))}
                </div>

                <div className="flex items-center rounded-lg border border-[#1f1f1f] bg-[#111] p-0.5" role="radiogroup" aria-label="検出エンジン">
                  {(['default', 'appi'] as Engine[]).map((e) => (
                    <button
                      key={e}
                      role="radio"
                      aria-checked={engine === e}
                      onClick={() => {
                        dispatch({ type: 'SET_ENGINE', payload: e });
                        resetResults();
                      }}
                      className={`px-4 py-2 text-sm font-medium rounded-md transition-all focus-visible:ring-2 focus-visible:ring-emerald-500/50 ${
                        engine === e
                          ? 'bg-[#1f1f1f] text-[#ededed]'
                          : 'text-[#666] hover:text-[#999]'
                      }`}
                    >
                      {ENGINE_LABELS[e]}
                    </button>
                  ))}
                </div>

                <button
                  onClick={runAnalysis}
                  disabled={loading || !inputText.trim()}
                  aria-label={loading ? 'スキャン中...' : '実行 (⌘+Enter)'}
                  title="⌘+Enter / Ctrl+Enter"
                  className="flex items-center gap-2 px-5 py-2 rounded-lg bg-[#ededed] text-[#0a0a0a] text-sm font-medium hover:bg-white disabled:opacity-30 disabled:cursor-not-allowed transition-all focus-visible:ring-2 focus-visible:ring-emerald-500 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0a0a0a]"
                >
                  <Play className="h-3.5 w-3.5" />
                  {loading ? 'Scanning...' : 'Run'}
                </button>

                <button
                  onClick={() => {
                    dispatch({ type: 'SET_INPUT_TEXT', payload: '' });
                    resetResults();
                  }}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg border border-[#1f1f1f] text-[#666] text-sm hover:text-[#999] hover:border-[#2a2a2a] transition-all focus-visible:ring-2 focus-visible:ring-emerald-500/50 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0a0a0a]"
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
                    role="alert"
                    className="flex items-center gap-3 px-4 py-3 rounded-lg border border-red-500/20 bg-red-500/5 text-red-400 text-sm"
                  >
                    <AlertTriangle className="h-4 w-4 shrink-0" aria-hidden="true" />
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
                        aria-label={copied ? 'コピー済み' : '結果をコピー'}
                        className="flex items-center gap-1.5 text-xs text-[#555] hover:text-[#999] transition-colors focus-visible:ring-2 focus-visible:ring-emerald-500/50 focus-visible:rounded"
                      >
                        {copied ? <Check className="h-3.5 w-3.5 text-emerald-400" /> : <Copy className="h-3.5 w-3.5" />}
                        {copied ? 'Copied' : 'Copy'}
                      </button>
                    </div>

                    <div
                      className="px-4 py-4 text-[15px] leading-relaxed min-h-[120px]"
                      style={{ fontFamily: "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace" }}
                      aria-live="polite"
                      aria-label="検出結果"
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
                                role="mark"
                                aria-label={`${seg.type}: ${seg.text} (信頼度${Math.round(seg.score * 100)}%)`}
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
                    .filter(([k]) => {
                      if (k === 'DEFAULT') return false;
                      if (hasResult) return true;
                      const appiTypes = ['MEDICAL_HISTORY', 'HEALTH_CHECKUP', 'DISABILITY', 'CRIMINAL_RECORD', 'CRIME_VICTIM', 'RACE', 'CREED', 'SOCIAL_STATUS', 'PERSON', 'ADDRESS', 'ORGANIZATION', 'DATE_OF_BIRTH', 'BANK_ACCOUNT'];
                      const defaultTypes = ['PERSON', 'EMAIL_ADDRESS', 'PHONE_NUMBER', 'LOCATION', 'DATE_TIME', 'URL'];
                      return engine === 'appi' ? appiTypes.includes(k) : defaultTypes.includes(k);
                    })
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
