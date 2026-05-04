# RunPod GPU Inference / Training Guide (S6 / U6 / #48)

S6 baseline comparison (`compare_baselines.py`) を RunPod GPU pod で実行する手順、および HuggingFace token-classification の GPU 学習手順 (#48 Phase 2 で実測)。
spaCy transformer (ja_core_news_trf, ja_ginza)、HuggingFace BERT (custom_bert) の推論を GPU で加速し、CNN 系 (ja_core_news_md, custom_cnn) との直接比較を可能にする。
学習用途では `runpod/pytorch:1.0.3-cu1290-torch290-ubuntu2204` + `accelerate launch -m pleno_ner_training.train_hf` で 20 700 steps を 11 分 / RTX A5000 community で完走済み (cost ~$0.05)。

CPU runbook (`runpod-training.md`) の構造を踏襲しつつ、GPU 固有の設定と **SSH security hardening** を加えている。

## 1. 推奨 GPU pod 表

| 項目 | 推奨値 | 備考 |
|---|---|---|
| GPU (1 枚構成) | **RTX 4090 (24GB VRAM, ~$0.7/h)** | transformer 単体推論 (ja_core_news_trf / ja_ginza / custom_bert)。SECURE cloud のみ |
| GPU (上位構成) | **A40 (48GB VRAM, ~$0.5/h)** | 複数 transformer を同時ロードする場合 |
| GPU (training 用最安) | **RTX A5000 (24GB VRAM, ~$0.16/h)** | COMMUNITY cloud で SCP 可。#48 deberta-v2-tiny 学習 11 分で完走、cost ~$0.05 |
| vCPU / RAM | 8 vCPU / 32 GB | bootstrap 後段は CPU で post-hoc に走るため余裕を持たせる |
| Disk | 50 GB | model cache + corpus + predictions |
| Template | `runpod/pytorch:1.0.3-cu1290-torch290-ubuntu2204` | 動作確認済 (#48 で実証)。命名規則 `<x.y.z>-cu<NNNN>-torch<NNN>-ubuntu<YYMM>` |
| SSH | 有効必須 | SCP/SFTP は exposed TCP 経由 |

VRAM 要件:
- `ja_core_news_trf` ~3GB
- `custom_bert` ~2GB
- 同時ロード ~6GB → RTX 4090 (24GB) で十分余裕

コスト目安: 6h × $0.7 = **約 $4.20 / full run** (R6 の 6h time-box 上限)。実測 ~2.2h であればその 1/3 で済む。

## 2. CUDA driver / Python 互換性

- **Base image**: `runpod/pytorch:2.4.0-py3.11-cuda12.1-devel-ubuntu22.04`
- **Python**: pod 起動後 `mise` または `uv` で 3.12 系を install (本リポジトリの pyproject に合わせる)
- **PyTorch**: CUDA 12.1 build。`spacy-transformers` (`thinc-pytorch`) と互換
- **spaCy**: `ja_core_news_trf` (3.8.x) は CUDA pipeline (`spacy.require_gpu()`) で動作

互換性確認コマンド (pod 内):

```bash
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.version.cuda)"
python -c "import spacy; spacy.require_gpu(); print('OK')"
```

## 3. Pod 起動 + SSH 設定 (security-hardened)

### 3.0 RunPod proxy SSH の落とし穴 (#48 で発見)

RunPod の proxy SSH (`ssh.runpod.io`) を使う場合、以下 3 点を全て満たさないと `Permission denied (publickey)` で reject される:

1. **SSH 公開鍵を RunPod console の "User Settings → SSH Public Keys" に登録**
   - team account を使っている場合は team 側にも別途登録 (user-level と team-level は独立)
   - 登録後、即時反映 (propagation 待ちは不要)

2. **SSH ユーザー名は `<pod-id>-<account-hash>`** (pod-id 単独ではない!)
   - RunPod console の pod 詳細 → "Connect" タブの SSH コマンドで実際の username が表示される
   - 例: `ssh ixemd21o4pqszq-64410f66@ssh.runpod.io -i ~/.ssh/id_ed25519`
   - pod-id 単独 (`ixemd21o4pqszq@ssh.runpod.io`) で接続すると認証 fail
   - account-hash は API key 紐付きアカウントの fingerprint。MCP の create-pod 戻り値には**含まれない**ので、初回は console UI から取得

3. **使用する Docker image が Docker Hub に存在する**
   - 存在しない tag (例: `runpod/pytorch:2.4.0-py3.11-cuda12.1-devel-ubuntu22.04` は manifest 不在) を指定すると pod が "RUNNING" 表示でも実体は起動失敗
   - 失敗していると proxy SSH 認証時点で reject される (sshd が起動しないため)
   - 確認手段: console pod 詳細 "Connect" タブに `error creating container: ... manifest unknown` と表示
   - 動作確認済 image: `runpod/pytorch:1.0.3-cu1290-torch290-ubuntu2204` (2026-05 時点最新フォーマット `<x.y.z>-cu<NNNN>-torch<NNN>-ubuntu<YYMM>`)

これらに気付かないまま `Permission denied (publickey)` だけを見ると鍵未登録と誤診し、無限に確認 loop に入る。**まず console UI で Connect タブの SSH コマンド全文を読み取って、それと自分のコマンドを diff する**ところから始める。

### 3.0.1 SECURE cloud vs COMMUNITY cloud の運用差 (#48 で発見)

RunPod には 2 つの cloud tier があり、本リポジトリの artifact 回収パターンに対する制約が異なる:

| 項目 | SECURE cloud | COMMUNITY cloud |
|---|---|---|
| Proxy SSH (`ssh.runpod.io`) | 利用可 (PTY 強制) | 利用可 |
| **Direct TCP SSH (exposed `:22`)** | **不可** (port が公開されない) | **可** (例 `193.183.22.61:1845`) |
| SCP / SFTP | **不可** (proxy SSH に subsystem 無し) | 可 (Direct TCP 経由) |
| `ssh ... 'cmd'` exec mode | 不可 ("Your SSH client doesn't support PTY") | 可 |
| GPU 在庫 | 安定 | 変動・取り合い |
| 価格 | 高 (RTX 4090 ~$0.7/h) | 安 (RTX A5000 ~$0.16/h) |

artifact 回収 (Section 8) と nohup 起動 (Section 6) に依存する本 runbook の流れは **COMMUNITY cloud 必須**。SECURE cloud で pod を立ててしまうと proxy SSH の `subsystem request failed` で SCP が一切通らず、artifact が取り出せない。

#48 Phase 2 では当初 SECURE cloud で起動し SCP 不可で詰まり、COMMUNITY cloud (`193.183.22.61:1845`) に切り替えて 11 分で training + 回収まで完走した。

### 3.0.2 RunPod MCP の port mapping 取得不可 (#48 で発見)

`mcp__runpod__create-pod` および `get-pod` の戻り値は **`publicIp:""` / port mapping を含まない**。Direct TCP の `<host>:<port>` を取るには:

1. Chrome MCP で `https://console.runpod.io/pods/<pod-id>` を開く
2. "Connect" タブ → "TCP Port Mapping" の `22 → <ext-port>` を読み取る
3. `Public IP` を別途取得 (同タブの Direct TCP SSH コマンドに含まれる)

つまり pod の create は MCP で自動化できるが、artifact 回収のための SSH 接続情報取得には **chrome MCP (console UI 読み取り) が必須**。`mcp__runpod__*` だけで完結させようとすると Direct TCP 経由の SCP に到達できない。

### 3.0.3 アクティブな bounded-timeout 監視 (#48 で発見)

長尺の training を nohup で投げたあと、`tail -f` を passive に張り続けると進捗が止まっても通知が来ない (SSH idle timeout / nohup buffer / VRAM stall いずれも sshd 側は alive のまま)。`Monitor` ツール側で **regex 一致回数 + bounded sleep** で能動 polling する:

```
# bad: 通知が来ないまま放置
ssh ... 'tail -f /workspace/nohup.out'

# good: 1 epoch ごとに進捗 regex で wake、5 epoch なら最大 N 分で抜ける
Monitor(
  command = "ssh ... 'grep -c \"\\d\\+/20700\" /workspace/nohup.out || true'",
  regex   = "^[0-9]+$",
  poll_seconds = 30,
  timeout_seconds = 1800
)
```

「定期チェックして一生通知が来なくて放置されてます」を踏まないために、待ち系コマンドには必ず `timeout_seconds` を付ける。

### Critical: SSH host key 検証の bypass フラグを絶対に使わない

CPU runbook (`runpod-training.md`) には SSH host key 検証 bypass フラグ (`-o StrictHostKeyChecking` を `no` に設定する形) の記載があるが、本 GPU runbook では **MITM 対策のため使用禁止** (peer-review item P1-8)。理由は RunPod pod の IP/PORT が deploy ごとに変わり、TOFU すら成立しないため。

### Approach 1: known_hosts pre-population (推奨)

```bash
# RunPod console (Pods 画面) で SSH host key fingerprint を確認しておく
# 同じ pod の fingerprint を ssh-keyscan で取得し、人間照合してから known_hosts に書き込む
ssh-keyscan -H -p $RUNPOD_PORT $RUNPOD_IP > /tmp/runpod-hostkey.txt
ssh-keygen -lf /tmp/runpod-hostkey.txt        # SHA256:... を表示

# RunPod console UI に表示される SHA256 fingerprint と人間の目で照合
# OK なら append (一致しなければ MITM の疑いあり、絶対に append しない)
cat /tmp/runpod-hostkey.txt >> ~/.ssh/known_hosts
rm /tmp/runpod-hostkey.txt
```

### Approach 2: RunPod fingerprint verification (代替)

RunPod console は SSH fingerprint を表示するため、初回接続時に SSH client が表示する fingerprint と人間照合し、yes/no で承認する。CI/自動化には不向きだが手動運用では十分。

### 本 runbook 内の SCP/SSH コマンドは全て secure default

以降のコマンドは `-o StrictHostKeyChecking=yes` (= デフォルト) で動作する前提で書く。known_hosts に当該 host が登録されていなければ即時 fail する。

## 4. 開始 5 分以内 nvidia-smi 確認

CPU runbook の `free -h` 必須チェックに対応する GPU 版必須チェック。pod 起動後 **5 分以内に必ず実行する**。

```bash
ssh root@$RUNPOD_IP -p $RUNPOD_PORT -i ~/.ssh/id_ed25519 'nvidia-smi'
```

確認項目:
- GPU が enumerate されている (`No devices were found` であれば即 terminate)
- Driver Version (>= 525 推奨)
- CUDA Version (12.x)
- Memory-Usage の Total が想定値 (RTX 4090 なら 24576 MiB)

VRAM 100% に達すると CPU runbook の RAM 100% と同様に SSHd が応答しなくなり、artifact 回収が不可能になる罠あり (CHANGELOG.md "Key Insights" 参照)。バッチサイズと同時ロード model 数で予防する。

## 5. RunPod API key + chrome MCP 取り扱い (P2-8)

### DO NOT hardcode

API key を runbook、scripts、log、commit message に **絶対書かない**。

### Source

- 1Password: `op://Personal/RunPod/api_key` (推奨、`op read` で env injection)
- env var: `${RUNPOD_API_KEY}` (.env.local から source、.gitignore 済み)

```bash
export RUNPOD_API_KEY="$(op read 'op://Personal/RunPod/api_key')"
```

### MCP log redaction

chrome MCP は接続時に session log を出力するため、`RUNPOD_API_KEY` パターンを redact する設定を session 開始前に確認:

```bash
chrome MCP show-config | grep -i 'redact\|RUNPOD_API_KEY'
```

redact rule が無効化されている場合は session を開始しない。

### Minimal scope

RunPod API token は **pod-management-only** scope を使う (org-admin scope は使わない)。誤って logs に流出しても被害が最小化する。

### Rotation

major run cycle 終了ごと、および外部に logs を共有する前後で必ず rotate する。Section 11 の terminate checklist にも含めている。

## 6. 推論 nohup 起動

データ転送と uv install が済んだあと、`compare_baselines` を nohup で background 実行する (SSH 切断耐性)。

```bash
ssh root@$RUNPOD_IP -p $RUNPOD_PORT -i ~/.ssh/id_ed25519 << 'EOF'
cd /workspace
nohup uv run --extra bench python -m pleno_ner_training.compare_baselines \
    --version v0.12.0 \
    --training-manifest data/benchmark/v0.12.0/ja/training_corpus_manifest.json \
    --pod-mode gpu \
    --output-dir experiments/artifacts/<run_id> > nohup.out 2>&1 &
echo $!
echo "INFERENCE_STARTED"
EOF
```

`<run_id>` は `date +%Y%m%d_%H%M%S` 等で一意化する。

## 7. 進捗確認

```bash
ssh root@$RUNPOD_IP -p $RUNPOD_PORT -i ~/.ssh/id_ed25519 \
  'tail -f /workspace/nohup.out'
```

Section 9 の budget table の inference time を超えても progress が止まらない場合、VRAM OOM か CPU stall を疑い、`nvidia-smi` を別 SSH session で再確認する。

## 8. Artifact 回収 (SCP, security-hardened)

```bash
mkdir -p packages/training/experiments/artifacts/<run_id>
scp -P $RUNPOD_PORT -i ~/.ssh/id_ed25519 \
    root@$RUNPOD_IP:/workspace/experiments/artifacts/<run_id>/*.json \
    packages/training/experiments/artifacts/<run_id>/
```

**重要**: host key 検証 bypass フラグ (Section 3 で言及) は付けない。Section 3 で known_hosts に登録済みであれば本コマンドはそのまま通る。登録されていなければ fail し、その時点で MITM 検知の余地が残る。

## 9. Per-baseline wall-clock budget table (P2-9)

| variant | model load | inference (500 docs) | total est. | notes |
|---|---|---|---|---|
| ja_core_news_trf | 5-10 min | ~10-15 min | **~25 min** | transformer; warm-up matters |
| custom_bert | 3-5 min | ~10-15 min | **~20 min** | BERT-base, smaller |
| ja_ginza | 3-5 min | ~5-10 min | **~15 min** | electra は除外、標準 ja_ginza |
| ja_core_news_md | 1-2 min | ~3-5 min | **~7 min** | small CNN, fast |
| custom_cnn | <1 min | ~2-3 min | **~4 min** | smallest |
| bootstrap CI (CPU post-hoc) | — | ~10-30 min | **~30 min** | not GPU; n=1000 × 5 variants × 7 percentiles |

**Total est.**: ~71 min inference (GPU) + 30 min bootstrap (CPU post-hoc) = **~1.7h**。
保守的に見積もって ~2.2h で R6 の 6h time-box に対し margin >= 3.8h。

## 10. Time-box 超過時の運用 (R6 + R12)

- **6h hard cap**: inference wall-clock のみ。bootstrap CI は CPU で post-hoc に実行するため、billable clock とは別カウントする (R6 lock)
- **超過時の動作**: orchestrator は `partial_run: true` を artifact に立て、`experiments/partial/<run_id>/` に raw predictions を archive する。aggregates (P50/P90/P95/P99) は **書き出さない** (peek bias 遮断、R12)
- **再キュー cap**: 2 attempts (P2-7)。3 回連続失敗時は measurement 自体を infeasible と宣言し、Risks table fallback (eyeball / S1 直接適用 / corpus repair) に切り替える
- `verdict_per_entity` は partial run の artifact から omit する。U5 の artifact writer が enforce する

## 11. Terminate チェックリスト

pod 破棄前に **全て** にチェックを入れること。

- [ ] Artifact (`*.json`) を SCP で local に回収済み
- [ ] `nohup.out` も非空なら回収 (debug 用)
- [ ] Pod を **stop** (suspend ではなく)
- [ ] Pod を **terminate** (RunPod console → Pods → 3点メニュー → Terminate Pod)
- [ ] Time-box clock を `experiments/log.jsonl` に記録 (U5 の LogJsonlEntry append)
- [ ] logs を外部に共有した場合は RunPod API key を rotate

terminate を忘れると課金が継続する。`runpod-gpu-compare-v12` を kick したあとは Section 11 をリマインダとして開いておく。
