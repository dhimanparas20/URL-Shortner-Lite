import hashlib
import io
import json
import os
import random
import re
import secrets
import string
import threading
import time
from collections import defaultdict
from functools import wraps
from urllib.parse import urlparse

import qrcode
import requests as http_requests
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_restful import Api, Resource
from pymongo import MongoClient, ASCENDING, DESCENDING

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=86400,
)
api = Api(app)

ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')

USE_JSON_FILE = os.getenv('USE_JSON_FILE', 'false').lower() == 'true'
DB_FILE = 'url_database.json'

# Rate limiting config
RATE_LIMIT_SHORTEN = int(os.getenv('RATE_LIMIT_SHORTEN', '30'))
RATE_LIMIT_WINDOW = int(os.getenv('RATE_LIMIT_WINDOW', '3600'))

MONGO_URI = os.getenv('MONGO_URI')
client = None
db = None
url_collection = None

if not USE_JSON_FILE:
    if not MONGO_URI:
        raise RuntimeError('MONGO_URI must be set when USE_JSON_FILE is false')
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command('ping')
    except Exception as e:
        raise RuntimeError(f'Cannot connect to MongoDB: {e}')
    db = client['url_shortener']
    url_collection = db['urls']
    try:
        url_collection.create_index([('short_url', ASCENDING)], unique=True)
        url_collection.create_index([('long_url', ASCENDING)])
    except Exception:
        pass

ALLOWED_SCHEMES = {'http', 'https'}
MAX_URL_LENGTH = 2048
MAX_CUSTOM_KEYWORD_LENGTH = 30
MIN_CUSTOM_KEYWORD_LENGTH = 3
SHORT_URL_LENGTH = 6
SHORT_URL_CHARS = string.ascii_letters + string.digits
MAX_CLICKS = 1_000_000_000


# ── Rate Limiter ──────────────────────────────────────────────
class RateLimiter:
    def __init__(self):
        self._hits = defaultdict(list)
        self._lock = threading.Lock()

    def is_limited(self, key, limit, window):
        now = time.time()
        with self._lock:
            self._hits[key] = [t for t in self._hits[key] if now - t < window]
            if len(self._hits[key]) >= limit:
                return True, int(self._hits[key][0] + window - now)
            self._hits[key].append(now)
            return False, 0

    def cleanup(self):
        now = time.time()
        with self._lock:
            for key in list(self._hits.keys()):
                self._hits[key] = [t for t in self._hits[key] if now - t < 3600]
                if not self._hits[key]:
                    del self._hits[key]


rate_limiter = RateLimiter()


def cleanup_loop():
    while True:
        time.sleep(300)
        rate_limiter.cleanup()


_cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
_cleanup_thread.start()


# ── Database Helpers ──────────────────────────────────────────
def load_database():
    if USE_JSON_FILE and os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                data = json.load(f)
            db_data = data.get('url_database', {})
            rev = data.get('reverse_lookup', {})
            for short_url, entry in db_data.items():
                if isinstance(entry, dict):
                    entry.setdefault('clicks', 0)
                    entry.setdefault('created_at', 0)
                    entry.setdefault('expires_at', None)
                    entry.setdefault('max_views', None)
                    entry.setdefault('ip_address', None)
                    entry.setdefault('password_hash', None)
                    entry.setdefault('click_history', [])
                    entry.setdefault('referrer', None)
                    entry.setdefault('user_agent', None)
            return db_data, rev
        except (json.JSONDecodeError, IOError):
            return {}, {}
    return {}, {}


def save_database(url_database, reverse_lookup):
    if USE_JSON_FILE:
        tmp = DB_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({
                'url_database': url_database,
                'reverse_lookup': reverse_lookup,
            }, f)
        os.replace(tmp, DB_FILE)


url_database, reverse_lookup = load_database()


# ── Utility Functions ─────────────────────────────────────────
def generate_short_url():
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(SHORT_URL_LENGTH))


def trim_keyword(keyword):
    return re.sub(r'[^a-zA-Z0-9]', '_', keyword)


def is_valid_url(url):
    if not url or len(url) > MAX_URL_LENGTH:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    if not parsed.netloc or '.' not in parsed.netloc:
        return False
    return True


