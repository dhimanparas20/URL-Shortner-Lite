from flask import Flask, request, redirect, render_template, jsonify, send_file
from flask_restful import Api, Resource
import string
import random
import os
import json
from pymongo import MongoClient
from dotenv import load_dotenv
import io
import qrcode
from datetime import datetime, timezone


# Load environment variables
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
        return data.get('url_database', {}), data.get('reverse_lookup', {}), data.get('clicks', {})
    return {}, {}, {}

def save_database(url_database, reverse_lookup, clicks):
    if USE_JSON_FILE:
        with open(DB_FILE, 'w') as f:
            json.dump({
                'url_database': url_database,
                'reverse_lookup': reverse_lookup,
                'clicks': clicks
            }, f)

url_database, reverse_lookup, clicks = load_database()

def generate_short_url():
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(6))

class URLShortener(Resource):
    def post(self):
        long_url = request.json['url']
        custom_keyword = request.json.get('custom_keyword')
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        if USE_JSON_FILE:
            if custom_keyword:
                if custom_keyword in url_database:
                    return {'error': 'Custom keyword already taken'}, 400
                short_url = custom_keyword
                url_collection.insert_one({
                    'short_url': short_url,
                    'long_url': long_url,
                    'clicks': 0,
                    'created_at': datetime.now(timezone.utc),
                    'ip_address': user_ip
                })
            else:
                if long_url in reverse_lookup:
                    return {'short_url': reverse_lookup[long_url]}, 200
                short_url = generate_short_url()
            url_database[short_url] = long_url
            reverse_lookup[long_url] = short_url
            clicks[short_url] = 0
            save_database(url_database, reverse_lookup, clicks)
        else:
            if custom_keyword:
                if url_collection.find_one({'short_url': custom_keyword}):
                    return {'error': 'Custom keyword already taken'}, 400
                short_url = custom_keyword
            else:
                existing_url = url_collection.find_one({'long_url': long_url})
                if existing_url:
                    return {'short_url': existing_url['short_url']}, 200
                short_url = generate_short_url()
            url_collection.insert_one({
                'short_url': short_url,
                'long_url': long_url,
                'clicks': 0,
                'created_at': datetime.now(timezone.utc),
                'ip_address': user_ip
            })
        return {'short_url': short_url}, 201

class URLRedirect(Resource):
    def get(self, short_url):
        if USE_JSON_FILE:
            long_url = url_database.get(short_url)
            if long_url:
                clicks[short_url] = clicks.get(short_url, 0) + 1
                save_database(url_database, reverse_lookup, clicks)
        else:
            url_doc = url_collection.find_one({'short_url': short_url})
            long_url = url_doc['long_url'] if url_doc else None
            if url_doc:
                url_collection.update_one({'short_url': short_url}, {'$inc': {'clicks': 1}})
        if long_url:
            return redirect(long_url)
        return {'error': 'URL not found'}, 404

class ReverseLookup(Resource):
    def post(self):
        keyword = request.json.get('keyword')
        if not keyword:
            return {'error': 'No keyword provided'}, 400
        if USE_JSON_FILE:
            long_url = url_database.get(keyword)
            count = clicks.get(keyword, 0)
        else:
            url_doc = url_collection.find_one({'short_url': keyword})
            long_url = url_doc['long_url'] if url_doc else None
            count = url_doc['clicks'] if url_doc and 'clicks' in url_doc else 0
        if long_url:
            return {'long_url': long_url, 'clicks': count}, 200
        return {'error': 'Short URL not found'}, 404

class ClickStats(Resource):
    def get(self, short_url):
        if USE_JSON_FILE:
            count = clicks.get(short_url, 0)
        else:
            url_doc = url_collection.find_one({'short_url': short_url})
            count = url_doc['clicks'] if url_doc else 0
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
