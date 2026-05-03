---
title: NER 評価基準は metric triad (corpus, metric, aggregation) で語る
date: 2026-05-03
issue: #55
related_prs: []
tags: [evaluation, ner, benchmarking, metrics, communication]
---

# NER 評価基準は metric triad (corpus, metric, aggregation) で語る

## 何が起きたか

社内で「F1 はいくつ？」という会話が交差した結果、3 つの異なる数字が同時に正としてやり取りされ、モデル品質の議論が噛み合わなくなった。

| 指標 | corpus | metric | aggregation |
|---|---|---|---|
| README dev F1 | dev split (~in-domain) | strict-span F1 (spaCy Scorer) | micro |
| v0.12.0 benchmark | adversarial 500 docs (88% negative) | strict-span F1 (spaCy Scorer) | micro |
| S6 held-out | v0.13.0 80 docs, 3 unseen templates | matched-precision-floor recall + token-overlap F1 | per-template |

3 つとも正しい計算結果。だが **「F1」という同じ名前** で呼ばれていたため、production 品質の判断軸がブレた。

## なぜ起きたか (root cause)

「F1」は scalar に見えて、実際は 3 つの自由度を持つ:

1. **corpus**: 何を評価しているか (in-domain / adversarial / unseen template)
2. **metric**: どう正解とみなすか (strict-span / token-overlap / precision-floor recall)
3. **aggregation**: どう集約するか (micro / macro / per-template)

corpus が違えば数字が 0.3 違う。metric が違えば 0.1 違う。aggregation が違えば順位が逆転する。**triad のうち 1 つでも省略すると比較不能になる**。

## 検出方法 / 教訓

- README, PR description, slack で数字を出すときは triad を **必ずセット** で書く。
- production-realistic な **headline metric を 1 つ** だけ定める。複数あると意思決定が遅れる。
- 推奨 headline: **v0.12.0 adversarial F1** (production の負荷分布に最も近い)
- dev F1 は overfit 監視用、S6 は generalization 監視用の **補助指標** として位置づける。

## 適用ガイド (再発防止)

### 数字を共有するときの最小フォーマット

```
F1 = 0.84  (corpus: adversarial-500, metric: strict-span, aggregation: micro)
```

または triad を 1 行で:

```
adversarial-500 / strict-span / micro: F1 = 0.84
```

### README に書くべきこと

- headline metric の triad を明示
- 補助指標は別セクションに分ける
- 数字を更新したら triad もレビューする (corpus が更新されている可能性)

### モデル比較の rule

- 同じ triad 内でしか比較しない
- 異なる triad の数字を「向上した」と並べない
- A/B 比較は **3 つすべての triad で同方向に動いたか** を確認 (1 つでも逆行したら trade-off)

## 関連

- README dev F1 セクション
- v0.12.0 リリースノート (adversarial benchmark)
- S6 held-out 評価レポート (v0.13.0)
