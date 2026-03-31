import { useState } from 'react';
import { motion } from 'framer-motion';
import { ShieldCheck, CheckCircle2, ArrowLeft, Zap, HardDrive, BookOpen } from 'lucide-react';
import Footer from '../components/Footer';
import { Link } from 'react-router-dom';
import scoresJa from '@scores';
import scoresEn from '@scores-en';
import scoresEnCnn from '@scores-en-cnn';
import externalScoresJa from '@external-scores-ja';
import externalScoresEn from '@external-scores-en';

type Lang = 'ja' | 'en';

const LANG_LABELS: Record<Lang, { name: string; model: string; version: string }> = {
  ja: { name: '日本語', model: 'pleno_ner_ja', version: 'v0.1.0' },
  en: { name: 'English', model: 'pleno_ner_en', version: 'v0.2.0' },
};

const SCORES: Record<Lang, typeof scoresJa> = { ja: scoresJa, en: scoresEn };

const ENTITY_CONFIG: Record<string, { labelJa: string; labelEn: string; threshold: number; recallMin: number; color: string; order: number }> = {
  PERSON:        { labelJa: '人名',     labelEn: 'Person',       threshold: 0.9,  recallMin: 0.85, color: '#3b82f6', order: 0 },
  ADDRESS:       { labelJa: '住所',     labelEn: 'Address',      threshold: 0.85, recallMin: 0.80, color: '#8b5cf6', order: 1 },
  ORGANIZATION:  { labelJa: '組織名',   labelEn: 'Organization', threshold: 0.85, recallMin: 0.80, color: '#06b6d4', order: 2 },
  DATE_OF_BIRTH: { labelJa: '生年月日', labelEn: 'Date of Birth',threshold: 0.8,  recallMin: 0.75, color: '#f59e0b', order: 3 },
  BANK_ACCOUNT:  { labelJa: '銀行口座', labelEn: 'Bank Account', threshold: 0.8,  recallMin: 0.75, color: '#10b981', order: 4 },
};

const ENTITIES = Object.keys(ENTITY_CONFIG);

function getBenchmarkData(lang: Lang) {
  const scores = SCORES[lang];
  return Object.entries(scores.ents_per_type)
    .filter(([entity]) => entity in ENTITY_CONFIG)
    .map(([entity, { p, r, f }]) => ({
      entity,
      label: lang === 'ja' ? ENTITY_CONFIG[entity].labelJa : ENTITY_CONFIG[entity].labelEn,
      precision: p,
      recall: r,
      f1: f,
      threshold: ENTITY_CONFIG[entity].threshold,
      recallMin: ENTITY_CONFIG[entity].recallMin,
      color: ENTITY_CONFIG[entity].color,
    }))
    .sort((a, b) => ENTITY_CONFIG[a.entity].order - ENTITY_CONFIG[b.entity].order);
}

function getOverall(lang: Lang) {
  const s = SCORES[lang];
  return { precision: s.ents_p, recall: s.ents_r, f1: s.ents_f, threshold: 0.88 };
}

// JA comparison models (external scores hardcoded — no JA external benchmark JSON exists yet)
const COMPARISON_MODELS_JA = [
  { name: 'pleno_ner_ja', label: 'pleno_ner_ja (ours)', shortLabel: 'ours', color: '#10b981', highlight: true },
  { name: 'bert_ner_ja', label: 'bert-ner-japanese (HF)', shortLabel: 'HF', color: '#f59e0b', highlight: false },
  { name: 'ja_core_news_lg', label: 'ja_core_news_lg', shortLabel: 'lg', color: '#6b7280', highlight: false },
  { name: 'ja_core_news_md', label: 'ja_core_news_md', shortLabel: 'md', color: '#9ca3af', highlight: false },
  { name: 'ja_core_news_sm', label: 'ja_core_news_sm', shortLabel: 'sm', color: '#d1d5db', highlight: false },
];

// EN comparison models
const COMPARISON_MODELS_EN = [
  { name: 'pleno_ner_en', label: 'pleno_ner_en (ours)', shortLabel: 'ours', color: '#10b981', highlight: true },
  { name: 'pleno_ner_en_cnn', label: 'pleno_ner_en_cnn', shortLabel: 'cnn', color: '#3b82f6', highlight: false },
  { name: 'en_core_web_md', label: 'en_core_web_md', shortLabel: 'md', color: '#9ca3af', highlight: false },
  { name: 'en_core_web_sm', label: 'en_core_web_sm', shortLabel: 'sm', color: '#d1d5db', highlight: false },
];

