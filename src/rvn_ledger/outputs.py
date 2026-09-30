"""Deterministic JSON and staged, rollback-capable publication.

This flat four-file layout is NOT a crash-atomic transaction. An exclusive staging
directory rejects a second publisher and makes checks fail while publication is
in progress. Caught replacement failures restore the previous files. A process
crash or failed rollback leaves staging/recovery evidence and fails closed; do
not consume files until the writer has finished and `cli.py check` succeeds.
Consumers must not race a publisher. Manifest hashes are consistency checks,
not signatures or independent proof of the raw source.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path

OUTPUT_NAMES = ('invoices.json', 'quarantine.json', 'audit.json')
MANIFEST_NAME = 'manifest.json'
STAGING_NAME = '.ledger-staging'


class OutputError(RuntimeError):
    """A published output set is missing, incomplete or altered."""


def _reject_floats(node) -> None:
    if isinstance(node, float):
        raise TypeError('money outputs never contain floats')
    if isinstance(node, dict):
        for value in node.values():
            _reject_floats(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _reject_floats(value)


def serialize(document) -> bytes:
    _reject_floats(document)
    # Only lone surrogates are escaped; valid Unicode remains literal UTF-8.
    # Preserve a rejected identity losslessly instead of replacing it with '?'.
    return (json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8', errors='backslashreplace')


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def publish(out_dir, files: dict, manifest_fields: dict) -> dict:
    """Stage/fsync all files, back up old files, replace manifest last; rollback on caught failure."""
    out_dir = Path(out_dir)
    if set(files) != set(OUTPUT_NAMES):
        raise ValueError(f'outputs must be exactly {OUTPUT_NAMES}')
    for name, data in files.items():
        if not isinstance(data, bytes):
            raise TypeError(f'{name}: output must be bytes')
    manifest = dict(manifest_fields)
    manifest['outputs'] = {name: {'sha256': sha256(files[name]), 'bytes': len(files[name])} for name in OUTPUT_NAMES}
    staged = {**files, MANIFEST_NAME: serialize(manifest)}
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = out_dir / STAGING_NAME
    try:
        staging.mkdir()  # Exclusive acquisition; never delete another writer's staging tree.
    except FileExistsError as exc:
        raise OutputError('publication is active or interrupted; preserve .ledger-staging for recovery') from exc
    moved = []
    preserve_recovery = False
    try:
        previous = staging / 'previous'
        previous.mkdir()
        for name, data in staged.items():
            with open(staging / name, 'wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            target = out_dir / name
            if target.exists():
                shutil.copyfile(target, previous / name)
        try:
            for name in (*OUTPUT_NAMES, MANIFEST_NAME):
                os.replace(staging / name, out_dir / name)
                moved.append(name)
        except OSError:
            try:
                for name in reversed(moved):
                    backup = previous / name
                    if backup.exists():
                        os.replace(backup, out_dir / name)
                    else:
                        (out_dir / name).unlink()
            except OSError as rollback_error:
                preserve_recovery = True
                raise OutputError('publication and rollback failed; .ledger-staging preserved, do not consume outputs') from rollback_error
            raise
    finally:
        if not preserve_recovery:
            shutil.rmtree(staging)
    return manifest


def check_outputs(out_dir) -> dict:
    """Verify file hashes and metadata; reject publication/recovery in progress."""
    out_dir = Path(out_dir)
    if (out_dir / STAGING_NAME).exists():
        raise OutputError('publication is active or interrupted (.ledger-staging exists)')
    manifest_path = out_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise OutputError(f'{MANIFEST_NAME} is missing: {out_dir} does not hold a completed run')
    try:
        manifest = json.loads(manifest_path.read_bytes().decode('utf-8'))
    except (UnicodeDecodeError, ValueError, RecursionError, OSError) as exc:
        raise OutputError(f'{MANIFEST_NAME} is unreadable or not valid JSON') from exc
    outputs = manifest.get('outputs') if isinstance(manifest, dict) else None
    if not isinstance(outputs, dict) or set(outputs) != set(OUTPUT_NAMES):
        raise OutputError(f'{MANIFEST_NAME} does not list exactly {OUTPUT_NAMES}')
    for name, expected in outputs.items():
        if not isinstance(expected, dict) or set(expected) != {'sha256', 'bytes'}:
            raise OutputError(f'{name}: invalid manifest output metadata')
        digest, size = expected['sha256'], expected['bytes']
        if (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)
                or type(size) is not int or size < 0):
            raise OutputError(f'{name}: invalid manifest hash or byte count')
        path = out_dir / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise OutputError(f'{name} is missing or unreadable') from exc
        if sha256(data) != digest or len(data) != size:
            raise OutputError(f'{name} differs from the manifest hash')
    if (out_dir / STAGING_NAME).exists():
        raise OutputError('publisher started during check; retry only after it finishes')
    return manifest
