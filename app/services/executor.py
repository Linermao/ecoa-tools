"""Tool execution service."""

import os
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from app.utils.config import get_config
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ProjectNotFoundError(Exception):
    """Raised when project directory is not found."""
    pass


class ProjectFileNotFoundError(Exception):
    """Raised when project file is not found in project directory."""
    pass


class ToolExecutor:
    """Service for executing ECOA tools."""

    def __init__(self):
        """Initialize tool executor."""
        self.config = get_config()

    def execute(
        self,
        tool_id: str,
        input_file: str,
        verbose: int = None
    ) -> Dict[str, any]:
        """
        Execute a tool with the given input file.

        Args:
            tool_id: Tool identifier (e.g., 'exvt')
            input_file: Path to input XML file
            verbose: Verbosity level (overrides default)

        Returns:
            Dictionary with execution result:
            {
                'success': bool,
                'tool': str,
                'input_file': str,
                'output_files': List[str],
                'stdout': str,
                'stderr': str,
                'return_code': int,
                'message': str
            }

        Raises:
            ValueError: If tool is not found or file doesn't exist
        """
        # Get tool configuration
        tool_config = self.config.get_tool(tool_id)
        if not tool_config:
            raise ValueError(f"Tool not found: {tool_id}")

        command = tool_config.get('command')
        if not command:
            raise ValueError(f"Command not defined for tool: {tool_id}")

        # Validate input file
        if not os.path.exists(input_file):
            raise ValueError(f"Input file not found: {input_file}")

        # Get verbose level
        if verbose is None:
            verbose = self.config.verbose

        # Get input file directory for context
        input_dir = os.path.dirname(os.path.abspath(input_file))
        input_filename = os.path.basename(input_file)

        # Build command - use filename only, run from file's directory
        cmd = [command, '-p', input_filename, '-v', str(verbose)]

        logger.info(f"Executing: {' '.join(cmd)} in directory: {input_dir}")

        # Execute tool
        try:
            result = subprocess.run(
                cmd,
                cwd=input_dir,  # Run in the input file's directory
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )

            # Find output files (search for generated files in input directory)
            output_files = self._find_output_files(
                input_dir,
                tool_config.get('output_types', [])
            )

            success = result.returncode == 0

            # Copy output files to outputs directory
            copied_files = []
            if success:
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                output_subdir = os.path.join(
                    self.config.outputs_dir,
                    f"{tool_id}_{timestamp}"
                )
                Path(output_subdir).mkdir(parents=True, exist_ok=True)

                for output_file in output_files:
                    dest = os.path.join(output_subdir, os.path.basename(output_file))
                    shutil.copy2(output_file, dest)
                    copied_files.append(dest)
                    logger.debug(f"Copied output file: {output_file} -> {dest}")

            return {
                'success': success,
                'tool': tool_id,
                'command': command,
                'input_file': input_file,
                'output_files': copied_files if success else [],
                'stdout': result.stdout,
                'stderr': result.stderr,
                'return_code': result.returncode,
                'message': self._get_message(result.returncode, tool_id)
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Tool execution timeout: {command}")
            return {
                'success': False,
                'tool': tool_id,
                'command': command,
                'input_file': input_file,
                'output_files': [],
                'stdout': '',
                'stderr': 'Execution timeout (5 minutes)',
                'return_code': -1,
                'message': f'Tool execution timeout: {tool_id}'
            }
        except Exception as e:
            logger.exception(f"Error executing tool: {e}")
            return {
                'success': False,
                'tool': tool_id,
                'command': command,
                'input_file': input_file,
                'output_files': [],
                'stdout': '',
                'stderr': str(e),
                'return_code': -1,
                'message': f'Execution error: {str(e)}'
            }

    def _find_output_files(self, directory: str, extensions: List[str]) -> List[str]:
        """
        Find generated output files in directory.

        Args:
            directory: Directory to search
            extensions: List of file extensions to match (e.g., ['.h', '.c'])

        Returns:
            List of output file paths
        """
        output_files = []

        for ext in extensions:
            # Find all files with the given extension
            for file_path in Path(directory).glob(f"*{ext}"):
                # Skip the input file itself
                if file_path.is_file():
                    output_files.append(str(file_path))

        return sorted(output_files)

    def _get_message(self, return_code: int, tool_id: str) -> str:
        """Get user-friendly message based on return code."""
        if return_code == 0:
            return f'Tool {tool_id} executed successfully'
        elif return_code < 0:
            return f'Tool {tool_id} execution failed'
        else:
            return f'Tool {tool_id} execution failed with code {return_code}'

    def execute_in_project(
        self,
        tool_id: str,
        project_name: str,
        project_file: str,
        verbose: int = None,
        checker: str = None,
        config_file: str = None
    ) -> Dict[str, any]:
        """
        Execute a tool in a project directory.

        Args:
            tool_id: Tool identifier (e.g., 'exvt')
            project_name: Project directory name (under projects_base_dir)
            project_file: Project file name (e.g., 'marx_brothers.project.xml')
            verbose: Verbosity level (overrides default)
            checker: Checker tool for validation (e.g., 'ecoa-exvt')
            config_file: Config file name (for asctg tool)

        Returns:
            Dictionary with execution result:
            {
                'success': bool,
                'tool': str,
                'project_name': str,
                'project_path': str,
                'project_file': str,
                'generated_files': List[str],
                'stdout': str,
                'stderr': str,
                'return_code': int,
                'message': str
            }

        Raises:
            ValueError: If tool is not found
            ProjectNotFoundError: If project directory doesn't exist
            ProjectFileNotFoundError: If project file doesn't exist
        """
        # Get tool configuration
        tool_config = self.config.get_tool(tool_id)
        if not tool_config:
            raise ValueError(f"Tool not found: {tool_id}")

        command = tool_config.get('command')
        if not command:
            raise ValueError(f"Command not defined for tool: {tool_id}")

        # Build project path
        project_path = os.path.join(self.config.projects_base_dir, project_name)

        # Validate project directory exists
        if not os.path.exists(project_path):
            raise ProjectNotFoundError(
                f"Project directory not found: {project_path}"
            )

        if not os.path.isdir(project_path):
            raise ValueError(f"Not a directory: {project_path}")

        # Validate project file exists
        project_file_path = os.path.join(project_path, project_file)
        if not os.path.exists(project_file_path):
            raise ProjectFileNotFoundError(
                f"Project file not found: {project_file_path}"
            )

        # Get verbose level
        if verbose is None:
            verbose = self.config.verbose

        # Check if tool requires a checker parameter
        checker_param = None
        for param in tool_config.get('parameters', []):
            if param.get('flag') == '-k':
                checker_param = param
                break

        # Get checker value - use provided, or default from config
        if checker_param:
            if checker is None:
                checker = checker_param.get('default', 'ecoa-exvt')
            logger.info(f"Using checker: {checker}")

        # Check if tool requires a config file parameter
        config_file_param = None
        for param in tool_config.get('parameters', []):
            if param.get('flag') == '-c':
                config_file_param = param
                break

        # Get config file value
        if config_file_param:
            if not config_file:
                raise ValueError(f"Tool {tool_id} requires config_file parameter")
            # Validate config file exists
            config_file_path = os.path.join(project_path, config_file)
            if not os.path.exists(config_file_path):
                raise ProjectFileNotFoundError(
                    f"Config file not found: {config_file_path}"
                )
            logger.info(f"Using config file: {config_file}")

        # Build command - use project file name only, run from project directory
        cmd = [command, '-p', project_file]

        # Add config file if required
        if config_file:
            cmd.extend(['-c', config_file])

        # Add checker if required
        if checker:
            cmd.extend(['-k', checker])

        # Add verbose flag based on tool's verbose_type
        verbose_type = tool_config.get('verbose_type', 'boolean')
        if verbose_type == 'boolean':
            # Boolean flag: just add -v without value
            cmd.append('-v')
        else:
            # Integer type: add -v with value
            cmd.extend(['-v', str(verbose)])

        logger.info(f"Executing tool '{tool_id}' in project '{project_name}'")
        logger.debug(f"Command: {' '.join(cmd)}")
        logger.debug(f"Working directory: {project_path}")

        # Execute tool in project directory
        try:
            result = subprocess.run(
                cmd,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )

            success = result.returncode == 0

            # Find generated files in project directory
            generated_files = []
            if success:
                output_types = tool_config.get('output_types', [])
                generated_files = self._find_output_files(project_path, output_types)
                logger.info(f"Found {len(generated_files)} generated files")

            return {
                'success': success,
                'tool': tool_id,
                'project_name': project_name,
                'project_path': project_path,
                'project_file': project_file,
                'generated_files': generated_files,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'return_code': result.returncode,
                'message': self._get_message(result.returncode, tool_id)
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Tool execution timeout: {command}")
            return {
                'success': False,
                'tool': tool_id,
                'project_name': project_name,
                'project_path': project_path,
                'project_file': project_file,
                'generated_files': [],
                'stdout': '',
                'stderr': 'Execution timeout (5 minutes)',
                'return_code': -1,
                'message': f'Tool execution timeout: {tool_id}'
            }
        except Exception as e:
            logger.exception(f"Error executing tool: {e}")
            return {
                'success': False,
                'tool': tool_id,
                'project_name': project_name,
                'project_path': project_path,
                'project_file': project_file,
                'generated_files': [],
                'stdout': '',
                'stderr': str(e),
                'return_code': -1,
                'message': f'Execution error: {str(e)}'
            }

    def save_uploaded_file(self, file_content: bytes, filename: str) -> str:
        """
        Save an uploaded file to the uploads directory.

        Args:
            file_content: File content as bytes
            filename: Original filename

        Returns:
            Path to saved file
        """
        uploads_dir = self.config.uploads_dir
        Path(uploads_dir).mkdir(parents=True, exist_ok=True)

        # Generate unique filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_name = Path(filename).stem
        extension = Path(filename).suffix
        unique_filename = f"{base_name}_{timestamp}{extension}"

        file_path = os.path.join(uploads_dir, unique_filename)

        with open(file_path, 'wb') as f:
            f.write(file_content)

        logger.info(f"Saved uploaded file: {filename} -> {file_path}")
        return file_path