const COMPARISON_MODELS: Record<Lang, typeof COMPARISON_MODELS_JA> = {
  ja: COMPARISON_MODELS_JA,
  en: COMPARISON_MODELS_EN,
};

function buildExternalScores(
  externalJson: ExternalJson,
  cnnScores?: typeof scoresEnCnn,
): Record<string, Record<string, number>> {
  const result: Record<string, Record<string, number>> = {};
  for (const entity of ENTITIES) {
    const fromJson = Object.fromEntries(
      Object.entries(externalJson).map(([model, data]) => [model, data.per_entity[entity]?.f ?? 0])
    );
    const fromCnn: Record<string, number> = cnnScores ? { pleno_ner_en_cnn: cnnScores.ents_per_type[entity]?.f ?? 0 } : {};
    result[entity] = { ...fromCnn, ...fromJson };
  }
  return result;
}

const EXTERNAL_SCORES: Record<Lang, Record<string, Record<string, number>>> = {
  ja: buildExternalScores(externalScoresJa),
  en: buildExternalScores(externalScoresEn, scoresEnCnn),
};

function getComparisonData(lang: Lang) {
  const ext = EXTERNAL_SCORES[lang];
  const scores = SCORES[lang];
  const ownModelName = lang === 'ja' ? 'pleno_ner_ja' : 'pleno_ner_en';
  return Object.fromEntries(
    Object.entries(ext).map(([entity, extScores]) => [
      entity,
      { [ownModelName]: scores.ents_per_type[entity]?.f ?? 0, ...extScores },
    ])
  );
}

type ExternalJson = Record<string, { per_entity: Record<string, { f: number }>; latency_ms_per_doc: number; model_size_mb?: number }>;

function buildMetric(
  externalJson: ExternalJson,
  ownScores: Record<string, typeof scoresJa>,
  extract: (data: ExternalJson[string]) => number,
  extractOwn: (s: typeof scoresJa) => number,
): Record<string, number> {
  const result: Record<string, number> = {};
  for (const [model, scores] of Object.entries(ownScores)) {
    result[model] = extractOwn(scores);
  }
  for (const [model, data] of Object.entries(externalJson)) {
    result[model] = extract(data);
  }
  return result;
}

