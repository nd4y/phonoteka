"""Idle policy is driven by explicit owner input, never background reads."""
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from starlette.requests import Request

from phonoteka import db, security
from phonoteka.app import create_app
from phonoteka.config import Settings
from test_backend import settings, authenticated


@pytest.fixture
def clock(monkeypatch):
    value = [2_000_000_000.0]
    # Dependency clock only: do not change the host/HTTP cookie clock or API tokens.
    monkeypatch.setattr(security, 'time', SimpleNamespace(time=lambda: value[0]))
    return value


def activity_rows(cfg):
    return db.fetchall(cfg.db_path, 'SELECT key,value FROM settings WHERE substr(key,1,?)=?',
                       (len(security.SESSION_ACTIVITY_PREFIX), security.SESSION_ACTIVITY_PREFIX))


def test_timeout_environment_defaults_legacy_alias_and_explicit_absolute(settings, monkeypatch):
    for key in ('SESSION_HOURS', 'SESSION_IDLE_MINUTES', 'SESSION_ABSOLUTE_HOURS'):
        monkeypatch.delenv(key, raising=False)
    config = Settings.from_env()
    assert config.session_idle_seconds == 3600 and config.session_absolute_seconds == 43200
    monkeypatch.setenv('SESSION_HOURS', '8')
    monkeypatch.setenv('SESSION_IDLE_MINUTES', '90')
    assert Settings.from_env().session_absolute_seconds == 28800
    monkeypatch.setenv('SESSION_ABSOLUTE_HOURS', '6')
    config = Settings.from_env()
    assert config.session_hours == 8 and config.session_absolute_seconds == 21600
    assert config.session_idle_seconds == 5400
    for changes in ({'session_hours': 0}, {'session_hours': 721}, {'session_idle_minutes': 0},
                    {'session_idle_minutes': 1441}, {'session_absolute_hours': 0},
                    {'session_absolute_hours': 721}, {'session_idle_minutes': True}):
        with pytest.raises(ValueError):
            replace(settings, **changes)
    monkeypatch.setenv('SESSION_IDLE_MINUTES', 'not-a-number')
    with pytest.raises(ValueError):
        Settings.from_env()


