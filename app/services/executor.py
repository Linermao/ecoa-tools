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

    def _find_cmakelists_dir(self, project_path: str) -> str:
        """
        Find directory containing CMakeLists.txt in project.

        Args:
            project_path: Project root directory

        Returns:
            Path to directory containing CMakeLists.txt

        Raises:
            FileNotFoundError: If CMakeLists.txt not found
        """
        # Common output directory names
        common_output_dirs = ["6-output", "6-Output", "Output", "output", "build-output"]

        # First check common output directories
        for dir_name in common_output_dirs:
            candidate = os.path.join(project_path, dir_name)
            if os.path.exists(candidate) and os.path.isdir(candidate):
                # Check if CMakeLists.txt is in this directory
                cmake_path = os.path.join(candidate, "CMakeLists.txt")
                if os.path.exists(cmake_path):
                    return candidate
                # Check parent directory (as per cmake_generator.py pattern)
                parent_dir = os.path.dirname(candidate)
                cmake_path = os.path.join(parent_dir, "CMakeLists.txt")
                if os.path.exists(cmake_path):
                    return parent_dir

        # Recursive search in project directory
        for root, dirs, files in os.walk(project_path):
            if "CMakeLists.txt" in files:
                return root

        raise FileNotFoundError(f"CMakeLists.txt not found in project: {project_path}")

    def compile_project(
        self,
        project_path: str,
        log_library: str = "log4cplus",
        cmake_options: List[str] = None,
        timeout: int = 600
    ) -> Dict[str, any]:
        """
        Execute CMake compilation in project directory.

        Args:
            project_path: Project root directory
            log_library: Logging library to use (log4cplus, zlog, lttng)
            cmake_options: Additional CMake options
            timeout: Compilation timeout in seconds

        Returns:
            Dictionary with compilation results
        """
        try:
            # Find CMakeLists.txt directory
            cmake_dir = self._find_cmakelists_dir(project_path)
            logger.info(f"Compiling in directory: {cmake_dir}")

            # Build compile commands
            build_dir = os.path.join(cmake_dir, "build")

            # Ensure build directory exists
            Path(build_dir).mkdir(parents=True, exist_ok=True)

            # Get compile configuration from config
            tool_config = self.config.get_tool("ldp")
            compile_config = tool_config.get("compile", {}) if tool_config else {}

            # Get default options from config
            default_cmake_options = compile_config.get("cmake_options", [])
            default_make_options = compile_config.get("make_options", ["-j"])

            # Replace ${log_library} placeholder in cmake options
            processed_cmake_options = []
            for opt in (cmake_options or default_cmake_options):
                processed_cmake_options.append(opt.replace("${log_library}", log_library))

            # Build CMake command
            cmake_cmd = ["cmake", "..", f"-DLDP_LOG_USE={log_library}"]
            cmake_cmd.extend(processed_cmake_options)

            # Build make command
            make_cmd = ["make"]
            make_cmd.extend(default_make_options)

            # Execute CMake
            logger.info(f"Running CMake in {build_dir}")
            cmake_result = subprocess.run(
                cmake_cmd,
                cwd=build_dir,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            cmake_success = cmake_result.returncode == 0

            # Execute make only if CMake succeeded
            make_result = None
            if cmake_success:
                logger.info(f"Running make in {build_dir}")
                make_result = subprocess.run(
                    make_cmd,
                    cwd=build_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
            else:
                # Create dummy make result for consistency
                make_result = subprocess.CompletedProcess(
                    args=make_cmd,
                    returncode=-1,
                    stdout="",
                    stderr="CMake failed, make not executed"
                )

            # Find executable files in build/bin or build directory
            executable_files = []
            bin_dir = os.path.join(build_dir, "bin")
            if os.path.exists(bin_dir):
                for file in os.listdir(bin_dir):
                    file_path = os.path.join(bin_dir, file)
                    if os.path.isfile(file_path) and os.access(file_path, os.X_OK):
                        executable_files.append(file)
            else:
                # Check build directory directly
                for file in os.listdir(build_dir):
                    file_path = os.path.join(build_dir, file)
                    if os.path.isfile(file_path) and os.access(file_path, os.X_OK):
                        executable_files.append(file)

            compile_success = cmake_success and (make_result.returncode == 0 if make_result else False)

            # Combine stdout and stderr
            combined_stdout = ""
            combined_stderr = ""
            if cmake_result:
                combined_stdout += f"=== CMake Output ===\n{cmake_result.stdout}\n"
                combined_stderr += f"=== CMake Errors ===\n{cmake_result.stderr}\n"
            if make_result:
                combined_stdout += f"=== Make Output ===\n{make_result.stdout}\n"
                combined_stderr += f"=== Make Errors ===\n{make_result.stderr}\n"

            return {
                "compile_success": compile_success,
                "compile_stdout": combined_stdout.strip(),
                "compile_stderr": combined_stderr.strip(),
                "compile_return_code": make_result.returncode if make_result else cmake_result.returncode,
                "executable_files": executable_files,
                "cmake_dir": cmake_dir,
                "build_dir": build_dir,
                "cmake_command": " ".join(cmake_cmd),
                "make_command": " ".join(make_cmd) if make_cmd else ""
            }

        except subprocess.TimeoutExpired:
            logger.error(f"Compilation timeout in project: {project_path}")
            return {
                "compile_success": False,
                "compile_stdout": "",
                "compile_stderr": "Compilation timeout",
                "compile_return_code": -1,
                "executable_files": [],
                "cmake_dir": "",
                "build_dir": "",
                "cmake_command": "",
                "make_command": ""
            }
        except FileNotFoundError as e:
            logger.error(f"CMakeLists.txt not found: {e}")
            return {
                "compile_success": False,
                "compile_stdout": "",
                "compile_stderr": str(e),
                "compile_return_code": -1,
                "executable_files": [],
                "cmake_dir": "",
                "build_dir": "",
                "cmake_command": "",
                "make_command": ""
            }
        except Exception as e:
            logger.exception(f"Unexpected compilation error: {e}")
            return {
                "compile_success": False,
                "compile_stdout": "",
                "compile_stderr": f"Unexpected error: {str(e)}",
                "compile_return_code": -1,
                "executable_files": [],
                "cmake_dir": "",
                "build_dir": "",
                "cmake_command": "",
                "make_command": ""
            }

    def execute_in_project(
        self,
        tool_id: str,
        project_name: str,
        project_file: str,
        verbose: int = None,
        checker: str = None,
        config_file: str = None,
        compile: bool = False,
        log_library: str = None,
        cmake_options: List[str] = None
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
            compile: Whether to compile the project after tool execution (for ldp tool)
            log_library: Logging library to use for compilation (log4cplus, zlog, lttng)
            cmake_options: Additional CMake options for compilation

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
                'message': str,
                'compile_success': bool (optional),
                'compile_stdout': str (optional),
                'compile_stderr': str (optional),
                'compile_return_code': int (optional),
                'executable_files': List[str] (optional),
                'cmake_dir': str (optional),
                'build_dir': str (optional)
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

            # Compile project if requested and tool is ldp
            compile_result = {}
            if success and compile and tool_id == 'ldp':
                # Get compile configuration
                ldp_config = self.config.get_tool('ldp')
                compile_config = ldp_config.get('compile', {}) if ldp_config else {}

                # Determine log_library value
                if log_library is None:
                    log_library = compile_config.get('default_log_library', 'log4cplus')

                # Determine cmake_options
                if cmake_options is None:
                    cmake_options = compile_config.get('cmake_options', [])

                # Determine timeout
                timeout = compile_config.get('timeout', 600)

                logger.info(f"Starting compilation with log_library={log_library}")
                compile_result = self.compile_project(
                    project_path=project_path,
                    log_library=log_library,
                    cmake_options=cmake_options,
                    timeout=timeout
                )
                logger.info(f"Compilation {'succeeded' if compile_result.get('compile_success') else 'failed'}")

            # Prepare result dictionary
            result_dict = {
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

            # Add compilation results if available
            if compile_result:
                result_dict.update({
                    'compile_success': compile_result.get('compile_success', False),
                    'compile_stdout': compile_result.get('compile_stdout', ''),
                    'compile_stderr': compile_result.get('compile_stderr', ''),
                    'compile_return_code': compile_result.get('compile_return_code', -1),
                    'executable_files': compile_result.get('executable_files', []),
                    'cmake_dir': compile_result.get('cmake_dir', ''),
                    'build_dir': compile_result.get('build_dir', '')
                })

            return result_dict

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
