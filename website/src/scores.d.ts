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
