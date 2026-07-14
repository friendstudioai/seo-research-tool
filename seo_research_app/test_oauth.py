"""Tests for Google OAuth flow — refresh token, PKCE, and disconnect."""

import os, json, sys, unittest
from unittest.mock import patch, MagicMock

# Set test env before importing app
os.environ['APP_MODE'] = 'test'
os.environ['FIRECRAWL_API_KEY'] = 'test-key'
os.environ['FLASK_SECRET_KEY'] = 'test-secret-key-32-chars-at-least!!'
os.environ['GOOGLE_OAUTH_CLIENT_ID'] = 'test-client-id.apps.googleusercontent.com'
os.environ['GOOGLE_OAUTH_CLIENT_SECRET'] = 'test-client-secret'
os.environ['GOOGLE_OAUTH_REDIRECT_URI'] = 'https://localhost/callback'
os.environ['TOKEN_ENCRYPTION_KEY'] = ''  # no encryption for tests
os.environ['DATABASE_URL'] = ''  # use SQLite

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as flask_app

app = flask_app.app
db = flask_app.db
User = flask_app.User
GoogleOAuthCredentials = flask_app.GoogleOAuthCredentials
SelectedSheet = flask_app.SelectedSheet


class OAuthTestBase(unittest.TestCase):
    """Base test class with app, client, and DB setup."""

    def setUp(self):
        app.config['TESTING'] = True
        app.config['SESSION_COOKIE_SECURE'] = False
        self.ctx = app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _mock_flow(self, has_refresh_token=True):
        """Create a mock Flow with fake credentials."""
        flow = MagicMock()
        flow.credentials = MagicMock()
        flow.credentials.token = 'ya29.fake-access-token'
        flow.credentials.refresh_token = '1//fake-refresh-token' if has_refresh_token else None
        flow.credentials.scopes = [
            'openid', 'email', 'profile',
            'https://www.googleapis.com/auth/drive.file'
        ]
        flow.credentials.expiry = None
        flow.credentials.valid = True
        return flow

    def _setup_session(self, state='test_state', verifier='test_verifier'):
        with self.client.session_transaction() as sess:
            sess['google_oauth_state'] = state
            sess['google_oauth_code_verifier'] = verifier

    def _callback_url(self, state='test_state', code='test_auth_code'):
        return f'/auth/google/callback?state={state}&code={code}'


# ──────────────────────────────────────────
# /auth/google/start
# ──────────────────────────────────────────

class TestStart(OAuthTestBase):

    def test_redirects_to_google(self):
        """Should redirect and save state + code_verifier in session."""
        with patch.object(flask_app.Flow, 'authorization_url',
                          return_value=(
                              'https://accounts.google.com/o/oauth2/auth?response_type=code'
                              '&client_id=test&scope=openid+email+profile+drive.file'
                              '&access_type=offline&prompt=consent&include_granted_scopes=true'
                              '&state=fakestate123', 'fakestate123')):
            resp = self.client.get('/auth/google/start')

        self.assertEqual(resp.status_code, 302)
        url = resp.headers['Location']
        self.assertIn('access_type=offline', url)
        self.assertIn('prompt=consent', url)
        self.assertIn('include_granted_scopes=true', url)

        with self.client.session_transaction() as sess:
            self.assertIn('google_oauth_state', sess)
            self.assertIn('google_oauth_code_verifier', sess)
            self.assertGreater(len(sess['google_oauth_state']), 10)
            self.assertGreater(len(sess['google_oauth_code_verifier']), 10)

    def test_authorization_url_params(self):
        """Verify exact params passed to Flow.authorization_url."""
        from google_auth_oauthlib.flow import Flow as RealFlow
        captured = {}

        def mock_auth_url(self, **kwargs):
            captured.update(kwargs)
            return ('https://accounts.google.com/o/oauth2/auth?state=fake', 'fakestate')

        with patch.object(RealFlow, 'authorization_url', mock_auth_url):
            self.client.get('/auth/google/start')

        self.assertEqual(captured.get('access_type'), 'offline')
        self.assertEqual(captured.get('prompt'), 'consent')
        self.assertEqual(captured.get('include_granted_scopes'), 'true')

    def test_sets_session_keys(self):
        """After start, session has state and code_verifier."""
        with patch.object(flask_app.Flow, 'authorization_url',
                          return_value=('https://accounts.google.com/o/oauth2/auth?state=fake', 'fakestate')):
            self.client.get('/auth/google/start')

        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get('google_oauth_state'))
            self.assertTrue(sess.get('google_oauth_code_verifier'))


# ──────────────────────────────────────────
# /auth/google/callback
# ──────────────────────────────────────────

