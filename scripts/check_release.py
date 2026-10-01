"""Bind documentation, console metadata and any release tag to one version."""
from importlib.metadata import version
import os
from pathlib import Path

from rvn_ledger import __version__
from rvn_ledger.pipeline import LEDGER_VERSION

root = Path(__file__).resolve().parents[1]
installed = version('rvn-ledger')
if not installed == __version__ == LEDGER_VERSION:
    raise SystemExit('Package, CLI and manifest versions differ')
if f'@v{installed}' not in (root / 'README.md').read_text(encoding='utf-8'):
    raise SystemExit('README pinned installation does not name the installed version')
if f'## {installed} ' not in (root / 'CHANGELOG.md').read_text(encoding='utf-8'):
    raise SystemExit('Changelog does not describe the installed version')
ref = os.environ.get('GITHUB_REF', '')
if ref.startswith('refs/tags/') and ref != f'refs/tags/v{installed}':
    raise SystemExit(f'Tag {ref} does not match version {installed}')
print(f'OK: release metadata {installed}')
