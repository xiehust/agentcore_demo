#!/usr/bin/env python3
"""Derive metrics from an existing evidence file without replaying any workload."""
import argparse
import copy
import json
from pathlib import Path

from aws_cli import write_json
from benchmark import summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('output already exists; do not overwrite evidence')
    document = copy.deepcopy(json.loads(args.results.read_text()))
    summarize(document)
    summary = {key: document.get(key) for key in
        ('status', 'error', 'rounds_requested', 'rounds_executed', 'summary', 'cleanup_errors')}
    summary['rounds_with_records'] = len(document['records'])
    summary['source'] = str(args.results)
    summary['completed_turns'] = sum(response['ok'] for record in document['records']
        for response in record['responses'])
    summary['answer_mismatches'] = sum(response.get('answer_matches_expected') is False
        for record in document['records'] for response in record['responses'])
    write_json(args.out, summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
