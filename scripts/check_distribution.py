"""Check Git objects, never an unstaged working copy, without printing matches.

Defaults to staged blobs. Use --ref HEAD to check a committed release. This is
an additional privacy/artifact check, not a replacement for a secret scanner.
"""
import argparse
import io
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "personal home path": re.compile(r"[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+|/(?:home|Users)/[a-z][^/\s]+", re.I),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "API key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[\w-]{30,}|AKIA[A-Z0-9]{16})\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "Steam account": re.compile(r"\b7656119\d{10}\b"),
    "credential in URL": re.compile(r"https?://[^/\s:'\"]+:[^/\s@'\"]+@", re.I),
}
EMAIL = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EXAMPLE_DOMAINS = {"example.com", "example.net", "example.org", "example.invalid"}
EXAMPLE_EMAILS = {"cait-gpt@users.noreply.github.com"}
FORBIDDEN_SUFFIXES = {'.exe', '.dll', '.lib', '.sav', '.dmp', '.mdmp', '.mp4', '.mkv',
                      '.mp3', '.wav', '.onnx', '.zip', '.7z', '.pem', '.key', '.jsonl',
                      '.pak', '.ucas', '.utoc', '.uasset', '.umap'}
FORBIDDEN_SUFFIXES.update({'.token', '.pfx', '.p12', '.db', '.sqlite', '.sqlite3'})
FORBIDDEN_NAMES = {'model-runs.json', 'campaigns.json', 'server.identity.json'}
FORBIDDEN_DIRS = {'.git', '.venv', '.codex', '.claude', '.gemini', 'node_modules',
                  'state', 'logs', 'telemetry', 'recordings', 'screenshots', 'saves'}
MAX_BLOB_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024

def git(root, *args, input=None):
    return subprocess.check_output(['git', *args], cwd=root, input=input)


def entries(root, ref):
    if ref is not None:
        tree = git(root, 'rev-parse', '--verify', '--end-of-options', ref + '^{tree}').decode().strip()
        records = git(root, 'ls-tree', '-rz', '--full-tree', tree)
    else:
        records = git(root, 'ls-files', '--stage', '-z')
    for record in records.split(b'\0'):
        if not record:
            continue
        metadata, name = record.split(b'\t', 1)
        mode, middle, last = metadata.decode().split()
        if ref is None:
            oid, stage = middle, last
        else:
            oid, stage = last, '0'
        yield mode, oid, stage, name.decode('utf-8')


def scan(root, ref=None):
    failures = []
    objects = list(entries(root, ref))
    total = 0
    pending = []
    ids = sorted({oid for _, oid, _, _ in objects})
    sizes = {}
    if ids:
        metadata = git(root, 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                       input=('\n'.join(ids) + '\n').encode())
        for row in metadata.decode().splitlines():
            oid, kind, size = row.split()
            sizes[oid] = (kind, int(size))
    for mode, oid, stage, name in objects:
        path = Path(name)
        filename = path.name.lower()
        display = name.encode('unicode_escape').decode('ascii')
        if mode not in {'100644', '100755'} or stage != '0':
            failures.append(f'{display}: symlink, submodule or unresolved index entry')
            continue
        if (path.suffix.lower() in FORBIDDEN_SUFFIXES
                or FORBIDDEN_DIRS.intersection(part.lower() for part in path.parts)
                or filename == '.env' or filename.endswith('.env')
                or (filename.startswith('.env.') and filename != '.env.example')
                or filename == '.mcp.json'
                or filename in FORBIDDEN_NAMES
                or filename.startswith(('client_secret', 'credentials'))):
            failures.append(f'{display}: forbidden runtime/configuration artifact')
            continue
        kind, size = sizes[oid]
        if kind != 'blob':
            failures.append(f'{display}: unexpected Git object type')
            continue
        total += size
        if size > MAX_BLOB_BYTES or total > MAX_TOTAL_BYTES:
            failures.append(f'{display}: distribution size budget exceeded')
            continue
        pending.append((display, oid))
    blobs = {}
    ids = sorted({oid for _, oid in pending})
    if ids:
        stream = io.BytesIO(git(root, 'cat-file', '--batch', input=('\n'.join(ids) + '\n').encode()))
        for expected in ids:
            oid, kind, size = stream.readline().decode().strip().split()
            if oid != expected or kind != 'blob' or int(size) != sizes[oid][1]:
                raise RuntimeError('Git object identity changed during scan')
            data = stream.read(int(size))
            if len(data) != int(size) or stream.read(1) != b'\n':
                raise RuntimeError('Incomplete Git object during scan')
            blobs[oid] = data
    for display, oid in pending:
        data = blobs[oid]
        if b'\0' in data:
            failures.append(f'{display}: unexpected binary content')
            continue
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            failures.append(f'{display}: unexpected binary content')
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    failures.append(f'{display}:{number}: {kind} (value withheld)')
            for match in EMAIL.finditer(line):
                email = match.group().lower()
                if email not in EXAMPLE_EMAILS and email.rsplit('@', 1)[1] not in EXAMPLE_DOMAINS:
                    failures.append(f'{display}:{number}: non-example email address (value withheld)')
    return len(objects), failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', help='Committed ref to check; defaults to the Git index')
    args = parser.parse_args(argv)
    count, failures = scan(ROOT, args.ref)
    if failures:
        print('\n'.join(failures))
        return 1
    print(f'Checked {count} tracked text files: no forbidden artifacts or identifier patterns.')
    return 0

if __name__ == '__main__':
    sys.exit(main())
