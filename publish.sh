#!/usr/bin/env bash
# Package or publish only the two standing Sylphx fork crates. Cargo bundles the
# shared source through temporary crate-local links, as in upstream's publisher.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
operation="${1:?usage: bash publish.sh package|publish [cargo arguments]}"
shift
case "$operation" in
    package|publish) ;;
    *) printf 'expected package or publish\n' >&2; exit 2 ;;
esac

if [ -n "$(git -C "$ROOT" status --porcelain)" ]; then
    printf 'error: commit source changes before packaging or publishing\n' >&2
    exit 1
fi

backup_dir="$(mktemp -d)"
created_files=()
cleanup() {
    local file crate
    for file in "${created_files[@]}"; do
        rm -f "$file"
    done
    for crate in rapier2d rapier3d; do
        if [ -f "$backup_dir/$crate.Cargo.toml" ]; then
            cp "$backup_dir/$crate.Cargo.toml" "$ROOT/crates/$crate/Cargo.toml"
        fi
    done
    rm -rf "$backup_dir"
}
trap cleanup EXIT

for crate in rapier2d rapier3d; do
    manifest="$ROOT/crates/$crate/Cargo.toml"
    cp "$manifest" "$backup_dir/$crate.Cargo.toml"
    sed 's#path = "../../src/lib.rs"#path = "src/lib.rs"#' \
        "$backup_dir/$crate.Cargo.toml" > "$manifest"
    ln -s ../../src "$ROOT/crates/$crate/src"
    created_files+=("$ROOT/crates/$crate/src")
    cp "$ROOT/LICENSE" "$ROOT/crates/$crate/LICENSE"
    created_files+=("$ROOT/crates/$crate/LICENSE")
done

# Only the layout is dirty; the clean-tree check above protects the source.
cargo "$operation" --registry crates-io --manifest-path "$ROOT/Cargo.toml" \
    -p sylphx-rapier2d -p sylphx-rapier3d --allow-dirty "$@"
