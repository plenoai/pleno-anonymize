import { motion } from 'framer-motion';
import { ShieldCheck, CheckCircle2, ArrowLeft } from 'lucide-react';
// CheckCircle2 used in entity cards
import Footer from '../components/Footer';
import { Link } from 'react-router-dom';
import scores from '@scores';

const ENTITY_CONFIG: Record<string, { label: string; threshold: number; color: string; order: number }> = {
  PERSON:        { label: '人名',     threshold: 0.9,  color: '#3b82f6', order: 0 },
  ADDRESS:       { label: '住所',     threshold: 0.85, color: '#8b5cf6', order: 1 },
  ORGANIZATION:  { label: '組織名',   threshold: 0.85, color: '#06b6d4', order: 2 },
  DATE_OF_BIRTH: { label: '生年月日', threshold: 0.8,  color: '#f59e0b', order: 3 },
  BANK_ACCOUNT:  { label: '銀行口座', threshold: 0.8,  color: '#10b981', order: 4 },
};

const BENCHMARK_DATA = Object.entries(scores.ents_per_type)
  .filter(([entity]) => entity in ENTITY_CONFIG)
  .map(([entity, { p, r, f }]) => ({
    entity,
    label: ENTITY_CONFIG[entity].label,
    precision: p,
    recall: r,
    f1: f,
    threshold: ENTITY_CONFIG[entity].threshold,
    color: ENTITY_CONFIG[entity].color,
  }))
  .sort((a, b) => ENTITY_CONFIG[a.entity].order - ENTITY_CONFIG[b.entity].order);

const OVERALL = { precision: scores.ents_p, recall: scores.ents_r, f1: scores.ents_f, threshold: 0.88 };

const COMPARISON_MODELS = [
  { name: 'pleno_ner_ja', label: 'pleno_ner_ja (ours)', shortLabel: 'ours', color: '#10b981', highlight: true },
  { name: 'bert_ner_ja', label: 'bert-ner-japanese (HF)', shortLabel: 'HF', color: '#f59e0b', highlight: false },
  { name: 'ja_core_news_lg', label: 'ja_core_news_lg', shortLabel: 'lg', color: '#6b7280', highlight: false },
  { name: 'ja_core_news_md', label: 'ja_core_news_md', shortLabel: 'md', color: '#9ca3af', highlight: false },
  { name: 'ja_core_news_sm', label: 'ja_core_news_sm', shortLabel: 'sm', color: '#d1d5db', highlight: false },
];

// ja_core_news: GPE→ADDRESS, ORG→ORGANIZATION, DATE→DATE_OF_BIRTH にマッピング済み
// bert-ner-japanese: 人名→PERSON, 地名→ADDRESS, 法人名/施設名→ORGANIZATION にマッピング済み
const EXTERNAL_SCORES: Record<string, Record<string, number>> = {
  PERSON:        { bert_ner_ja: 0.8860, ja_core_news_lg: 0.8543, ja_core_news_md: 0.8698, ja_core_news_sm: 0.4724 },
  ADDRESS:       { bert_ner_ja: 0.6903, ja_core_news_lg: 0.6604, ja_core_news_md: 0.7299, ja_core_news_sm: 0.2444 },
  ORGANIZATION:  { bert_ner_ja: 0.5908, ja_core_news_lg: 0.5900, ja_core_news_md: 0.5578, ja_core_news_sm: 0.4419 },
  DATE_OF_BIRTH: { bert_ner_ja: 0,      ja_core_news_lg: 0.9060, ja_core_news_md: 0.9343, ja_core_news_sm: 0.8675 },
  BANK_ACCOUNT:  { bert_ner_ja: 0,      ja_core_news_lg: 0,      ja_core_news_md: 0,      ja_core_news_sm: 0      },
};

const COMPARISON_DATA: Record<string, Record<string, number>> = Object.fromEntries(
  Object.entries(EXTERNAL_SCORES).map(([entity, ext]) => [
    entity,
    { pleno_ner_ja: scores.ents_per_type[entity]?.f ?? 0, ...ext },
  ])
);

const ENTITY_LABELS: Record<string, string> = {
  PERSON: '人名', ADDRESS: '住所', ORGANIZATION: '組織名',
  DATE_OF_BIRTH: '生年月日', BANK_ACCOUNT: '銀行口座', _overall: 'Overall F1',
};

const SIZE_DATA: Record<string, number> = {
  pleno_ner_ja: 6.3, bert_ner_ja: 440, ja_core_news_lg: 583.8, ja_core_news_md: 56.0, ja_core_news_sm: 13.8,
};
const LATENCY_DATA: Record<string, number> = {
  pleno_ner_ja: 2.8, bert_ner_ja: 17.7, ja_core_news_lg: 6.9, ja_core_news_md: 6.8, ja_core_news_sm: 6.7,
};


