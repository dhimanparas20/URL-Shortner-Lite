import io
import json
import os
import random
import re
import secrets
import string
import time
from functools import wraps
from urllib.parse import urlparse

import qrcode
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    jsonify,
    make_response,
    redirect,
    request,
    render_template,
    send_file,
    session,
    url_for,
)
from flask_restful import Api, Resource
from pymongo import MongoClient

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true',
    MAX_CONTENT_LENGTH=1 * 1024 * 1024,
)
api = Api(app)

ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'changeme')

USE_JSON_FILE = os.getenv('USE_JSON_FILE', 'false').lower() == 'true'
DB_FILE = 'url_database.json'

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

ALLOWED_SCHEMES = {'http', 'https'}
MAX_URL_LENGTH = 2048
MAX_CUSTOM_KEYWORD_LENGTH = 30
MIN_CUSTOM_KEYWORD_LENGTH = 3
SHORT_URL_LENGTH = 6
SHORT_URL_CHARS = string.ascii_letters + string.digits
MAX_CLICKS = 1_000_000_000


def load_database():
    if USE_JSON_FILE and os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                data = json.load(f)
            return data.get('url_database', {}), data.get('reverse_lookup', {})
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


def sanitize_for_display(text):
    if not text:
        return ''
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


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
        'seconds': 1,
        'minutes': 60,
        'hours': 3600,
        'days': 86400,
        'weeks': 604800,
        'months': 2592000,
    }
    multiplier = multipliers.get(expiry_unit)
    if multiplier is None:
        return None
    return time.time() + (expiry_value * multiplier)


def render_not_found():
    resp = make_response(render_template('not_found.html'), 404)
    resp.headers['Content-Type'] = 'text/html; charset=utf-8'
    return resp


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
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if user_ip and ',' in user_ip:
            user_ip = user_ip.split(',')[0].strip()

        if not long_url:
            return {'error': 'URL is required'}, 400

        if not is_valid_url(long_url):
            return {'error': 'Invalid URL. Only http and https URLs with valid domains are allowed'}, 400

        if custom_keyword:
            if not is_valid_keyword(custom_keyword):
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
            })
        return {'short_url': short_url}, 201


class URLRedirect(Resource):
    def get(self, short_url):
        now = time.time()

        if USE_JSON_FILE:
            url_data = url_database.get(short_url)
            if not url_data:
                return render_not_found()

            if isinstance(url_data, str):
                url_database[short_url] = {
                    'long_url': url_data,
                    'clicks': 0,
                    'created_at': now,
                    'expires_at': None,
                    'max_views': None,
                }
                url_data = url_database[short_url]

            long_url = url_data['long_url']
            expires_at = url_data.get('expires_at')
            max_views = url_data.get('max_views')
            clicks = url_data.get('clicks', 0)

            if expires_at and now > expires_at:
                del url_database[short_url]
                save_database(url_database, reverse_lookup)
                return render_not_found()

            if max_views is not None and clicks >= max_views:
                del url_database[short_url]
                save_database(url_database, reverse_lookup)
                return render_not_found()

            clicks += 1
            url_data['clicks'] = clicks
            save_database(url_database, reverse_lookup)

            if max_views is not None and clicks >= max_views:
                del url_database[short_url]
                save_database(url_database, reverse_lookup)

            return redirect(long_url)

        url_doc = url_collection.find_one({'short_url': short_url})
        if not url_doc:
            return render_not_found()

        expires_at = url_doc.get('expires_at')
        max_views = url_doc.get('max_views')
        clicks = url_doc.get('clicks', 0)

        if expires_at and now > expires_at:
            url_collection.delete_one({'short_url': short_url})
            return render_not_found()

        if max_views is not None and clicks >= max_views:
            url_collection.delete_one({'short_url': short_url})
            return render_not_found()

        url_collection.update_one({'short_url': short_url}, {'$inc': {'clicks': 1}})
        new_clicks = clicks + 1

        if max_views is not None and new_clicks >= max_views:
            url_collection.delete_one({'short_url': short_url})

        return redirect(url_doc['long_url'])


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
                long_url = url_data
                count = 0
            else:
                long_url = url_data['long_url']
                count = url_data.get('clicks', 0)
        else:
            url_doc = url_collection.find_one({'short_url': keyword})
            if not url_doc:
                return {'error': 'Short URL not found'}, 404
            long_url = url_doc['long_url']
            count = url_doc.get('clicks', 0)

        return {'long_url': long_url, 'clicks': count}, 200


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
            session.regenerate = True
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
    if USE_JSON_FILE:
        urls = []
        for short_url, data in url_database.items():
            if isinstance(data, str):
                data = {
                    'long_url': data,
                    'clicks': 0,
                    'created_at': 0,
                    'expires_at': None,
                    'max_views': None,
                    'ip_address': None,
                }
            urls.append({
                'short_url': short_url,
                'long_url': data.get('long_url', ''),
                'clicks': data.get('clicks', 0),
                'created_at': data.get('created_at', 0),
                'expires_at': data.get('expires_at'),
                'max_views': data.get('max_views'),
                'ip_address': data.get('ip_address'),
            })
    else:
        urls = []
        for doc in url_collection.find():
            urls.append({
                'short_url': doc.get('short_url', ''),
                'long_url': doc.get('long_url', ''),
                'clicks': doc.get('clicks', 0),
                'created_at': doc.get('created_at', 0),
                'expires_at': doc.get('expires_at'),
                'max_views': doc.get('max_views'),
                'ip_address': doc.get('ip_address'),
            })
    return jsonify(urls)


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


api.add_resource(URLShortener, '/shorten')
api.add_resource(ReverseLookup, '/unshorten')
api.add_resource(ClickStats, '/stats/<string:short_url>')
api.add_resource(QRCodeGen, '/qr/<string:short_url>')


@app.route('/')
def index():
    return render_template('index.html')


api.add_resource(URLRedirect, '/<string:short_url>')


if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG', 'false').lower() == 'true', port=5000, threaded=True, host="0.0.0.0")
