"""Logging configuration for ECOA Tools API."""

import logging
import os
import sys
from pathlib import Path
from datetime import datetime
import uuid


class RequestContextFilter(logging.Filter):
    """Filter to add request context to log records."""

    def filter(self, record):
        record.request_id = getattr(record, 'request_id', 'N/A')
        return True


def setup_logger(name: str, log_dir: str = "logs", level: int = logging.INFO) -> logging.Logger:
    """
    Set up a logger with file and console handlers.

    Args:
        name: Logger name
        log_dir: Directory to store log files
        level: Logging level

    Returns:
        Configured logger instance
    """
    # Create logger
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Clear existing handlers
    logger.handlers.clear()

    # Create logs directory if it doesn't exist
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Create formatters
    detailed_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(name)s | %(request_id)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_formatter = logging.Formatter(
        fmt='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    )

    # Add context filter
    context_filter = RequestContextFilter()

    # File handler - detailed logs
    log_file = os.path.join(log_dir, f"{name}_{datetime.now().strftime('%Y%m%d')}.log")
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(detailed_formatter)
    file_handler.addFilter(context_filter)
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(context_filter)
    logger.addHandler(console_handler)

    # Prevent propagation to root logger
    logger.propagate = False

    return logger


def get_logger(name: str) -> logging.Logger:
    """Get an existing logger or create a new one."""
    return logging.getLogger(name)


class RequestContext:
    """Context manager for request-scoped logging."""

    def __init__(self, logger: logging.Logger, request_id: str = None):
        self.logger = logger
        self.request_id = request_id or str(uuid.uuid4())[:8]
        self.extra = {'request_id': self.request_id}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def info(self, msg, *args, **kwargs):
        # Merge extra dict to avoid overwriting
        merged_extra = {**self.extra, **kwargs.pop('extra', {})}
        self.logger.info(msg, *args, extra=merged_extra, **kwargs)

    def debug(self, msg, *args, **kwargs):
        merged_extra = {**self.extra, **kwargs.pop('extra', {})}
        self.logger.debug(msg, *args, extra=merged_extra, **kwargs)

    def warning(self, msg, *args, **kwargs):
        merged_extra = {**self.extra, **kwargs.pop('extra', {})}
        self.logger.warning(msg, *args, extra=merged_extra, **kwargs)

    def error(self, msg, *args, **kwargs):
        merged_extra = {**self.extra, **kwargs.pop('extra', {})}
        self.logger.error(msg, *args, extra=merged_extra, **kwargs)

    def exception(self, msg, *args, **kwargs):
        merged_extra = {**self.extra, **kwargs.pop('extra', {})}
        self.logger.exception(msg, *args, extra=merged_extra, **kwargs)