const BarChart = ({ value, max = 1, color, delay = 0 }: { value: number; max?: number; color: string; delay?: number }) => (
  <div className="h-2 w-full rounded-full bg-[#f0f0f0] dark:bg-[#2a2a2a] overflow-hidden">
    <motion.div
      className="h-full rounded-full"
      style={{ backgroundColor: color }}
      initial={{ width: 0 }}
      animate={{ width: `${(value / max) * 100}%` }}
           transition={{ duration: 0.8, delay, ease: 'easeOut' }}
    />
  </div>
);

const ScoreRing = ({ value, size = 160, strokeWidth = 10, color = '#3b82f6', delay = 0 }: {
  value: number; size?: number; strokeWidth?: number; color?: string; delay?: number;
}) => {
  const radius = (size - strokeWidth) / 2;
  const circumference = radius * 2 * Math.PI;

  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={radius} fill="none" strokeWidth={strokeWidth}
          className="stroke-[#f0f0f0] dark:stroke-[#2a2a2a]" />
        <motion.circle cx={size / 2} cy={size / 2} r={radius} fill="none" strokeWidth={strokeWidth}
          stroke={color} strokeLinecap="round" strokeDasharray={circumference}
          initial={{ strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: circumference * (1 - value) }}
                   transition={{ duration: 1.2, delay, ease: 'easeOut' }}
        />
      </svg>
      <div className="absolute flex flex-col items-center">
        <span className="text-3xl font-bold text-[#171717] dark:text-[#ededed]">
          {(value * 100).toFixed(1)}
        </span>
        <span className="text-xs text-[#666] dark:text-[#8f8f8f]">F1 Score</span>
      </div>
    </div>
  );
};