def is_valid_keyword(keyword):
    if not keyword:
        return False
    if len(keyword) < MIN_CUSTOM_KEYWORD_LENGTH or len(keyword) > MAX_CUSTOM_KEYWORD_LENGTH:
        return False
    return bool(re.fullmatch(r'[a-zA-Z0-9_-]+', keyword))


def hash_password(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest()


def verify_password(password, password_hash):
    return hash_password(password) == password_hash


def calculate_expires_at(expiry_value, expiry_unit):
    if not expiry_value or not expiry_unit:
        return None
    try:
        expiry_value = int(expiry_value)
    except (ValueError, TypeError):
        return None
    if expiry_value <= 0:
        return None
    multipliers = {
        'seconds': 1, 'minutes': 60, 'hours': 3600,
        'days': 86400, 'weeks': 604800, 'months': 2592000,
    }
    multiplier = multipliers.get(expiry_unit)
    if multiplier is None:
        return None
    return time.time() + (expiry_value * multiplier)


def render_not_found():
    resp = make_response(render_template('not_found.html'), 404)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


def record_click(url_data, referrer, user_agent):
    now = time.time()
    url_data['clicks'] = url_data.get('clicks', 0) + 1
    history = url_data.setdefault('click_history', [])
    history.append({
        'ts': now,
        'ref': referrer[:500] if referrer else None,
        'ua': user_agent[:500] if user_agent else None,
    })
    if len(history) > 10000:
        url_data['click_history'] = history[-5000:]
    url_data['referrer'] = referrer[:500] if referrer else url_data.get('referrer')
    url_data['user_agent'] = user_agent[:500] if user_agent else url_data.get('user_agent')


def record_click_mongo(short_url, referrer, user_agent):
    now = time.time()
    url_collection.update_one(
        {'short_url': short_url},
        {
            '$inc': {'clicks': 1},
            '$push': {
                'click_history': {
                    '$each': [{'ts': now, 'ref': (referrer[:500] if referrer else None), 'ua': (user_agent[:500] if user_agent else None)}],
                    '$slice': -10000,
                }
            },
            '$set': {
                'referrer': referrer[:500] if referrer else None,
                'user_agent': user_agent[:500] if user_agent else None,
            }
        }
    )


# ── Resources ─────────────────────────────────────────────────
class URLShortener(Resource):
    def post(self):
        if not request.is_json:
            return {'error': 'Content-Type must be application/json'}, 415

        data = request.get_json(silent=True)
        if not data:
            return {'error': 'Invalid JSON body'}, 400

        long_url = data.get('url', '').strip()
        custom_keyword = data.get('custom_keyword')
        expiry_value = data.get('expiry_value')
        expiry_unit = data.get('expiry_unit')
        max_views_raw = data.get('max_views')
        password = data.get('password')
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if user_ip and ',' in user_ip:
            user_ip = user_ip.split(',')[0].strip()

        if not long_url:
            return {'error': 'URL is required'}, 400
        if not is_valid_url(long_url):
            return {'error': 'Invalid URL. Only http and https URLs with valid domains are allowed'}, 400

        if custom_keyword and not is_valid_keyword(custom_keyword):
            return {
                'error': f'Custom keyword must be {MIN_CUSTOM_KEYWORD_LENGTH}-{MAX_CUSTOM_KEYWORD_LENGTH} characters, alphanumeric, hyphens, or underscores only'
            }, 400

        expires_at = calculate_expires_at(expiry_value, expiry_unit)

        max_views = None
        if max_views_raw is not None:
            try:
                max_views = int(max_views_raw)
                if max_views <= 0 or max_views > MAX_CLICKS:
                    max_views = None
            except (ValueError, TypeError):
                max_views = None

        password_hash = None
        if password and str(password).strip():
            password_hash = hash_password(str(password).strip())

        now = time.time()

        if USE_JSON_FILE:
            if custom_keyword:
                if custom_keyword in url_database:
                    return {'error': 'Custom keyword already taken'}, 409
                short_url = trim_keyword(custom_keyword)
            else:
                if long_url in reverse_lookup:
                    return {'short_url': reverse_lookup[long_url]}, 200
                short_url = generate_short_url()
                attempts = 0
                while short_url in url_database and attempts < 10:
                    short_url = generate_short_url()
                    attempts += 1

            url_database[short_url] = {
                'long_url': long_url,
                'clicks': 0,
                'created_at': now,
                'expires_at': expires_at,
                'max_views': max_views,
                'ip_address': user_ip,
                'password_hash': password_hash,
                'click_history': [],
                'referrer': None,
                'user_agent': None,
            }
            reverse_lookup[long_url] = short_url
            save_database(url_database, reverse_lookup)
        else:
            if custom_keyword:
                if url_collection.find_one({'short_url': custom_keyword}):
                    return {'error': 'Custom keyword already taken'}, 409
                short_url = trim_keyword(custom_keyword)
            else:
                existing_url = url_collection.find_one({'long_url': long_url})
                if existing_url:
                    return {'short_url': existing_url['short_url']}, 200
                short_url = generate_short_url()
                attempts = 0
                while url_collection.find_one({'short_url': short_url}) and attempts < 10:
                    short_url = generate_short_url()
                    attempts += 1

            url_collection.insert_one({
                'short_url': short_url,
                'long_url': long_url,
                'clicks': 0,
                'created_at': now,
                'expires_at': expires_at,
                'max_views': max_views,
                'ip_address': user_ip,
                'password_hash': password_hash,
                'click_history': [],
                'referrer': None,
                'user_agent': None,
            })

        return {'short_url': short_url, 'has_password': password_hash is not None}, 201


class URLRedirect(Resource):
    def _handle(self, short_url):
        now = time.time()

        if USE_JSON_FILE:
            url_data = url_database.get(short_url)
            if not url_data:
                return render_not_found()

            if isinstance(url_data, str):
                url_database[short_url] = {
                    'long_url': url_data, 'clicks': 0, 'created_at': now,
                    'expires_at': None, 'max_views': None, 'ip_address': None,
                    'password_hash': None, 'click_history': [], 'referrer': None, 'user_agent': None,
                }
                url_data = url_database[short_url]

            long_url = url_data['long_url']
            expires_at = url_data.get('expires_at')
            max_views = url_data.get('max_views')
            clicks = url_data.get('clicks', 0)
            password_hash = url_data.get('password_hash')

            if expires_at and now > expires_at:
                del url_database[short_url]
                reverse_lookup.pop(long_url, None)
                save_database(url_database, reverse_lookup)
                return render_not_found()

            if max_views is not None and clicks >= max_views:
                del url_database[short_url]
                reverse_lookup.pop(long_url, None)
                save_database(url_database, reverse_lookup)
                return render_not_found()

            if password_hash:
                if request.method == 'POST':
                    entered = request.form.get('password', '')
                    if verify_password(entered, password_hash):
                        referrer = request.headers.get('Referer', '')
                        user_agent = request.headers.get('User-Agent', '')
                        record_click(url_data, referrer, user_agent)
                        save_database(url_database, reverse_lookup)
                        if max_views is not None and url_data['clicks'] >= max_views:
                            del url_database[short_url]
                            save_database(url_database, reverse_lookup)
                        return redirect(long_url)
                    resp = make_response(render_template('password_verify.html', short_url=short_url, error='Incorrect password'), 403)
                    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
                    return resp
                resp = make_response(render_template('password_verify.html', short_url=short_url, error=None))
                resp.headers['Content-Type'] = 'text/html; charset=utf-8'
                return resp

            referrer = request.headers.get('Referer', '')
            user_agent = request.headers.get('User-Agent', '')
            record_click(url_data, referrer, user_agent)
            save_database(url_database, reverse_lookup)

            if max_views is not None and url_data['clicks'] >= max_views:
                del url_database[short_url]
                reverse_lookup.pop(long_url, None)
                save_database(url_database, reverse_lookup)

            return redirect(long_url)

        url_doc = url_collection.find_one({'short_url': short_url})
        if not url_doc:
            return render_not_found()

        expires_at = url_doc.get('expires_at')
        max_views = url_doc.get('max_views')
        clicks = url_doc.get('clicks', 0)
        password_hash = url_doc.get('password_hash')

        if expires_at and now > expires_at:
            url_collection.delete_one({'short_url': short_url})
            return render_not_found()

        if max_views is not None and clicks >= max_views:
            url_collection.delete_one({'short_url': short_url})
            return render_not_found()

        if password_hash:
            if request.method == 'POST':
                entered = request.form.get('password', '')
                if verify_password(entered, password_hash):
                    referrer = request.headers.get('Referer', '')
                    user_agent = request.headers.get('User-Agent', '')
                    record_click_mongo(short_url, referrer, user_agent)
                    new_doc = url_collection.find_one({'short_url': short_url})
                    if max_views is not None and new_doc and new_doc.get('clicks', 0) >= max_views:
                        url_collection.delete_one({'short_url': short_url})
                    return redirect(url_doc['long_url'])
                resp = make_response(render_template('password_verify.html', short_url=short_url, error='Incorrect password'), 403)
                resp.headers['Content-Type'] = 'text/html; charset=utf-8'
                return resp
            resp = make_response(render_template('password_verify.html', short_url=short_url, error=None))
            resp.headers['Content-Type'] = 'text/html; charset=utf-8'
            return resp

        referrer = request.headers.get('Referer', '')
        user_agent = request.headers.get('User-Agent', '')
        record_click_mongo(short_url, referrer, user_agent)

        new_doc = url_collection.find_one({'short_url': short_url})
        if max_views is not None and new_doc and new_doc.get('clicks', 0) >= max_views:
            url_collection.delete_one({'short_url': short_url})

        return redirect(url_doc['long_url'])

    def get(self, short_url):
        return self._handle(short_url)

    def post(self, short_url):
        return self._handle(short_url)


class ReverseLookup(Resource):
    def post(self):
        if not request.is_json:
            return {'error': 'Content-Type must be application/json'}, 415
        data = request.get_json(silent=True)
        if not data:
            return {'error': 'Invalid JSON body'}, 400

        keyword = data.get('keyword', '').strip()
        if not keyword:
            return {'error': 'No keyword provided'}, 400
        if len(keyword) > MAX_URL_LENGTH:
            return {'error': 'Input too long'}, 400

        if '://' in keyword:
            try:
                parsed = urlparse(keyword)
                path = parsed.path.rstrip('/')
                keyword = path.split('/')[-1] if path else ''
            except ValueError:
                pass

        if not keyword:
            return {'error': 'Invalid keyword'}, 400

        if USE_JSON_FILE:
            url_data = url_database.get(keyword)
            if not url_data:
                return {'error': 'Short URL not found'}, 404
            if isinstance(url_data, str):
                return {'long_url': url_data, 'clicks': 0, 'has_password': False}, 200
            return {
                'long_url': url_data['long_url'],
                'clicks': url_data.get('clicks', 0),
                'has_password': url_data.get('password_hash') is not None,
            }, 200
        else:
            url_doc = url_collection.find_one({'short_url': keyword})
            if not url_doc:
                return {'error': 'Short URL not found'}, 404
            return {
                'long_url': url_doc['long_url'],
                'clicks': url_doc.get('clicks', 0),
                'has_password': url_doc.get('password_hash') is not None,
            }, 200


class ClickStats(Resource):
    def get(self, short_url):
        if len(short_url) > MAX_CUSTOM_KEYWORD_LENGTH:
            return {'clicks': 0}, 200
        if USE_JSON_FILE:
            url_data = url_database.get(short_url)
            if not url_data:
                return {'clicks': 0}, 200
            count = url_data.get('clicks', 0) if isinstance(url_data, dict) else 0
        else:
            url_doc = url_collection.find_one({'short_url': short_url})
            count = url_doc.get('clicks', 0) if url_doc else 0
        return {'clicks': count}, 200


class QRCodeGen(Resource):
    def get(self, short_url):
        if len(short_url) > MAX_CUSTOM_KEYWORD_LENGTH:
            abort(404)
        exists = False
        if USE_JSON_FILE:
            exists = short_url in url_database
        else:
            exists = url_collection.find_one({'short_url': short_url}) is not None
        if not exists:
            abort(404)
        url = request.host_url + short_url
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')


class URLMetadataPreview(Resource):
    def post(self):
        if not request.is_json:
            return {'error': 'Content-Type must be application/json'}, 415
        data = request.get_json(silent=True)
        if not data:
            return {'error': 'Invalid JSON body'}, 400

        url = data.get('url', '').strip()
        if not url or not is_valid_url(url):
            return {'error': 'Invalid URL'}, 400

        try:
            resp = http_requests.get(url, timeout=5, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; URLShortener/1.0)',
            }, allow_redirects=True)
            resp.raise_for_status()
            title_match = re.search(r'<title[^>]*>(.*?)</title>', resp.text[:10000], re.IGNORECASE | re.DOTALL)
            title = title_match.group(1).strip() if title_match else None
            title = re.sub(r'\s+', ' ', title)[:200] if title else None
            return {'title': title, 'final_url': resp.url}, 200
        except Exception:
            return {'title': None, 'final_url': url}, 200


