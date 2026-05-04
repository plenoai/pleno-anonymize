---
title: OCI registry token cache miss when challenge overrides scope
date: 2026-05-04
issue: pii-scanner-oci #33
related_prs: [feat(pii-scanner-oci)]
tags: [oci, auth, cache-key, registry, bearer-token]
---

# OCI registry token cache miss when challenge overrides scope

## 何が起きたか

`pleno-pii-scanner-oci` の token negotiation テスト 2 件が `401 Unauthorized` で失敗。

- `test_401_then_200_with_anon_token`
- `test_basic_auth_token_path`

両方とも flow としては正しい:

1. manifest を GET → 401 + `WWW-Authenticate: Bearer realm=...,scope="repository:lib/alpine:pull"`
2. realm endpoint で token を取得
3. token をキャッシュ → 同じ URL を再 GET → 200 を期待

しかし 3 が再び 401 を返していた。token は確かに取得・キャッシュされているのに、再 GET 時の `Authorization` header が空だった。

## なぜ起きたか (root cause)

`_negotiate_token` 内で、challenge ヘッダから受け取った scope が **元の lookup scope を上書き** していた:

```python
# bug
scope = params.get("scope", scope)  # narrower scope from challenge
self._tokens[(registry, scope)] = token  # cached under narrowed scope
```

一方 `_auth_headers` は上書き前の元 scope で lookup:

```python
async def _auth_headers(self, registry, scope, *, ref):
    cached = self._tokens.get((registry, scope))  # original scope → cache miss
    ...
```

具体例:

- 元の lookup scope: `repository:library/alpine:pull` (Docker Hub の `library/` prefix を補ったもの)
- challenge が返す narrower scope: `repository:lib/alpine:pull`

cache key が `(registry, "repository:lib/alpine:pull")` になり、再 GET 時の lookup `(registry, "repository:library/alpine:pull")` がミス → header 空 → 401。

## 検出方法 / 教訓

**「lookup key と storage key の不一致」** は cache 系バグの典型形態。今回は scope 文字列の正規化が path によって違ったことに気づくのが遅れた。

challenge の `scope` 値は **realm endpoint への request 用** であって、cache key としての**識別子ではない**。

- realm endpoint には challenge の scope を渡す (registry が narrower scope を要求しているため、broader scope だと 403)
- cache key には connector が最初に決めた lookup scope を使う (連続する request が同じ key で再利用するため)

## 適用ガイド

```python
async def _negotiate_token(self, response, registry, scope):
    challenge = parse_challenge(response.headers["WWW-Authenticate"])
    realm = challenge["realm"]
    service = challenge.get("service", "")
    # broader scope は 403 を招くので realm exchange には challenge の値を使う
    token_scope = challenge.get("scope", scope)
    ...
    token = await auth.fetch_token(self._client, realm, token_scope, service)
    # lookup key は元の `scope` を維持 — 上書きしない
    self._tokens[(registry, scope)] = token
```

cache 系の API を設計するとき:

- **storage key は同じ resource にアクセスする path で一意**であるべき
- 中間処理で別の表現に変換する場合、変換前/変換後を取り違えない
- test に「同じ key で 2 回呼ぶ」path を含めると即検出できる

## 関連

- OCI Distribution Spec v1.1 §6.1 (Bearer challenge / realm exchange)
- pii-scanner-oci tests/test_connector.py::TestTokenNegotiation
