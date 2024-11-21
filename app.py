from flask import Flask, request, redirect, render_template
from flask_restful import Api, Resource
import string
import random
import os
import json

app = Flask(__name__)
api = Api(app)

# File-based database
DB_FILE = 'url_database.json'

def load_database():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            data = json.load(f)
        return data.get('url_database', {}), data.get('reverse_lookup', {})
    return {}, {}

def save_database(url_database, reverse_lookup):
    with open(DB_FILE, 'w') as f:
        json.dump({
            'url_database': url_database,
            'reverse_lookup': reverse_lookup
        }, f)

url_database, reverse_lookup = load_database()

def generate_short_url():
    characters = string.ascii_letters + string.digits
    short_url = ''.join(random.choice(characters) for _ in range(6))
    return short_url

class URLShortener(Resource):
    def post(self):
        long_url = request.json['url']
        
        # Check if the URL has already been shortened
        if long_url in reverse_lookup:
            return {'short_url': reverse_lookup[long_url]}, 200
        
        # If not, create a new short URL
        short_url = generate_short_url()
        url_database[short_url] = long_url
        reverse_lookup[long_url] = short_url
        
        # Save the updated database
        save_database(url_database, reverse_lookup)
        
        return {'short_url': short_url}, 201

class URLRedirect(Resource):
    def get(self, short_url):
        long_url = url_database.get(short_url)
        if long_url:
            return redirect(long_url)
        return {'error': 'URL not found'}, 404

api.add_resource(URLShortener, '/shorten')
api.add_resource(URLRedirect, '/<string:short_url>')

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000, threaded=True, host="0.0.0.0")

