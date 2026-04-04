"""学習済み TokenClassification モデルを ONNX + INT8量子化でエクスポートする.

- optimum による ONNX エクスポート
- INT8 ダイナミック量子化
- HuggingFace Hub への push (--push-to オプション)
"""

from __future__ import annotations

import shutil
from pathlib import Path

from optimum.onnxruntime import ORTModelForTokenClassification, ORTQuantizer
from optimum.onnxruntime.configuration import AutoQuantizationConfig
from transformers import AutoTokenizer


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="学習済みモデルを ONNX + INT8量子化でエクスポート"
    )
    parser.add_argument(
        "--model",
        type=Path,
        required=True,
        help="学習済みモデルディレクトリ",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="ONNX出力ディレクトリ",
    )
    parser.add_argument(
        "--push-to",
        type=str,
        default=None,
        help="HuggingFace Hub リポジトリ名 (例: 0xhikae/ja-ner-onnx)",
    )
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)

    # Step 1: ONNX エクスポート (FP32)
    print(f"Exporting model to ONNX from {args.model}...")
    ort_model = ORTModelForTokenClassification.from_pretrained(
        args.model,
        export=True,
    )
    ort_model.save_pretrained(str(args.output))

    # トークナイザーもコピー
    print("Saving tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    tokenizer.save_pretrained(str(args.output))

    print(f"FP32 ONNX model saved to {args.output}")

    # Step 2: INT8 ダイナミック量子化
    print("Applying INT8 dynamic quantization...")
    quantizer = ORTQuantizer.from_pretrained(args.output)
    qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=False)

    quantizer.quantize(
        save_dir=str(args.output),
        quantization_config=qconfig,
    )

    # 量子化後のファイルをリネーム
    quantized_path = args.output / "model_quantized.onnx"
    optimized_path = args.output / "model_optimized.onnx"
    if optimized_path.exists() and not quantized_path.exists():
        shutil.move(str(optimized_path), str(quantized_path))

    # 出力ファイル一覧
    print(f"\n=== Export Summary ===")
    print(f"Output directory: {args.output}")
    for f in sorted(args.output.iterdir()):
        if f.is_file():
            size_mb = f.stat().st_size / (1024 * 1024)
            print(f"  {f.name}: {size_mb:.1f} MB")

    # Step 3: HuggingFace Hub push
    if args.push_to:
        print(f"\nPushing to HuggingFace Hub: {args.push_to}...")
        from huggingface_hub import HfApi

        api = HfApi()
        api.create_repo(args.push_to, exist_ok=True)
        api.upload_folder(
            folder_path=str(args.output),
            repo_id=args.push_to,
            commit_message="Upload ONNX quantized NER model (DeBERTa v2 base Japanese)",
        )
        print(f"Pushed to https://huggingface.co/{args.push_to}")


if __name__ == "__main__":
    main()
