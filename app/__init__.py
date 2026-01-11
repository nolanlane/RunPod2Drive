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

    return app
