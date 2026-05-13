import io
import json
import os
import random
import re
import string
import time

import qrcode
from dotenv import load_dotenv
from flask import Flask, make_response, request, redirect, render_template, send_file
from flask_restful import Api, Resource
from pymongo import MongoClient

load_dotenv()
app = Flask(__name__)
api = Api(app)

USE_JSON_FILE = os.getenv('USE_JSON_FILE', 'false').lower() == 'true'
DB_FILE = 'url_database.json'

MONGO_URI = os.getenv('MONGO_URI')
client = MongoClient(MONGO_URI)
db = client['url_shortener']
url_collection = db['urls']


def load_database():
    if USE_JSON_FILE and os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            data = json.load(f)
        return data.get('url_database', {}), data.get('reverse_lookup', {})
    return {}, {}


def save_database(url_database, reverse_lookup):
    if USE_JSON_FILE:
        with open(DB_FILE, 'w') as f:
            json.dump({
                'url_database': url_database,
                'reverse_lookup': reverse_lookup,
            }, f)


url_database, reverse_lookup = load_database()


def generate_short_url():
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(6))


def trim_keyword(keyword):
    return re.sub(r'[^a-zA-Z0-9]', '_', keyword)


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
        long_url = request.json['url']
        custom_keyword = request.json.get('custom_keyword')
        expiry_value = request.json.get('expiry_value')
        expiry_unit = request.json.get('expiry_unit')
        max_views_raw = request.json.get('max_views')
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

        expires_at = calculate_expires_at(expiry_value, expiry_unit)

        try:
            max_views = int(max_views_raw) if max_views_raw is not None else None
            if max_views is not None and max_views <= 0:
                max_views = None
        except (ValueError, TypeError):
            max_views = None

        now = time.time()

        if USE_JSON_FILE:
            if custom_keyword:
                if custom_keyword in url_database:
                    return {'error': 'Custom keyword already taken'}, 400
                short_url = trim_keyword(custom_keyword)
            else:
                if long_url in reverse_lookup:
                    return {'short_url': reverse_lookup[long_url]}, 200
                short_url = generate_short_url()

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
                if len(custom_keyword) < 3:
                    return {'error': 'Custom keyword must be at least 3 characters long'}, 400
                elif len(custom_keyword) > 30:
                    return {'error': 'Custom keyword must be at most 30 characters long'}, 400
                if url_collection.find_one({'short_url': custom_keyword}):
                    return {'error': 'Custom keyword already taken'}, 400
                short_url = trim_keyword(custom_keyword)
            else:
                existing_url = url_collection.find_one({'long_url': long_url})
                if existing_url:
                    return {'short_url': existing_url['short_url']}, 200
                short_url = generate_short_url()

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
        keyword = request.json.get('keyword')
        if not keyword:
            return {'error': 'No keyword provided'}, 400
        if 'http' in keyword or 'https' in keyword:
            if keyword.endswith('/'):
                keyword = keyword.split('/')[-2]
            else:
                keyword = keyword.split('/')[-1]

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
        url = request.host_url + short_url
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return send_file(buf, mimetype='image/png')


api.add_resource(URLShortener, '/shorten')
api.add_resource(URLRedirect, '/<string:short_url>')
api.add_resource(ReverseLookup, '/unshorten')
api.add_resource(ClickStats, '/stats/<string:short_url>')
api.add_resource(QRCodeGen, '/qr/<string:short_url>')


@app.route('/')
def index():
    return render_template('index.html')


if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True, host="0.0.0.0")
