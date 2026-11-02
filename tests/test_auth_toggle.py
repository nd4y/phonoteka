"""Explicit open access keeps CSRF, token scopes and the auth-on boundary."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from phonoteka import db
from phonoteka.app import create_app
from phonoteka.config import Settings
from test_backend import settings, authenticated


def test_authentication_is_default_and_configuration_is_strict(monkeypatch, settings):
    monkeypatch.delenv('AUTH_ENABLED', raising=False)
    assert Settings.from_env().auth_enabled is True
    for value, expected in [('true', True), ('1', True), ('false', False), ('0', False)]:
        monkeypatch.setenv('AUTH_ENABLED', value)
        assert Settings.from_env().auth_enabled is expected
    for value in ('', 'flase', 'no', 'disabled'):
        monkeypatch.setenv('AUTH_ENABLED', value)
        with pytest.raises(ValueError, match='AUTH_ENABLED'):
            Settings.from_env()
    with pytest.raises(ValueError, match='auth_enabled'):
        replace(settings, auth_enabled='false')


def test_open_access_needs_no_password_or_persistent_session(settings):
    cfg = replace(settings, auth_enabled=False, owner_password_file=None)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        response = client.get('/api/auth/session')
        session = response.json()
        assert session['authenticated'] and session['auth_enabled'] is False
        assert session['username'] is None and session['idle_expires_at'] is None
        assert session['absolute_expires_at'] is None and session['csrf_token']
        assert 'set-cookie' not in response.headers
        for path in ('/api/summary', '/api/tracks', '/api/jobs', '/api/tokens'):
            assert client.get(path).status_code == 200
        assert db.fetchall(cfg.db_path, 'SELECT * FROM sessions') == []
        assert db.setting(cfg.db_path, 'owner_password_hash') is None


def test_open_access_mutations_require_csrf_and_exact_origin(settings):
    cfg = replace(settings, auth_enabled=False)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        token = client.get('/api/auth/session').json()['csrf_token']
        good = {'Origin': cfg.app_url, 'X-CSRF-Token': token}
        for headers in ({}, {'Origin': cfg.app_url}, {'X-CSRF-Token': token},
                        {**good, 'Origin': 'https://attacker.example'},
                        {**good, 'Sec-Fetch-Site': 'cross-site'},
                        {**good, 'X-CSRF-Token': 'wrong'}):
            assert client.post('/api/jobs', json={'kind': 'scan'}, headers=headers).status_code == 403
        assert db.fetchall(cfg.db_path, 'SELECT * FROM jobs') == []
        response = client.post('/api/jobs', json={'kind': 'scan'},
                               headers={**good, 'Idempotency-Key': 'open-access-scan'})
        assert response.status_code == 202, response.text
        assert client.post('/api/auth/activity', json={}, headers=good).status_code == 200


def test_bearer_credentials_are_not_upgraded_to_open_access(settings):
    cfg = replace(settings, auth_enabled=False)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        csrf = client.get('/api/auth/session').json()['csrf_token']
        headers = {'Origin': cfg.app_url, 'X-CSRF-Token': csrf}
        token = client.post('/api/tokens', headers=headers,
                            json={'name': 'Read only', 'scopes': ['library:read'], 'days': 1})
        assert token.status_code == 201, token.text
        bearer = {'Authorization': 'Bearer ' + token.json()['token']}
        assert client.get('/api/tracks', headers=bearer).status_code == 200
        assert client.post('/api/jobs', json={'kind': 'scan'}, headers=bearer).status_code == 403
        assert client.get('/api/tokens', headers=bearer).status_code == 403
        assert client.get('/api/tracks', headers={'Authorization': 'Bearer invalid'}).status_code == 401


def test_open_mode_cannot_log_in_or_revoke_saved_sessions(settings):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        before = db.fetchall(settings.db_path, 'SELECT * FROM sessions')
        saved_hash = db.setting(settings.db_path, 'owner_password_hash')
    cfg = replace(settings, auth_enabled=False)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        for path in ('login', 'logout', 'logout-all'):
            response = client.post('/api/auth/' + path, json={'username': 'owner', 'password': 'irrelevant'})
            assert response.status_code == 409
        assert db.fetchall(cfg.db_path, 'SELECT * FROM sessions') == before
        assert db.setting(cfg.db_path, 'owner_password_hash') == saved_hash


def test_reenable_rejects_open_mode_csrf_and_requires_login(settings):
    cfg = replace(settings, auth_enabled=False)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        csrf = client.get('/api/auth/session').json()['csrf_token']
        cookies = dict(client.cookies)
    with TestClient(create_app(settings), base_url=settings.app_url, cookies=cookies) as client:
        assert client.get('/api/auth/session').json()['auth_enabled'] is True
        assert client.get('/api/auth/session').json()['authenticated'] is False
        assert client.get('/api/summary').status_code == 401
        assert client.post('/api/jobs', json={'kind': 'scan'}, headers={
            'Origin': cfg.app_url, 'X-CSRF-Token': csrf}).status_code == 401
        headers = authenticated(client, settings)
        assert client.get('/api/summary').status_code == 200
        assert client.post('/api/auth/logout', headers=headers).status_code == 200


def test_enabled_first_start_without_password_still_fails(settings):
    cfg = replace(settings, owner_password_file=None)
    with pytest.raises(RuntimeError, match='OWNER_PASSWORD_FILE'):
        with TestClient(create_app(cfg), base_url=cfg.app_url):
            pass
