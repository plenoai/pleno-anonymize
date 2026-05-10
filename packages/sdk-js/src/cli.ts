#!/usr/bin/env node
import { parseArgs } from "node:util";
import { readFile } from "node:fs/promises";
import { PlenoAnonymize, PlenoAnonymizeError } from "./client.js";
import { scanPaths } from "./scanner.js";
import type { Finding, Language, ScanSummary } from "./types.js";

const HELP = `pleno-anonymize — PII detection / redaction CLI

Usage:
  pleno-anonymize scan <path...> [options]
  pleno-anonymize analyze [text] [options]
  pleno-anonymize redact  [text] [options]
  pleno-anonymize health  [options]

Options:
  --endpoint <url>        API base URL (default: $PLENO_ANONYMIZE_ENDPOINT or https://pleno-anonymize.fly.dev)
  --api-key <key>         Bearer token (default: $PLENO_ANONYMIZE_API_KEY)
  --language <ja|en>      Detection language (default: ja)
  --entities <list>       Comma-separated entity types to restrict to
  --json                  Emit JSON output
  --concurrency <n>       Parallel requests for scan (default: 4)
  --max-bytes <n>         Per-file byte cap for scan (default: 262144)
  --ignore <list>         Extra directory names to ignore (comma-separated)
  --ext <list>            Restrict scan to extensions (comma-separated, e.g. ".md,.py")
  --fail-on-findings      Exit non-zero (2) when scan finds PII
  --no-color              Disable ANSI colors
  -f, --file <path>       Read input text from file (analyze / redact)
  -h, --help              Show this help

Examples:
  pleno-anonymize scan . --fail-on-findings
  echo "山田太郎 090-1234-5678" | pleno-anonymize analyze --language ja
  pleno-anonymize redact "Contact john@example.com" --json
`;

const COLOR = !process.env.NO_COLOR && process.stdout.isTTY;
const c = {
  red: (s: string) => (COLOR ? `\x1b[31m${s}\x1b[0m` : s),
  yellow: (s: string) => (COLOR ? `\x1b[33m${s}\x1b[0m` : s),
  green: (s: string) => (COLOR ? `\x1b[32m${s}\x1b[0m` : s),
  cyan: (s: string) => (COLOR ? `\x1b[36m${s}\x1b[0m` : s),
  dim: (s: string) => (COLOR ? `\x1b[2m${s}\x1b[0m` : s),
  bold: (s: string) => (COLOR ? `\x1b[1m${s}\x1b[0m` : s),
};

interface CommonFlags {
  endpoint?: string;
  apiKey?: string;
  language: Language;
  entities?: string[];
  json: boolean;
}

async function main(argv: string[]): Promise<number> {
  if (argv.length === 0 || argv[0] === "-h" || argv[0] === "--help") {
    process.stdout.write(HELP);
    return 0;
  }

  const command = argv[0];
  const rest = argv.slice(1);

  try {
    switch (command) {
      case "scan":
        return await runScan(rest);
      case "analyze":
        return await runAnalyze(rest);
      case "redact":
        return await runRedact(rest);
      case "health":
        return await runHealth(rest);
      case "--version":
      case "-v":
      case "version": {
        const pkg = await loadPackageJson();
        process.stdout.write(`${pkg.name} ${pkg.version}\n`);
        return 0;
      }
      default:
        process.stderr.write(c.red(`unknown command: ${command}\n\n`));
        process.stdout.write(HELP);
        return 1;
    }
  } catch (err) {
    if (err instanceof PlenoAnonymizeError) {
      process.stderr.write(c.red(`error: ${err.message}\n`));
      if (err.body) {
        process.stderr.write(c.dim(`${JSON.stringify(err.body)}\n`));
      }
      return 1;
    }
    process.stderr.write(c.red(`error: ${(err as Error).message}\n`));
    return 1;
  }
}

type ParseArgsOption = { type: "string" | "boolean"; short?: string; multiple?: boolean };

function parseCommon(
  args: string[],
  extra: Record<string, ParseArgsOption> = {},
): { flags: CommonFlags; values: Record<string, unknown>; positionals: string[] } {
  const options: Record<string, ParseArgsOption> = {
    endpoint: { type: "string" },
    "api-key": { type: "string" },
    language: { type: "string" },
    entities: { type: "string" },
    json: { type: "boolean" },
    help: { type: "boolean", short: "h" },
    "no-color": { type: "boolean" },
    ...extra,
  };
  const parsed = parseArgs({ args, allowPositionals: true, options });
  const values = parsed.values as Record<string, unknown>;
  const positionals = parsed.positionals;

  if (values["no-color"]) {
    for (const k of Object.keys(c)) {
      (c as Record<string, (s: string) => string>)[k] = (s: string) => s;
    }
  }
  const language = (values.language as string | undefined) ?? "ja";
  if (language !== "ja" && language !== "en") {
    throw new Error(`unsupported language: ${language} (must be ja or en)`);
  }
  const entitiesRaw = values.entities as string | undefined;
  const flags: CommonFlags = {
    endpoint: values.endpoint as string | undefined,
    apiKey: values["api-key"] as string | undefined,
    language,
    entities: entitiesRaw
      ? entitiesRaw.split(",").map((s) => s.trim()).filter(Boolean)
      : undefined,
    json: Boolean(values.json),
  };
  return { flags, values, positionals };
}

function makeClient(flags: CommonFlags): PlenoAnonymize {
  return new PlenoAnonymize({
    endpoint: flags.endpoint,
    apiKey: flags.apiKey,
    defaultLanguage: flags.language,
  });
}

