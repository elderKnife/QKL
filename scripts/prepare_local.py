#!/usr/bin/env python3
"""Create the ignored SDK configuration required by a fresh checkout.

Uses only the standard library. Existing configuration and keys are preserved.
"""
import shutil
from pathlib import Path


def prepare(root):
    root = Path(root)
    sdk = root / 'vendor/python-sdk'
    template, target = sdk / 'client_config.py.template', sdk / 'client_config.py'
    if not template.is_file():
        raise SystemExit('Vendored SDK is missing; clone the complete QKL repository.')
    if not target.exists():
        # Exclusive creation avoids replacing an existing local SDK configuration.
        with template.open('rb') as source, target.open('xb') as destination:
            shutil.copyfileobj(source, destination)
        print('Created vendor/python-sdk/client_config.py from the bundled template.')
    else:
        print('Keeping existing vendor/python-sdk/client_config.py.')
    for directory in ('logs/sdk', 'logs/services', 'data/reports/v2', 'tools'):
        (root / directory).mkdir(parents=True, exist_ok=True)


if __name__ == '__main__':
    prepare(Path(__file__).resolve().parents[1])
