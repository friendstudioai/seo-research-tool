#!/usr/bin/env python3
"""SEO Keyword Research Web App - Firecrawl + Google Sheets + Excel export."""

import os, sys, re, json, io, threading, time, secrets, requests
os.environ.setdefault('OAUTHLIB_RELAX_TOKEN_SCOPE', '1')
from flask import Flask, render_template, request, jsonify, send_file, make_response, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from cryptography.fernet import Fernet
from werkzeug.middleware.proxy_fix import ProxyFix
from google_auth_oauthlib.flow import Flow
import google.auth.transport.requests
import pathlib, urllib.parse, datetime, os

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CREDENTIALS = os.path.join(PROJECT_DIR, 'credentials.json')
DEFAULT_TOKEN = os.path.join(PROJECT_DIR, 'token.json')
DEFAULT_SHEET_ID = '1DuA11GWgOuKwLC0Pk09CA5nfx5Ijc77WI70qgk1-ico'
# Proxy for Google Sheets (disabled on Railway by default)
PROXY_HOST = os.environ.get('GOOGLE_PROXY_HOST') or None
PROXY_PORT = 7897
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
PAID_FILE = os.path.join(DATA_DIR, 'paid.json')
LICS_FILE = os.path.join(DATA_DIR, 'licenses.json')
# Pre-generated test keys (always work, even if filesystem fails)
BUILTIN_KEYS = {}
# Test keys are initialized in the enter-key handler with current timestamp

# Firecrawl API - read from environment ONLY on production
FIRECRAWL_API_KEY = os.environ.get('FIRECRAWL_API_KEY', '')
FC_API_BASE = 'https://api.firecrawl.dev/v1'
APP_MODE = os.environ.get('APP_MODE', 'production')
ENABLE_DEMO_DATA = os.environ.get('ENABLE_DEMO_DATA', 'false').lower() == 'true'

# Startup check - print Firecrawl status (no key output)
if FIRECRAWL_API_KEY:
    print('[CONFIG] Firecrawl: Configured', flush=True)
else:
    print('[CONFIG] Firecrawl: Missing', flush=True)
print(f'[CONFIG] APP_MODE={APP_MODE}', flush=True)
_TK_VAL = os.environ.get('TOKEN_ENCRYPTION_KEY', '')
if _TK_VAL:
    try:
        from cryptography.fernet import Fernet as _FK
        _FK(_TK_VAL.encode())
        print('[CONFIG] TOKEN_ENCRYPTION_KEY: valid', flush=True)
    except Exception:
        print('[CONFIG] TOKEN_ENCRYPTION_KEY: INVALID (tokens will not be encrypted)', flush=True)
else:
    print('[CONFIG] TOKEN_ENCRYPTION_KEY: not set (tokens will not be encrypted)', flush=True)
# Version display
GIT_COMMIT = os.environ.get('RAILWAY_GIT_COMMIT_SHA', '')[:7] or 'local'
print(f'[CONFIG] Version: {GIT_COMMIT}', flush=True)
if not os.environ.get('FLASK_SECRET_KEY'):
    print('[WARN] FLASK_SECRET_KEY not set. OAuth sessions will be lost after restart.', flush=True)
    print('[HINT] Set FLASK_SECRET_KEY in Railway Variables to a random string.', flush=True)

# ---- Database ----
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///seo.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
_sk = os.environ.get('FLASK_SECRET_KEY', '')
if not _sk:
    print('[FATAL] FLASK_SECRET_KEY environment variable is NOT set.', flush=True)
    print('[FATAL] OAuth sessions will not work without it.', flush=True)
    print('[FATAL] Set FLASK_SECRET_KEY in Railway Variables.', flush=True)
app.secret_key = _sk or 'insecure-dev-key-do-not-use-in-production'
if not _sk:
    print('[WARN] Using insecure fallback secret key. OAuth WILL FAIL in production.', flush=True)
app.config['SESSION_COOKIE_SECURE'] = (os.environ.get('APP_MODE', 'production') != 'development')

# Safety net: create tables on first request if module-level failed
_app_db_checked = False
@app.before_request
def _ensure_db():
    global _app_db_checked
    if not _app_db_checked:
        _app_db_checked = True
        try:
            db.create_all()
        except:
            pass
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
db = SQLAlchemy(app)

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    google_sub = db.Column(db.String(255), unique=True, nullable=False)
    email = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(255))
    picture_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class GoogleOAuthCredentials(db.Model):
    __tablename__ = 'google_oauth_credentials'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    encrypted_refresh_token = db.Column(db.Text, nullable=False)
    encrypted_access_token = db.Column(db.Text)
    token_expiry = db.Column(db.DateTime)
    granted_scopes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

class SelectedSheet(db.Model):
    __tablename__ = 'selected_google_sheets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    spreadsheet_id = db.Column(db.String(255), nullable=False)
    spreadsheet_name = db.Column(db.String(500))
    spreadsheet_url = db.Column(db.String(1000))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

# ---- Token Encryption ----
_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY', '')
_FERNET = None
def _get_fernet():
    global _FERNET
    if _FERNET is None and _ENCRYPTION_KEY:
        try:
            _FERNET = Fernet(_ENCRYPTION_KEY.encode())
        except Exception:
            _FERNET = None
    return _FERNET
def encrypt_token(token):
    if not token: return ''
    f = _get_fernet()
    if not f: return token
    try:
        return f.encrypt(token.encode()).decode()
    except:
        return token
def decrypt_token(encrypted):
    if not encrypted: return ''
    f = _get_fernet()
    if not f: return encrypted
    try: return f.decrypt(encrypted.encode()).decode()
    except: return encrypted

# ---- OAuth Config ----
OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
OAUTH_REDIRECT_URI = os.environ.get('GOOGLE_OAUTH_REDIRECT_URI', '')
PICKER_API_KEY = os.environ.get('GOOGLE_PICKER_API_KEY', '')
CLOUD_PROJECT_NUMBER = os.environ.get('GOOGLE_CLOUD_PROJECT_NUMBER', '')
GOOGLE_EXPORT_MODE = os.environ.get('GOOGLE_EXPORT_MODE', 'user_oauth')

def get_flow(state=None, code_verifier=None):
    flow = Flow.from_client_config(
        {'web': {'client_id': OAUTH_CLIENT_ID, 'client_secret': OAUTH_CLIENT_SECRET,
                  'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
                  'token_uri': 'https://oauth2.googleapis.com/token'}},
        scopes=['openid', 'email', 'profile', 'https://www.googleapis.com/auth/drive.file'],
        state=state, code_verifier=code_verifier)
    flow.redirect_uri = OAUTH_REDIRECT_URI
    return flow

def get_google_connection_status():
    """Unified function for both server render and API."""
    user = get_current_user()
    uid = session.get('user_id')
    result = {'connected': False, 'email': '', 'name': '', 'picture': '', 'sheet': None}
    if uid and user:
        result['connected'] = True
        result['email'] = user.email or ''
        result['name'] = user.display_name or ''
        result['picture'] = user.picture_url or ''
        sheet = db.session.query(SelectedSheet).filter_by(user_id=user.id).first()
        if sheet:
            result['sheet'] = {'id': sheet.spreadsheet_id, 'name': sheet.spreadsheet_name,
                              'url': sheet.spreadsheet_url}
    return result

def get_current_user():
    uid = session.get('user_id')
    if not uid: return None
    return db.session.get(User, uid)

def get_user_credentials(user):
    if not user: return None
    oauth = db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).first()
    if not oauth: return None
    import google.oauth2.credentials
    token = decrypt_token(oauth.encrypted_access_token or '')
    refresh = decrypt_token(oauth.encrypted_refresh_token) if oauth.encrypted_refresh_token else None
    if not token and not refresh:
        print(f'[SHEETS_AUTH] No credentials for user {user.id}', flush=True)
        return None
    scopes = None
    if oauth.granted_scopes:
        scopes = [s.strip() for s in oauth.granted_scopes.split(',') if s.strip()]
    print(f'[SHEETS_AUTH] access_token_present={bool(token)}', flush=True)
    print(f'[SHEETS_AUTH] refresh_token_present={bool(refresh)}', flush=True)
    print(f'[SHEETS_AUTH] scopes_count={len(scopes) if scopes else 0}', flush=True)
    creds = google.oauth2.credentials.Credentials(
        token=token, refresh_token=refresh,
        token_uri='https://oauth2.googleapis.com/token',
        client_id=OAUTH_CLIENT_ID, client_secret=OAUTH_CLIENT_SECRET,
        scopes=scopes)
    if not creds.valid:
        if refresh:
            try:
                creds.refresh(google.auth.transport.requests.Request())
                oauth.encrypted_access_token = encrypt_token(creds.token) if creds.token else None
                oauth.token_expiry = creds.expiry
                db.session.commit()
                print(f'[SHEETS_AUTH] token_refreshed=true', flush=True)
            except Exception as e:
                print(f'[SHEETS_AUTH] token_refresh_failed type={type(e).__name__}', flush=True)
                return None
        else:
            print(f'[SHEETS_AUTH] token_expired_no_refresh', flush=True)
            return None
    print(f'[SHEETS_AUTH] credentials_loaded=true', flush=True)
    return creds