class URLAnalytics(Resource):
    def get(self, short_url):
        if len(short_url) > MAX_CUSTOM_KEYWORD_LENGTH:
            return {'error': 'Not found'}, 404

        if USE_JSON_FILE:
            url_data = url_database.get(short_url)
            if not url_data or isinstance(url_data, str):
                return {'error': 'Not found'}, 404
            history = url_data.get('click_history', [])
        else:
            url_doc = url_collection.find_one({'short_url': short_url})
            if not url_doc:
                return {'error': 'Not found'}, 404
            history = url_doc.get('click_history', [])

        now = time.time()
        daily = defaultdict(int)
        hourly = defaultdict(int)
        referrers = defaultdict(int)
        user_agents = defaultdict(int)

        for entry in history:
            ts = entry.get('ts', 0)
            day = time.strftime('%Y-%m-%d', time.gmtime(ts))
            hour = time.strftime('%H:00', time.gmtime(ts))
            daily[day] += 1
            hourly[hour] += 1
            ref = entry.get('ref') or 'Direct'
            referrers[ref] += 1
            ua = entry.get('ua') or 'Unknown'
            if 'Mobile' in ua or 'Android' in ua or 'iPhone' in ua:
                user_agents['Mobile'] += 1
            elif 'Chrome' in ua:
                user_agents['Chrome'] += 1
            elif 'Firefox' in ua:
                user_agents['Firefox'] += 1
            elif 'Safari' in ua:
                user_agents['Safari'] += 1
            else:
                user_agents['Other'] += 1

        sorted_days = sorted(daily.items())[-30:]
        sorted_hours = sorted(hourly.items())[-24:]
        top_refs = sorted(referrers.items(), key=lambda x: -x[1])[:10]
        top_uas = sorted(user_agents.items(), key=lambda x: -x[1])[:5]

        return {
            'total_clicks': len(history),
            'daily': [{'date': d, 'clicks': c} for d, c in sorted_days],
            'hourly': [{'hour': h, 'clicks': c} for h, c in sorted_hours],
            'referrers': [{'name': r, 'clicks': c} for r, c in top_refs],
            'user_agents': [{'name': u, 'clicks': c} for u, c in top_uas],
        }, 200


