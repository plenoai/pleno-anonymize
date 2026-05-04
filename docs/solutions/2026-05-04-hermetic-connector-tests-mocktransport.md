---
title: Hermetic SourceConnector tests via httpx.MockTransport — 100% coverage without network
date: 2026-05-04
issue: ADR-0007
related_prs: [pii-scanner-postgres, pii-scanner-oci, pii-scanner-github, pii-scanner-aws, pii-scanner-slack]
tags: [testing, httpx, async, connectors, hermetic, ci]
---

# Hermetic SourceConnector tests via httpx.MockTransport

## 何が起きたか

`pleno-pii-scanner` のマルチソース展開で、GitHub / Slack / S3 / OCI registry / PostgreSQL / GCS 等、外部 API に依存する SourceConnector を量産する必要が生じた。「real な network 叩き + cassette 録画」の VCR 方式は CI で flaky になり、credential rotation のたびに cassette を再録する運用負債を発生させていた。並列で 5+ teammates が同時に connector を書く構成では、外部依存テストは破綻する。

## なぜ起きたか (root cause)

VCR 方式の本質的な問題:

- **cassette は record モードで初回録画**が必要 → real credentials が CI に必要
- registry 側の 仕様変更 (rate-limit header 形式等) で cassette が古くなり、現実と乖離
- 多数の teammates が並列で connector を書くと、誰も「再録画した cassette が正しい」と判断できない

外部依存テストは「fast / hermetic / deterministic」の 3 属性を一度に失う。

## 検出方法 / 教訓

`httpx.MockTransport` を使って、テスト内で **完全な request/response プロトコル** をモックする。connector が `httpx.AsyncClient` 経由で全 HTTP を行う前提を置けば、テストは:

1. 外部 network を一切叩かない
2. `Authorization` header / pagination cursor / 429 backoff を URL/header レベルで assert できる
3. `pytest -q` が 1 秒以下で完結する
4. CI に credential 不要 → forks の PR でも CI が green

## 適用ガイド

### connector 側の制約

`SourceConnector.__init__` に optional `client: httpx.AsyncClient | None = None` を追加し、test 時は MockTransport を組み込んだ client を注入する。production では None → 内部で生成、`_owns_client` フラグで lifecycle を分ける:

```python
def __init__(self, config, *, client=None):
    if client is None:
        self._client = httpx.AsyncClient(timeout=30.0)
        self._owns_client = True
    else:
        self._client = client
        self._owns_client = False

async def close(self):
    if self._owns_client:
        await self._client.aclose()
```

### test 側のパターン

```python
def _build_handler(routes: dict[str, Callable[[httpx.Request], httpx.Response]]):
    """Build a MockTransport handler from a `path → responder` map."""
    def handler(request: httpx.Request) -> httpx.Response:
        for suffix, responder in routes.items():
            if request.url.path.endswith(suffix):
                return responder(request)
        return httpx.Response(404, content=b"unmatched: " + str(request.url).encode())
    return handler

async def test_full_pipeline():
    routes = {
        "/manifests/v1": lambda r: httpx.Response(200, json=manifest_body),
        "/blobs/sha256:cccc...": lambda r: httpx.Response(200, content=config_blob),
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_build_handler(routes))
    ) as client:
        c = OciConnector(OciConfig(references=("ghcr.io/acme/api:v1",)), client=client)
        ...
```

### git-shell-out seam (clone_fn)

git host connectors (GitHub/GitLab/Bitbucket/Azure DevOps) は最終的に `git clone --depth=1` を shell out するが、test では実 clone は不要。`clone_fn` / `enumerate_fn` の callable seam を constructor に渡し、test では `lambda url, dest: dest.write_text(...)` のような fake で差し替える:

```python
class GithubConnector:
    def __init__(self, config, *, clone_fn=None, enumerate_fn=None, client=None):
        self._clone_fn = clone_fn or _default_clone
        self._enumerate_fn = enumerate_fn or _default_enumerate
```

これで connector の HTTP 部分は MockTransport、shell-out 部分は callable injection という二段構えで完全 hermetic。

### unmatched path → 明示エラー

handler の fallback は `httpx.Response(404, content=...)` で **どの URL がマッチしなかったか** を返す。silent passthrough や exception ではなく 404 にすることで、テスト失敗時のスタックトレースから「URL のタイポ or routes の漏れ」と即判断できる。

### registry pollution の回避

connector entry-point を持つ pii-scanner package を増やすと、`importlib.metadata.entry_points` が他の package のものを拾ってきて test が flaky になる:

```python
@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    monkeypatch.setattr(_registry_mod, "entry_points", lambda **_: [])
    _registry_mod._reset_for_tests()
    yield
    _registry_mod._reset_for_tests()
```

## 検証 (本 PR 群)

| connector | tests | coverage |
|---|---|---|
| pii-scanner-github | 86 | 100% |
| pii-scanner-aws | 125 | 100% |
| pii-scanner-slack | 133 | 100% |
| pii-scanner-postgres | 58 | 100% |
| pii-scanner-oci | 71 | 99% (1 defensive guard) |

合計 473 tests、全実行 < 5 sec、外部 network ゼロ。

## 関連

- ADR-0007 §16 (connector test discipline)
- httpx docs: [MockTransport](https://www.python-httpx.org/advanced/#mock-transports)
- 関連 learning: 並列 teammates の workspace conflict resolution