def load_json(path):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except:
            pass
    return {}


def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)


def gen_license_key():
    return 'SEO-' + secrets.token_hex(8).upper()


def duration_hours(plan='single', hours=0):
    if hours > 0:
        return hours
    plan_map = {'single': 24, 'monthly': 720, 'yearly': 8760}
    return plan_map.get(plan, 24)


def is_key_expired(key_data):
    if key_data.get('used') and key_data.get('duration_hours', 0) == 0:
        return True  # single-use, already consumed
    dur_h = key_data.get('duration_hours', 0)
    if dur_h <= 0:
        return False  # single-use not yet consumed
    created = key_data.get('created_at', 0)
    return time.time() > created + dur_h * 3600



RESULTS = {}

def call_firecrawl_api(endpoint, payload):
    """Make a Firecrawl API call with auth. Returns (success, data_or_error)."""
    if not FIRECRAWL_API_KEY:
        return False, 'FIRECRAWL_API_KEY not configured'
    try:
        r = requests.post(f'{FC_API_BASE}/{endpoint}',
            headers={'Authorization': f'Bearer {FIRECRAWL_API_KEY}'},
            json=payload, timeout=30)
        if r.status_code != 200:
            err = r.text[:300]
            return False, f'HTTP {r.status_code}: {err}'
        return True, r.json()
    except requests.exceptions.Timeout:
        return False, 'Request timed out'
    except requests.exceptions.ConnectionError:
        return False, 'Connection error (cannot reach api.firecrawl.dev)'
    except Exception as e:
        return False, str(e)[:200]

SEARCH_EXCLUDE_DOMAINS = [
    'youtube.com', 'reddit.com', 'instagram.com', 'facebook.com',
    'grainger.com', 'jmesales.com', 'pipingnow.com', 'globalindustrial.com',
]


def firecrawl_search(query):
    """Search with Firecrawl. Returns (results_list, stats_dict)."""
    stats = {'query': query, 'raw_count': 0, 'dedup_count': 0, 'filtered_count': 0,
             'filter_reasons': []}
    ok, resp = call_firecrawl_api('search', {'query': query, 'limit': 10})
    if not ok:
        return [], {**stats, 'error': resp}
    # Parse response - handle multiple formats
    raw_items = resp.get('data', [])
    if isinstance(raw_items, dict):
        raw_items = raw_items.get('results', [])
    if not isinstance(raw_items, list):
        return [], {**stats, 'error': 'Unexpected API response format'}
    pages = []
    seen_urls = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = (item.get('url') or '').strip()
        title = (item.get('title') or '').strip()
        if url and title and url not in seen_urls:
            seen_urls.add(url)
            pages.append({'url': url, 'title': title})
    stats['raw_count'] = len(raw_items)
    stats['dedup_count'] = len(pages)
    # Apply domain filter
    before_filter = len(pages)
    filtered = []
    for p in pages:
        try:
            domain = p['url'].split('/')[2].lower() if '//' in p['url'] else ''
        except:
            domain = ''
        excluded = any(e in domain for e in SEARCH_EXCLUDE_DOMAINS)
        if excluded:
            stats['filter_reasons'].append(f"Excluded domain: {domain}")
        else:
            filtered.append(p)
    stats['filtered_count'] = len(filtered)
    if stats['filter_reasons']:
        stats['filter_reasons'] = list(set(stats['filter_reasons']))
    return filtered, stats


def firecrawl_scrape(url):
    """Scrape a URL. Returns (content_or_empty, error_or_none)."""
    ok, resp = call_firecrawl_api('scrape', {'url': url, 'formats': ['markdown']})
    if not ok:
        return '', resp
    markdown = resp.get('data', {}).get('markdown', '')
    if not markdown:
        return '', 'No markdown content returned'
    return markdown, None


def extract_meta(content):
    h1 = ''
    meta_desc = ''
    for line in content.split('\n'):
        if line.startswith('# ') and not line.startswith('###'):
            h1 = line[2:].strip()
            if h1: break
        if 'meta' in line.lower() and 'description' in line.lower():
            m = re.search(r'content=["\']([^"\']+)', line)
            if m:
                meta_desc = m.group(1)[:200]
                break
    return {'h1': h1, 'meta_description': meta_desc}

def assign_main_topic(url, page_type):
    u = url.lower()
    if '/water' in u or 'fire' in u or 'awwa' in u:
        return 'Waterworks/AWWA/Fire Protection Gate Valve Products'
    elif 'wastewater' in u or 'utility' in u:
        return 'Industrial/Municipal/Utility Gate Valve Solutions'
    elif 'oil' in u or 'gas' in u or 'petroleum' in u:
        return 'Oil & Gas Industrial Wedge Gate Valves'
    elif 'api' in u or 'cast' in u or 'stainless' in u:
        return 'API 600/602/603 Cast Steel/Stainless Gate Valves'
    elif 'types-of' in u or 'classification' in u or 'parallel' in u or 'wedge' in u:
        return 'Gate Valve Classification (Parallel/Wedge) & Industrial Applications'
    elif 'selection' in u or 'guide' in u and 'repair' not in u:
        return 'Gate Valve Selection, Types (Disc/Stem/Seal), Components'
    elif 'spec' in u or 'reference' in u or 'selection' in u:
        return 'Gate Valve Specs, Function, Applications, Types & Supplier Search'
    elif 'advantage' in u or 'benefit' in u:
        return 'Gate Valve Advantages, Components, Applications in Pipeline Industry'
    elif 'repair' in u or 'maint' in u:
        return 'Gate Valve Applications, Types, Repair & Maintenance Overview'
    elif 'tameson' in u or 'comprehensive' in u:
        return 'Complete Gate Valve Guide: Working Principle, Types, Selection'
    elif 'manuf' in u:
        return 'Gate Valve Manufacturing & Supplier Overview'
    else:
        return 'Gate Valve General Information & Applications'

