# RunPod CPU Training Guide

spaCy CNN学習をRunPod CPUポッドで実行する手順。

## 推奨構成

| 項目 | 推奨値 | 備考 |
|---|---|---|
| インスタンス | CPU5 Compute-Optimized | 5+ GHz, DDR5 |
| vCPU / RAM | 4 vCPU / 8 GB | 2vCPU/4GBはOOMリスク |
| コスト | $0.14/hr | 学習30-60分 = $0.07-0.14/回 |
| テンプレート | Runpod Ubuntu 20.04 | Python 3.13同梱 |
| SSH | 有効必須 | SCP/SFTPはexposed TCP経由 |

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

# アーカイブ作成
tar czf /tmp/ner-train-data.tar.gz data/processed/ja-v02/ configs/train_cnn.cfg
```

### 2. ポッドデプロイ (Chrome)

1. https://console.runpod.io/deploy?type=CPU&cpu=cpu5c-4-8&template=runpod-ubuntu
2. SSH terminal access にチェック
3. "Deploy on-demand" クリック
4. Pods画面でSSH接続情報を確認:
   - **SSH over exposed TCP** の `ssh root@<IP> -p <PORT>` を使用（SCP対応）

### 3. データ転送 & 依存関係インストール

```bash
# IPとPORTはRunPod Pods画面から取得
export RUNPOD_IP=213.173.111.68
export RUNPOD_PORT=13653

# アップロード
scp -o StrictHostKeyChecking=no -P $RUNPOD_PORT -i ~/.ssh/id_ed25519 \
  /tmp/ner-train-data.tar.gz root@$RUNPOD_IP:/root/

# 展開 & 依存関係
ssh -o StrictHostKeyChecking=no -p $RUNPOD_PORT root@$RUNPOD_IP -i ~/.ssh/id_ed25519 '
  cd /root && tar xzf ner-train-data.tar.gz 2>/dev/null
  pip install spacy 2>&1 | tail -1
  pip install ja_core_news_sm@https://github.com/explosion/spacy-models/releases/download/ja_core_news_sm-3.8.0/ja_core_news_sm-3.8.0-py3-none-any.whl 2>&1 | tail -1
  echo "READY"
'
```

### 4. 学習実行 (nohup)

```bash
# nohupで実行（SSH切断に耐える）
ssh -o StrictHostKeyChecking=no -p $RUNPOD_PORT root@$RUNPOD_IP -i ~/.ssh/id_ed25519 '
  cd /root && nohup python3.13 -m spacy train configs/train_cnn.cfg \
    --output output/ja-v02 \
    --paths.train data/processed/ja-v02/train.spacy \
    --paths.dev data/processed/ja-v02/dev.spacy \
    > /root/train.log 2>&1 & echo $! && echo "TRAINING_STARTED"
'
```

### 5. 進捗確認

```bash
ssh -o StrictHostKeyChecking=no -p $RUNPOD_PORT root@$RUNPOD_IP -i ~/.ssh/id_ed25519 \
  'tail -10 /root/train.log'
```

### 6. モデルダウンロード & ポッド破棄

```bash
# model-bestをバックアップ
ssh ... 'cd /root && tar czf /tmp/model-best.tar.gz output/ja-v02/model-best/'
scp -o StrictHostKeyChecking=no -P $RUNPOD_PORT -i ~/.ssh/id_ed25519 \
  root@$RUNPOD_IP:/tmp/model-best.tar.gz /tmp/model-best.tar.gz

# ローカルに展開
cd packages/training
rm -rf output/ja-v02/model-best
tar xzf /tmp/model-best.tar.gz

# RunPodポッドをTerminate（Chrome: Pods → 3点メニュー → Terminate Pod）
```

### 7. ベンチマーク評価 (ローカル)

```bash
for v in v0.4.0 v0.5.0 v0.12.0; do
  uv run python -m pleno_ner_training.evaluate_benchmark \
    --model output/ja-v02/model-best --language ja --version $v
done
```

## 注意事項

- **pythonコマンド**: RunPod Ubuntu 20.04では `python3.13` を使用（`python3` は3.8）
- **メモリ**: 4GB RAMではOOMするため、4vCPU/8GBを使用
- **SSH切断対策**: 必ず `nohup` でバックグラウンド実行
- **コスト管理**: 学習完了後は必ずポッドをTerminate