const SIZE_DATA: Record<Lang, Record<string, number>> = {
  ja: buildMetric(
    externalScoresJa,
    { pleno_ner_ja: scoresJa },
    (d) => d.model_size_mb ?? 0,
    (s) => s.model_size_mb ?? 0,
  ),
  en: buildMetric(
    externalScoresEn,
    { pleno_ner_en: scoresEn, pleno_ner_en_cnn: scoresEnCnn },
    (d) => d.model_size_mb ?? 0,
    (s) => s.model_size_mb ?? 0,
  ),
};
const LATENCY_DATA: Record<Lang, Record<string, number>> = {
  ja: buildMetric(
    externalScoresJa,
    { pleno_ner_ja: scoresJa },
    (d) => d.latency_ms_per_doc,
    (s) => s.latency_ms_per_doc ?? 0,
  ),
  en: buildMetric(
    externalScoresEn,
    { pleno_ner_en: scoresEn, pleno_ner_en_cnn: scoresEnCnn },
    (d) => d.latency_ms_per_doc,
    (s) => s.latency_ms_per_doc ?? 0,
  ),
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

const ScoreRing = ({ value, size = 160, strokeWidth = 10, color = '#3b82f6', delay = 0, label = 'F1 Score' }: {
  value: number; size?: number; strokeWidth?: number; color?: string; delay?: number; label?: string;
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
        <span className="text-xs text-[#666] dark:text-[#8f8f8f]">{label}</span>
      </div>
    </div>
  );
};


function MethodologySection() {
  return (
    <motion.div className="mb-16 rounded-2xl border border-[#eaeaea] dark:border-[#333] bg-[#fafafa] dark:bg-[#111] p-8"
      initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
      <div className="mb-6 flex items-center gap-2">
        <BookOpen className="h-5 w-5 text-[#666] dark:text-[#8f8f8f]" />
        <h2 className="text-xl font-semibold text-[#171717] dark:text-[#ededed]">Methodology</h2>
      </div>
      <div className="grid gap-6 md:grid-cols-2">
        <div>
          <h3 className="mb-3 text-sm font-semibold text-[#171717] dark:text-[#ededed]">Training</h3>
          <dl className="space-y-2 text-sm text-[#666] dark:text-[#8f8f8f]">
            <div className="flex justify-between">
              <dt>Architecture</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">spaCy TransitionBasedParser v2</dd>
            </div>
            <div className="flex justify-between">
              <dt>JA Backbone</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">cl-tohoku/bert-base-japanese-v3</dd>
            </div>
            <div className="flex justify-between">
              <dt>EN Backbone</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">roberta-base</dd>
            </div>
            <div className="flex justify-between">
              <dt>Max Epochs</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">30</dd>
            </div>
            <div className="flex justify-between">
              <dt>Optimizer</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">Adam (lr=5e-5, warmup)</dd>
            </div>
          </dl>
        </div>
        <div>
          <h3 className="mb-3 text-sm font-semibold text-[#171717] dark:text-[#ededed]">Evaluation</h3>
          <dl className="space-y-2 text-sm text-[#666] dark:text-[#8f8f8f]">
            <div className="flex justify-between">
              <dt>Metric</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">Exact span match (P/R/F1)</dd>
            </div>
            <div className="flex justify-between">
              <dt>Overall F1 Threshold</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">&ge; 88%</dd>
            </div>
            <div className="flex justify-between">
              <dt>Entities</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">5 PII types</dd>
            </div>
            <div className="flex justify-between">
              <dt>External Baselines</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">spaCy + HuggingFace</dd>
            </div>
            <div className="flex justify-between">
              <dt>Latency</dt>
              <dd className="font-mono text-[#171717] dark:text-[#ededed]">CPU, ms/doc avg</dd>
            </div>
          </dl>
        </div>
      </div>
      <div className="mt-6 rounded-lg bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-800/50 p-4">
        <p className="text-xs text-amber-800 dark:text-amber-300">
          <strong>Safety Note:</strong> Per-entity recall minimums are enforced to prevent PII leakage.
          A model with high F1 but low recall may miss sensitive entities in production.
          Our acceptance criteria require recall &ge; 75-85% per entity type.
        </p>
      </div>
    </motion.div>
  );
}

export default function BenchmarkPage() {
  const [lang, setLang] = useState<Lang>('ja');
  const benchmarkData = getBenchmarkData(lang);
  const overall = getOverall(lang);
  const otherLang: Lang = lang === 'ja' ? 'en' : 'ja';
  const otherOverall = getOverall(otherLang);

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
          <span className="text-sm text-[#666] dark:text-[#8f8f8f]">Model Benchmark</span>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-10">
        {/* Language Tabs */}
        <div className="mb-8 flex items-center gap-2">
          {(['ja', 'en'] as const).map((l) => (
            <button key={l} onClick={() => setLang(l)}
              className={`rounded-lg px-4 py-2 text-sm font-medium transition-all ${
                lang === l
                  ? 'bg-[#171717] text-white dark:bg-white dark:text-[#171717]'
                  : 'bg-[#f5f5f5] text-[#666] dark:bg-[#222] dark:text-[#8f8f8f] hover:bg-[#eaeaea] dark:hover:bg-[#333]'
              }`}>
              {LANG_LABELS[l].name}
              <span className="ml-2 font-mono text-xs opacity-60">{LANG_LABELS[l].version}</span>
            </button>
          ))}
        </div>

        {/* Overall Score */}
        <motion.div className="mb-16 rounded-2xl border border-[#eaeaea] dark:border-[#333] bg-[#fafafa] dark:bg-[#111] p-8"
          key={lang} initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, delay: 0.1 }}>
          <div className="grid gap-8 md:grid-cols-[1fr_auto_1fr]">
            <div className="flex flex-col justify-center">
              <h2 className="mb-1 text-xl font-semibold text-[#171717] dark:text-[#ededed]">
                Overall Performance
                <span className="ml-2 text-sm font-normal text-[#999] dark:text-[#666]">{LANG_LABELS[lang].model}</span>
              </h2>
              <p className="mb-6 text-xs text-[#999] dark:text-[#666]">
                {LANG_LABELS[otherLang].name}: F1 {(otherOverall.f1 * 100).toFixed(1)}%
              </p>
              <div className="space-y-4">
                {[
                  { label: 'Precision', value: overall.precision, color: '#3b82f6', delay: 0.2 },
                  { label: 'Recall', value: overall.recall, color: '#8b5cf6', delay: 0.3 },
                  { label: 'F1 Score', value: overall.f1, color: '#10b981', delay: 0.4 },
                ].map(({ label, value, color, delay }) => (
                  <div key={label}>
                    <div className="mb-1 flex justify-between text-sm">
                      <span className="text-[#666] dark:text-[#8f8f8f]">{label}</span>
                      <span className="font-mono font-medium text-[#171717] dark:text-[#ededed]">{(value * 100).toFixed(1)}%</span>
                    </div>
                    <BarChart value={value} color={color} delay={delay} />
                  </div>
                ))}
              </div>
            </div>
            <div className="hidden md:flex items-center">
              <div className="h-full w-px bg-[#eaeaea] dark:bg-[#333]" />
            </div>
            <div className="flex items-center justify-center">
              <ScoreRing value={overall.f1} size={180} strokeWidth={12} color="#10b981" delay={0.3} />
            </div>
          </div>
        </motion.div>

        {/* Model Comparison */}
        <motion.div className="mb-16" key={`comparison-${lang}`}
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <h2 className="mb-2 text-2xl font-bold text-[#171717] dark:text-[#ededed]">Model Comparison</h2>
          <p className="mb-8 text-sm text-[#666] dark:text-[#8f8f8f]">
            {lang === 'ja'
              ? 'F1 Score comparison against spaCy built-in Japanese models on PII detection test set'
              : 'F1 Score comparison against spaCy English models and CNN variant on PII detection test set'}
          </p>
          <div className="mb-6 flex flex-wrap gap-4">
            {COMPARISON_MODELS[lang].map((m) => (
              <div key={m.name} className="flex items-center gap-2 text-sm">
                <div className="h-3 w-3 rounded-sm" style={{ backgroundColor: m.color }} />
                <span className={m.highlight ? 'font-semibold text-[#171717] dark:text-[#ededed]' : 'text-[#666] dark:text-[#8f8f8f]'}>
                  {m.label}
                </span>
              </div>
            ))}
          </div>
          <div className="grid gap-4 grid-cols-2 md:grid-cols-3 lg:grid-cols-5">
            {Object.entries(getComparisonData(lang)).map(([entity, scores], i) => (
              <motion.div key={entity}
                className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-4"
                initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.4, delay: i * 0.06 }}>
                <div className="mb-4 text-center">
                  <div className="font-mono text-xs font-semibold text-[#171717] dark:text-[#ededed]">{entity}</div>
                  <div className="text-[10px] text-[#999] dark:text-[#666]">
                    {lang === 'ja' ? ENTITY_CONFIG[entity]?.labelJa : ENTITY_CONFIG[entity]?.labelEn}
                  </div>
                </div>
                <div className="flex items-end justify-center gap-1.5" style={{ height: 180 }}>
                  {COMPARISON_MODELS[lang].map((model, mi) => {
                    const score = scores[model.name];
                    const heightPct = Math.max(score * 100, 2);
                    return (
                      <div key={model.name} className="flex flex-col items-center gap-1" style={{ width: 28 }}>
                        <span className={`font-mono leading-none ${model.highlight ? 'font-bold text-[#171717] dark:text-[#ededed]' : 'text-[#bbb] dark:text-[#555]'}`}
                          style={{ fontSize: 9 }}>
                          {score > 0 ? (score * 100).toFixed(0) : '—'}
                        </span>
                        <div className="w-full rounded-t bg-[#f5f5f5] dark:bg-[#222] overflow-hidden relative" style={{ height: 160 }}>
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

        {/* Size & Latency */}
        <motion.div className="mb-16" key={`size-latency-${lang}`}
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <div className="grid gap-4 md:grid-cols-2">
            {[
              { title: 'Model Size', unit: 'MB', data: SIZE_DATA[lang], subtitle: 'Lower is better', icon: HardDrive },
              { title: 'Inference Latency', unit: 'ms', data: LATENCY_DATA[lang], subtitle: 'Lower is better (ms/doc, CPU)', icon: Zap },
            ].map(({ title, unit, data, subtitle, icon: Icon }) => (
              <div key={title} className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-6">
                <div className="mb-1 flex items-center gap-2">
                  <Icon className="h-4 w-4 text-[#666] dark:text-[#8f8f8f]" />
                  <h3 className="text-lg font-semibold text-[#171717] dark:text-[#ededed]">{title}</h3>
                </div>
                <p className="mb-6 text-xs text-[#999] dark:text-[#666]">{subtitle}</p>
                <div className="flex items-end justify-center gap-4" style={{ height: 200 }}>
                  {COMPARISON_MODELS[lang].map((model, mi) => {
                    const val = data[model.name];
                    const maxVal = Math.max(...Object.values(data));
                    const heightPct = (val / maxVal) * 100;
                    return (
                      <div key={model.name} className="flex flex-col items-center gap-1.5" style={{ width: 48 }}>
                        <span className={`font-mono text-xs ${model.highlight ? 'font-bold text-[#171717] dark:text-[#ededed]' : 'text-[#999] dark:text-[#666]'}`}>
                          {val < 10 ? val.toFixed(1) : Math.round(val)}
                          <span className="text-[9px]">{unit}</span>
                        </span>
                        <div className="w-full rounded-t bg-[#f5f5f5] dark:bg-[#222] overflow-hidden relative" style={{ height: 150 }}>
                          <motion.div className="absolute bottom-0 w-full rounded-t"
                            style={{ backgroundColor: model.highlight ? '#10b981' : model.color }}
                            initial={{ height: 0 }}
                            animate={{ height: `${heightPct}%` }}
                            transition={{ duration: 0.7, delay: mi * 0.1, ease: 'easeOut' }}
                          />
                        </div>
                        <span className="text-[9px] text-center text-[#999] dark:text-[#666] leading-tight">{model.shortLabel}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </motion.div>

        {/* Per-Entity Results */}
        <motion.div className="mb-16" key={`entities-${lang}`}
          initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5 }}>
          <h2 className="mb-8 text-2xl font-bold text-[#171717] dark:text-[#ededed]">Entity Performance</h2>
          <div className="space-y-4">
            {benchmarkData.map((item, i) => {
              const recallOk = item.recall >= item.recallMin;
              const f1Ok = item.f1 >= item.threshold;
              const passed = recallOk && f1Ok;
              return (
                <motion.div key={item.entity}
                  className="rounded-xl border border-[#eaeaea] dark:border-[#333] bg-white dark:bg-[#171717] p-6"
                  initial={{ opacity: 0, x: -20 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.4, delay: i * 0.08 }}>
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
                      {passed
                        ? <CheckCircle2 className="ml-1 h-5 w-5 text-emerald-500" />
                        : <span className="ml-1 rounded bg-red-100 dark:bg-red-900/30 px-1.5 py-0.5 text-[10px] font-medium text-red-600 dark:text-red-400">FAIL</span>
                      }
                    </div>
                  </div>
                  <div className="grid gap-3 md:grid-cols-4">
                    {[
                      { label: 'Precision', value: item.precision },
                      { label: 'Recall', value: item.recall },
                    ].map(({ label, value }, j) => (
                      <div key={label}>
                        <div className="mb-1 flex justify-between text-xs text-[#666] dark:text-[#8f8f8f]">
                          <span>{label}</span>
                          <span className="font-mono">{(value * 100).toFixed(1)}%</span>
                        </div>
                        <BarChart value={value} color={item.color} delay={i * 0.08 + j * 0.05} />
                      </div>
                    ))}
                    <div>
                      <div className="mb-1 flex justify-between text-xs text-[#666] dark:text-[#8f8f8f]">
                        <span>F1 Threshold</span>
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
                    <div>
                      <div className="mb-1 flex justify-between text-xs text-[#666] dark:text-[#8f8f8f]">
                        <span>Recall Min</span>
                        <span className={`font-mono ${!recallOk ? 'text-red-500' : ''}`}>{(item.recallMin * 100).toFixed(0)}%</span>
                      </div>
                      <div className="relative h-2 w-full rounded-full bg-[#f0f0f0] dark:bg-[#2a2a2a] overflow-hidden">
                        <div className="absolute h-full rounded-full bg-[#ddd] dark:bg-[#444]"
                          style={{ width: `${item.recallMin * 100}%` }} />
                        <motion.div className="absolute h-full rounded-full"
                          style={{ backgroundColor: recallOk ? item.color : '#ef4444' }}
                          initial={{ width: 0 }}
                          animate={{ width: `${item.recall * 100}%` }}
                          transition={{ duration: 0.8, delay: i * 0.08, ease: 'easeOut' }} />
                      </div>
                    </div>
                  </div>
                </motion.div>
              );
            })}
          </div>
        </motion.div>

        {/* Methodology */}
        <MethodologySection />

      </main>

      <Footer />
    </div>
  );
}
