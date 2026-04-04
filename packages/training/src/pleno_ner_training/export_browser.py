"""Export spaCy CNN model weights to browser-compatible binary format.

Produces a single .bin file containing all weight tensors for the tok2vec
(HashEmbedCNN) and NER (TransitionBasedParser) components, ready for
pure-TypeScript inference in the pleno-audit Chrome extension.

Binary format (little-endian throughout):
  [4B] magic "PNER"  [4B] version  [4B] config JSON len  [NB] config JSON
  [4B] tensor count
  Per tensor: [1B] name len  [NB] name  [1B] ndim  [ndim*4B] shape  [4B] data len  [NB] float32 data

Usage:
    python -m pleno_ner_training.export_browser [--model-dir output/ja-v02/model-best]
"""

from __future__ import annotations

import json
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MAGIC = b"PNER"
VERSION = 1

CONFIG = {
    "labels": ["ADDRESS", "BANK_ACCOUNT", "DATE_OF_BIRTH", "ORGANIZATION", "PERSON"],
    "moves": [
        "B-ADDRESS", "B-PERSON", "B-ORGANIZATION", "B-DATE_OF_BIRTH", "B-BANK_ACCOUNT",
        "I-ADDRESS", "I-PERSON", "I-ORGANIZATION", "I-DATE_OF_BIRTH", "I-BANK_ACCOUNT",
        "L-ADDRESS", "L-PERSON", "L-ORGANIZATION", "L-DATE_OF_BIRTH", "L-BANK_ACCOUNT",
        "U-ADDRESS", "U-PERSON", "U-ORGANIZATION", "U-DATE_OF_BIRTH", "U-BANK_ACCOUNT",
        "U-", "O",
    ],
    "embed_sizes": [2000, 1000, 1000, 1000],
    "width": 128,
    "depth": 4,
    "hidden_width": 64,
    "maxout_pieces_tok2vec": 3,
    "maxout_pieces_ner": 2,
    "n_features": 3,
}

# Expected tensor specs: (name, shape_tuple)
# shape=None means "extract from model, verify ndim only"
TENSOR_SPECS: list[tuple[str, tuple[int, ...]]] = [
    ("tok2vec.embed_norm.E", (2000, 128)),
    ("tok2vec.embed_prefix.E", (1000, 128)),
    ("tok2vec.embed_suffix.E", (1000, 128)),
    ("tok2vec.embed_shape.E", (1000, 128)),
    ("tok2vec.mix.maxout.W", (128, 3, 512)),
    ("tok2vec.mix.maxout.b", (128, 3)),
    ("tok2vec.mix.ln.G", (128,)),
    ("tok2vec.mix.ln.b", (128,)),
    *[
        (f"tok2vec.cnn.{i}.maxout.W", (128, 3, 384))
        for i in range(4)
    ],
    *[
        (f"tok2vec.cnn.{i}.maxout.b", (128, 3))
        for i in range(4)
    ],
    *[
        (f"tok2vec.cnn.{i}.ln.G", (128,))
        for i in range(4)
    ],
    *[
        (f"tok2vec.cnn.{i}.ln.b", (128,))
        for i in range(4)
    ],
    ("ner.lower.W", (64, 128)),
    ("ner.lower.b", (64,)),
    ("ner.precomp.W", (3, 64, 2, 64)),
    ("ner.precomp.b", (64, 2)),
    ("ner.precomp.pad", (1, 3, 64, 2)),
    ("ner.upper.W", (22, 64)),
    ("ner.upper.b", (22,)),
]


@dataclass
class Tensor:
    name: str
    data: np.ndarray  # float32, C-contiguous


