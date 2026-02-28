"""Main entry point for ECOA Tools API."""

import sys
from app.app import create_app
from app.utils.config import get_config
from app.utils.logger import get_logger


def main():
    """Run the Flask application."""
    # Get configuration
    config = get_config()

    # Create app
    app = create_app()

    # Run server
    logger = get_logger('ecoa_tools')
    logger.info(f"Starting server on {config.server_host}:{config.server_port}")

    app.run(
        host=config.server_host,
        port=config.server_port,
        debug=config.server_debug
    )


if __name__ == '__main__':
    main()