# ── Admin ─────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_authenticated'):
            if request.accept_mimetypes.accept_html and not request.path.startswith('/api/'):
                return redirect(url_for('admin_login'))
            return {'error': 'Unauthorized'}, 401
        return f(*args, **kwargs)
    return decorated


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if not username or not password:
            return render_template('admin_login.html', error='Username and password are required')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.clear()
            session['admin_authenticated'] = True
            session.permanent = True
            return redirect(url_for('admin_panel'))
        return render_template('admin_login.html', error='Invalid credentials')
    return render_template('admin_login.html', error=None)


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin_panel():
    return render_template('admin.html')


@app.route('/api/admin/urls')
@admin_required
def admin_get_urls():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    search = request.args.get('search', '').strip().lower()
    status_filter = request.args.get('filter', 'all')
    sort_field = request.args.get('sort', 'created_at')
    sort_dir = request.args.get('dir', 'desc')

    per_page = min(max(per_page, 1), 100)
    page = max(page, 1)
    now = time.time()

    if USE_JSON_FILE:
        all_urls = []
        for short_url, data in url_database.items():
            if isinstance(data, str):
                data = {
                    'long_url': data, 'clicks': 0, 'created_at': 0, 'expires_at': None,
                    'max_views': None, 'ip_address': None, 'password_hash': None,
                    'click_history': [], 'referrer': None, 'user_agent': None,
                }
            is_expired = (data.get('expires_at') and now > data['expires_at']) or \
                         (data.get('max_views') is not None and data.get('clicks', 0) >= data['max_views'])
            all_urls.append({
                'short_url': short_url,
                'long_url': data.get('long_url', ''),
                'clicks': data.get('clicks', 0),
                'created_at': data.get('created_at', 0),
                'expires_at': data.get('expires_at'),
                'max_views': data.get('max_views'),
                'ip_address': data.get('ip_address'),
                'has_password': data.get('password_hash') is not None,
                'is_expired': is_expired,
            })

        if search:
            all_urls = [u for u in all_urls if search in u['short_url'].lower() or search in u['long_url'].lower() or search in (u['ip_address'] or '').lower()]
        if status_filter == 'active':
            all_urls = [u for u in all_urls if not u['is_expired']]
        elif status_filter == 'expired':
            all_urls = [u for u in all_urls if u['is_expired']]

        reverse_sort = sort_dir == 'desc'
        all_urls.sort(key=lambda u: u.get(sort_field, 0) or 0, reverse=reverse_sort)

        total = len(all_urls)
        start = (page - 1) * per_page
        page_urls = all_urls[start:start + per_page]
    else:
        mongo_filter = {}
        if search:
            mongo_filter['$or'] = [
                {'short_url': {'$regex': re.escape(search), '$options': 'i'}},
                {'long_url': {'$regex': re.escape(search), '$options': 'i'}},
                {'ip_address': {'$regex': re.escape(search), '$options': 'i'}},
            ]
        if status_filter == 'active':
            mongo_filter['$and'] = [
                {'$or': [{'expires_at': None}, {'expires_at': {'$gt': now}}]},
                {'$or': [{'max_views': None}, {'$expr': {'$lt': ['$clicks', '$max_views']}}]},
            ]
        elif status_filter == 'expired':
            mongo_filter['$or'] = [
                {'expires_at': {'$lte': now, '$ne': None}},
                {'$and': [{'max_views': {'$ne': None}}, {'$expr': {'$gte': ['$clicks', '$max_views']}}]},
            ]

        total = url_collection.count_documents(mongo_filter)
        sort_order = DESCENDING if sort_dir == 'desc' else ASCENDING
        skip = (page - 1) * per_page
        cursor = url_collection.find(mongo_filter).sort(sort_field, sort_order).skip(skip).limit(per_page)

        page_urls = []
        for doc in cursor:
            is_expired = (doc.get('expires_at') and now > doc['expires_at']) or \
                         (doc.get('max_views') is not None and doc.get('clicks', 0) >= doc['max_views'])
            page_urls.append({
                'short_url': doc.get('short_url', ''),
                'long_url': doc.get('long_url', ''),
                'clicks': doc.get('clicks', 0),
                'created_at': doc.get('created_at', 0),
                'expires_at': doc.get('expires_at'),
                'max_views': doc.get('max_views'),
                'ip_address': doc.get('ip_address'),
                'has_password': doc.get('password_hash') is not None,
                'is_expired': is_expired,
            })

    return jsonify({
        'urls': page_urls,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': max(1, -(-total // per_page)),
    })