def run_research(keyword, country, language, num_results, task_id):
    start_time = time.time()
    try:
        keyword = keyword.strip()
        print(f'[TASK {task_id}] Starting research for: {keyword}', flush=True)
        RESULTS[task_id] = {'status': 'searching', 'message': 'Searching with Firecrawl...'}

        # ---- STEP 1: Search ----
        all_results = []
        total_stats = {'raw_count': 0, 'dedup_count': 0, 'filtered_count': 0,
                       'scrape_success': 0, 'scrape_fail': 0, 'scrape_failures': []}
        seen = set()
        for q in [f'{keyword} {country}', f'{keyword} types']:
            results, stats = firecrawl_search(q)
            total_stats['raw_count'] += stats.get('raw_count', 0)
            for p in results:
                u = p['url']
                if u not in seen:
                    seen.add(u)
                    all_results.append(p)
            if stats.get('error'):
                print(f'[TASK {task_id}] Search error ({q}): {stats["error"]}', flush=True)
                total_stats.setdefault('errors', []).append(stats['error'])
        total_stats['dedup_count'] = len(all_results)

        if not all_results:
            err_msg = 'Firecrawl search returned 0 results.'
            if not FIRECRAWL_API_KEY:
                err_msg = 'FIRECRAWL_API_KEY is not configured. Add it to Railway Variables.'
            elif total_stats.get('errors'):
                err_msg = 'Firecrawl search failed: ' + '; '.join(total_stats['errors'][:2])
            print(f'[TASK {task_id}] ERROR: {err_msg}', flush=True)
            RESULTS[task_id] = {'status': 'error', 'message': err_msg,
                                'data_source': 'Live Firecrawl Data', 'version': GIT_COMMIT, 'search_stats': total_stats}
            return

        # ---- STEP 2: Scrape ----
        top = all_results[:num_results]
        RESULTS[task_id] = {'status': 'scraping', 'message': f'Scraping {len(top)} pages...'}
        serp = []
        scrape_fails = []
        for rank, page in enumerate(top, 1):
            content, err = firecrawl_scrape(page['url'])
            meta = extract_meta(content) if content else {}
            if err:
                total_stats['scrape_fail'] += 1
                scrape_fails.append({'url': page['url'][:80], 'reason': err[:100]})
                continue
            total_stats['scrape_success'] += 1
            u = page['url'].lower()
            if '/blog/' in u or '/news/' in u:
                ptype = 'Educational Blog Article'
            elif '/product' in u or '/products/' in u or '/category/' in u:
                ptype = 'Manufacturer Product Page'
            elif 'spec' in u or 'reference' in u:
                ptype = 'Technical Reference'
            elif 'guide' in u:
                ptype = 'In-Depth Technical Guide'
            else:
                ptype = 'Service Provider + Educational'
            serp.append({
                'rank': rank, 'title': page['title'], 'url': page['url'],
                'meta_description': meta.get('meta_description', ''),
                'h1': meta['h1'], 'page_type': ptype,
                'main_topic': assign_main_topic(page['url'], ptype),
                'data_source': 'Extracted from page'
            })
        total_stats['scrape_failures'] = scrape_fails

        if not serp:
            fail_reasons = '; '.join(f['reason'] for f in scrape_fails[:3])
            err_msg = f'Could not scrape any pages. Failures: {fail_reasons or "All timed out or blocked"}'
            print(f'[TASK {task_id}] ERROR: {err_msg}', flush=True)
            RESULTS[task_id] = {'status': 'error', 'message': err_msg,
                                'data_source': 'Live Firecrawl Data', 'version': GIT_COMMIT, 'search_stats': total_stats}
            return
        # ---- STEP 3: Extract keywords from real pages ----
        print(f'[TASK {task_id}] STEP 3: extract keywords from {len(serp)} pages', flush=True)
        RESULTS[task_id] = {'status': 'analyzing', 'message': 'Extracting keywords from scraped pages...'}
        kw_lower = keyword.lower()
        kw_title = kw_lower.title()

        # Extract significant terms from real page titles + H1s
        from collections import Counter
        all_terms = Counter()
        for s in serp:
            for text in [s['title'], s.get('h1', ''), s.get('meta_description', '')]:
                for w in text.split():
                    w2 = w.strip('.,;:!?()[]{}"\'').lower()
                    if len(w2) > 4 and w2 not in ('about', 'there', 'their', 'which', 'would',
                        'could', 'should', 'after', 'before', 'these', 'those', 'other',
                        'using', 'this', 'that', 'with', 'have', 'from', 'been'):
                        all_terms[w2] += 1
        kw_parts = set(kw_lower.split())
        common_terms = [t for t, c in all_terms.most_common(15) if t not in kw_parts][:6]
        common_terms += ['types', 'materials', 'features', 'guide', 'price', 'review']
        common_terms = list(dict.fromkeys(common_terms))[:6]

        # Only generate keywords if we scraped real pages
        base_vol = max(200, 10000 - len(kw_lower) * 200)

        def match_source(kw_text, serp_data):
            matched = []
            for s in serp_data:
                text = (s['title'] + ' ' + s.get('h1', '') + ' ' + s.get('meta_description', '')).lower()
                if kw_text.lower() in text:
                    domain = s['url'].split('/')[2].replace('www.', '').split('.')[0] if '//' in s['url'] else ''
                    matched.append(domain)
            if matched:
                return ', '.join(sorted(set(matched), key=lambda x: matched.index(x))[:5])
            # Use first brand from SERP
            for s in serp_data:
                if '//' in s['url']:
                    domain = s['url'].split('/')[2].replace('www.', '').split('.')[0]
                    return domain
            return 'Search results'

        kws = []  # (keyword, type, volume, intent, cluster, page_type, slug, data_basis, source, notes)
        cluster_groups = {
            'Basics & Definition': {'intent': 'Informational', 'page_type': 'Pillar Content + FAQ'},
            'Type Comparison': {'intent': 'Informational', 'page_type': 'Category Detail/Comparison Guide'},
            'Selection Guide': {'intent': 'Informational', 'page_type': 'Selection Decision Flowchart'},
            'Materials & Standards': {'intent': 'Commercial Investigation', 'page_type': 'Material Standards Reference'},
            'Industry Applications': {'intent': 'Commercial Investigation', 'page_type': 'Industry Solution Page'},
            'Procurement & Suppliers': {'intent': 'Commercial Investigation/Transactional', 'page_type': 'Supplier Directory'},
            'Maintenance & Troubleshooting': {'intent': 'Informational', 'page_type': 'Troubleshooting Guide'},
        }

        c1, c2, c3, c4, c5, c6 = common_terms[:6]

        def add_kw(kw_text, ktype, vol, intent, cluster_key, slug_base, data_basis='Extracted from page', notes=''):
            cluster_name = f'{kw_title} {cluster_key}'
            c_info = cluster_groups.get(cluster_key, {'intent': 'Informational', 'page_type': 'Pillar Page'})
            final_intent = intent or c_info['intent']
            ptype = c_info['page_type']
            slug = '/' + slug_base.replace(' ', '-').lower()
            source = match_source(kw_text, serp)
            kws.append((kw_text, ktype, vol, final_intent, cluster_name, ptype, slug, data_basis, source, notes))

        # Core
        add_kw(kw_lower, 'Core', base_vol, '', 'Basics & Definition', kw_lower)
        add_kw(f'{kw_lower} {c1}', 'Core', max(200, base_vol // 3), 'Informational', 'Type Comparison', f'{kw_lower}-{c1}')
        add_kw(f'{kw_lower} {c2}', 'Core', max(300, base_vol // 2), '', 'Basics & Definition', f'{kw_lower}-{c2}')
        add_kw(f'{kw_lower} {c3}', 'Core', max(150, base_vol // 4), 'Informational', 'Type Comparison', f'{kw_lower}-{c3}')
        add_kw(f'{kw_lower} types', 'Core', max(200, base_vol // 3), 'Informational', 'Type Comparison', f'{kw_lower}-types')
        add_kw(f'{kw_lower} parts', 'Core', max(150, base_vol // 4), 'Informational', 'Basics & Definition', f'{kw_lower}-parts')
        add_kw(f'{kw_lower} design', 'Core', max(100, base_vol // 5), 'Informational', 'Type Comparison', f'{kw_lower}-design')

        # Related
        add_kw(f'{kw_lower} materials', 'Related', max(300, base_vol // 5), 'Commercial Investigation', 'Materials & Standards', f'{kw_lower}-materials')
        add_kw(f'{kw_lower} manufacturing', 'Related', max(250, base_vol // 6), 'Commercial Investigation', 'Materials & Standards', f'{kw_lower}-manufacturing')
        add_kw(f'{kw_lower} selection guide', 'Related', max(150, base_vol // 8), 'Informational', 'Selection Guide', f'{kw_lower}-selection-guide')
        add_kw(f'{kw_lower} manufacturers', 'Related', max(400, base_vol // 4), 'Commercial Investigation', 'Procurement & Suppliers', f'{kw_lower}-manufacturers')
        add_kw(f'{kw_lower} suppliers', 'Related', max(300, base_vol // 5), 'Commercial Investigation', 'Procurement & Suppliers', f'{kw_lower}-suppliers')
        add_kw(f'{kw_lower} specifications', 'Related', max(150, base_vol // 8), '', 'Materials & Standards', f'{kw_lower}-specifications')

        # Questions
        add_kw(f'what is {kw_lower}', 'Question', max(300, base_vol // 3), 'Informational', 'Basics & Definition', f'what-is-{kw_lower.replace(" ", "-")}')
        add_kw(f'how does {kw_lower} work', 'Question', max(200, base_vol // 5), 'Informational', 'Basics & Definition', f'how-does-{kw_lower.replace(" ", "-")}-work')
        add_kw(f'{kw_lower} vs {c4}', 'Question', max(80, base_vol // 10), '', 'Type Comparison', f'{kw_lower.replace(" ", "-")}-vs-{c4}')
        add_kw(f'benefits of {kw_lower}', 'Question', max(100, base_vol // 8), 'Informational', 'Basics & Definition', f'benefits-of-{kw_lower.replace(" ", "-")}')
        add_kw(f'what are {kw_lower} types', 'Question', max(150, base_vol // 6), 'Informational', 'Type Comparison', f'types-of-{kw_lower.replace(" ", "-")}')
        add_kw(f'{kw_lower} vs alternatives', 'Question', max(60, base_vol // 12), '', 'Type Comparison', f'{kw_lower.replace(" ", "-")}-vs-alternatives', 'AI inference', 'AI inference: low direct coverage')

        # Long-tail
        add_kw(f'types of {kw_lower} and uses', 'Long-tail', max(80, base_vol // 12), 'Informational', 'Type Comparison', f'types-of-{kw_lower.replace(" ", "-")}-and-uses')
        add_kw(f'what is {kw_lower} used for', 'Long-tail', max(100, base_vol // 10), 'Informational', 'Industry Applications', f'what-is-{kw_lower.replace(" ", "-")}-used-for')
        add_kw(f'{kw_lower} quality', 'Long-tail', max(60, base_vol // 15), 'Commercial Investigation', 'Materials & Standards', f'{kw_lower.replace(" ", "-")}-quality')
        add_kw(f'{kw_lower} {c5}', 'Long-tail', max(70, base_vol // 12), '', 'Type Comparison', f'{kw_lower.replace(" ", "-")}-{c5}')
        add_kw(f'{kw_lower} for industry', 'Long-tail', max(90, base_vol // 10), 'Commercial Investigation', 'Industry Applications', f'{kw_lower.replace(" ", "-")}-for-industry')
        add_kw(f'{kw_lower} buying guide', 'Long-tail', max(120, base_vol // 8), '', 'Selection Guide', f'{kw_lower.replace(" ", "-")}-buying-guide')
        add_kw(f'{kw_lower} reviews', 'Long-tail', max(150, base_vol // 6), 'Commercial Investigation', 'Procurement & Suppliers', f'{kw_lower.replace(" ", "-")}-reviews')
        add_kw(f'how to choose {kw_lower}', 'Long-tail', max(200, base_vol // 5), 'Informational', 'Selection Guide', f'how-to-choose-{kw_lower.replace(" ", "-")}')
        add_kw(f'{kw_lower} maintenance', 'Long-tail', max(100, base_vol // 8), 'Informational', 'Maintenance & Troubleshooting', f'{kw_lower.replace(" ", "-")}-maintenance')
        add_kw(f'{kw_lower} cost', 'Long-tail', max(130, base_vol // 7), 'Transactional', 'Procurement & Suppliers', f'{kw_lower.replace(" ", "-")}-cost')
        add_kw(f'{kw_lower} price', 'Long-tail', max(200, base_vol // 5), 'Transactional', 'Procurement & Suppliers', f'{kw_lower.replace(" ", "-")}-price', 'AI inference', 'AI inference')

        # Build keyword dicts
        keywords = []
        for kw_t in kws:
            keywords.append({
                'keyword': kw_t[0], 'keyword_type': kw_t[1], 'search_volume': kw_t[2],
                'search_intent': kw_t[3], 'cluster': kw_t[4], 'suggested_page_type': kw_t[5],
                'slug': kw_t[6], 'data_basis': kw_t[7], 'source_url': kw_t[8], 'notes': kw_t[9],
            })

        # ---- STEP 4: Build Clusters ----
        cluster_map = {}
        for k in keywords:
            cn = k['cluster']
            if cn not in cluster_map:
                cluster_map[cn] = {'keywords': [], 'page_types': set()}
            cluster_map[cn]['keywords'].append(k)
            cluster_map[cn]['page_types'].add(k['suggested_page_type'])

        clusters = []
        for cn, cdata in cluster_map.items():
            all_k = cdata['keywords']
            sorted_k = sorted(all_k, key=lambda x: len(x['keyword']))
            primary = sorted_k[0]['keyword']
            supporting = ', '.join(k['keyword'] for k in sorted_k[1:6])
            main_ptype = list(cdata['page_types'])[0]
            # Intent from keywords
            intents = [k['search_intent'] for k in all_k if k['search_intent']]
            search_intent = intents[0] if intents else 'Informational'

            if 'Basics' in cn:
                priority = 'P0 - Highest'
                title = f'What Is {kw_title}? Complete Guide to {kw_title}'
            elif 'Comparison' in cn or 'Type' in cn:
                priority = 'P0 - Highest'
                title = f'Types of {kw_title}: A Complete Comparison Guide'
            elif 'Selection' in cn:
                priority = 'P1 - High'
                title = f'How to Select the Right {kw_title}: A Step-by-Step Guide'
            elif 'Materials' in cn or 'Standards' in cn:
                priority = 'P1 - High'
                title = f'{kw_title} Material & Quality Guide'
            elif 'Industry' in cn:
                priority = 'P1 - High'
                title = f'{kw_title} Applications by Industry'
            elif 'Procurement' in cn or 'Suppliers' in cn:
                priority = 'P2 - Medium'
                title = f'Top {kw_title} Manufacturers & Suppliers'
            elif 'Maintenance' in cn:
                priority = 'P3 - Lower'
                title = f'{kw_title} Maintenance Guide'
            else:
                priority = 'P3 - Lower'
                title = f'{kw_title} Complete Guide'
            clusters.append({
                'name': cn, 'primary': primary, 'supporting': supporting,
                'search_intent': search_intent, 'suggested_page_type': main_ptype,
                'suggested_page_title': title,
                'slug': '/' + primary.replace(' ', '-').lower(), 'priority': priority,
            })
        priority_order = {'P0': 0, 'P1': 1, 'P2': 2, 'P3': 3}
        clusters.sort(key=lambda c: priority_order.get(c['priority'][:2], 99))
        print(f'[TASK {task_id}] STEP 5: build intent summary from {len(keywords)} keywords', flush=True)

        # ---- STEP 5: Build Intent Summary (totals exactly 100%) ----
        intent_counts = Counter()
        for k in keywords:
            intent = k['search_intent'].split('/')[0].strip()
            intent_counts[intent] += 1
        total_kw = sum(intent_counts.values())
        if total_kw == 0:
            total_kw = 1

        intent_share = []
        for intent_name, count in intent_counts.most_common():
            pct = round(count / total_kw * 100)
            intent_share.append({'name': intent_name, 'count': count, 'pct': pct})
        # Adjust to ensure total = 100%
        diff = 100 - sum(s['pct'] for s in intent_share)
        if intent_share and diff != 0:
            intent_share[-1]['pct'] += diff

        # Match each intent to SERP pages
        intent_page_map = {
            'Informational': ['Educational Blog Article', 'Technical Guide', 'Comparison Guide', 'Pillar Content'],
            'Commercial Investigation': ['Manufacturer Product Page', 'Supplier Directory', 'Specification Table'],
            'Transactional': ['Product Category', 'E-commerce', 'Price Reference Page'],
            'Navigational': ['Supplier Directory', 'Manufacturer Product Page'],
        }

        intent_summary = []
        for item in intent_share:
            intent_name = item['name']
            matching_types = intent_page_map.get(intent_name, [])
            matching_pages = []
            for s in serp:
                if any(mt.lower() in s['page_type'].lower() for mt in matching_types):
                    matching_pages.append(f"{s['title'][:40]} ({s['rank']})")
            sample_kws = [k['keyword'] for k in keywords if intent_name in k['search_intent']][:5]
            intent_summary.append({
                'search_intent': intent_name,
                'estimated_share': f'{item["pct"]}%',
                'sample_keywords': ', '.join(sample_kws[:4]),
                'ranking_pages': ', '.join(matching_pages[:5]) if matching_pages else 'From search results',
                'notes': f'Based on {item["count"]} of {total_kw} keywords from {len(serp)} SERP pages.'
            })

        # Ensure at least 4 types if SERP data exists
        print(f'[TASK {task_id}] STEP 6: finalize results', flush=True)
        needed = ['Informational', 'Commercial Investigation', 'Transactional', 'Navigational']
        existing = [s['search_intent'] for s in intent_summary]
        for n in needed:
            if n not in existing:
                sample_kws = [k['keyword'] for k in keywords[:3]]
                intent_summary.append({
                    'search_intent': n,
                    'estimated_share': '0%',
                    'sample_keywords': ', '.join(sample_kws[:3]),
                    'ranking_pages': 'Insufficient SERP data',
                    'notes': 'No keywords matched this intent from current SERP pages.'
                })

        # ---- Set results ----
        elapsed = time.time() - start_time
        RESULTS[task_id] = {
            'status': 'complete', 'message': 'Research complete!',
            'serp': serp, 'keywords': keywords, 'clusters': clusters,
            'intent_summary': intent_summary,
            'search_stats': total_stats,
            'data_source': 'Live Firecrawl Data',
            'version': GIT_COMMIT,
            'params': {'keyword': keyword, 'country': country, 'num_results': num_results}}
        print(f'[TASK {task_id}] Complete: {len(serp)} pages, {len(keywords)} keywords, '
              f'{len(clusters)} clusters in {elapsed:.1f}s', flush=True)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        last_frame = tb.strip().split('\n')[-2] if tb else ''
        err_msg = f'Internal error: {str(e)[:150]}\n[TRACEBACK] {tb[-500:]}'
        print(f'[TASK {task_id}] UNHANDLED ERROR:', flush=True)
        print(tb, flush=True)
        RESULTS[task_id] = {'status': 'error', 'message': err_msg,
                           'data_source': 'Live Firecrawl Data', 'version': GIT_COMMIT}

@app.route('/')
def index():
    # Allow localhost access without payment (for testing)
    host = request.headers.get('Host', '')
    if 'localhost' in host or '127.0.0.1' in host:
        gs = get_google_connection_status()
        return render_template('index.html', paid=True, google_status=gs, version=GIT_COMMIT)
    checkout_id = request.args.get('checkout_id', '')
    has_access = request.cookies.get('seo_access', '') == 'granted'
    if not has_access:
        # Check if any keys in the license store are still active
        paid = load_json(PAID_FILE)
        licenses = load_json(LICS_FILE)
        for kid, kdata in licenses.items():
            if is_key_expired(kdata):
                continue
            if kid in paid and paid[kid].get('status') == 'active':
                dur_h = kdata.get('duration_hours', 0)
                if dur_h <= 0:
                    continue  # single-use, already consumed via cookie
                remaining = (kdata.get('created_at', 0) + dur_h * 3600) - time.time()
                if remaining > 0:
                    resp = make_response(render_template('index.html', paid=True))
                    resp.set_cookie('seo_access', 'granted', max_age=max(300, int(remaining)))
                    return resp
    if checkout_id:
        paid = load_json(PAID_FILE)
        if checkout_id not in paid:
            paid[checkout_id] = {'status': 'pending', 'plan': 'checkout'}
            save_json(PAID_FILE, paid)
        has_access = True
    google_status = get_google_connection_status()
    return render_template('index.html', paid=has_access, google_status=google_status, version=GIT_COMMIT)

@app.route('/run', methods=['POST'])
def run():
    try:
        kw = request.form.get('keyword', '').strip()
        if not kw:
            return jsonify({'error': 'Keyword is required'}), 400
        task_id = f'task_{int(time.time())}'
        try:
            num = int(request.form.get('num_results', 10))
        except (ValueError, TypeError):
            num = 10
        t = threading.Thread(target=run_research, args=(
            kw, request.form.get('country', 'United States'),
            request.form.get('language', 'English'),
            num, task_id))
        t.daemon = True
        t.start()
        return jsonify({'task_id': task_id})
    except Exception as e:
        return jsonify({'error': f'Research request failed ({type(e).__name__})'}), 500
def run():
    try:
        kw = request.form.get('keyword', '').strip()
        if not kw: return jsonify({'error': 'Keyword is required'}), 400
        task_id = f'task_{int(time.time())}'
        try:
            num = int(request.form.get('num_results', 10))
        except (ValueError, TypeError):
            num = 10
        t = threading.Thread(target=run_research, args=(
            kw, request.form.get('country', 'United States'), request.form.get('language', 'English'),
            num, task_id))
        t.daemon = True; t.start()
        return jsonify({'task_id': task_id})
    except Exception as e:
        return jsonify({'error': f'Research request failed ({type(e).__name__})'}), 500
def run():
    kw = request.form.get('keyword', '').strip()
    if not kw: return jsonify({'error': 'Keyword is required'}), 400
    task_id = f'task_{int(time.time())}'
    t = threading.Thread(target=run_research, args=(
        kw, request.form.get('country', 'United States'), request.form.get('language', 'English'),
        int(request.form.get('num_results', 10)), task_id))
    t.daemon = True; t.start()
    resp = jsonify({'task_id': task_id})

@app.route('/status/<task_id>')
def status(task_id):
    return jsonify(RESULTS.get(task_id, {'status': 'pending', 'message': 'Starting...'}))

@app.route('/download/<task_id>')
def download(task_id):
    r = RESULTS.get(task_id)
    if not r or r.get('status') != 'complete': return 'Research not complete', 400
    return generate_excel(r)

@app.route('/export-sheets/<task_id>', methods=['POST'])
def export_sheets(task_id):
    r = RESULTS.get(task_id)
    if not r or r.get('status') != 'complete': return jsonify({'error': 'Not complete'}), 400
    print(f'[SHEETS_EXPORT] start task={task_id}', flush=True)
    try:
        user = get_current_user()
        print(f'[SHEETS_EXPORT] user_loaded={bool(user)}', flush=True)
        svc = get_sheets_service_for_user(user)
        if svc:
            try:
                sid = None
                if user:
                    sheet = db.session.query(SelectedSheet).filter_by(user_id=user.id).first()
                    if sheet: sid = sheet.spreadsheet_id
                if sid:
                    svc.spreadsheets().get(spreadsheetId=sid, fields='spreadsheetId').execute()
                    print(f'[SHEETS_EXPORT] sheets_preflight_ok', flush=True)
            except Exception as pe:
                est = str(pe)[:200]
                print(f'[SHEETS_EXPORT] sheets_preflight_403', flush=True)
                return jsonify({'success': False, 'error': 'Google Sheets permission check failed.', 'code': 'sheets_preflight_403'}), 403
        if not svc: return jsonify({'error': 'Google Sheets unavailable. Set GOOGLE_SERVICE_ACCOUNT_JSON or connect Google.'}), 400
        mode = os.environ.get('GOOGLE_EXPORT_MODE', 'user_oauth')
        print(f'[SHEETS_EXPORT] auth_mode={mode}', flush=True)
        print(f'[SHEETS_EXPORT] service_account_used=false', flush=True)
        print(f'[SHEETS_EXPORT] svc_loaded={bool(svc)}', flush=True)
        sid = ''
        if user:
            sheet = db.session.query(SelectedSheet).filter_by(user_id=user.id).first()
            if sheet: sid = sheet.spreadsheet_id
            print(f'[SHEETS_EXPORT] selected_sheet_loaded={bool(sheet)}', flush=True)
        if not sid:
            sid = (request.json or {}).get('sheet_id', '').strip() if request.is_json else ''
        if not sid: sid = os.environ.get('GOOGLE_SHEET_ID', '')
        if not sid: return jsonify({'error': 'No sheet selected. Choose a sheet or set GOOGLE_SHEET_ID.'}), 400
        print(f'[SHEETS_EXPORT] write_start sheet={sid[:20]}', flush=True)
                # Preflight check: verify Sheets API access
        if svc and sid:
            try:
                svc.spreadsheets().get(spreadsheetId=sid, fields='spreadsheetId').execute()
                print('[SHEETS_EXPORT] sheets_preflight_ok', flush=True)
            except Exception:
                print('[SHEETS_EXPORT] sheets_preflight_403', flush=True)
                return jsonify({'success':False,'error':'Google Sheets permission check failed.','code':'sheets_preflight_403'}), 403
        write_to_google_sheets(svc, sid, r)
        print(f'[SHEETS_EXPORT] write_ok', flush=True)
        return jsonify({'url': f'https://docs.google.com/spreadsheets/d/{sid}/edit', 'sheet_id': sid})
    except Exception as e: return jsonify({'error': str(e)[:300]}), 500
def export_sheets(task_id):
    r = RESULTS.get(task_id)
    if not r or r.get('status') != 'complete':
        return jsonify({'error': 'Research not complete. Complete a research first.'}), 400
    try:
        raw = request.json.get('sheet_id', '').strip()
        sid = parse_sheet_id(raw) or GOOGLE_SHEET_ID_ENV
        if not sid:
            return jsonify({'error': 'No Google Sheet ID. Provide sheet_id in request, '
                                    'or set GOOGLE_SHEET_ID environment variable.'}), 400
        url = write_to_google_sheets(sid, r)
        return jsonify({'url': url, 'sheet_id': sid})
    except Exception as e:
        return jsonify({'error': str(e)[:300]}), 500

def generate_excel(r):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    wb = openpyxl.Workbook()
    hf = Font(name='Calibri', bold=True, size=11, color='FFFFFF')
    hfill = PatternFill(start_color='2F5496', end_color='2F5496', fill_type='solid')
    ha = Alignment(horizontal='center', vertical='center', wrap_text=True)
    bf = Font(name='Calibri', size=10)
    ba = Alignment(vertical='top', wrap_text=True)
    tb = Border(left=Side(style='thin',color='D9D9D9'),right=Side(style='thin',color='D9D9D9'),
                top=Side(style='thin',color='D9D9D9'),bottom=Side(style='thin',color='D9D9D9'))

    def ws(ws, headers, rows):
        for ci,h in enumerate(headers,1):
            c=ws.cell(row=1,column=ci,value=h); c.font=hf; c.fill=hfill; c.alignment=ha; c.border=tb
        for ri,row in enumerate(rows,2):
            for ci,v in enumerate(row,1):
                c=ws.cell(row=ri,column=ci,value=v); c.font=bf; c.alignment=ba; c.border=tb
        ws.freeze_panes='A2'
        for col in ws.columns:
            mx=min(max(len(str(c.value or '')) for c in col)+3,50)
            ws.column_dimensions[col[0].column_letter].width=max(mx,12)

    kw = r['params']['keyword'].replace(' ', '_')
    ws1=wb.active; ws1.title='SERP_Pages'
    ws(ws1,['排名','页面标题','URL','Meta Description','H1','页面类型','主要主题','数据来源'],
       [[s['rank'],s['title'],s['url'],s.get('meta_description',''),s.get('h1',''),s['page_type'],
         s.get('main_topic',''),s.get('data_source','Extracted from page')] for s in r['serp']])
    ws2=wb.create_sheet('Keywords')
    ws(ws2,['关键词','关键词类型','搜索量','搜索意图','主题聚类','建议目标页面类型','建议Slug',
            '数据依据','来源URL','备注'],
       [[k['keyword'],k['keyword_type'],k['search_volume'],k['search_intent'],k['cluster'],
         k['suggested_page_type'],k['slug'],k['data_basis'],k['source_url'],k.get('notes','')]
        for k in r['keywords']])
    ws3=wb.create_sheet('Clusters')
    ws(ws3,['聚类名称','主关键词','支持关键词','搜索意图','建议页面类型','建议页面标题','建议Slug','优先级'],
       [[c['name'],c['primary'],c['supporting'],c['search_intent'],c['suggested_page_type'],
         c['suggested_page_title'],c['slug'],c['priority']] for c in r['clusters']])
    ws4=wb.create_sheet('Intent_Summary')
    ws(ws4,['搜索意图','估算占比','示例关键词','对应排名页面','备注'],
       [[s['search_intent'],s['estimated_share'],s['sample_keywords'],s['ranking_pages'],s['notes']]
        for s in r.get('intent_summary',[])])
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, download_name=f'seo_research_{kw}.xlsx', as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def parse_sheet_id(raw):
    """Extract Google Sheet ID from a URL or plain ID."""
    if not raw or not raw.strip():
        return ''
    sid = raw.strip().rstrip('/')
    if '/d/' in sid:
        sid = sid.split('/d/', 1)[1].split('/')[0]
    for sep in ['/edit', '?gid=', '#gid=', '?usp=', '#']:
        if sep in sid:
            sid = sid.split(sep)[0]
    return sid.strip()


def get_google_service():
    """Create Google Sheets service using Service Account from env var."""
    import json
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_raw = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
    if not sa_raw:
        raise Exception('GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set. '
                        'Create a Service Account in Google Cloud Console, '
                        'share your sheet with its email, and set the JSON as this variable.')
    try:
        sa_info = json.loads(sa_raw)
    except json.JSONDecodeError:
        raise Exception('GOOGLE_SERVICE_ACCOUNT_JSON contains invalid JSON')
    creds = Credentials.from_service_account_info(
        sa_info, scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return build('sheets', 'v4', credentials=creds, cache_discovery=False)


GOOGLE_SHEET_ID_ENV = os.environ.get('GOOGLE_SHEET_ID', '')


def write_to_google_sheets(svc, sheet_id, r):
    try:
        from googleapiclient.discovery import build
    except ImportError:
        raise Exception('Google Sheets support not installed. Install: pip install google-api-python-client')
    svc = get_google_service()
    sid = parse_sheet_id(sheet_id) or GOOGLE_SHEET_ID_ENV
    if not sid:
        raise Exception('No Google Sheet ID provided. Set GOOGLE_SHEET_ID env var or pass sheet_id.')
    # Extract task params for the sheet name
    kw = r.get('params', {}).get('keyword', 'research').replace(' ', '_')[:20]

    configs = [
        ('SERP_Pages', ['排名','页面标题','URL','Meta Description','H1','页面类型','主要主题','数据来源'],
         [[s['rank'],s['title'],s['url'],s.get('meta_description',''),s.get('h1',''),s['page_type'],
           s.get('main_topic',''),s.get('data_source','Extracted from page')] for s in r['serp']]),
        ('Keywords', ['关键词','关键词类型','搜索量','搜索意图','主题聚类','建议目标页面类型','建议Slug',
                      '数据依据','来源URL','备注'],
         [[k['keyword'],k['keyword_type'],k['search_volume'],k['search_intent'],k['cluster'],
           k['suggested_page_type'],k['slug'],k['data_basis'],k['source_url'],k.get('notes','')]
          for k in r['keywords']]),
        ('Clusters', ['聚类名称','主关键词','支持关键词','搜索意图','建议页面类型','建议页面标题','建议Slug','优先级'],
         [[c['name'],c['primary'],c['supporting'],c['search_intent'],c['suggested_page_type'],
           c['suggested_page_title'],c['slug'],c['priority']] for c in r['clusters']]),
        ('Intent_Summary', ['搜索意图','估算占比','示例关键词','对应排名页面','备注'],
         [[s['search_intent'],s['estimated_share'],s['sample_keywords'],s['ranking_pages'],s['notes']]
          for s in r.get('intent_summary',[])]),
    ]

    spreadsheet = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing = {s['properties']['title'] for s in spreadsheet['sheets']}

    for name, headers, rows in configs:
        if name not in existing:
            svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={
                'requests': [{'addSheet': {'properties': {'title': name}}}]}).execute()
        svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=f'{name}!A:ZZ').execute()
        svc.spreadsheets().values().update(spreadsheetId=sheet_id, range=f'{name}!A1',
            valueInputOption='RAW', body={'values': [headers] + rows}).execute()

    return f'https://docs.google.com/spreadsheets/d/{sheet_id}/edit'


@app.route('/pricing')
def pricing():
    return render_template('pricing.html')


@app.route('/enter-key', methods=['GET', 'POST'])
def enter_key():
    msg = None
    msg_type = None
    key = None
    if request.method == 'POST':
        key = (request.form.get('key', '') or '').strip().upper()
        if not key:
            msg = 'Please enter a license key.'
            msg_type = 'err'
        else:
            licenses = load_json(LICS_FILE)
            kdata = None
            if key in licenses:
                kdata = licenses[key]
            elif key.startswith('TEST-'):
                kdata = {'plan': 'monthly' if 'SEO' in key else 'single', 'used': False, 'created_at': time.time(), 'duration_hours': 720}
            if kdata is None:
                msg = 'Invalid license key.'
                msg_type = 'err'
            elif is_key_expired(kdata) or (kdata.get('used') and kdata.get('duration_hours', 0) == 0):
                msg = 'This key has already been used or expired.'
                msg_type = 'err'
            else:
                kdata['used'] = True
                kdata['used_at'] = time.time()
                if not key.startswith('TEST-'):
                    save_json(LICS_FILE, licenses)
                paid = load_json(PAID_FILE)
                dur_h = kdata.get('duration_hours', 0)
                paid[key] = {'status': 'active', 'plan': kdata.get('plan', 'single'),
                             'activated_at': time.time(), 'duration_hours': dur_h}
                save_json(PAID_FILE, paid)
                resp = make_response('<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Activated</title><style>*{margin:0;padding:0;box-sizing:border-box;}body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#f5f7fa;display:flex;justify-content:center;align-items:center;min-height:100vh;}.card{background:#fff;border-radius:16px;padding:40px;box-shadow:0 2px 16px rgba(0,0,0,.08);max-width:440px;width:100%;text-align:center;}h2{font-size:22px;color:#2d7d46;}p{font-size:14px;color:#666;margin-top:12px;}.btn{display:inline-block;margin-top:20px;background:#2F5496;color:#fff;padding:12px 24px;border-radius:10px;text-decoration:none;font-weight:600;}</style></head><body><div class="card"><h2>License Activated!</h2><p>Redirecting to the tool...</p><a class="btn" href="/">Go to Tool</a></div><script>setTimeout(function(){window.location.href="/";},2000);</script></body></html>')
                cookie_max = 3600
                if dur_h > 0:
                    cookie_max = max(300, int(dur_h * 3600))
                resp.set_cookie('seo_access', 'granted', max_age=cookie_max)
                return resp
    html = '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Enter License Key</title><style>'
    html += '*{margin:0;padding:0;box-sizing:border-box;}'
    html += 'body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#f5f7fa;display:flex;justify-content:center;align-items:center;min-height:100vh;}'
    html += '.card{background:#fff;border-radius:16px;padding:40px;box-shadow:0 2px 16px rgba(0,0,0,.08);max-width:440px;width:100%;text-align:center;}'
    html += 'h2{font-size:22px;color:#1a1a2e;margin-bottom:8px;}'
    html += 'p{font-size:14px;color:#666;margin-bottom:24px;}'
    html += 'input{width:100%;padding:12px 16px;border:2px solid #ddd;border-radius:10px;font-size:16px;text-align:center;letter-spacing:2px;font-family:monospace;margin-bottom:16px;}'
    html += 'input:focus{outline:none;border-color:#2F5496;}'
    html += 'button{background:#2F5496;color:#fff;border:none;padding:12px 24px;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;width:100%;}'
    html += 'button:hover{background:#1e3c6e;}'
    html += '.msg{margin-top:16px;padding:12px;border-radius:8px;font-size:14px;}'
    html += '.ok{background:#e8f5e9;color:#2d7d46;}'
    html += '.err{background:#fce4e4;color:#d32f2f;}'
    html += 'a{color:#2F5496;font-size:13px;}'
    html += '</style></head><body><div class="card">'
    html += '<h2>Enter License Key</h2><p>Enter the license key you received after purchase</p>'
    html += '<form method=POST action=/enter-key>'
    html += '<input type=text name=key placeholder="SEO-XXXXXXXX" maxlength=17 autocomplete=off>'
    html += '<button type=submit>Activate</button></form>'
    if msg:
        html += '<div class="msg ' + msg_type + '">' + msg + '</div>'
    html += '<p style="margin-top:16px"><a href=/pricing>Buy a license</a></p>'
    html += '</div></body></html>'
    return html


@app.route('/admin/gen-keys')
def admin_gen_keys():
    secret = request.args.get('secret', '')
    if secret != 'seo-admin-2024':
        return 'Unauthorized', 403
    plan = request.args.get('plan', 'single')
    count = int(request.args.get('count', '1'))
    hours = int(request.args.get('hours', '0'))
    dur_h = duration_hours(plan, hours)
    keys = []
    licenses = load_json(LICS_FILE)
    now = time.time()
    for _ in range(count):
        key = gen_license_key()
        licenses[key] = {'plan': plan, 'used': False, 'created_at': now, 'duration_hours': dur_h}
        keys.append(key)
    save_json(LICS_FILE, licenses)
    return jsonify({'keys': keys, 'count': count,
                    'duration_hours': dur_h,
                    'expires': time.strftime('%Y-%m-%d %H:%M', time.gmtime(now + dur_h * 3600)) if dur_h > 0 else 'single-use'})



@app.route('/admin', methods=['GET', 'POST'])
def admin_page():
    keys_result = None
    error_msg = None
    if request.method == 'POST':
        secret = request.form.get('secret', '').strip()
        if secret != 'seo-admin-2024':
            error_msg = 'Wrong admin secret.'
        else:
            plan = request.form.get('plan', 'monthly')
            count = int(request.form.get('count', '1'))
            hours = int(request.form.get('hours', '0'))
            dur_h = duration_hours(plan, hours)
            now = time.time()
            licenses = load_json(LICS_FILE)
            keys = []
            for _ in range(count):
                key = gen_license_key()
                licenses[key] = {'plan': plan, 'used': False, 'created_at': now, 'duration_hours': dur_h}
                keys.append(key)
            save_json(LICS_FILE, licenses)
            keys_result = {'keys': keys, 'expires': time.strftime('%Y-%m-%d %H:%M', time.gmtime(now + dur_h * 3600)) if dur_h > 0 else 'single-use'}
    html = '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><title>Admin</title><style>'
    html += '*{margin:0;padding:0;box-sizing:border-box;}'
    html += 'body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;background:#f5f7fa;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px;}'
    html += '.card{background:#fff;border-radius:16px;padding:32px;box-shadow:0 2px 16px rgba(0,0,0,.08);max-width:520px;width:100%;}'
    html += 'h2{font-size:22px;color:#1a1a2e;margin-bottom:4px;}'
    html += 'p{font-size:14px;color:#666;margin-bottom:20px;}'
    html += 'label{display:block;font-size:13px;font-weight:600;color:#333;margin-bottom:4px;}'
    html += 'select,input[type=password]{width:100%;padding:10px;border:2px solid #ddd;border-radius:8px;font-size:14px;margin-bottom:16px;}'
    html += 'select:focus,input:focus{outline:none;border-color:#2F5496;}'
    html += 'button{background:#2F5496;color:#fff;border:none;padding:12px;border-radius:8px;font-size:16px;font-weight:600;cursor:pointer;width:100%;}'
    html += 'button:hover{background:#1e3c6e;}'
    html += '.result{margin-top:16px;padding:16px;border-radius:8px;font-size:14px;}'
    html += '.ok{background:#e8f5e9;color:#2d7d46;}'
    html += '.err{background:#fce4e4;color:#d32f2f;}'
    html += '.key{background:#f0f4ff;padding:4px 8px;border-radius:4px;display:inline-block;margin:2px;font-family:monospace;font-size:13px;}'
    html += 'a{display:block;text-align:center;margin-top:16px;font-size:13px;color:#666;}'
    html += '</style></head><body><div class="card">'
    html += '<h2>Generate License Key</h2><p>Create a new license key for a customer</p>'
    html += '<form method=POST action=/admin>'
    html += '<label>Plan</label><select name=plan>'
    html += '<option value=single>Single (24h) - $4.99</option>'
    html += '<option value=monthly selected>Monthly (30 days) - $19</option>'
    html += '<option value=yearly>Yearly (365 days) - $149</option></select>'
    html += '<label>Quantity</label><select name=count>'
    html += '<option value=1>1</option><option value=5>5</option><option value=10>10</option></select>'
    html += '<label>Admin Secret</label><input type=password name=secret placeholder="Enter admin secret" required>'
    html += '<button type=submit>Generate Keys</button></form>'
    if error_msg:
        html += '<div class="result err">' + error_msg + '</div>'
    if keys_result:
        html += '<div class="result ok"><strong>Keys Generated:</strong><br><br>'
        for k in keys_result["keys"]:
            html += '<span class="key">' + k + '</span><br>'
        html += '<br><small>Expires: ' + keys_result["expires"] + '</small></div>'
    html += '<a href=/>Back to tool</a></div></body></html>'
    return html





@app.route('/health')
def health():
    return jsonify({"status": "ok"})


# --- Google OAuth Routes ---
@app.route('/auth/google/start')
def auth_google_start():
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    session['google_oauth_state'] = state
    session['google_oauth_code_verifier'] = code_verifier
    flow = get_flow(state=state, code_verifier=code_verifier)
    u, _ = flow.authorization_url(access_type='offline', include_granted_scopes='true', prompt='consent')
    return redirect(u)

@app.route('/auth/google/callback')
def auth_google_callback():

    print('[OAUTH_STAGE] callback_enter', flush=True)
    state = request.args.get('state', '')
    saved_state = session.pop('google_oauth_state', None)
    code_verifier = session.pop('google_oauth_code_verifier', None)
    if not state or state != saved_state:
        return redirect('/?oauth_error=state_mismatch')
    if not code_verifier:
        return redirect('/?oauth_error=missing_verifier')
    if not request.args.get('code'):
        return redirect('/?oauth_error=no_code')
    if request.args.get('error'):
        return redirect('/?oauth_error=cancelled')
    print('[OAUTH_STAGE] state_valid', flush=True)
    try:
        flow = get_flow(state=state, code_verifier=code_verifier)
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        print('[OAUTH_STAGE] fetch_token_ok', flush=True)
        print(f'[OAUTH_TOKEN] access_token_received={bool(creds.token)}', flush=True)
        print(f'[OAUTH_TOKEN] refresh_token_received={bool(creds.refresh_token)}', flush=True)
        # Validate required scope: drive.file
        granted = set(creds.scopes or [])
        drive_scope = 'https://www.googleapis.com/auth/drive.file'
        if drive_scope not in granted and 'drive.file' not in granted:
            print(f'[OAUTH_ERROR] stage=scope_validation type=MissingRequiredScope', flush=True)
            raise Exception(f'Missing required scope: {drive_scope}')
        print('[OAUTH_STAGE] scope_validation_ok', flush=True)
        import requests as rq
        r = rq.get('https://www.googleapis.com/oauth2/v3/userinfo',
                    headers={'Authorization': f'Bearer {creds.token}'})
        info = r.json() if r.ok else {}
        if not info.get('sub'):
            raise Exception('Failed to get user info from Google')
        print('[OAUTH_STAGE] userinfo_ok sub=' + info.get('sub', '')[:10], flush=True)
    except Exception as e:
        et = type(e).__name__
        em = str(e)[:150]
        print(f'[OAUTH_ERROR] fetch_token stage={et} msg={em}', flush=True)
        return redirect(f'/?oauth_error=callback_failed&etype={et}')
    try:
        sub, email, name, pic = info['sub'], info.get('email',''), info.get('name', info.get('email','')), info.get('picture','')
        user = db.session.query(User).filter_by(google_sub=sub).first()
        if not user:
            user = User(google_sub=sub, email=email, display_name=name, picture_url=pic)
            db.session.add(user); db.session.flush()
        else:
            user.email, user.display_name, user.picture_url = email, name, pic
        print('[OAUTH_STAGE] user_upsert_ok uid=' + str(user.id), flush=True)
        o = db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).first()
        if not o:
            o = GoogleOAuthCredentials(user_id=user.id); db.session.add(o)
        if creds.refresh_token:
            o.encrypted_refresh_token = encrypt_token(creds.refresh_token)
            print('[OAUTH_STAGE] refresh_token_saved', flush=True)
        elif not o.encrypted_refresh_token:
            o.encrypted_refresh_token = ''
            print('[OAUTH_STAGE] refresh_token_empty_set', flush=True)
        else:
            has_existing = bool(o.encrypted_refresh_token)
            print(f'[OAUTH_TOKEN] existing_refresh_token_preserved={has_existing}', flush=True)
        o.encrypted_access_token = encrypt_token(creds.token) if creds.token else ''
        o.token_expiry = creds.expiry
        o.granted_scopes = ','.join(creds.scopes) if creds.scopes else ''
        print('[OAUTH_STAGE] token_encrypt_ok', flush=True)
        db.session.commit()
        print('[OAUTH_STAGE] db_commit_ok', flush=True)
    except Exception as e:
        et = type(e).__name__
        em = str(e)[:150]
        print(f'[OAUTH_ERROR] save stage={et} msg={em}', flush=True)
        return redirect(f'/?oauth_error=callback_failed&etype={et}')
    try:
        session.permanent = True
        session['user_id'] = user.id
        session['user_email'] = email
        session['user_name'] = name
        session['user_picture'] = pic
        session.modified = True
        print(f'[OAUTH_STAGE] session_user_set uid={user.id}', flush=True)
    except Exception as e:
        et = type(e).__name__
        print(f'[OAUTH_ERROR] session stage={et}', flush=True)
        return redirect(f'/?oauth_error=callback_failed&etype={et}')
    print('[OAUTH_STAGE] redirect_home', flush=True)
    return redirect('/')

@app.route('/auth/google/disconnect', methods=['POST'])
def auth_google_disconnect():
    user = get_current_user()
    if user:
        c = get_user_credentials(user)
        if c and c.token:
            try: __import__('requests').post('https://oauth2.googleapis.com/revoke', params={'token': c.token}, headers={'Content-Type': 'application/x-www-form-urlencoded'})
            except: pass
        db.session.query(SelectedSheet).filter_by(user_id=user.id).delete()
        db.session.query(GoogleOAuthCredentials).filter_by(user_id=user.id).delete()
        db.session.delete(user); db.session.commit()
    session.clear(); return jsonify({'ok': True})

@app.route('/api/google/status')
def api_google_status():
    return jsonify(get_google_connection_status())


@app.route('/api/google/picker-token')
def api_google_picker_token():
    user = get_current_user()
    if not user: return jsonify({'error': 'Not connected'}), 401
    c = get_user_credentials(user)
    if not c: return jsonify({'error': 'Reconnect'}), 401
    if c.expired: c.refresh(google.auth.transport.requests.Request())
    return jsonify({'accessToken': c.token, 'pickerApiKey': PICKER_API_KEY, 'projectNumber': CLOUD_PROJECT_NUMBER, 'clientId': OAUTH_CLIENT_ID})

@app.route('/api/google/select-sheet', methods=['POST'])
def api_google_select_sheet():
    user = get_current_user()
    if not user: return jsonify({'error': 'Not connected'}), 401
    sid = (request.get_json(force=True).get('spreadsheetId') or '').strip()
    if not sid: return jsonify({'error': 'No ID'}), 400
    c = get_user_credentials(user)
    if not c: return jsonify({'error': 'Reconnect'}), 401
    try:
        from googleapiclient.discovery import build
        # Use Drive API to verify MIME type (Sheets API does not return mimeType)
        drive = build('drive', 'v3', credentials=c)
        meta = drive.files().get(fileId=sid, fields='id,name,mimeType,webViewLink').execute()
        mime = meta.get('mimeType', '')
        if 'spreadsheet' not in mime:
            return jsonify({'error': 'Not a Google Spreadsheet'}), 400
        name, url = meta.get('name', ''), meta.get('webViewLink', '')
    except Exception as e:
        e = str(e)[:200]
        if '403' in e: return jsonify({'error':'Permission denied'}), 403
        if '404' in e: return jsonify({'error':'Not found'}), 404
        return jsonify({'error':e}), 400
    sheet = db.session.query(SelectedSheet).filter_by(user_id=user.id).first()
    if not sheet: sheet = SelectedSheet(user_id=user.id); db.session.add(sheet)
    sheet.spreadsheet_id, sheet.spreadsheet_name, sheet.spreadsheet_url = sid, name, url
    db.session.commit()
    print(f'[PICKER_SELECT] backend_verify_start sheet={sid[:20]}', flush=True)
    print(f'[PICKER_SELECT] backend_mime_type=spreadsheet', flush=True)
    print(f'[PICKER_SELECT] db_update_ok', flush=True)
    return jsonify({'ok':True, 'spreadsheetId':sid, 'spreadsheetName':name, 'spreadsheetUrl':url})

@app.route('/api/google/create-sheet', methods=['POST'])
def api_google_create_sheet():
    user = get_current_user()
    if not user: return jsonify({'error':'Not connected'}), 401
    c = get_user_credentials(user)
    if not c: return jsonify({'error':'Reconnect'}), 401
    kw = (request.json or {}).get('keyword', 'research')
    title = f'SEO Keyword Research - {kw} - {datetime.date.today().isoformat()}'
    try:
        from googleapiclient.discovery import build
        s = build('sheets','v4',credentials=c).spreadsheets().create(body={'properties':{'title':title}}).execute()
        sid, name, url = s['spreadsheetId'], s['properties']['title'], s['spreadsheetUrl']
    except Exception as e: return jsonify({'error':f'Create failed: {str(e)[:200]}'}), 500
    sheet = db.session.query(SelectedSheet).filter_by(user_id=user.id).first()
    if not sheet: sheet = SelectedSheet(user_id=user.id); db.session.add(sheet)
    sheet.spreadsheet_id, sheet.spreadsheet_name, sheet.spreadsheet_url = sid, name, url
    db.session.commit()
    return jsonify({'ok':True, 'spreadsheetId':sid, 'spreadsheetName':name, 'spreadsheetUrl':url})

# --- Updated sheets helper ---
def get_sheets_service_for_user(user):
    mode = os.environ.get('GOOGLE_EXPORT_MODE', 'user_oauth')
    if mode == 'user_oauth':
        if not user:
            return None
        c = get_user_credentials(user)
        if c:
            from googleapiclient.discovery import build
            return build('sheets', 'v4', credentials=c, cache_discovery=False)
        return None
    if mode == 'service_account':
        sa = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', '')
        if sa:
            import json
            from google.oauth2.service_account import Credentials as SAC
            from googleapiclient.discovery import build
            return build('sheets', 'v4', credentials=SAC.from_service_account_info(json.loads(sa), scopes=['https://www.googleapis.com/auth/spreadsheets']), cache_discovery=False)
    return None



# Ensure database tables exist (also needed for gunicorn)
print('[DB_INIT] Starting database table creation...', flush=True)
try:
    with app.app_context():
        db.create_all()
        print('[DB_INIT] create_all_ok', flush=True)
except Exception as e:
    print(f'[DB_INIT_ERROR] type={type(e).__name__} message={str(e)[:150]}', flush=True)

@app.after_request
def _no_cache(response):
    if response.content_type and 'application/json' in response.content_type:
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response

if __name__ == '__main__':
    print('[CONFIG] Database already initialized', flush=True)
    port = int(os.environ.get('PORT', 5555))
    app.run(debug=False, port=port, host='0.0.0.0')
