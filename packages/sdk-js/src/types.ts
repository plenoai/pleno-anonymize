export type Language = "ja" | "en";

export interface AnalyzeRequest {
  text: string;
  language?: Language;
  entities?: string[];
}

export interface Finding {
  entity_type: string;
  start: number;
  end: number;
  score: number;
  text: string;
}

export interface RedactRequest {
  text?: string;
  image?: string;
  language?: Language;
  entities?: string[];
  operators?: Record<string, Record<string, unknown>>;
  fillColor?: [number, number, number];
}

export interface RedactResult {
  text?: string;
  items?: unknown[];
  image?: string;
}

export interface FileScanResult {
  path: string;
  bytes: number;
  language: Language;
  findings: Finding[];
  truncated: boolean;
  skipped?: "binary" | "too-large" | "read-error";
  error?: string;
}

export interface ScanSummary {
  files: FileScanResult[];
  totalFindings: number;
  byEntity: Record<string, number>;
  scannedFiles: number;
  skippedFiles: number;
}
