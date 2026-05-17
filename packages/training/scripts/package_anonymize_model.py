"""Package a trained spaCy model as a `pleno_anonymize_<lang>` wheel.

`spacy package` builds artifacts under the `<lang>_<name>` convention and the
package name is fixed by `meta['lang'] + '_' + meta['name']`. The SDK and
server resolve models by the `pleno_anonymize_<lang>` import name, so this
script wraps `spacy package` and rewrites the resulting layout / setup.py /
meta.json to the namespaced form before producing the wheel.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _rewrite_setup_py(setup_py: Path, model_name: str) -> None:
    setup_py.write_text(
        f"""#!/usr/bin/env python
import io
import json
from os import path, walk
from shutil import copy
from setuptools import setup

MODEL_NAME = {model_name!r}


def load_meta(fp):
    with io.open(fp, encoding='utf8') as f:
        return json.load(f)


def load_readme(fp):
    if path.exists(fp):
        with io.open(fp, encoding='utf8') as f:
            return f.read()
    return ""


def list_files(data_dir):
    output = []
    for root, _, filenames in walk(data_dir):
        for filename in filenames:
            if not filename.startswith('.'):
                output.append(path.join(root, filename))
    output = [path.relpath(p, path.dirname(data_dir)) for p in output]
    output.append('meta.json')
    return output


def list_requirements(meta):
    requirements = []
    if 'setup_requires' in meta:
        requirements += meta['setup_requires']
    if 'requirements' in meta:
        requirements += meta['requirements']
    return requirements


def setup_package():
    root = path.abspath(path.dirname(__file__))
    meta_path = path.join(root, 'meta.json')
    meta = load_meta(meta_path)
    readme_path = path.join(root, 'README.md')
    readme = load_readme(readme_path)
    model_dir = path.join(MODEL_NAME, MODEL_NAME + '-' + meta['version'])

    copy(meta_path, path.join(MODEL_NAME))
    copy(meta_path, model_dir)

    setup(
        name=MODEL_NAME,
        description=meta.get('description'),
        long_description=readme,
        author=meta.get('author'),
        author_email=meta.get('email'),
        url=meta.get('url'),
        version=meta['version'],
        license=meta.get('license'),
        packages=[MODEL_NAME],
        package_data={{MODEL_NAME: list_files(model_dir)}},
        install_requires=list_requirements(meta),
        zip_safe=False,
        entry_points={{'spacy_models': ['{{m}} = {{m}}'.format(m=MODEL_NAME)]}}
    )


if __name__ == '__main__':
    setup_package()
"""
    )


def _rewrite_init_py(init_py: Path) -> None:
    init_py.write_text(
        '''"""spaCy model package entrypoint.

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
'''
    )


def _rewrite_meta_name(meta_path: Path, name: str) -> None:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["name"] = name
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def package_model(
    model_path: Path,
    output_dir: Path,
    language: str,
    version: str,
    *,
    build_wheel: bool,
) -> Path:
    if language not in {"ja", "en"}:
        raise SystemExit(f"unsupported language: {language!r}")

    spacy_name = f"ner_{language}"  # produces `<lang>_ner_<lang>` from spacy package
    legacy_pkg = f"{language}_{spacy_name}"
    legacy_dir = output_dir / f"{legacy_pkg}-{version}"
    target_pkg = f"pleno_anonymize_{language}"
    target_dir = output_dir / f"{target_pkg}-{version}"

    if legacy_dir.exists():
        shutil.rmtree(legacy_dir)
    if target_dir.exists():
        shutil.rmtree(target_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "-m", "spacy", "package",
        str(model_path), str(output_dir),
        "--name", spacy_name,
        "--version", version,
        "--force",
    ]
    subprocess.run(cmd, check=True)

    if not legacy_dir.exists():
        raise SystemExit(f"spacy package output not found at {legacy_dir}")

    # Rename the outer package directory and the inner python package + model
    # directory so the wheel is `pleno_anonymize_<lang>` end-to-end.
    legacy_dir.rename(target_dir)
    (target_dir / legacy_pkg).rename(target_dir / target_pkg)
    inner_legacy = target_dir / target_pkg / f"{legacy_pkg}-{version}"
    inner_target = target_dir / target_pkg / f"{target_pkg}-{version}"
    inner_legacy.rename(inner_target)

    _rewrite_setup_py(target_dir / "setup.py", target_pkg)
    _rewrite_init_py(target_dir / target_pkg / "__init__.py")
    new_meta_name = f"anonymize_{language}"
    for meta_path in (
        target_dir / "meta.json",
        target_dir / target_pkg / "meta.json",
        inner_target / "meta.json",
    ):
        if meta_path.exists():
            _rewrite_meta_name(meta_path, new_meta_name)

    # Strip stale egg-info / build artifacts that may still reference the
    # legacy name from spacy's internal templating.
    for stale in target_dir.glob(f"{legacy_pkg}.egg-info"):
        shutil.rmtree(stale)

    if build_wheel:
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", str(target_dir)],
            check=True,
        )

    return target_dir


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True, type=Path, help="trained spaCy model dir (e.g. output/ja-v02/model-best)")
    p.add_argument("--output", required=True, type=Path, help="output dir (e.g. packages/models)")
    p.add_argument("--language", required=True, choices=("ja", "en"))
    p.add_argument("--version", required=True)
    p.add_argument("--build-wheel", action="store_true")
    args = p.parse_args()

    target = package_model(
        args.model, args.output, args.language, args.version,
        build_wheel=args.build_wheel,
    )
    print(f"packaged: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
