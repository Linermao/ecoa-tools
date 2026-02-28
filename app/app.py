"""Flask application factory."""

from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from app.utils.config import get_config
from app.utils.logger import setup_logger, get_logger
from app.routes.tools import bp as tools_bp


def create_app(config_path: str = "config.yaml"):
    """
    Create and configure Flask application.

    Args:
        config_path: Path to configuration file

    Returns:
        Configured Flask application
    """
    # Load configuration
    config = get_config(config_path)

    # Setup logger
    logger = setup_logger(
        'ecoa_tools',
        log_dir=config.logs_dir,
        level=10  # DEBUG level
    )
    logger.info("Initializing ECOA Tools API")

    # Create Flask app
    app = Flask(__name__)

    # Configuration
    app.config['MAX_CONTENT_LENGTH'] = config.max_upload_size
    app.config['DEBUG'] = config.server_debug

    # Register blueprints
    app.register_blueprint(tools_bp)

    # Root endpoint
    @app.route('/')
    def index():
        """API root endpoint."""
        return jsonify({
            'name': config.get('api.title', 'ECOA Tools API'),
            'version': config.get('api.version', '1.0.0'),
            'description': config.get('api.description', ''),
            'endpoints': {
                'tools': '/api/tools',
                'execute': '/api/tools/execute',
                'health': '/health'
            }
        })

    # Health check endpoint
    @app.route('/health')
    def health():
        """Health check endpoint."""
        return jsonify({
            'status': 'healthy',
            'service': 'ecoa-tools-api'
        })

    # Error handlers
    @app.errorhandler(HTTPException)
    def handle_http_exception(e):
        """Handle HTTP exceptions."""
        return jsonify({
            'success': False,
            'error': e.name,
            'message': e.description
        }), e.code

    @app.errorhandler(Exception)
    def handle_exception(e):
        """Handle unexpected exceptions."""
        logger.exception(f"Unhandled exception: {e}")
        return jsonify({
            'success': False,
            'error': 'Internal Server Error',
            'message': str(e) if app.config['DEBUG'] else 'An unexpected error occurred'
        }), 500

    logger.info("ECOA Tools API initialized successfully")
    return app
