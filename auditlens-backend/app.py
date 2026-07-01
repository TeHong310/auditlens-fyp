from flask import Flask, jsonify
from flask_jwt_extended import JWTManager
from flask_cors import CORS
from config import Config
from routes.auth import auth_bp
from routes.documents import documents_bp
from routes.matching import matching_bp
from routes.reviews import reviews_bp
from routes.admin import admin_bp
from routes.ocr_review import ocr_review_bp

app = Flask(__name__)

app.config['JWT_SECRET_KEY']           = Config.JWT_SECRET_KEY
app.config['JWT_ACCESS_TOKEN_EXPIRES'] = Config.JWT_ACCESS_TOKEN_EXPIRES
jwt = JWTManager(app)

CORS(app, origins=Config.FRONTEND_ORIGINS, supports_credentials=True)

app.register_blueprint(auth_bp,      url_prefix='/auth')
app.register_blueprint(documents_bp, url_prefix='/documents')
app.register_blueprint(matching_bp,  url_prefix='/matching')
app.register_blueprint(reviews_bp,   url_prefix='/reviews')
app.register_blueprint(admin_bp,     url_prefix='/admin')
app.register_blueprint(ocr_review_bp, url_prefix='/ocr-review')

@app.route('/')
def hello_world():
    return jsonify({'message': 'Welcome to AuditLens API!'})

if __name__ == '__main__':
    app.run(debug=True)