class TestCallback(OAuthTestBase):

    @patch('requests.get')
    def test_saves_new_refresh_token(self, mock_get):
        """Callback saves refresh_token when Google returns one."""
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            'sub': 'google_sub_123', 'email': 'test@example.com',
            'name': 'Test User', 'picture': ''
        }
        flow = self._mock_flow(has_refresh_token=True)
        self._setup_session()

        with patch.object(flask_app.Flow, 'from_client_config', return_value=flow):
            resp = self.client.get(self._callback_url())

        self.assertEqual(resp.status_code, 302)
        user = db.session.query(User).filter_by(google_sub='google_sub_123').first()
        self.assertIsNotNone(user, "User should be created")
        oauth = db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).first()
        self.assertIsNotNone(oauth, "OAuth credentials should be created")
        self.assertNotEqual(oauth.encrypted_refresh_token, '')
        self.assertIn('fake-refresh-token', oauth.encrypted_refresh_token)

    @patch('requests.get')
    def test_preserves_existing_refresh_token(self, mock_get):
        """When Google returns no new refresh_token, existing one is preserved."""
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            'sub': 'google_sub_456', 'email': 'existing@example.com',
            'name': 'Existing User', 'picture': ''
        }
        # Pre-create user with a refresh token
        user = User(google_sub='google_sub_456', email='existing@example.com',
                    display_name='Existing User')
        db.session.add(user)
        db.session.flush()
        oauth = GoogleOAuthCredentials(
            user_id=user.id,
            encrypted_refresh_token='encrypted-old-refresh-token',
            encrypted_access_token='encrypted-old-access-token')
        db.session.add(oauth)
        db.session.commit()

        flow = self._mock_flow(has_refresh_token=False)
        self._setup_session()

        with patch.object(flask_app.Flow, 'from_client_config', return_value=flow):
            resp = self.client.get(self._callback_url())

        self.assertEqual(resp.status_code, 302)
        oauth = db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).first()
        self.assertIsNotNone(oauth)
        self.assertEqual(oauth.encrypted_refresh_token, 'encrypted-old-refresh-token',
                         "Existing refresh token should NOT be overwritten")

    @patch('requests.get')
    def test_new_user_no_refresh_does_not_crash(self, mock_get):
        """First-time connection without refresh_token doesn't crash."""
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            'sub': 'google_sub_no_refresh', 'email': 'norefresh@example.com',
            'name': 'No Refresh', 'picture': ''
        }
        flow = self._mock_flow(has_refresh_token=False)
        self._setup_session()

        with patch.object(flask_app.Flow, 'from_client_config', return_value=flow):
            resp = self.client.get(self._callback_url())

        self.assertEqual(resp.status_code, 302)
        user = db.session.query(User).filter_by(google_sub='google_sub_no_refresh').first()
        self.assertIsNotNone(user)
        oauth = db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).first()
        self.assertIsNotNone(oauth)
        # Refresh token may be empty string or None, but DB commit should succeed

    @patch('requests.get')
    def test_does_not_overwrite_refresh_with_empty(self, mock_get):
        """Pre-existing refresh token not overwritten when Google returns None."""
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            'sub': 'google_sub_overwrite', 'email': 'overwrite@example.com',
            'name': 'Overwrite Test', 'picture': ''
        }
        # Pre-create with a real refresh token
        user = User(google_sub='google_sub_overwrite', email='overwrite@example.com',
                    display_name='Overwrite Test')
        db.session.add(user)
        db.session.flush()
        oauth = GoogleOAuthCredentials(
            user_id=user.id,
            encrypted_refresh_token='encrypted-real-refresh-token',
            encrypted_access_token='encrypted-old-access-token')
        db.session.add(oauth)
        db.session.commit()

        flow = self._mock_flow(has_refresh_token=False)
        self._setup_session()

        with patch.object(flask_app.Flow, 'from_client_config', return_value=flow):
            resp = self.client.get(self._callback_url())

        self.assertEqual(resp.status_code, 302)
        oauth = db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).first()
        self.assertEqual(oauth.encrypted_refresh_token, 'encrypted-real-refresh-token',
                         "Refresh token was overwritten!")
        # Access token should be updated
        self.assertIn('fake-access-token', oauth.encrypted_access_token)


# ──────────────────────────────────────────
# /auth/google/disconnect
# ──────────────────────────────────────────

