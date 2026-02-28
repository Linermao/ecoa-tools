"""Configuration loading and management."""

import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional


class Config:
    """Application configuration."""

    def __init__(self, config_path: str = "config.yaml"):
        """
        Load configuration from YAML file.

        Args:
            config_path: Path to configuration file
        """
        self.config_path = config_path
        self._config: Dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from YAML file."""
        if not os.path.exists(self.config_path):
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")

        with open(self.config_path, 'r', encoding='utf-8') as f:
            self._config = yaml.safe_load(f)

    def reload(self) -> None:
        """Reload configuration from file."""
        self._load_config()

    def get(self, key: str, default: Any = None) -> Any:
        """
        Get configuration value by dot-notation key.

        Args:
            key: Configuration key (e.g., 'tools.exvt.command')
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        keys = key.split('.')
        value = self._config

        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default

        return value

    @property
    def verbose(self) -> int:
        """Get global verbose setting."""
        return self.get('verbose', 3)

    @property
    def uploads_dir(self) -> str:
        """Get uploads directory path."""
        return self.get('uploads_dir', 'uploads')

    @property
    def outputs_dir(self) -> str:
        """Get outputs directory path."""
        return self.get('outputs_dir', 'outputs')

    @property
    def logs_dir(self) -> str:
        """Get logs directory path."""
        return self.get('logs_dir', 'logs')

    @property
    def projects_base_dir(self) -> str:
        """Get projects base directory path."""
        return self.get('projects_base_dir', 'projects')

    @property
    def tools(self) -> Dict[str, Any]:
        """Get all tools configuration."""
        return self.get('tools', {})

    def get_tool(self, tool_id: str) -> Optional[Dict[str, Any]]:
        """
        Get configuration for a specific tool.

        Args:
            tool_id: Tool identifier

        Returns:
            Tool configuration or None if not found
        """
        return self.get(f'tools.{tool_id}')

    def get_tool_command(self, tool_id: str) -> Optional[str]:
        """Get command for a specific tool."""
        tool = self.get_tool(tool_id)
        return tool.get('command') if tool else None

    @property
    def max_upload_size(self) -> int:
        """Get maximum upload size in bytes."""
        return self.get('api.max_upload_size', 16777216)

    @property
    def server_host(self) -> str:
        """Get server host."""
        return self.get('server.host', '0.0.0.0')

    @property
    def server_port(self) -> int:
        """Get server port."""
        return self.get('server.port', 5000)

    @property
    def server_debug(self) -> bool:
        """Get server debug mode."""
        return self.get('server.debug', False)

    def ensure_directories(self) -> None:
        """Ensure all required directories exist."""
        for dir_path in [self.uploads_dir, self.outputs_dir, self.logs_dir]:
            Path(dir_path).mkdir(parents=True, exist_ok=True)


# Global configuration instance
_config: Optional[Config] = None


def get_config(config_path: str = "config.yaml") -> Config:
    """
    Get global configuration instance.

    Args:
        config_path: Path to configuration file

    Returns:
        Configuration instance
    """
    global _config
    if _config is None:
        _config = Config(config_path)
        _config.ensure_directories()
    return _config
