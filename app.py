from flask import Flask, request, redirect, render_template
from flask_restful import Api, Resource
import string
import random
import os
import json
from pymongo import MongoClient
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
api = Api(app)

# Flag to decide whether to use external JSON file or MongoDB
USE_JSON_FILE = os.getenv('USE_JSON_FILE', 'false').lower() == 'true'

# File-based database
DB_FILE = 'url_database.json'

# MongoDB setup
MONGO_URI = os.getenv('MONGO_URI')
client = MongoClient(MONGO_URI)
db = client['url_shortener']
url_collection = db['urls']

def load_database():
    if USE_JSON_FILE and os.path.exists(DB_FILE):
        # Load from the JSON file
        with open(DB_FILE, 'r') as f:
            data = json.load(f)
        return data.get('url_database', {}), data.get('reverse_lookup', {})
    # Return empty dictionaries if using MongoDB (we'll query it directly)
    return {}, {}

def save_database(url_database, reverse_lookup):
    if USE_JSON_FILE:
        # Save to the JSON file
        with open(DB_FILE, 'w') as f:
            json.dump({
                'url_database': url_database,
                'reverse_lookup': reverse_lookup
            }, f)
    # No action required for MongoDB (it's saved directly in the operations)

# Load the database (only if using JSON file)
url_database, reverse_lookup = load_database()

def generate_short_url():
    characters = string.ascii_letters + string.digits
    short_url = ''.join(random.choice(characters) for _ in range(6))
    return short_url

class URLShortener(Resource):
    def post(self):
        long_url = request.json['url']
        custom_keyword = request.json.get('custom_keyword')  # Get custom keyword if provided
        
        if USE_JSON_FILE:
            # JSON file-based logic
            if custom_keyword:
                if custom_keyword in url_database:
                    return {'error': 'Custom keyword already taken'}, 400
                short_url = custom_keyword
            else:
                if long_url in reverse_lookup:
                    return {'short_url': reverse_lookup[long_url]}, 200
                short_url = generate_short_url()

            url_database[short_url] = long_url
            reverse_lookup[long_url] = short_url
            save_database(url_database, reverse_lookup)
        else:
            # MongoDB logic
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
                'long_url': long_url
            })
        
        return {'short_url': short_url}, 201

class URLRedirect(Resource):
    def get(self, short_url):
        if USE_JSON_FILE:
            long_url = url_database.get(short_url)
        else:
            url_doc = url_collection.find_one({'short_url': short_url})
            long_url = url_doc['long_url'] if url_doc else None
        
        if long_url:
            return redirect(long_url)
        return {'error': 'URL not found'}, 404

api.add_resource(URLShortener, '/shorten')
api.add_resource(URLRedirect, '/<string:short_url>')

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=False, port=5000, threaded=True, host="0.0.0.0")