def _extract_tok2vec_tensors(nlp) -> list[Tensor]:
    """Extract all tok2vec weight tensors by walking the model tree."""
    tok2vec_model = nlp.get_pipe("tok2vec").model

    # Collect HashEmbed layers (NORM, PREFIX, SUFFIX, SHAPE) in order
    hashembed_nodes = [
        node for node in tok2vec_model.walk() if node.name == "hashembed"
    ]
    if len(hashembed_nodes) != 4:
        raise ValueError(
            f"Expected 4 hashembed layers, found {len(hashembed_nodes)}"
        )

    embed_names = ["embed_norm", "embed_prefix", "embed_suffix", "embed_shape"]
    tensors: list[Tensor] = []
    for embed_name, node in zip(embed_names, hashembed_nodes):
        E = node.get_param("E")
        tensors.append(Tensor(f"tok2vec.{embed_name}.E", np.asarray(E, dtype=np.float32)))

    # Collect Maxout + LayerNorm nodes for mixing layer and CNN layers
    maxout_nodes = [
        node for node in tok2vec_model.walk() if node.name == "maxout"
    ]
    layernorm_nodes = [
        node for node in tok2vec_model.walk() if node.name == "layernorm"
    ]

    # First maxout/layernorm = mixing layer, next 4 = CNN layers
    if len(maxout_nodes) < 5:
        raise ValueError(
            f"Expected >= 5 maxout layers, found {len(maxout_nodes)}"
        )
    if len(layernorm_nodes) < 5:
        raise ValueError(
            f"Expected >= 5 layernorm layers, found {len(layernorm_nodes)}"
        )

    # Mixing layer
    mix_maxout = maxout_nodes[0]
    mix_ln = layernorm_nodes[0]
    tensors.append(Tensor("tok2vec.mix.maxout.W", np.asarray(mix_maxout.get_param("W"), dtype=np.float32)))
    tensors.append(Tensor("tok2vec.mix.maxout.b", np.asarray(mix_maxout.get_param("b"), dtype=np.float32)))
    tensors.append(Tensor("tok2vec.mix.ln.G", np.asarray(mix_ln.get_param("G"), dtype=np.float32)))
    tensors.append(Tensor("tok2vec.mix.ln.b", np.asarray(mix_ln.get_param("b"), dtype=np.float32)))

    # CNN layers (4 residual blocks)
    for i in range(4):
        cnn_maxout = maxout_nodes[1 + i]
        cnn_ln = layernorm_nodes[1 + i]
        tensors.append(Tensor(f"tok2vec.cnn.{i}.maxout.W", np.asarray(cnn_maxout.get_param("W"), dtype=np.float32)))
        tensors.append(Tensor(f"tok2vec.cnn.{i}.maxout.b", np.asarray(cnn_maxout.get_param("b"), dtype=np.float32)))
        tensors.append(Tensor(f"tok2vec.cnn.{i}.ln.G", np.asarray(cnn_ln.get_param("G"), dtype=np.float32)))
        tensors.append(Tensor(f"tok2vec.cnn.{i}.ln.b", np.asarray(cnn_ln.get_param("b"), dtype=np.float32)))

    return tensors


def _extract_ner_tensors(nlp) -> list[Tensor]:
    """Extract NER (TransitionBasedParser) weight tensors."""
    ner_model = nlp.get_pipe("ner").model

    tensors: list[Tensor] = []

    # Walk to find the linear/affine layers
    # The NER model structure: tok2vec >> lower >> upper
    # lower = PrecomputableAffine, upper = Linear
    # We need to find them by walking

    # Approach: collect all nodes and identify by parameter shapes
    all_nodes = list(ner_model.walk())

    lower_node = None
    upper_node = None
    precomp_node = None

    for node in all_nodes:
        param_names = node.param_names
        if "W" in param_names:
            W = node.get_param("W")
            W_shape = W.shape
            # PrecomputableAffine has W shape (n_features, nO, nP, nI) = (3, 64, 2, 64)
            if len(W_shape) == 4 and W_shape == (3, 64, 2, 64):
                precomp_node = node
            # Lower linear: W shape (64, 128)
            elif len(W_shape) == 2 and W_shape == (64, 128):
                lower_node = node
            # Upper linear: W shape (22, 64)
            elif len(W_shape) == 2 and W_shape == (22, 64):
                upper_node = node

    if lower_node is None:
        raise ValueError("Could not find NER lower linear layer (64, 128)")
    if precomp_node is None:
        raise ValueError("Could not find NER precomputable affine layer (3, 64, 2, 64)")
    if upper_node is None:
        raise ValueError("Could not find NER upper linear layer (22, 64)")

    tensors.append(Tensor("ner.lower.W", np.asarray(lower_node.get_param("W"), dtype=np.float32)))
    tensors.append(Tensor("ner.lower.b", np.asarray(lower_node.get_param("b"), dtype=np.float32)))
    tensors.append(Tensor("ner.precomp.W", np.asarray(precomp_node.get_param("W"), dtype=np.float32)))
    tensors.append(Tensor("ner.precomp.b", np.asarray(precomp_node.get_param("b"), dtype=np.float32)))
    tensors.append(Tensor("ner.precomp.pad", np.asarray(precomp_node.get_param("pad"), dtype=np.float32)))
    tensors.append(Tensor("ner.upper.W", np.asarray(upper_node.get_param("W"), dtype=np.float32)))
    tensors.append(Tensor("ner.upper.b", np.asarray(upper_node.get_param("b"), dtype=np.float32)))

    return tensors


def _validate_tensors(tensors: list[Tensor]) -> None:
    """Validate extracted tensors against expected specs."""
    tensor_map = {t.name: t for t in tensors}

    for name, expected_shape in TENSOR_SPECS:
        if name not in tensor_map:
            raise ValueError(f"Missing tensor: {name}")
        actual_shape = tuple(tensor_map[name].data.shape)
        if actual_shape != expected_shape:
            raise ValueError(
                f"Shape mismatch for {name}: expected {expected_shape}, got {actual_shape}"
            )

    expected_names = {name for name, _ in TENSOR_SPECS}
    extra = set(tensor_map.keys()) - expected_names
    if extra:
        raise ValueError(f"Unexpected tensors: {extra}")