@app.route('/api/admin/urls/delete-expired', methods=['DELETE'])
@admin_required
def admin_delete_expired():
    now = time.time()
    deleted = 0
    if USE_JSON_FILE:
        expired_keys = []
        for short_url, data in url_database.items():
            if isinstance(data, str):
                continue
            expires_at = data.get('expires_at')
            max_views = data.get('max_views')
            clicks = data.get('clicks', 0)
            if (expires_at and now > expires_at) or (max_views is not None and clicks >= max_views):
                expired_keys.append(short_url)
        for key in expired_keys:
            url_data = url_database.pop(key)
            long_url = url_data.get('long_url', '') if isinstance(url_data, dict) else url_data
            reverse_lookup.pop(long_url, None)
            deleted += 1
        if deleted:
            save_database(url_database, reverse_lookup)
    else:
        result = url_collection.delete_many({
            '$or': [
                {'expires_at': {'$lte': now, '$ne': None}},
                {'$and': [
                    {'max_views': {'$ne': None}},
                    {'$expr': {'$gte': ['$clicks', '$max_views']}}
                ]}
            ]
        })
        deleted = result.deleted_count
    return jsonify({'deleted': deleted})


@app.route('/api/admin/urls/delete-selected', methods=['POST'])
@admin_required
def admin_delete_selected():
    if not request.is_json:
        return {'error': 'Content-Type must be application/json'}, 415
    data = request.get_json(silent=True)
    if not data or not isinstance(data.get('short_urls'), list):
        return {'error': 'short_urls array required'}, 400

    short_urls = data['short_urls']
    if len(short_urls) > 500:
        return {'error': 'Too many URLs (max 500)'}, 400

    deleted = 0
    if USE_JSON_FILE:
        for short_url in short_urls:
            if short_url in url_database:
                url_data = url_database.pop(short_url)
                long_url = url_data.get('long_url', '') if isinstance(url_data, dict) else url_data
                reverse_lookup.pop(long_url, None)
                deleted += 1
        if deleted:
            save_database(url_database, reverse_lookup)
    else:
        result = url_collection.delete_many({'short_url': {'$in': short_urls}})
        deleted = result.deleted_count
    return jsonify({'deleted': deleted})