async function runScan(args: string[]): Promise<number> {
  const { flags, values, positionals } = parseCommon(args, {
    concurrency: { type: "string" },
    "max-bytes": { type: "string" },
    ignore: { type: "string" },
    ext: { type: "string" },
    "fail-on-findings": { type: "boolean" },
  });
  if (values.help) {
    process.stdout.write(HELP);
    return 0;
  }
  const targets = positionals.length > 0 ? positionals : ["."];
  const client = makeClient(flags);
  const concurrency = values.concurrency ? Number(values.concurrency as string) : undefined;
  const maxBytes = values["max-bytes"] ? Number(values["max-bytes"] as string) : undefined;
  const ignore = values.ignore
    ? (values.ignore as string).split(",").map((s) => s.trim()).filter(Boolean)
    : undefined;
  const includeExtensions = values.ext
    ? (values.ext as string).split(",").map((s) => s.trim()).filter(Boolean)
    : undefined;

  const summary = await scanPaths(client, targets, {
    language: flags.language,
    entities: flags.entities,
    concurrency,
    maxBytes,
    ignore,
    includeExtensions,
    onFile: flags.json
      ? undefined
      : (file) => {
          if (file.skipped === "binary") return;
          if (file.error) {
            process.stderr.write(c.yellow(`! ${file.path}: ${file.error}\n`));
            return;
          }
          if (file.findings.length === 0) return;
          process.stdout.write(c.bold(`${file.path}\n`));
          for (const f of file.findings) {
            process.stdout.write(
              `  ${c.cyan(f.entity_type.padEnd(18))} ${c.dim(`@${f.start}-${f.end} score=${f.score.toFixed(2)}`)}  ${formatSnippet(f)}\n`,
            );
          }
        },
  });

  if (flags.json) {
    process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
  } else {
    printScanFooter(summary);
  }

  if (Boolean(values["fail-on-findings"]) && summary.totalFindings > 0) return 2;
  return 0;
}

function printScanFooter(summary: ScanSummary): void {
  const entries = Object.entries(summary.byEntity).sort((a, b) => b[1] - a[1]);
  process.stdout.write("\n");
  process.stdout.write(
    c.bold(
      `scanned ${summary.scannedFiles} file(s), ${summary.totalFindings} finding(s)`,
    ) + "\n",
  );
  if (summary.skippedFiles > 0) {
    process.stdout.write(c.dim(`skipped ${summary.skippedFiles} file(s)`) + "\n");
  }
  for (const [entity, count] of entries) {
    process.stdout.write(`  ${c.cyan(entity.padEnd(18))} ${count}\n`);
  }
}

function formatSnippet(f: Finding): string {
  const single = f.text.replace(/\s+/g, " ").trim();
  return single.length > 60 ? `${single.slice(0, 57)}...` : single;
}

async function runAnalyze(args: string[]): Promise<number> {
  const { flags, values, positionals } = parseCommon(args, {
    file: { type: "string", short: "f" },
  });
  if (values.help) {
    process.stdout.write(HELP);
    return 0;
  }
  const text = await resolveText(positionals, values.file as string | undefined);
  const client = makeClient(flags);
  const findings = await client.analyze(text, {
    language: flags.language,
    entities: flags.entities,
  });
  if (flags.json) {
    process.stdout.write(JSON.stringify(findings, null, 2) + "\n");
    return findings.length > 0 ? 0 : 0;
  }
  if (findings.length === 0) {
    process.stdout.write(c.green("no PII detected\n"));
    return 0;
  }
  for (const f of findings) {
    process.stdout.write(
      `${c.cyan(f.entity_type.padEnd(18))} ${c.dim(`@${f.start}-${f.end} score=${f.score.toFixed(2)}`)}  ${formatSnippet(f)}\n`,
    );
  }
  return 0;
}

async function runRedact(args: string[]): Promise<number> {
  const { flags, values, positionals } = parseCommon(args, {
    file: { type: "string", short: "f" },
  });
  if (values.help) {
    process.stdout.write(HELP);
    return 0;
  }
  const text = await resolveText(positionals, values.file as string | undefined);
  const client = makeClient(flags);
  const result = await client.redact(text, {
    language: flags.language,
    entities: flags.entities,
  });
  if (flags.json) {
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
    return 0;
  }
  process.stdout.write(`${result.text ?? ""}\n`);
  return 0;
}

async function runHealth(args: string[]): Promise<number> {
  const { flags } = parseCommon(args);
  const client = makeClient(flags);
  const result = await client.health();
  if (flags.json) {
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  } else {
    process.stdout.write(`${result.status ?? "ok"}\n`);
  }
  return 0;
}

async function resolveText(positionals: string[], filePath?: string): Promise<string> {
  if (filePath) return (await readFile(filePath, "utf8")).replace(/\r\n/g, "\n");
  if (positionals.length > 0) return positionals.join(" ");
  if (!process.stdin.isTTY) return await readStdin();
  throw new Error("provide text as an argument, --file, or via stdin");
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of process.stdin) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : (chunk as Buffer));
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function loadPackageJson(): Promise<{ name: string; version: string }> {
  const url = new URL("../package.json", import.meta.url);
  const data = JSON.parse(await readFile(url, "utf8"));
  return { name: data.name, version: data.version };
}

main(process.argv.slice(2)).then(
  (code) => process.exit(code),
  (err) => {
    process.stderr.write(`fatal: ${(err as Error).stack ?? err}\n`);
    process.exit(1);
  },
);
