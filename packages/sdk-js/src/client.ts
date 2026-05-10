import type {
  AnalyzeRequest,
  Finding,
  Language,
  RedactRequest,
  RedactResult,
} from "./types.js";

export const DEFAULT_ENDPOINT = "https://pleno-anonymize.fly.dev";

export interface ClientOptions {
  endpoint?: string;
  apiKey?: string;
  fetch?: typeof fetch;
  timeoutMs?: number;
  defaultLanguage?: Language;
  userAgent?: string;
}

export class PlenoAnonymizeError extends Error {
  readonly status?: number;
  readonly body?: unknown;

  constructor(message: string, opts: { status?: number; body?: unknown } = {}) {
    super(message);
    this.name = "PlenoAnonymizeError";
    this.status = opts.status;
    this.body = opts.body;
  }
}

export class PlenoAnonymize {
  readonly endpoint: string;
  readonly defaultLanguage: Language;
  private readonly fetchImpl: typeof fetch;
  private readonly apiKey?: string;
  private readonly timeoutMs: number;
  private readonly userAgent: string;

  constructor(options: ClientOptions = {}) {
    const endpoint =
      options.endpoint ??
      process.env.PLENO_ANONYMIZE_ENDPOINT ??
      DEFAULT_ENDPOINT;
    this.endpoint = endpoint.replace(/\/+$/, "");
    this.apiKey = options.apiKey ?? process.env.PLENO_ANONYMIZE_API_KEY;
    this.fetchImpl = options.fetch ?? globalThis.fetch;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.defaultLanguage = options.defaultLanguage ?? "ja";
    this.userAgent = options.userAgent ?? "pleno-anonymize-sdk-js";

    if (!this.fetchImpl) {
      throw new PlenoAnonymizeError(
        "global fetch is not available; pass options.fetch or run on Node 18+",
      );
    }
  }

  async analyze(
    text: string,
    options: { language?: Language; entities?: string[] } = {},
  ): Promise<Finding[]> {
    const body: AnalyzeRequest = {
      text,
      language: options.language ?? this.defaultLanguage,
      ...(options.entities ? { entities: options.entities } : {}),
    };
    return this.request<Finding[]>("/api/analyze", body);
  }

  async redact(
    input: string | RedactRequest,
    options: {
      language?: Language;
      entities?: string[];
      operators?: Record<string, Record<string, unknown>>;
    } = {},
  ): Promise<RedactResult> {
    const body: RedactRequest =
      typeof input === "string"
        ? {
            text: input,
            language: options.language ?? this.defaultLanguage,
            ...(options.entities ? { entities: options.entities } : {}),
            ...(options.operators ? { operators: options.operators } : {}),
          }
        : { language: this.defaultLanguage, ...input };

    const payload: Record<string, unknown> = { ...body };
    if (body.fillColor) {
      payload.fill_color = body.fillColor;
      delete (payload as { fillColor?: unknown }).fillColor;
    }
    return this.request<RedactResult>("/api/redact", payload);
  }

  async health(): Promise<{ status: string }> {
    return this.request<{ status: string }>("/health", undefined, { method: "GET" });
  }

  private async request<T>(
    path: string,
    body?: unknown,
    init: { method?: string } = {},
  ): Promise<T> {
    const url = `${this.endpoint}${path}`;
    const method = init.method ?? (body === undefined ? "GET" : "POST");
    const headers: Record<string, string> = {
      accept: "application/json",
      "user-agent": this.userAgent,
    };
    if (body !== undefined) headers["content-type"] = "application/json";
    if (this.apiKey) headers.authorization = `Bearer ${this.apiKey}`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await this.fetchImpl(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      const raw = await res.text();
      let parsed: unknown = undefined;
      if (raw) {
        try {
          parsed = JSON.parse(raw);
        } catch {
          parsed = raw;
        }
      }
      if (!res.ok) {
        throw new PlenoAnonymizeError(
          `pleno-anonymize ${method} ${path} failed: ${res.status} ${res.statusText}`,
          { status: res.status, body: parsed },
        );
      }
      return parsed as T;
    } catch (err) {
      if (err instanceof PlenoAnonymizeError) throw err;
      if ((err as { name?: string }).name === "AbortError") {
        throw new PlenoAnonymizeError(
          `pleno-anonymize ${method} ${path} timed out after ${this.timeoutMs}ms`,
        );
      }
      throw new PlenoAnonymizeError(
        `pleno-anonymize ${method} ${path} request failed: ${(err as Error).message}`,
      );
    } finally {
      clearTimeout(timer);
    }
  }
}