class TestDisconnect(OAuthTestBase):

    @patch('requests.get')
    def test_only_current_user_deleted(self, mock_get):
        """Disconnect deletes only the current user's data."""
        user_a = User(google_sub='sub_a', email='a@example.com')
        user_b = User(google_sub='sub_b', email='b@example.com')
        db.session.add_all([user_a, user_b])
        db.session.flush()
        oauth_a = GoogleOAuthCredentials(user_id=user_a.id,
                                         encrypted_refresh_token='rt_a',
                                         encrypted_access_token='at_a')
        oauth_b = GoogleOAuthCredentials(user_id=user_b.id,
                                         encrypted_refresh_token='rt_b',
                                         encrypted_access_token='at_b')
        db.session.add_all([oauth_a, oauth_b])
        sheet_a = SelectedSheet(user_id=user_a.id, spreadsheet_id='sheet_a',
                                spreadsheet_name='Sheet A', spreadsheet_url='url_a')
        sheet_b = SelectedSheet(user_id=user_b.id, spreadsheet_id='sheet_b',
                                spreadsheet_name='Sheet B', spreadsheet_url='url_b')
        db.session.add_all([sheet_a, sheet_b])
        db.session.commit()

        # Log in as user A
        with self.client.session_transaction() as sess:
            sess['user_id'] = user_a.id
            sess.permanent = True

        with patch('requests.post') as mock_post:
            mock_post.return_value.ok = True
            with patch.object(flask_app, 'get_user_credentials') as mock_creds:
                mock_creds.return_value = MagicMock(token='fake-token-a')
                self.client.post('/auth/google/disconnect')

        # User B should still exist
        self.assertIsNotNone(
            db.session.query(User).filter_by(google_sub='sub_b').first())
        self.assertIsNotNone(
            db.session.query(GoogleOAuthCredentials).filter_by(user_id=user_b.id).first())
        self.assertIsNotNone(
            db.session.query(SelectedSheet).filter_by(user_id=user_b.id).first())
        # User A should be gone
        self.assertIsNone(
            db.session.query(User).filter_by(google_sub='sub_a').first())

    def test_clears_session(self):
        """After disconnect, session clear → /api/google/status shows disconnected."""
        user = User(google_sub='sub_disconnect', email='dis@example.com')
        db.session.add(user)
        db.session.flush()
        oauth = GoogleOAuthCredentials(user_id=user.id,
                                       encrypted_refresh_token='rt',
                                       encrypted_access_token='at')
        db.session.add(oauth)
        db.session.commit()

        with self.client.session_transaction() as sess:
            sess['user_id'] = user.id

        with patch('requests.post') as mock_post:
            mock_post.return_value.ok = True
            with patch.object(flask_app, 'get_user_credentials') as mock_creds:
                mock_creds.return_value = MagicMock(token='fake-token')
                self.client.post('/auth/google/disconnect')

        resp = self.client.get('/api/google/status')
        data = resp.get_json()
        self.assertIs(data['connected'], False)


# ──────────────────────────────────────────
# /health
# ──────────────────────────────────────────

class TestHealth(OAuthTestBase):

    def test_health_returns_200(self):
        resp = self.client.get('/health')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {'status': 'ok'})


# ──────────────────────────────────────────
# /api/google/status
# ──────────────────────────────────────────

class TestApiStatus(OAuthTestBase):

    @patch('requests.get')
    def test_connected_after_callback(self, mock_get):
        """After successful callback, status returns connected=true."""
        mock_get.return_value.ok = True
        mock_get.return_value.json.return_value = {
            'sub': 'status_test_sub', 'email': 'status@example.com',
            'name': 'Status Test', 'picture': ''
        }
        flow = self._mock_flow(has_refresh_token=True)
        self._setup_session()

        with patch.object(flask_app.Flow, 'from_client_config', return_value=flow):
            self.client.get(self._callback_url())

        resp = self.client.get('/api/google/status')
        data = resp.get_json()
        self.assertIs(data['connected'], True)
        self.assertEqual(data['email'], 'status@example.com')
        self.assertEqual(data['name'], 'Status Test')

    def test_not_connected_by_default(self):
        """Before any OAuth, status returns connected=false."""
        resp = self.client.get('/api/google/status')
        data = resp.get_json()
        self.assertIs(data['connected'], False)


# ──────────────────────────────────────────
# Session state preservation
# ──────────────────────────────────────────

class TestSessionState(OAuthTestBase):

    def test_state_preserved_across_requests(self):
        """State and verifier survive across requests in the same session."""
        import secrets as s
        real_state = s.token_urlsafe(32)
        real_verifier = s.token_urlsafe(64)
        with self.client.session_transaction() as sess:
            sess['google_oauth_state'] = real_state
            sess['google_oauth_code_verifier'] = real_verifier

        with self.client.session_transaction() as sess:
            self.assertEqual(sess['google_oauth_state'], real_state)
            self.assertEqual(sess['google_oauth_code_verifier'], real_verifier)


if __name__ == '__main__':
    unittest.main()
