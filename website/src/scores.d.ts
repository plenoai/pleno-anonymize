interface EntityScore {
  p: number;
  r: number;
  f: number;
}
interface Scores {
  ents_p: number;
  ents_r: number;
  ents_f: number;
  ents_per_type: Record<string, EntityScore>;
}

declare module '@scores' {
  const scores: Scores;
  export default scores;
}

declare module '@scores-en' {
  const scores: Scores;
  export default scores;
}

declare module '@scores-en-cnn' {
  const scores: Scores;
  export default scores;
}

interface ExternalModelResult {
  per_entity: Record<string, EntityScore>;
  latency_ms_per_doc: number;
}

declare module '@external-scores-ja' {
  const scores: Record<string, ExternalModelResult>;
  export default scores;
}

declare module '@external-scores-en' {
  const scores: Record<string, ExternalModelResult>;
  export default scores;
}