def _serialize_binary(tensors: list[Tensor], output_path: Path) -> None:
    """Serialize tensors to the PNER binary format."""
    config_bytes = json.dumps(CONFIG, separators=(",", ":")).encode("utf-8")

    # Order tensors according to TENSOR_SPECS
    tensor_map = {t.name: t for t in tensors}
    ordered = [tensor_map[name] for name, _ in TENSOR_SPECS]

    with output_path.open("wb") as f:
        # Header
        f.write(MAGIC)
        f.write(struct.pack("<I", VERSION))
        f.write(struct.pack("<I", len(config_bytes)))
        f.write(config_bytes)

        # Tensor count
        f.write(struct.pack("<I", len(ordered)))

        for tensor in ordered:
            name_bytes = tensor.name.encode("ascii")
            if len(name_bytes) > 255:
                raise ValueError(f"Tensor name too long (>255 bytes): {tensor.name}")

            data = np.ascontiguousarray(tensor.data, dtype=np.float32)
            data_bytes = data.tobytes()
            shape = data.shape

            f.write(struct.pack("B", len(name_bytes)))
            f.write(name_bytes)
            f.write(struct.pack("B", len(shape)))
            for dim in shape:
                f.write(struct.pack("<I", dim))
            f.write(struct.pack("<I", len(data_bytes)))
            f.write(data_bytes)

    print(f"Written {output_path} ({output_path.stat().st_size:,} bytes)")


def _verify_binary(path: Path) -> None:
    """Read back the binary and verify all tensor shapes match expectations."""
    with path.open("rb") as f:
        magic = f.read(4)
        assert magic == MAGIC, f"Bad magic: {magic!r}"

        (version,) = struct.unpack("<I", f.read(4))
        assert version == VERSION, f"Bad version: {version}"

        (config_len,) = struct.unpack("<I", f.read(4))
        config_json = json.loads(f.read(config_len).decode("utf-8"))
        assert config_json == CONFIG, "Config mismatch"

        (n_tensors,) = struct.unpack("<I", f.read(4))
        assert n_tensors == len(TENSOR_SPECS), f"Expected {len(TENSOR_SPECS)} tensors, got {n_tensors}"

        spec_map = dict(TENSOR_SPECS)

        for _ in range(n_tensors):
            (name_len,) = struct.unpack("B", f.read(1))
            name = f.read(name_len).decode("ascii")

            (ndim,) = struct.unpack("B", f.read(1))
            shape = tuple(struct.unpack("<I", f.read(4))[0] for _ in range(ndim))

            (data_len,) = struct.unpack("<I", f.read(4))
            data = np.frombuffer(f.read(data_len), dtype=np.float32)

            expected_shape = spec_map.get(name)
            assert expected_shape is not None, f"Unknown tensor: {name}"
            assert shape == expected_shape, f"{name}: expected {expected_shape}, got {shape}"

            expected_elements = 1
            for d in shape:
                expected_elements *= d
            assert data.size == expected_elements, (
                f"{name}: expected {expected_elements} elements, got {data.size}"
            )

            # Sanity: no NaN/Inf
            assert np.all(np.isfinite(data)), f"{name}: contains NaN or Inf"

        # Ensure we consumed the entire file
        remaining = f.read()
        assert len(remaining) == 0, f"Trailing {len(remaining)} bytes"

    print(f"Verification passed: {n_tensors} tensors, all shapes correct")


def main(model_dir: str = "output/ja-v02/model-best") -> None:
    import spacy

    model_path = Path(model_dir)
    if not model_path.exists():
        print(f"Error: model directory not found: {model_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading model from {model_path} ...")
    nlp = spacy.load(model_path)

    print("Extracting tok2vec tensors ...")
    tok2vec_tensors = _extract_tok2vec_tensors(nlp)
    print(f"  -> {len(tok2vec_tensors)} tensors")

    print("Extracting NER tensors ...")
    ner_tensors = _extract_ner_tensors(nlp)
    print(f"  -> {len(ner_tensors)} tensors")

    all_tensors = tok2vec_tensors + ner_tensors
    print(f"Total: {len(all_tensors)} tensors")

    _validate_tensors(all_tensors)
    print("Validation passed")

    output_path = model_path.parent / "model-browser.bin"
    _serialize_binary(all_tensors, output_path)

    print("Verifying binary ...")
    _verify_binary(output_path)

    # Print summary
    total_params = sum(t.data.size for t in all_tensors)
    print("\nSummary:")
    print(f"  Tensors: {len(all_tensors)}")
    print(f"  Parameters: {total_params:,}")
    print(f"  File size: {output_path.stat().st_size:,} bytes")
    print(f"  Output: {output_path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export spaCy NER model to browser binary")
    parser.add_argument(
        "--model-dir",
        default="output/ja-v02/model-best",
        help="Path to spaCy model directory (default: output/ja-v02/model-best)",
    )
    args = parser.parse_args()
    main(model_dir=args.model_dir)
