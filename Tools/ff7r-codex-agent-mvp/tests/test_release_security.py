"""Public-release security regressions; all data and Git repos are synthetic."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import subprocess

import pytest
from mcp.server.transport_security import TransportSecurityMiddleware
from starlette.requests import Request

from ff7r_agent import mcp_http_server
from ff7r_agent.runtime_identity import default_runtime_directory
from ff7r_agent.safety import default_runtime_dir


@pytest.mark.parametrize('host,origin,status', [
    ('127.0.0.1:8766', None, None),
    ('localhost:8766', 'http://localhost:8766', None),
    ('[::1]:8766', 'http://[::1]:8766', None),
    ('attacker.invalid:8766', None, 421),
    ('localhost.attacker.invalid:8766', None, 421),
    ('127.0.0.1:9999', None, 421),
    ('127.0.0.1:8766', 'https://attacker.invalid', 403),
    ('127.0.0.1:8766', 'null', 403),
    ('127.0.0.1:8766', 'http://localhost:9999', 403),
])
def test_http_rejects_foreign_hosts_and_browser_origins(monkeypatch, host, origin, status):
    settings = mcp_http_server.mcp.settings
    monkeypatch.setattr(settings, 'transport_security', settings.transport_security)
    monkeypatch.setenv('FF7R_MCP_HTTP_HOST', '127.0.0.1')
    monkeypatch.setenv('FF7R_MCP_HTTP_PORT', '8766')
    mcp_http_server.configure_http_server()
    headers = [(b'host', host.encode()), (b'content-type', b'application/json')]
    if origin is not None:
        headers.append((b'origin', origin.encode()))
    request = Request({'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': headers})
    response = asyncio.run(TransportSecurityMiddleware(settings.transport_security).validate_request(request, is_post=True))
    assert (response.status_code if response else None) == status


def test_runtime_defaults_do_not_reuse_source_installation(monkeypatch, tmp_path):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.delenv('FF7R_AUTONOMY_RUNTIME_DIR', raising=False)
    assert default_runtime_dir() == default_runtime_directory() == tmp_path / 'ff7r-cait-gpt'
    custom = tmp_path / 'isolated-operator-state'
    monkeypatch.setenv('FF7R_AUTONOMY_RUNTIME_DIR', str(custom))
    assert default_runtime_dir() == default_runtime_directory() == custom


@pytest.fixture
def distribution(tmp_path):
    spec = importlib.util.spec_from_file_location('release_distribution_check',
        Path(__file__).resolve().parents[3] / 'scripts/check_distribution.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / 'synthetic-repository'
    root.mkdir()
    subprocess.run(['git', 'init', '--quiet', str(root)], check=True)
    return module, root


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments])


def test_distribution_checks_staged_blob_even_when_working_copy_is_clean(distribution):
    module, root = distribution
    path = root / 'sample.txt'
    path.write_text('ghp_' + 'A' * 36)
    git(root, 'add', 'sample.txt')
    path.write_text('No secret here.\n')
    count, failures = module.scan(root)
    assert count == 1 and any('GitHub token' in failure for failure in failures)
    assert all('A' * 36 not in failure for failure in failures)


def test_distribution_can_check_commit_instead_of_clean_index(distribution):
    module, root = distribution
    path = root / 'sample.txt'
    path.write_text('ghp_' + 'B' * 36)
    git(root, 'add', 'sample.txt')
    git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-qm', 'Synthetic fixture')
    path.write_text('No secret here.\n')
    git(root, 'add', 'sample.txt')
    assert module.scan(root)[1] == []
    assert any('GitHub token' in failure for failure in module.scan(root, 'HEAD')[1])


@pytest.mark.parametrize('name,data,reason', [
    ('.env.production', b'SAMPLE=example', 'forbidden'),
    ('sample.txt', b'\x00binary', 'binary'),
    ('contact.txt', ('fixture' + '@private-mail' + '.invalid').encode(), 'email'),
])
def test_distribution_rejects_runtime_files_binary_and_personal_contacts(distribution, name, data, reason):
    module, root = distribution
    (root / name).write_bytes(data)
    git(root, 'add', name)
    assert any(reason in failure for failure in module.scan(root)[1])


def test_distribution_rejects_symlink_without_following_it(distribution):
    module, root = distribution
    oid = subprocess.check_output(['git', '-C', str(root), 'hash-object', '-w', '--stdin'],
                                  input=b'outside-private-file').decode().strip()
    git(root, 'update-index', '--add', '--cacheinfo', f'120000,{oid},link')
    assert any('symlink' in failure for failure in module.scan(root)[1])
