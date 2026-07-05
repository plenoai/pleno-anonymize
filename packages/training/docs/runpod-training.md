# RunPod CPU Training Guide

spaCy CNN学習をRunPod CPUポッドで実行する手順。

`make runpod-train-ja` / `make runpod-train-en` (実体は
`scripts/runpod_train.py`) が create-pod → データ転送 → 学習実行 → artifact
回収 → delete-pod を1コマンドで実行する。ポッドのIP/PORTは実行の都度
RunPod APIから取得するため、本ドキュメントにハードコードされたIPは存在しない
（していた場合は陳腐化した古い手順）。`try/finally` で学習が失敗しても pod は
必ず削除されるため、手動 Terminate 忘れによるコスト垂れ流しは起きない。

## 推奨構成

| 項目 | 推奨値 | 備考 |
|---|---|---|
| インスタンス | CPU5 Compute-Optimized (`cpuFlavorIds=["cpu5c"]`) | 5+ GHz, DDR5, AMD EPYC 4564P |
| vCPU / RAM | **8 vCPU / 16 GB** (`--vcpu-count 8`, スクリプトのデフォルト) | OOM回避のため必須（下記参照） |
| コスト | $0.28/hr | 学習30-60分 = $0.14-0.28/回 |
| イメージ | `runpod/base:0.6.2-cpu` (スクリプトのデフォルト、`--image` で上書き可) | Python 3.13同梱の RunPod Ubuntu 20.04 系イメージ |
| SSH | 有効必須 | `scripts/runpod_train.py` がポッド作成時に `22/tcp` を公開し、SSH/SCPで接続する |

これらは全て `scripts/runpod_train.py` の CLI 引数のデフォルト値であり、
`--cpu-flavor-id` / `--vcpu-count` / `--image` 等で明示的に上書きできる。
値が古くなった場合は https://console.runpod.io/deploy?type=CPU で最新の
イメージ/フレーバーを確認し、フラグで上書きするか本ドキュメントとスクリプトの
デフォルトを更新すること（IPのような一過性の値をハードコードしない）。

## OOM（メモリ不足）に関する重要な注意

**2 vCPU / 4 GB および 4 vCPU / 8 GB ではOOMが発生します。必ず 8 vCPU / 16 GB 以上を使用してください。**

### 過去のOOM事例

| 構成 | データ量 | メモリ使用量 | 結果 |
|---|---|---|---|
| 2 vCPU / 4 GB ($0.07) | 15,965 docs | 4GB超 | OOM: プロセスkilled |
| 4 vCPU / 8 GB ($0.14) | 28,167 docs | 8.8GB (iter4) → 100% (iter8) | iter4は動作、iter8でOOM |
| 4 vCPU / 8 GB ($0.14) | 34,241 docs | 100% | OOM: SSH接続不可 |
| **8 vCPU / 16 GB ($0.28)** | **34,241 docs** | **11GB** | **安定稼働** |

### なぜOOMが起きるか

- spaCyの学習はデータ量に比例してメモリを消費する
- 特にDocBinの読み込みとバッチ処理で大量のメモリを使用
- RunPodのコンテナRAMは表示値通り（ホストRAMは共有だが利用不可）
- メモリ100%に達するとSSHdも応答不能になり、ログ取得・モデル回収が不可能

### OOM回避のルール

1. **8 vCPU / 16 GB ($0.28/hr) を最小構成とする** (`scripts/runpod_train.py` のデフォルト)
2. データ量が40,000件を超える場合は `--vcpu-count 16` (16 vCPU / 32 GB, $0.56/hr) を検討
3. `scripts/runpod_train.py` は学習コマンドをブロッキング実行するため、学習中の
   メモリ使用量を見たい場合は別ターミナルから
   `ssh -p <port> root@<ip> free -h` （IP/PORTは実行中のログに出力される）
4. **コスト節約のために小さいインスタンスを選ぶと、OOMでデータとコストの両方を失う** — 大きめを選ぶ方が結果的に安い

## 手順

### 1. データ準備 (ローカル)

```bash
cd packages/training

# augment → convert
uv run python -m pleno_ner_training.augment_ja_data \
  --input data/raw/ja-v02/generated_merged.json \
  --output data/raw/ja-v02/augmented.json \
  --augment-count 5000

uv run python -m pleno_ner_training.convert_to_docbin \
  --language ja \
  --input data/raw/ja-v02/augmented.json \
  --output-dir data/processed/ja-v02
```

`data/processed/ja-v02` のように既定の `data/processed/ja` 以外のディレクトリを
使う場合は、次のステップで `--data-dir` を明示的に渡す（後述）。

### 2. 学習実行 (1コマンド)

デフォルトの `data/processed/ja` / `configs/train_cnn.cfg` (ja) または
`data/processed/en` / `configs/train_cnn_en.cfg` (en) で学習する場合:

```bash
export RUNPOD_API_KEY=...  # https://console.runpod.io/user/settings

make runpod-train-ja   # 日本語
make runpod-train-en   # 英語
```

任意のデータ/configを使う場合は `scripts/runpod_train.py` を直接呼ぶ:

```bash
RUNPOD_API_KEY=... uv run --extra training python scripts/runpod_train.py \
  --language ja \
  --train-config configs/train_cnn.cfg \
  --data-dir data/processed/ja-v02 \
  --local-output-dir output/ja-v02/model-best
```

このコマンド1つで以下が全て実行される（詳細は `scripts/runpod_train.py --help`）:

1. RunPod REST API (`POST /pods`) でポッド作成 (CPU5 Compute-Optimized, 8 vCPU/16GB)
2. SSHが疎通するまでポーリング
3. `--train-config` / `--data-dir` を `scp` でアップロード
4. 言語に応じた `pip install`（ja は `ja_core_news_sm` の wheel も含む）
5. `python3.13 -m spacy train <config> --output <dir> --paths.train ... --paths.dev ...` を実行
6. `output/**/model-best` を `scp` でローカルに回収
7. **成功・失敗を問わず** `DELETE /pods/{id}` でポッドを削除 (`try/finally`)

### 3. 計画だけ確認したい場合 (課金なし)

API を一切呼ばずに、作成されるポッドスペック・アップロードされるファイル・
実行される remote コマンドを表示する:

```bash
make runpod-train-ja DRY_RUN=1
# あるいは
uv run python scripts/runpod_train.py --language ja --dry-run
```

### 4. ベンチマーク評価 (ローカル)

```bash
for v in v0.4.0 v0.5.0 v0.12.0; do
  uv run python -m pleno_ner_training.evaluate_benchmark \
    --model output/ja-v02/model-best --language ja --version $v
done
```

## 注意事項

- **pythonコマンド**: RunPod Ubuntu 20.04系イメージでは `python3` が3.8を指すため、
  `scripts/runpod_train.py` は既定で `python3.13` を使う（`--python-bin` で上書き可）。
- **メモリ**: 4GB RAMではOOMするため、8 vCPU/16GB 以上を使用（上記参照）。
- **コスト管理**: `scripts/runpod_train.py` が学習完了後・失敗後を問わず自動でポッドを
  削除するため、手動 Terminate 操作は不要（`--dry-run` はポッドを一切作成しない）。
- **SSH鍵**: 既定は `~/.ssh/id_ed25519`（`--ssh-key` で上書き可）。RunPodアカウントに
  対応する公開鍵を登録しておくこと。
