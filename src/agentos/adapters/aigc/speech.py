"""Thin client for an already registered local-model-scheduler speech command.

This client never loads a model. Stable command receipts survive lost HTTP replies.
The private scheduler home is supplied by the node's protected configuration.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--scheduler-home', type=Path, required=True)
    parser.add_argument('--runtime', default='index-tts')
    parser.add_argument('request', type=Path)
    args = parser.parse_args()
    request = args.request.resolve()
    receipt = request.with_name('speech-receipt.json')
    job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'agentos-speech:' + str(request)))
    # Persist the identity before the first external write; interrupted jobs are never replayed.
    if not receipt.exists():
        receipt.write_text(json.dumps({'job_id': job_id, 'state': 'prepared'}), encoding='utf-8')
    spec = importlib.util.spec_from_file_location('managed_client', args.scheduler_home / 'client.py')
    if spec is None or spec.loader is None:
        raise RuntimeError('scheduler_client_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client: Any = module.Client(args.scheduler_home)
    submitted = client.call('POST', '/v1/jobs', {'runtime': args.runtime, 'job_id': job_id,
                                               'args': [str(request)]})
    if submitted.get('id') != job_id:
        raise RuntimeError('speech_receipt_mismatch')
    receipt.write_text(json.dumps({'job_id': job_id, 'state': submitted['state']}), encoding='utf-8')
    client.wait(job_id, timeout=5400)
    receipt.write_text(json.dumps({'job_id': job_id, 'state': 'completed'}), encoding='utf-8')
    print(json.dumps({'job_id': job_id, 'state': 'completed'}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('managed_speech_failed_or_pending; inspect the saved receipt', file=sys.stderr)
        raise SystemExit(1) from None