@app.route('/api/admin/urls/<short_url>', methods=['DELETE'])
@admin_required
def admin_delete_url(short_url):
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', short_url):
        return {'error': 'Invalid short URL format'}, 400
    if USE_JSON_FILE:
        if short_url in url_database:
            url_data = url_database.pop(short_url)
            long_url = url_data.get('long_url', '') if isinstance(url_data, dict) else url_data
            reverse_lookup.pop(long_url, None)
            save_database(url_database, reverse_lookup)
            return jsonify({'success': True})
        return jsonify({'error': 'Not found'}), 404
    else:
        result = url_collection.delete_one({'short_url': short_url})
        if result.deleted_count:
            return jsonify({'success': True})
        return jsonify({'error': 'Not found'}), 404


@app.route('/health')
def health_check():
    status = {'status': 'ok', 'timestamp': time.time()}
    if not USE_JSON_FILE:
        try:
            client.admin.command('ping')
            status['mongodb'] = 'connected'
        except Exception:
            status['mongodb'] = 'disconnected'
            status['status'] = 'degraded'
    return jsonify(status)


@app.route('/api/preview')
def preview_url():
    url = request.args.get('url', '').strip()
    if not url or not is_valid_url(url):
        return {'error': 'Invalid URL'}, 400
    try:
        resp = http_requests.get(url, timeout=5, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; URLShortener/1.0)',
        }, allow_redirects=True)
        resp.raise_for_status()
        title_match = re.search(r'<title[^>]*>(.*?)</title>', resp.text[:10000], re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else None
        title = re.sub(r'\s+', ' ', title)[:200] if title else None
        return {'title': title, 'final_url': resp.url}, 200
    except Exception:
        return {'title': None, 'final_url': url}, 200


# ── Error Handlers ────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    return render_not_found()

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({'error': 'Method not allowed'}), 405

@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({'error': 'Request too large'}), 413

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500


@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


# ── Rate Limited Shorten Wrapper ──────────────────────────────
class RateLimitedShorten(Resource):
    def post(self):
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if user_ip and ',' in user_ip:
            user_ip = user_ip.split(',')[0].strip()

        limited, retry_in = rate_limiter.is_limited(
            f'shorten:{user_ip}', RATE_LIMIT_SHORTEN, RATE_LIMIT_WINDOW
        )
        if limited:
            return {'error': f'Rate limit exceeded. Try again in {retry_in} seconds.'}, 429

        return URLShortener().post()


# ── Route Registration ────────────────────────────────────────
api.add_resource(RateLimitedShorten, '/shorten')
api.add_resource(ReverseLookup, '/unshorten')
api.add_resource(ClickStats, '/stats/<string:short_url>')
api.add_resource(QRCodeGen, '/qr/<string:short_url>')
api.add_resource(URLMetadataPreview, '/api/preview-url')
api.add_resource(URLAnalytics, '/api/analytics/<string:short_url>')


@app.route('/')
def index():
    return render_template('index.html')


api.add_resource(URLRedirect, '/<string:short_url>')


if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true', port=5000, threaded=True, host="0.0.0.0")