def test_background_reads_do_not_extend_idle_or_write_activity(settings, clock):
    cfg = replace(settings, session_idle_minutes=1, session_absolute_hours=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        original = activity_rows(cfg)
        initial = client.get('/api/auth/session').json()
        assert initial['idle_expires_at'] == clock[0] + 60
        assert initial['absolute_expires_at'] == clock[0] + 3600
        assert initial['activity_interval_seconds'] == 30
        assert '_activity' not in initial and 'token_hash' not in initial
        start = clock[0]
        for seconds in (20, 40, 59):
            clock[0] = start + seconds
            for path in ('/api/summary', '/api/jobs', '/api/auth/session', '/api/tracks'):
                assert client.get(path).status_code == 200
            assert activity_rows(cfg) == original
        clock[0] = start + 60
        assert client.get('/api/auth/session').json()['authenticated'] is False
        assert client.get('/api/tracks').status_code == 401
        assert client.post('/api/auth/activity', json={}, headers=headers).status_code == 401
        assert activity_rows(cfg) == []
        assert authenticated(client, cfg)


def test_only_valid_explicit_activity_refreshes_idle(settings, clock):
    cfg = replace(settings, session_idle_minutes=1, session_absolute_hours=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        start = clock[0]
        clock[0] = start + 45
        touched = client.post('/api/auth/activity', json={}, headers=headers)
        assert touched.status_code == 200
        assert touched.json() == client.get('/api/auth/session').json()
        assert touched.json()['authenticated'] is True
        assert touched.json()['csrf_token'] == headers['X-CSRF-Token']
        assert touched.json()['username'] == cfg.owner_username
        assert touched.json()['idle_expires_at'] == start + 105
        assert touched.json()['absolute_expires_at'] == start + 3600
        assert 'set-cookie' not in touched.headers
        clock[0] = start + 80
        assert client.get('/api/tracks').status_code == 200
        clock[0] = start + 105
        assert client.post('/api/auth/activity', json={}, headers=headers).status_code == 401
        assert client.get('/api/auth/session').json()['authenticated'] is False


def test_event_stream_revalidation_does_not_renew_idle(settings, clock, monkeypatch):
    cfg = replace(settings, session_idle_minutes=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        queued = client.post('/api/jobs', json={'kind': 'scan'},
                             headers={**headers, 'Idempotency-Key': 'session-idle-stream'})
        assert queued.status_code == 202
        before = activity_rows(cfg)
        start = clock[0]
        calls = []

        async def disconnected(_request):
            calls.append(True)
            if len(calls) == 2:
                assert activity_rows(cfg) == before
            clock[0] = start + 30 * len(calls)
            return len(calls) > 2

        monkeypatch.setattr(Request, 'is_disconnected', disconnected)
        response = client.get('/api/jobs/' + queued.json()['id'] + '/events')
        assert response.status_code == 200 and ': connected' in response.text
        assert len(calls) == 2  # The stream closes at expiry, before disconnect.
        assert activity_rows(cfg) == []
        assert client.get('/api/tracks').status_code == 401


def test_activity_cannot_extend_absolute_deadline(settings, clock):
    cfg = replace(settings, session_idle_minutes=60, session_absolute_hours=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        response = client.post('/api/auth/login', json={'username': 'owner', 'password': 'test-strong-owner-password-123'},
                               headers={'Origin': cfg.app_url})
        assert response.status_code == 200 and 'Max-Age=3600' in response.headers['set-cookie']
        headers = {'Origin': cfg.app_url, 'X-CSRF-Token': response.json()['csrf_token']}
        start = clock[0]
        clock[0] = start + 3599
        response = client.post('/api/auth/activity', json={}, headers=headers)
        assert response.status_code == 200
        assert response.json()['idle_expires_at'] == response.json()['absolute_expires_at'] == start + 3600
        clock[0] = start + 3600
        assert client.get('/api/tracks').status_code == 401
        assert client.post('/api/auth/activity', json={}, headers=headers).status_code == 401


@pytest.mark.parametrize('observed_expiry', [False, True])
def test_increasing_idle_config_never_resurrects_expired_cookie(settings, clock, observed_expiry):
    cfg = replace(settings, session_idle_minutes=1, session_absolute_hours=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        token = client.cookies.get(security.cookie_name(cfg))
        clock[0] += 61
        if observed_expiry:
            assert client.get('/api/auth/session').json()['authenticated'] is False
    longer = replace(cfg, session_idle_minutes=120)
    old = {**headers, 'Cookie': security.cookie_name(cfg) + '=' + token}
    with TestClient(create_app(longer), base_url=longer.app_url) as client:
        assert client.get('/api/tracks', headers=old).status_code == 401
        assert client.post('/api/auth/activity', json={}, headers=old).status_code == 401
        assert authenticated(client, longer)


def test_shorter_startup_policy_stays_expired_after_later_expansion(settings, clock):
    cfg = replace(settings, session_idle_minutes=2, session_absolute_hours=2)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        authenticated(client, cfg)
        token = client.cookies.get(security.cookie_name(cfg))
    clock[0] += 61
    with TestClient(create_app(replace(cfg, session_idle_minutes=1)), base_url=cfg.app_url):
        pass  # No request needed: the tighter startup policy is persisted.
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        assert client.get('/api/tracks', headers={'Cookie': security.cookie_name(cfg) + '=' + token}).status_code == 401
        assert authenticated(client, cfg)


def test_valid_session_can_adopt_longer_idle_policy_only_after_explicit_activity(settings, clock):
    cfg = replace(settings, session_idle_minutes=1, session_absolute_hours=2)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        token = client.cookies.get(security.cookie_name(cfg))
        start = clock[0]
    clock[0] = start + 45
    longer = replace(cfg, session_idle_minutes=2)
    old = {**headers, 'Cookie': security.cookie_name(cfg) + '=' + token}
    with TestClient(create_app(longer), base_url=longer.app_url) as client:
        assert client.get('/api/auth/session', headers=old).json()['idle_expires_at'] == start + 60
        response = client.post('/api/auth/activity', json={}, headers=old)
        assert response.status_code == 200
        assert response.json()['idle_expires_at'] == start + 165
        assert response.json()['absolute_expires_at'] == start + 7200


def test_increasing_absolute_config_cannot_revive_old_session(settings, clock):
    cfg = replace(settings, session_idle_minutes=1440, session_hours=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        authenticated(client, cfg)
        token = client.cookies.get(security.cookie_name(cfg))
    clock[0] += 3601
    with TestClient(create_app(replace(cfg, session_hours=12)), base_url=cfg.app_url) as client:
        assert client.get('/api/tracks', headers={'Cookie': security.cookie_name(cfg) + '=' + token}).status_code == 401
        assert authenticated(client, cfg)


def test_activity_requires_owner_csrf_and_origin_and_does_not_touch_on_denial(settings, clock):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        before = activity_rows(settings)
        clock[0] += 15
        cases = [({'Origin': settings.app_url}, {}),
                 ({**headers, 'Origin': 'https://evil.test'}, {}),
                 ({'X-CSRF-Token': headers['X-CSRF-Token']}, {}),
                 ({**headers, 'Sec-Fetch-Site': 'cross-site'}, {})]
        for bad, body in cases:
            assert client.post('/api/auth/activity', json=body, headers=bad).status_code == 403
            assert activity_rows(settings) == before
        assert client.post('/api/auth/activity', json={'automatic': True}, headers=headers).status_code == 422
        assert activity_rows(settings) == before
        assert client.post('/api/auth/activity', json={}, headers=headers).status_code == 200


def test_activity_rechecks_session_under_lock_after_logout_race(settings, clock, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        headers = authenticated(client, settings)
        original = security.require_session
        def revoked_before_transaction(*args, **kwargs):
            active = original(*args, **kwargs)
            with db.transaction(settings.db_path) as conn:
                security.revoke_sessions(conn, active['token_hash'])
            return active
        monkeypatch.setattr(security, 'require_session', revoked_before_transaction)
        assert client.post('/api/auth/activity', json={}, headers=headers).status_code == 401
        assert activity_rows(settings) == []
        assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM sessions')['n'] == 0


def test_logout_all_password_reset_and_legacy_state_clean_metadata(settings, clock):
    with TestClient(create_app(settings), base_url=settings.app_url) as one:
        headers = authenticated(one, settings)
        with TestClient(create_app(settings), base_url=settings.app_url) as two:
            second = authenticated(two, settings)
            assert len(activity_rows(settings)) == 2
            assert one.post('/api/auth/logout', headers=headers).status_code == 200
            assert len(activity_rows(settings)) == 1
            assert two.post('/api/auth/logout-all', headers=second).status_code == 200
            assert activity_rows(settings) == []
        headers = authenticated(one, settings)
        digest = security.token_hash(one.cookies.get(security.cookie_name(settings)))
        # Existing schema2 sessions have no idle metadata after upgrading.
        with db.transaction(settings.db_path) as conn:
            conn.execute('DELETE FROM settings WHERE key=?', (security.SESSION_ACTIVITY_PREFIX + digest,))
        assert one.get('/api/auth/session').json()['authenticated'] is False
        assert one.post('/api/auth/activity', json={}, headers=headers).status_code == 401
        authenticated(one, settings)
        settings.owner_password_file.write_text('replacement-strong-password-456', encoding='utf-8')
        security.bootstrap(settings)
        assert activity_rows(settings) == []
        assert one.get('/api/tracks').status_code == 401
        response = one.post('/api/auth/login', json={'username': 'owner', 'password': 'replacement-strong-password-456'},
                            headers={'Origin': settings.app_url})
        assert response.status_code == 200


def test_password_reset_during_verification_cannot_issue_old_password_session(settings, clock, monkeypatch):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        original = security.HASHER
        reset_pending = [True]

        def verify_then_reset(hashed, password):
            verified = original.verify(hashed, password)
            if reset_pending[0]:
                reset_pending[0] = False
                settings.owner_password_file.write_text('replacement-strong-password-456', encoding='utf-8')
                security.bootstrap(settings)
            return verified

        monkeypatch.setattr(security, 'HASHER', SimpleNamespace(
            verify=verify_then_reset, hash=original.hash, check_needs_rehash=original.check_needs_rehash))
        response = client.post('/api/auth/login', json={'username': 'owner', 'password': 'test-strong-owner-password-123'},
                               headers={'Origin': settings.app_url})
        assert response.status_code == 401 and response.json()['code'] == 'invalid_credentials'
        assert db.fetchone(settings.db_path, 'SELECT COUNT(*) AS n FROM sessions')['n'] == 0
        assert activity_rows(settings) == []
        response = client.post('/api/auth/login', json={'username': 'owner', 'password': 'replacement-strong-password-456'},
                               headers={'Origin': settings.app_url})
        assert response.status_code == 200 and response.json()['authenticated'] is True


def test_session_cap_is_twenty_with_tied_clock_and_exact_prefix_cleanup(settings, clock):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        db.set_setting(settings.db_path, 'sessionXactivity:unrelated', 'preserved')
        db.set_setting(settings.db_path, security.SESSION_ACTIVITY_PREFIX + '0' * 64, '{}')
        first = None
        for _ in range(23):
            authenticated(client, settings)
            first = first or client.cookies.get(security.cookie_name(settings))
        rows = activity_rows(settings)
        sessions = db.fetchall(settings.db_path, 'SELECT token_hash FROM sessions')
        assert len(rows) == len(sessions) == 20
        assert {row['key'][len(security.SESSION_ACTIVITY_PREFIX):] for row in rows} == {row['token_hash'] for row in sessions}
        assert client.get('/api/tracks').status_code == 200
        assert client.get('/api/tracks', headers={'Cookie': security.cookie_name(settings) + '=' + first}).status_code == 401
        assert db.setting(settings.db_path, 'sessionXactivity:unrelated') == 'preserved'
        report = client.get('/api/reports/operations').text
        assert security.SESSION_ACTIVITY_PREFIX not in report
        with db.connect(settings.db_path) as conn:
            assert conn.execute('PRAGMA user_version').fetchone()[0] == 2
            db.verify_schema(conn)


def test_api_tokens_do_not_inherit_or_extend_cookie_idle_state(settings, clock):
    cfg = replace(settings, session_idle_minutes=1)
    with TestClient(create_app(cfg), base_url=cfg.app_url) as client:
        headers = authenticated(client, cfg)
        response = client.post('/api/tokens', json={'name': 'idle independent test', 'scopes': ['library:read']}, headers=headers)
        assert response.status_code == 201
        bearer = {'Authorization': 'Bearer ' + response.json()['token']}
        before = activity_rows(cfg)
        clock[0] += 30
        assert client.get('/api/summary', headers=bearer).status_code == 200
        assert activity_rows(cfg) == before
        assert client.post('/api/auth/activity', json={}, headers={**headers, **bearer}).status_code == 403
        assert activity_rows(cfg) == before
        clock[0] += 31
        assert client.get('/api/tracks').status_code == 401
        assert client.get('/api/tracks', headers=bearer).status_code == 200


@pytest.mark.parametrize('corrupt', ['not-json', '{"version":1}', '{"version":1,"created":0,"last_activity":NaN,"idle_expires":10,"auth_epoch":1}'])
def test_malformed_activity_fails_closed_and_new_login_still_works(settings, clock, corrupt):
    with TestClient(create_app(settings), base_url=settings.app_url) as client:
        authenticated(client, settings)
        digest = security.token_hash(client.cookies.get(security.cookie_name(settings)))
        db.set_setting(settings.db_path, security.SESSION_ACTIVITY_PREFIX + digest, corrupt)
        assert client.get('/api/auth/session').json()['authenticated'] is False
        assert authenticated(client, settings)