export default function BenchmarkPage() {
  return (
    <div className="min-h-screen bg-white dark:bg-[#0a0a0a]">
      {/* Header */}
      <header className="sticky top-0 z-50 border-b border-[#eaeaea] dark:border-[#333] bg-white/80 dark:bg-[#0a0a0a]/80 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-5xl items-center justify-between px-6">
          <Link to="/" className="flex items-center gap-2 text-[#171717] dark:text-[#ededed] hover:opacity-70 transition-opacity">
            <ArrowLeft className="h-4 w-4" />
            <ShieldCheck className="h-5 w-5" />
            <span className="font-semibold">pleno-anonymize</span>
          </Link>
          <span className="text-sm text-[#666] dark:text-[#8f8f8f]">Model Benchmark v0.1.0</span>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-10">
        {/* Overall Score */}
        <motion.div className="mb-16 rounded-2xl border border-[#eaeaea] dark:border-[#333] bg-[#fafafa] dark:bg-[#111] p-8"
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}          transition={{ duration: 0.5, delay: 0.1 }}>
          <div className="grid gap-8 md:grid-cols-[1fr_auto_1fr]">
            <div className="flex flex-col justify-center">
              <h2 className="mb-6 text-xl font-semibold text-[#171717] dark:text-[#ededed]">Overall Performance</h2>
              <div className="space-y-4">
                <div>
                  <div className="mb-1 flex justify-between text-sm">
                    <span className="text-[#666] dark:text-[#8f8f8f]">Precision</span>
                    <span className="font-mono font-medium text-[#171717] dark:text-[#ededed]">{(OVERALL.precision * 100).toFixed(1)}%</span>
                  </div>
                  <BarChart value={OVERALL.precision} color="#3b82f6" delay={0.2} />
                </div>
                <div>
                  <div className="mb-1 flex justify-between text-sm">
                    <span className="text-[#666] dark:text-[#8f8f8f]">Recall</span>
                    <span className="font-mono font-medium text-[#171717] dark:text-[#ededed]">{(OVERALL.recall * 100).toFixed(1)}%</span>
                  </div>
                  <BarChart value={OVERALL.recall} color="#8b5cf6" delay={0.3} />
                </div>
                <div>
                  <div className="mb-1 flex justify-between text-sm">
                    <span className="text-[#666] dark:text-[#8f8f8f]">F1 Score</span>
                    <span className="font-mono font-medium text-[#171717] dark:text-[#ededed]">{(OVERALL.f1 * 100).toFixed(1)}%</span>
                  </div>
                  <BarChart value={OVERALL.f1} color="#10b981" delay={0.4} />
                </div>
              </div>
            </div>

            <div className="hidden md:flex items-center">
              <div className="h-full w-px bg-[#eaeaea] dark:bg-[#333]" />
            </div>

            <div className="flex items-center justify-center">
              <ScoreRing value={OVERALL.f1} size={180} strokeWidth={12} color="#10b981" delay={0.3} />
            </div>
          </div>
        </motion.div>

        {/* Model Comparison */}
        <motion.div className="mb-16"
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}          transition={{ duration: 0.5 }}>
          <h2 className="mb-2 text-2xl font-bold text-[#171717] dark:text-[#ededed]">Model Comparison</h2>
          <p className="mb-8 text-sm text-[#666] dark:text-[#8f8f8f]">
            F1 Score comparison against spaCy built-in Japanese models on PII detection test set
          </p>

          {/* Legend */}
          <div className="mb-6 flex flex-wrap gap-4">
            {COMPARISON_MODELS.map((m) => (
              <div key={m.name} className="flex items-center gap-2 text-sm">
                <div className="h-3 w-3 rounded-sm" style={{ backgroundColor: m.color }} />
                <span className={m.highlight ? 'font-semibold text-[#171717] dark:text-[#ededed]' : 'text-[#666] dark:text-[#8f8f8f]'}>
                  {m.label}
                </span>
              </div>
            ))}
          </div>

          <div className="grid gap-4 grid-cols-2 md:grid-cols-3 lg:grid-cols-6">
            {Object.entries(COMPARISON_DATA).map(([entity, scores], i) => (
              <motion.div key={entity}
                className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-4"
                initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}                transition={{ duration: 0.4, delay: i * 0.06 }}>
                <div className="mb-4 text-center">
                  <div className="font-mono text-xs font-semibold text-[#171717] dark:text-[#ededed]">
                    {entity === '_overall' ? 'OVERALL' : entity}
                  </div>
                  <div className="text-[10px] text-[#999] dark:text-[#666]">{ENTITY_LABELS[entity]}</div>
                </div>
                <div className="flex items-end justify-center gap-1.5" style={{ height: 180 }}>
                  {COMPARISON_MODELS.map((model, mi) => {
                    const score = scores[model.name];
                    const heightPct = Math.max(score * 100, 2);
                    return (
                      <div key={model.name} className="flex flex-col items-center gap-1" style={{ width: 28 }}>
                        <span className={`font-mono leading-none ${model.highlight ? 'font-bold text-[#171717] dark:text-[#ededed]' : 'text-[#bbb] dark:text-[#555]'}`}
                          style={{ fontSize: 9 }}>
                          {score > 0 ? (score * 100).toFixed(0) : '—'}
                        </span>
                        <div className="w-full rounded-t bg-[#f5f5f5] dark:bg-[#222] overflow-hidden relative"
                          style={{ height: 160 }}>
                          <motion.div
                            className={`absolute bottom-0 w-full rounded-t ${model.highlight ? 'shadow-sm' : ''}`}
                            style={{ backgroundColor: model.color }}
                            initial={{ height: 0 }}
                            animate={{ height: `${heightPct}%` }}
                                                       transition={{ duration: 0.7, delay: i * 0.06 + mi * 0.08, ease: 'easeOut' }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              </motion.div>
            ))}
          </div>
        </motion.div>

        {/* Size & Latency Comparison */}
        <motion.div className="mb-16"
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}          transition={{ duration: 0.5 }}>
          <div className="grid gap-4 md:grid-cols-2">
            {/* Model Size */}
            <div className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-6">
              <h3 className="mb-1 text-lg font-semibold text-[#171717] dark:text-[#ededed]">Model Size</h3>
              <p className="mb-6 text-xs text-[#999] dark:text-[#666]">Lower is better</p>
              <div className="flex items-end justify-center gap-4" style={{ height: 200 }}>
                {COMPARISON_MODELS.map((model, mi) => {
                  const size = SIZE_DATA[model.name];
                  const maxSize = Math.max(...Object.values(SIZE_DATA));
                  const heightPct = (size / maxSize) * 100;
                  return (
                    <div key={model.name} className="flex flex-col items-center gap-1.5" style={{ width: 48 }}>
                      <span className={`font-mono text-xs ${model.highlight ? 'font-bold text-[#171717] dark:text-[#ededed]' : 'text-[#999] dark:text-[#666]'}`}>
                        {size < 10 ? size.toFixed(1) : Math.round(size)}
                        <span className="text-[9px]">MB</span>
                      </span>
                      <div className="w-full rounded-t bg-[#f5f5f5] dark:bg-[#222] overflow-hidden relative" style={{ height: 150 }}>
                        <motion.div
                          className="absolute bottom-0 w-full rounded-t"
                          style={{ backgroundColor: model.highlight ? '#10b981' : model.color }}
                          initial={{ height: 0 }}
                          animate={{ height: `${heightPct}%` }}
                                                   transition={{ duration: 0.7, delay: mi * 0.1, ease: 'easeOut' }}
                        />
                      </div>
                      <span className="text-[9px] text-center text-[#999] dark:text-[#666] leading-tight">
                        {model.shortLabel}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Inference Latency */}
            <div className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-6">
              <h3 className="mb-1 text-lg font-semibold text-[#171717] dark:text-[#ededed]">Inference Latency</h3>
              <p className="mb-6 text-xs text-[#999] dark:text-[#666]">Lower is better (ms/doc, CPU)</p>
              <div className="flex items-end justify-center gap-4" style={{ height: 200 }}>
                {COMPARISON_MODELS.map((model, mi) => {
                  const latency = LATENCY_DATA[model.name];
                  const maxLatency = Math.max(...Object.values(LATENCY_DATA));
                  const heightPct = (latency / maxLatency) * 100;
                  return (
                    <div key={model.name} className="flex flex-col items-center gap-1.5" style={{ width: 48 }}>
                      <span className={`font-mono text-xs ${model.highlight ? 'font-bold text-[#171717] dark:text-[#ededed]' : 'text-[#999] dark:text-[#666]'}`}>
                        {latency.toFixed(1)}
                        <span className="text-[9px]">ms</span>
                      </span>
                      <div className="w-full rounded-t bg-[#f5f5f5] dark:bg-[#222] overflow-hidden relative" style={{ height: 150 }}>
                        <motion.div
                          className="absolute bottom-0 w-full rounded-t"
                          style={{ backgroundColor: model.highlight ? '#10b981' : model.color }}
                          initial={{ height: 0 }}
                          animate={{ height: `${heightPct}%` }}
                                                   transition={{ duration: 0.7, delay: mi * 0.1, ease: 'easeOut' }}
                        />
                      </div>
                      <span className="text-[9px] text-center text-[#999] dark:text-[#666] leading-tight">
                        {model.shortLabel}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        </motion.div>

        {/* Per-Entity Results */}
        <motion.div className="mb-16"
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }}          transition={{ duration: 0.5 }}>
          <h2 className="mb-8 text-2xl font-bold text-[#171717] dark:text-[#ededed]">Entity Performance</h2>
          <div className="space-y-4">
            {BENCHMARK_DATA.map((item, i) => (
              <motion.div key={item.entity}
                className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-6"
                initial={{ opacity: 0, x: -20 }} animate={{ opacity: 1, x: 0 }}                transition={{ duration: 0.4, delay: i * 0.08 }}>
                <div className="mb-4 flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <div className="h-3 w-3 rounded-full" style={{ backgroundColor: item.color }} />
                    <div>
                      <span className="font-mono text-sm font-semibold text-[#171717] dark:text-[#ededed]">{item.entity}</span>
                      <span className="ml-2 text-sm text-[#666] dark:text-[#8f8f8f]">{item.label}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-2xl font-bold text-[#171717] dark:text-[#ededed]">
                      {(item.f1 * 100).toFixed(1)}
                    </span>
                    <span className="text-xs text-[#666] dark:text-[#8f8f8f]">F1</span>
                    <CheckCircle2 className="ml-1 h-5 w-5 text-emerald-500" />
                  </div>
                </div>
                <div className="grid gap-3 md:grid-cols-3">
                  <div>
                    <div className="mb-1 flex justify-between text-xs text-[#666] dark:text-[#8f8f8f]">
                      <span>Precision</span>
                      <span className="font-mono">{(item.precision * 100).toFixed(1)}%</span>
                    </div>
                    <BarChart value={item.precision} color={item.color} delay={i * 0.08} />
                  </div>
                  <div>
                    <div className="mb-1 flex justify-between text-xs text-[#666] dark:text-[#8f8f8f]">
                      <span>Recall</span>
                      <span className="font-mono">{(item.recall * 100).toFixed(1)}%</span>
                    </div>
                    <BarChart value={item.recall} color={item.color} delay={i * 0.08 + 0.05} />
                  </div>
                  <div>
                    <div className="mb-1 flex justify-between text-xs text-[#666] dark:text-[#8f8f8f]">
                      <span>Threshold</span>
                      <span className="font-mono">{(item.threshold * 100).toFixed(0)}%</span>
                    </div>
                    <div className="relative h-2 w-full rounded-full bg-[#f0f0f0] dark:bg-[#2a2a2a] overflow-hidden">
                      <div className="absolute h-full rounded-full bg-[#ddd] dark:bg-[#444]"
                        style={{ width: `${item.threshold * 100}%` }} />
                      <motion.div className="absolute h-full rounded-full" style={{ backgroundColor: item.color }}
                        initial={{ width: 0 }}
                        animate={{ width: `${item.f1 * 100}%` }}
                                               transition={{ duration: 0.8, delay: i * 0.08, ease: 'easeOut' }} />
                    </div>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        </motion.div>

      </main>

      <Footer />
    </div>
  );
}
