"""Fast checks on the image pins in the charts' values files.

These need only Python and PyYAML (no helm, no cluster), so they can run against
every image-bump PR, including the draft PRs Heatshield opens.

Run them with:

    uv run --no-project --with pytest --with pyyaml pytest tests/image_pins

The downgrade test compares against a base ref: $IMAGE_PINS_BASE_REF, else
origin/$GITHUB_BASE_REF (set on pull_request workflows), else origin/release-2.1.2.
It is skipped when that ref is not available.
"""

import os
import re
import subprocess

import pytest
import yaml

from tests import git_root_dir

VALUES_FILES = sorted(
    p.relative_to(git_root_dir).as_posix()
    for p in [git_root_dir / "values.yaml", *(git_root_dir / "charts").glob("*/values*.yaml")]
)
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")  # Docker's tag grammar
FLOATING_TAGS = {"latest", "main", "master", "stable", "edge", "nightly"}


def load(text: str):
    return yaml.safe_load(text) or {}


def image_pins(node, path=()):
    """Yield (yaml path, repository, tag) for every mapping that has both keys."""
    if isinstance(node, dict):
        repository, tag = node.get("repository"), node.get("tag")
        if isinstance(repository, str) and "tag" in node:
            registry = node.get("registry")
            if isinstance(registry, str) and registry and not repository.startswith(registry):
                repository = f"{registry}/{repository}"
            yield ".".join(path), repository, tag
        for key, value in node.items():
            yield from image_pins(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from image_pins(value, (*path, str(index)))


def pins_in(text: str) -> dict[str, tuple[str, object]]:
    return {path: (repository, tag) for path, repository, tag in image_pins(load(text))}


ALL_PINS = [
    (file, path, repository, tag)
    for file in VALUES_FILES
    for path, repository, tag in image_pins(load((git_root_dir / file).read_text()))
]


def version_key(tag: str):
    """Comparable key for tags like 2.1.27, 8.19.23-2 or 2026.10.23; None if not numeric."""
    parts = re.split(r"[.-]", tag)
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def base_ref() -> str | None:
    ref = os.environ.get("IMAGE_PINS_BASE_REF")
    if not ref:
        ref = f"origin/{os.environ['GITHUB_BASE_REF']}" if os.environ.get("GITHUB_BASE_REF") else "origin/release-2.1.2"
    found = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref], cwd=git_root_dir, capture_output=True, check=False)
    return ref if found.returncode == 0 else None


def file_at(ref: str, file: str) -> str | None:
    shown = subprocess.run(["git", "show", f"{ref}:{file}"], cwd=git_root_dir, capture_output=True, text=True, check=False)
    return shown.stdout if shown.returncode == 0 else None


def test_found_values_files_and_pins():
    assert "charts/astronomer/values.yaml" in VALUES_FILES
    assert len(ALL_PINS) > 20, "expected the charts to pin many images"


@pytest.mark.parametrize("file", VALUES_FILES)
def test_values_file_is_valid_yaml(file):
    assert isinstance(load((git_root_dir / file).read_text()), dict)


@pytest.mark.parametrize(("file", "path", "repository", "tag"), ALL_PINS, ids=[f"{f}:{p}" for f, p, _, _ in ALL_PINS])
def test_tag_is_pinned(file, path, repository, tag):
    if tag is None:
        pytest.skip("tag is left to the chart's appVersion")
    assert isinstance(tag, str), f"{file}:{path} tag must be a string, got {tag!r} (quote it in YAML)"
    assert TAG_PATTERN.match(tag), f"{file}:{path} tag {tag!r} is not a valid image tag"
    assert tag.lower() not in FLOATING_TAGS, f"{file}:{path} uses floating tag {tag!r}"


def test_astronomer_images_come_from_quay():
    wrong = [
        f"{file}:{path} {repository}"
        for file, path, repository, _ in ALL_PINS
        if "/ap-" in repository and not repository.startswith("quay.io/astronomer/")
    ]
    assert not wrong, "Astronomer ap-* images must come from quay.io/astronomer:\n" + "\n".join(wrong)


def test_no_image_tag_is_downgraded():
    ref = base_ref()
    if ref is None:
        pytest.skip("no base ref to compare against")
    downgrades = []
    for file in VALUES_FILES:
        before = file_at(ref, file)
        if before is None:
            continue
        old_pins = pins_in(before)
        for path, (repository, tag) in pins_in((git_root_dir / file).read_text()).items():
            old_repository, old_tag = old_pins.get(path, (None, None))
            if old_repository != repository or not isinstance(tag, str) or not isinstance(old_tag, str):
                continue
            new_key, old_key = version_key(tag), version_key(old_tag)
            if new_key is not None and old_key is not None and new_key < old_key:
                downgrades.append(f"{file}:{path} {repository} {old_tag} -> {tag}")
    assert not downgrades, f"image tags moved backwards compared with {ref}:\n" + "\n".join(downgrades)
