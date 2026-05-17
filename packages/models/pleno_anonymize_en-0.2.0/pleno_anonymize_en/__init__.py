"""spaCy model package entrypoint.

spaCy's `load_model_from_init_py` derives the inner data dir from
`meta['lang'] + '_' + meta['name']`, which forces the wheel to be named
`<lang>_<name>`. This package follows the `pleno_anonymize_<lang>`
convention instead so it sits in the SDK namespace, so we resolve the
data path from the package name and call `load_model_from_path` directly.
"""

from pathlib import Path

from spacy.util import get_model_meta, load_model_from_path

_PKG_DIR = Path(__file__).parent
_PKG_NAME = _PKG_DIR.name

__version__ = get_model_meta(_PKG_DIR)["version"]


def load(**overrides):
    meta = get_model_meta(_PKG_DIR)
    data_path = _PKG_DIR / f"{_PKG_NAME}-{meta['version']}"
    return load_model_from_path(data_path, meta=meta, **overrides)
