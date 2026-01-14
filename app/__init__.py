import os
from flask import Flask
from flask_socketio import SocketIO
from app.config import AppConfig

# Initialize SocketIO globally (so we can import it if needed, though we use current_app extensions mostly)
socketio = SocketIO(cors_allowed_origins="*", async_mode='eventlet')

def create_app(config_class=AppConfig):
    app = Flask(__name__, template_folder='templates', static_folder='static')

    # Load config
    config = config_class()
    app.config['SECRET_KEY'] = config.SECRET_KEY

    # Set env vars that might be needed by libraries
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = config.OAUTHLIB_INSECURE_TRANSPORT

    # Initialize extensions
    socketio.init_app(app, async_mode='eventlet')

    # Register blueprints
    from app.api.routes import api
    from app.views import views

    app.register_blueprint(api)
    app.register_blueprint(views)

    # Add Security Headers
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # Basic CSP - permissive for scripts/styles to allow CDN usage as seen in index.html
        # but restricting object-src and base-uri for security
        response.headers['Content-Security-Policy'] = "default-src 'self' https:; script-src 'self' 'unsafe-inline' https:; style-src 'self' 'unsafe-inline' https:; img-src 'self' data: https:; object-src 'none'; base-uri 'self';"
        return response

    return app
