#!/usr/bin/env python3
"""Verify the two fork archives through a consumer outside the Rapier workspace."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.36.0-sylphx.1"
NAMES = ("rapier2d", "rapier3d")
FEATURES = ["serde-serialize", "debug-render", "parallel", "enhanced-determinism"]
TARGET = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "target")).resolve()
os.environ["CARGO_TARGET_DIR"] = str(TARGET)
os.environ["CARGO_INCREMENTAL"] = "0"
# Standalone archive tests do not inherit the workspace's dev opt-level = 1.
os.environ["CARGO_PROFILE_TEST_OPT_LEVEL"] = "1"
TARGET.mkdir(parents=True, exist_ok=True)
RESULTS = TARGET / "package-check"
RESULTS.mkdir(exist_ok=True)


def cargo(*args, capture=False, check=True):
    command = ["nice", "-n", "19", "ionice", "-c3", "prlimit",
               "--as=17179869184", "--", "timeout", "600", "cargo", *args]
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, cwd=ROOT, text=True, check=check,
                          capture_output=capture)


def consumer(directory, packages):
    directory.mkdir()
    dependencies = "\n".join(
        f'{name} = {{ package = "sylphx-{name}", version = "={VERSION}", '
        f'path = {json.dumps(str(packages[name]))}, '
        f'features = {json.dumps(FEATURES)} }}'
        for name in NAMES
    )
    (directory / "Cargo.toml").write_text(
        '[package]\nname = "rapier-package-consumer"\nversion = "0.0.0"\n'
        'edition = "2024"\npublish = false\n\n[workspace]\n\n'
        f'[dependencies]\n{dependencies}\n'
    )
    (directory / "src").mkdir()
    (directory / "src/main.rs").write_text(
        'use rapier2d::prelude as d2;\nuse rapier3d::prelude as d3;\n'
        'fn main() {\n    let _ = d2::RigidBodySet::new();\n'
        '    let _ = d3::RigidBodySet::new();\n}\n'
    )
    (directory / "tests").mkdir()
    shutil.copy2(ROOT / "crates/rapier3d/tests/voxels_ball_rolling.rs",
                 directory / "tests/voxels_ball_rolling.rs")
    return directory / "Cargo.toml"


def graph(manifest, label):
    result = cargo("metadata", "--format-version", "1", "--manifest-path",
                   str(manifest), capture=True)
    metadata = json.loads(result.stdout)
    packages = {p["id"]: p for p in metadata["packages"]}

    def identity(package_id):
        package = packages[package_id]
        return (package["name"], package["version"], package["source"] or "local")

    normalized = sorted([
        {
            "package": identity(node["id"]),
            "features": sorted(node["features"]),
            "dependencies": sorted((dep["name"], identity(dep["pkg"]))
                                   for dep in node["deps"]),
        }
        for node in metadata["resolve"]["nodes"]
        if node["id"] != metadata["resolve"]["root"]
    ], key=lambda node: node["package"])
    (RESULTS / f"{label}-graph.json").write_text(json.dumps(normalized, indent=2) + "\n")
    return normalized


with tempfile.TemporaryDirectory(prefix="rapier-consumer-") as scratch:
    scratch = Path(scratch)
    source_packages = {name: ROOT / "crates" / name for name in NAMES}
    source_manifest = consumer(scratch / "source", source_packages)
    tested_graph = graph(source_manifest, "tested")
    cargo("test", "--manifest-path", str(source_manifest))

    print("+ bash publish.sh package", flush=True)
    subprocess.run(["nice", "-n", "19", "ionice", "-c3", "prlimit",
                    "--as=17179869184", "--", "timeout", "600", "bash",
                    str(ROOT / "publish.sh"), "package"], cwd=ROOT, check=True)

    packaged = {}
    for name in NAMES:
        archive = TARGET / "package" / f"sylphx-{name}-{VERSION}.crate"
        with tarfile.open(archive) as crate:
            crate.extractall(scratch / "archives")
        package = scratch / "archives" / f"sylphx-{name}-{VERSION}"
        manifest = tomllib.loads((package / "Cargo.toml").read_text())
        assert manifest["package"]["name"] == f"sylphx-{name}"
        assert manifest["package"]["version"] == VERSION
        assert manifest["package"]["license"] == "Apache-2.0"
        assert manifest["lib"]["name"] == name
        assert (package / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
        assert not manifest.get("patch"), "the archive must not depend on a root patch"
        for source in (ROOT / "src").rglob("*.rs"):
            assert (package / "src" / source.relative_to(ROOT / "src")).read_bytes() == source.read_bytes()
        packaged[name] = package
        print(f"ARCHIVE {archive.name} sha256={hashlib.sha256(archive.read_bytes()).hexdigest()}",
              flush=True)

    # Negative control: the same consumer and regression must fail with upstream's
    # pre-fix narrow phase, not merely fail to compile or resolve dependencies.
    baseline = scratch / "upstream-rapier3d"
    shutil.copytree(packaged["rapier3d"], baseline)
    pair_update = subprocess.run(
        ["git", "show", "b716d375efc0201003f0cd9ef7168eee0b62c177:src/geometry/narrow_phase/pair_update.rs"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout
    (baseline / "src/geometry/narrow_phase/pair_update.rs").write_bytes(pair_update)
    baseline_manifest = consumer(scratch / "baseline", {**packaged, "rapier3d": baseline})
    result = cargo("test", "--manifest-path", str(baseline_manifest),
                   "--test", "voxels_ball_rolling", capture=True, check=False)
    (RESULTS / "upstream-regression.log").write_text(result.stdout + result.stderr)
    assert result.returncode != 0 and (
        "ball_rolling_on_voxels_floor_keeps_the_contact_graph_consistent ... FAILED" in result.stdout
    ), "the upstream negative control must reach and fail the voxel regression"
    print("UPSTREAM_NEGATIVE_CONTROL: voxel regression failed as expected", flush=True)

    packaged_manifest = consumer(scratch / "packaged", packaged)
    shutil.copy2(source_manifest.parent / "Cargo.lock", packaged_manifest.parent / "Cargo.lock")
    packaged_graph = graph(packaged_manifest, "packaged")
    assert tested_graph == packaged_graph, "packaged and tested dependency graphs differ"
    print(f"GRAPH_MATCH: {len(packaged_graph)} packages, versions, features, and edges identical", flush=True)
    cargo("build", "--locked", "--manifest-path", str(packaged_manifest))
    cargo("test", "--locked", "--manifest-path", str(packaged_manifest))
    for name in NAMES:
        cargo("test", "--manifest-path", str(packaged[name] / "Cargo.toml"),
              "--lib", "--tests")
    print("PACKAGED_CONSUMER: passed; imports unchanged; fixed voxel regression passed", flush=True)
