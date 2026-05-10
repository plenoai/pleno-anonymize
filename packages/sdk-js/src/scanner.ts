import { readdir, readFile, stat } from "node:fs/promises";
import { join, relative, resolve } from "node:path";
import type { PlenoAnonymize } from "./client.js";
import type {
  FileScanResult,
  Finding,
  Language,
  ScanSummary,
} from "./types.js";

export interface ScanOptions {
  language?: Language;
  entities?: string[];
  maxBytes?: number;
  concurrency?: number;
  ignore?: string[];
  includeExtensions?: string[];
  followSymlinks?: boolean;
  onFile?: (result: FileScanResult) => void;
}

const DEFAULT_IGNORE = new Set([
  ".git",
  "node_modules",
  ".venv",
  "venv",
  "__pycache__",
  ".mypy_cache",
  ".pytest_cache",
  ".ruff_cache",
  "dist",
  "build",
  "target",
  ".next",
  ".turbo",
  ".cache",
]);

const SCAN_EXTENSIONS = new Set([
  ".txt", ".md", ".markdown", ".rst",
  ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".env", ".cfg", ".conf",
  ".csv", ".tsv",
  ".log",
  ".html", ".htm", ".xml", ".svg",
  ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
  ".py", ".rb", ".go", ".rs", ".java", ".kt", ".swift", ".php", ".cs", ".c", ".h", ".cpp", ".hpp",
  ".sh", ".bash", ".zsh", ".fish",
  ".sql",
]);

const DEFAULT_MAX_BYTES = 256 * 1024;

export async function scanPaths(
  client: PlenoAnonymize,
  paths: string[],
  options: ScanOptions = {},
): Promise<ScanSummary> {
  const ignore = new Set([...DEFAULT_IGNORE, ...(options.ignore ?? [])]);
  const allow = options.includeExtensions
    ? new Set(options.includeExtensions.map((e) => (e.startsWith(".") ? e : `.${e}`)))
    : SCAN_EXTENSIONS;
  const concurrency = Math.max(1, options.concurrency ?? 4);
  const maxBytes = options.maxBytes ?? DEFAULT_MAX_BYTES;

  const files: string[] = [];
  for (const p of paths) {
    await collectFiles(resolve(p), files, ignore, allow, options.followSymlinks ?? false);
  }

  const queue = [...files];
  const results: FileScanResult[] = [];
  const workers = Array.from({ length: Math.min(concurrency, queue.length) || 1 }, async () => {
    while (queue.length > 0) {
      const next = queue.shift();
      if (!next) break;
      const result = await scanSingleFile(client, next, {
        language: options.language ?? client.defaultLanguage,
        entities: options.entities,
        maxBytes,
      });
      results.push(result);
      options.onFile?.(result);
    }
  });
  await Promise.all(workers);

  results.sort((a, b) => a.path.localeCompare(b.path));

  const byEntity: Record<string, number> = {};
  let totalFindings = 0;
  let scannedFiles = 0;
  let skippedFiles = 0;
  for (const r of results) {
    if (r.skipped) {
      skippedFiles += 1;
      continue;
    }
    scannedFiles += 1;
    for (const f of r.findings) {
      totalFindings += 1;
      byEntity[f.entity_type] = (byEntity[f.entity_type] ?? 0) + 1;
    }
  }

  return { files: results, totalFindings, byEntity, scannedFiles, skippedFiles };
}

async function collectFiles(
  abs: string,
  out: string[],
  ignore: Set<string>,
  allow: Set<string>,
  followSymlinks: boolean,
): Promise<void> {
  let info;
  try {
    info = await stat(abs);
  } catch {
    return;
  }

  if (info.isFile()) {
    if (matchExtension(abs, allow)) out.push(abs);
    return;
  }
  if (!info.isDirectory()) return;

  let entries;
  try {
    entries = await readdir(abs, { withFileTypes: true });
  } catch {
    return;
  }
  for (const entry of entries) {
    if (ignore.has(entry.name)) continue;
    const child = join(abs, entry.name);
    if (entry.isSymbolicLink() && !followSymlinks) continue;
    if (entry.isDirectory()) {
      await collectFiles(child, out, ignore, allow, followSymlinks);
    } else if (entry.isFile() && matchExtension(child, allow)) {
      out.push(child);
    }
  }
}

function matchExtension(path: string, allow: Set<string>): boolean {
  const dot = path.lastIndexOf(".");
  if (dot < 0) return false;
  return allow.has(path.slice(dot).toLowerCase());
}

interface SingleFileOptions {
  language: Language;
  entities?: string[];
  maxBytes: number;
}

async function scanSingleFile(
  client: PlenoAnonymize,
  abs: string,
  options: SingleFileOptions,
): Promise<FileScanResult> {
  const display = relative(process.cwd(), abs) || abs;
  let info;
  try {
    info = await stat(abs);
  } catch (err) {
    return errorResult(display, options.language, "read-error", (err as Error).message);
  }

  let buffer: Buffer;
  try {
    buffer = await readFile(abs);
  } catch (err) {
    return errorResult(display, options.language, "read-error", (err as Error).message);
  }

  if (looksBinary(buffer)) {
    return {
      path: display,
      bytes: info.size,
      language: options.language,
      findings: [],
      truncated: false,
      skipped: "binary",
    };
  }

  const truncated = buffer.length > options.maxBytes;
  const slice = truncated ? buffer.subarray(0, options.maxBytes) : buffer;
  const text = slice.toString("utf8");
  if (!text.trim()) {
    return {
      path: display,
      bytes: info.size,
      language: options.language,
      findings: [],
      truncated,
    };
  }

  let findings: Finding[];
  try {
    findings = await client.analyze(text, {
      language: options.language,
      entities: options.entities,
    });
  } catch (err) {
    return errorResult(display, options.language, "read-error", (err as Error).message);
  }

  return {
    path: display,
    bytes: info.size,
    language: options.language,
    findings,
    truncated,
  };
}

function errorResult(
  path: string,
  language: Language,
  reason: NonNullable<FileScanResult["skipped"]>,
  message: string,
): FileScanResult {
  return {
    path,
    bytes: 0,
    language,
    findings: [],
    truncated: false,
    skipped: reason,
    error: message,
  };
}

function looksBinary(buf: Buffer): boolean {
  const sample = buf.subarray(0, Math.min(buf.length, 8000));
  for (let i = 0; i < sample.length; i += 1) {
    if (sample[i] === 0) return true;
  }
  return false;
}

export async function scanFile(
  client: PlenoAnonymize,
  path: string,
  options: Omit<ScanOptions, "onFile" | "concurrency" | "ignore" | "includeExtensions" | "followSymlinks"> = {},
): Promise<FileScanResult> {
  return scanSingleFile(client, resolve(path), {
    language: options.language ?? client.defaultLanguage,
    entities: options.entities,
    maxBytes: options.maxBytes ?? DEFAULT_MAX_BYTES,
  });
}
