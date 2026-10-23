"""Reject secrets, personal paths and runtime artifacts in the tracked export.

This conservative local check supplements a dedicated secret scanner. It never
prints matching values. Run after staging the intended files with Git.
"""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "personal home path": re.compile(r"[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"']+|/home/[a-z][^/\s]+", re.I),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "API key": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[\w-]{30,}|AKIA[A-Z0-9]{16})\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    "Steam account": re.compile(r"\b7656119(?!8000000000\b)\d{10}\b"),
}
FORBIDDEN_SUFFIXES = {'.exe', '.dll', '.lib', '.sav', '.dmp', '.mdmp', '.mp4', '.mkv',
                      '.mp3', '.wav', '.onnx', '.zip', '.7z', '.pem', '.key', '.jsonl',
                      '.pak', '.ucas', '.utoc', '.uasset', '.umap'}
FORBIDDEN_DIRS = {'.git', '.venv', 'node_modules', 'state', 'logs', 'Telemetry', 'recordings', 'screenshots', 'saves'}

def main():
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    failures = []
    count = 0
    for name in filter(None, names):
        path = ROOT / name
        count += 1
        if (path.suffix.lower() in FORBIDDEN_SUFFIXES or FORBIDDEN_DIRS.intersection(Path(name).parts)
                or path.name == '.env' or path.name.endswith('.env')
                or path.name.startswith(('client_secret', 'credentials'))):
            failures.append(f'{name}: forbidden runtime/configuration artifact')
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except UnicodeDecodeError:
            failures.append(f'{name}: unexpected binary content')
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if pattern.search(line):
                    failures.append(f'{name}:{number}: {kind} (value withheld)')
    if failures:
        print('\n'.join(failures))
        return 1
    print(f'Checked {count} tracked text files: no forbidden artifacts or identifier patterns.')
    return 0

if __name__ == '__main__':
    sys.exit(main())
