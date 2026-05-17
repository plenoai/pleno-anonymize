#!/usr/bin/env python
import io
import json
from os import path, walk
from shutil import copy
from setuptools import setup

# Package name is decoupled from spaCy's default `<lang>_<name>` convention so
# the wheel sits inside the `pleno_anonymize_*` namespace alongside the SDK.
# meta['lang'] stays "en" so the English tokenizer is selected at load time.
MODEL_NAME = "pleno_anonymize_en"


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
        package_data={MODEL_NAME: list_files(model_dir)},
        install_requires=list_requirements(meta),
        zip_safe=False,
        entry_points={'spacy_models': ['{m} = {m}'.format(m=MODEL_NAME)]}
    )


if __name__ == '__main__':
    setup_package()
