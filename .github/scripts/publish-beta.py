#!/usr/bin/env python3
"""Publish a tested beta tag using the workflow's short-lived repository token."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import tomllib
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def release_payload(tag: str) -> dict:
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    runtime = runpy.run_path(str(ROOT / 'repowatch/__init__.py'))['__version__']
    if not re.fullmatch(r'\d+\.\d+\.\d+b\d+', version) or tag != f'v{version}' or version != runtime:
        raise ValueError('Tag, project version and runtime version must match a beta version')
    notes = (ROOT / f'docs/releases/{tag}.md').read_text().strip()
    if not notes:
        raise ValueError('Release notes are empty')
    return {'tag_name': tag, 'name': f'RepoWatch {version} (beta)', 'body': notes,
            'draft': False, 'prerelease': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--check', action='store_true', help='Validate without accessing GitHub')
    args = parser.parse_args()
    payload = release_payload(args.tag)
    if args.check:
        print(f'PASS: {args.tag}, package versions and release notes are consistent')
        return
    repository = os.environ['GITHUB_REPOSITORY']
    token = os.environ['GITHUB_TOKEN']
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository name')
    if os.environ.get('GITHUB_REF') != f'refs/tags/{args.tag}':
        raise ValueError('Releases can only be published from the matching pushed tag')
    base = f'https://api.github.com/repos/{repository}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
               'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json'}
    # Retrying a completed workflow must not create a second release or overwrite one.
    try:
        with urlopen(Request(f'{base}/releases/tags/{args.tag}', headers=headers), timeout=30) as response:
            existing = json.load(response)
    except HTTPError as error:
        if error.code != 404:
            raise
    else:
        if existing['prerelease'] and not existing['draft']:
            print(f"Beta already published: {existing['html_url']}")
            return
        raise RuntimeError('An existing release requires manual review; refusing to replace it')
    request = Request(f'{base}/releases', data=json.dumps(payload).encode(), headers=headers, method='POST')
    with urlopen(request, timeout=30) as response:
        published = json.load(response)
    print(f"Published beta: {published['html_url']}")


if __name__ == '__main__':
    main()
