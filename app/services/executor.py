"""Tool execution service."""

import os
import subprocess
import shutil
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from datetime import datetime

from app.utils.config import get_config
from app.utils.logger import setup_logger

# Initialize logger for this module
logger = setup_logger('app.services.executor')


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

    def _create_vscode_launch_config(
        self,
        project_path: str,
        project_name: str,
        build_dir: str,
        cmake_dir: str
    ) -> None:
        """
        Create VSCode launch.json configuration for the project.

        Creates .vscode/launch.json in the project directory with debug configuration
        for the generated executables.

        Args:
            project_path: Project root directory
            project_name: Name of the project
            executable_files: List of executable file names
            build_dir: Build directory path
            cmake_dir: CMake directory path (for relative path calculation)
        """

        # Create .vscode directory
        vscode_dir = os.path.join(project_path, ".vscode")
        Path(vscode_dir).mkdir(parents=True, exist_ok=True)

        # Calculate relative path from project_path to executable
        # cmake_dir is like: project_path/6-Output
        # build_dir is like: cmake_dir/build
        # executable is like: build_dir/bin/executable

        # Get relative path from project_path to build_dir
        try:
            rel_build_dir = os.path.relpath(build_dir, project_path)
        except ValueError:
            # On different drives on Windows, use absolute path
            rel_build_dir = build_dir

        # Build the program path for VSCode (relative to workspace folder)
        # ${workspaceFolder} will be replaced by VSCode with the project root
        program_path = f"${{workspaceFolder}}/{rel_build_dir}/bin/platform"

        # Build cwd path
        cwd_path = f"${{workspaceFolder}}/{rel_build_dir}/bin"

        launch_json_content = {
            "version": "0.2.0",
            "configurations": [
                {
                    "name": f"Debug platform",
                    "type": "cppdbg",
                    "request": "launch",
                    "program": program_path,
                    "cwd": cwd_path,
                    "args": [],
                    "stopAtEntry": True,
                    "MIMode": "gdb",
                }
            ]
        }

        launch_json_path = os.path.join(vscode_dir, "launch.json")
        with open(launch_json_path, 'w') as f:
            json.dump(launch_json_content, f, indent=2)

        logger.info(f"Created VSCode launch.json at: {launch_json_path}")

    def _handle_ldp_compilation(
        self,
        project_path: str,
        project_name: str,
        compile_flag: Optional[bool],
        log_library: Optional[str],
        cmake_options: Optional[List[str]]
    ) -> Dict[str, any]:
        """
        Handle compilation logic for LDP tool.

        Args:
            project_path: Project root directory
            project_name: Name of the project
            compile_flag: Whether to compile (True/False/None for auto)
            log_library: Logging library to use
            cmake_options: Additional CMake options

        Returns:
            Compilation result dictionary, or empty dict if compilation was skipped
        """
        tool_config = self.config.get_tool("ldp")
        compile_config = tool_config.get('compile', {}) if tool_config else {}

        # Determine if compilation should be performed
        should_compile = self._should_compile(compile_flag, compile_config)

        if not should_compile:
            logger.info(f"Compilation skipped for LDP (compile={compile_flag}, config.enabled={compile_config.get('enabled', False)})")
            return {}

        # Determine log_library value
        if log_library is None:
            log_library = compile_config.get('default_log_library', 'log4cplus')

        # Determine cmake_options
        if cmake_options is None:
            cmake_options = compile_config.get('cmake_options', [])

        # Determine timeout
        timeout = compile_config.get('timeout', 600)

        logger.info(f"Starting LDP compilation with log_library={log_library}")
        compile_result = self._compile_ldp_project(
            project_path=project_path,
            log_library=log_library,
            cmake_options=cmake_options,
            timeout=timeout,
        )
        logger.info(f"LDP compilation {'succeeded' if compile_result.get('compile_success') else 'failed'}")

        # Create VSCode launch.json if compilation succeeded
        if compile_result.get('compile_success'):
            self._create_vscode_launch_config(
                project_path=project_path,
                project_name=project_name,
                build_dir=compile_result.get('build_dir', ''),
                cmake_dir=compile_result.get('cmake_dir', '')
            )

        return compile_result

    def _handle_csmgvt_compilation(
        self,
        project_path: str,
        compile_flag: Optional[bool]
    ) -> Dict[str, any]:
        """
        Handle compilation logic for csmgvt tool.

        Args:
            project_path: Project root directory
            compile_flag: Whether to compile (True/False/None for auto)

        Returns:
            Compilation result dictionary, or empty dict if compilation was skipped
        """
        tool_config = self.config.get_tool("csmgvt")
        compile_config = tool_config.get('compile', {}) if tool_config else {}

        # Determine if compilation should be performed
        should_compile = self._should_compile(compile_flag, compile_config)

        if not should_compile:
            logger.info(f"Compilation skipped for csmgvt (compile={compile_flag}, config.enabled={compile_config.get('enabled', False)})")
            return {}

        # Determine timeout
        timeout = compile_config.get('timeout', 600)

        logger.info("Starting csmgvt compilation")
        compile_result = self._compile_csmgvt_project(
            project_path=project_path,
            timeout=timeout,
        )
        logger.info(f"csmgvt compilation {'succeeded' if compile_result.get('compile_success') else 'failed'}")
        return compile_result

    def _should_compile(self, compile_flag: Optional[bool], compile_config: Dict) -> bool:
        """
        Determine if compilation should be performed based on flag and config.

        Args:
            compile_flag: User-provided compile flag (True/False/None)
            compile_config: Compilation configuration from tool config

        Returns:
            True if compilation should be performed, False otherwise
        """
        if compile_flag is True:
            return True
        elif compile_flag is False:
            return False
        else:  # compile_flag is None, use configuration
            return compile_config.get('enabled', False)

    def _get_message_for_tool(
        self,
        return_code: int,
        tool_id: str,
        compile_result: Dict[str, any]
    ) -> str:
        """
        Get user-friendly message based on return code and compilation result.

        For LDP/csmgvt:
            - Tool success + compile success = "executed and compiled successfully"
            - Tool success + compile failure = "executed successfully but compilation failed"
            - Tool failure = "execution failed"

        Args:
            return_code: Tool execution return code
            tool_id: Tool identifier
            compile_result: Compilation result dictionary (if any)

        Returns:
            User-friendly message
        """
        if return_code == 0:
            # Tool executed successfully
            if compile_result:
                compile_success = compile_result.get('compile_success', False)
                if compile_success:
                    return f'Tool {tool_id} executed and compiled successfully'
                else:
                    return f'Tool {tool_id} executed successfully but compilation failed'
            else:
                return f'Tool {tool_id} executed successfully'
        elif return_code < 0:
            return f'Tool {tool_id} execution failed'
        else:
            return f'Tool {tool_id} execution failed with code {return_code}'

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

    def _get_pkg_config_path(self, package: str) -> str:
        """
        Get package installation path using pkg-config with fallback methods.

        Tries multiple methods in order:
        1. Parse include path from --cflags (most reliable, works on Ubuntu)
        2. Fall back to --variable=prefix
        3. Return common system paths if pkg-config fails

        Args:
            package: Package name (e.g., 'log4cplus', 'apr-1', 'cunit')

        Returns:
            Installation path (returns '/usr' if package is installed but path not found)

        Raises:
            FileNotFoundError: If pkg-config is not found or package is not installed
        """
        # Check if pkg-config exists
        if not shutil.which("pkg-config"):
            raise FileNotFoundError("pkg-config not found. Please install pkg-config.")

        # Method 1: Parse from --cflags (most reliable on Ubuntu)
        try:
            result = subprocess.run(
                ["pkg-config", "--cflags", package],
                capture_output=True,
                text=True,
                check=True
            )
            cflags = result.stdout.strip()
            # Parse -I flags to get include paths
            for part in cflags.split():
                if part.startswith("-I"):
                    path = part[2:]  # Remove -I prefix
                    # Remove trailing /include or /include/apr-1 etc.
                    if "/include" in path:
                        path = path.split("/include")[0]
                        if path:
                            logger.debug(f"Found {package} path from cflags: {path}")
                            return path
        except subprocess.CalledProcessError:
            logger.debug(f"pkg-config --cflags failed for {package}")

        # Method 2: Try --variable=prefix (original method)
        try:
            result = subprocess.run(
                ["pkg-config", "--variable=prefix", package],
                capture_output=True,
                text=True,
                check=True
            )
            prefix = result.stdout.strip()
            if prefix:
                logger.debug(f"Found {package} path from prefix: {prefix}")
                return prefix
        except subprocess.CalledProcessError:
            logger.debug(f"pkg-config --variable=prefix failed for {package}")

        # Method 3: Try --variable=libdir (sometimes more reliable)
        try:
            result = subprocess.run(
                ["pkg-config", "--variable=libdir", package],
                capture_output=True,
                text=True,
                check=True
            )
            libdir = result.stdout.strip()
            if libdir:
                # libdir is usually /usr/lib or /usr/lib/x86_64-linux-gnu
                # Get the parent and strip /lib or /lib64
                path = libdir
                if "/lib" in path:
                    path = path.split("/lib")[0]
                if path:
                    logger.debug(f"Found {package} path from libdir: {path}")
                    return path
        except subprocess.CalledProcessError:
            logger.debug(f"pkg-config --variable=libdir failed for {package}")

        # Method 4: Verify package exists and return default /usr path
        try:
            subprocess.run(
                ["pkg-config", "--exists", package],
                check=True,
                capture_output=True
            )
            logger.info(f"pkg-config found {package} but couldn't determine path, using /usr")
            return "/usr"
        except subprocess.CalledProcessError:
            raise FileNotFoundError(f"Package {package} not found by pkg-config. Please install it.")

    def _compile_ldp_project(
        self,
        project_path: str,
        log_library: str = "log4cplus",
        cmake_options: List[str] = None,
        timeout: int = 600
    ) -> Dict[str, any]:
        """
        Execute CMake compilation for LDP-generated projects.

        For LDP: tool success + make success = overall success
                tool success + make failure = overall error

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
            logger.info(f"Compiling LDP project in directory: {cmake_dir}")

            # Build compile commands
            build_dir = os.path.join(cmake_dir, "build")
            os.makedirs(build_dir, exist_ok=True)

            # Get dependency paths using pkg-config
            log4cplus_dir = self._get_pkg_config_path("log4cplus")
            apr_dir = self._get_pkg_config_path("apr-1")
            cunit_dir = self._get_pkg_config_path("cunit")
            logger.info(f"Using pkg-config paths: log4cplus={log4cplus_dir}, apr={apr_dir}, cunit={cunit_dir}")

            # Find cmake_config.cmake path
            cmake_config_path = os.path.join(cmake_dir, "cmake_config.cmake")
            if not os.path.exists(cmake_config_path):
                ecoa_tools_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                cmake_config_path = os.path.join(ecoa_tools_root, "cmake_config.cmake")
                if not os.path.exists(cmake_config_path):
                    logger.warning("cmake_config.cmake not found, will skip -C parameter")
                    cmake_config_path = None

            if cmake_config_path:
                cmake_config_path = os.path.abspath(cmake_config_path)
                logger.info(f"Using cmake_config.cmake: {cmake_config_path}")

            # Get compile configuration
            tool_config = self.config.get_tool("ldp")
            compile_config = tool_config.get("compile", {}) if tool_config else {}
            default_cmake_options = compile_config.get("cmake_options", [])

            # Build CMake command with all required paths
            cmake_cmd = [
                "cmake",
                "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
                f"-DAPR_DIR={apr_dir}",
                f"-DLOG4CPLUS_DIR={log4cplus_dir}",
                f"-DCUNIT_DIR={cunit_dir}",
                f"-DLDP_LOG_USE={log_library}",
                "-B", build_dir,
                "-S", cmake_dir
            ]

            # Add cmake_config.cmake if found
            if cmake_config_path:
                cmake_cmd.extend(["-C", cmake_config_path])

            # Add any additional cmake options
            if cmake_options or default_cmake_options:
                for opt in (cmake_options or default_cmake_options):
                    cmake_cmd.append(opt.replace("${log_library}", log_library))

            # Execute CMake
            logger.info(f"Running CMake: {' '.join(cmake_cmd)}")
            cmake_result = subprocess.run(
                cmake_cmd,
                cwd=cmake_dir,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            cmake_success = cmake_result.returncode == 0

            # Build make command
            make_cmd = ["make", "--no-print-directory", "-C", build_dir, "all"]

            # Execute make only if CMake succeeded
            make_result = None
            if cmake_success:
                logger.info(f"Running make: {' '.join(make_cmd)}")
                make_result = subprocess.run(
                    make_cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                # Create dummy make result for consistency
                make_result = subprocess.CompletedProcess(
                    args=make_cmd,
                    returncode=-1,
                    stdout="",
                    stderr="CMake failed, make not executed"
                )

            # Find executable files
            executable_files = self._find_executable_files(build_dir)

            compile_success = cmake_success and (make_result.returncode == 0 if make_result else False)

            # Combine stdout and stderr
            combined_stdout = f"=== CMake Output ===\n{cmake_result.stdout}\n"
            combined_stderr = f"=== CMake Errors ===\n{cmake_result.stderr}\n"
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
            logger.error(f"LDP compilation timeout in project: {project_path}")
            return self._compile_error_result("Compilation timeout")
        except FileNotFoundError as e:
            logger.error(f"CMakeLists.txt not found: {e}")
            return self._compile_error_result(str(e))
        except Exception as e:
            logger.exception(f"Unexpected LDP compilation error: {e}")
            return self._compile_error_result(f"Unexpected error: {str(e)}")

    def _compile_csmgvt_project(
        self,
        project_path: str,
        timeout: int = 600
    ) -> Dict[str, any]:
        """
        Execute simple CMake compilation for csmgvt projects.

        Uses simple compilation: mkdir build && cd build && cmake .. && make

        Args:
            project_path: Project root directory
            timeout: Compilation timeout in seconds

        Returns:
            Dictionary with compilation results
        """
        try:
            # Find CMakeLists.txt directory
            cmake_dir = self._find_cmakelists_dir(project_path)
            logger.info(f"Compiling csmgvt project in directory: {cmake_dir}")

            # Build compile commands
            build_dir = os.path.join(cmake_dir, "build")
            os.makedirs(build_dir, exist_ok=True)

            # Get compile configuration
            tool_config = self.config.get_tool("csmgvt")
            compile_config = tool_config.get("compile", {}) if tool_config else {}
            default_make_options = compile_config.get("make_options", ["-j"])

            # Simple CMake command: cmake .. (run from build directory)
            cmake_cmd = ["cmake", ".."]

            # Execute CMake from build directory
            logger.info(f"Running CMake: {' '.join(cmake_cmd)}")
            cmake_result = subprocess.run(
                cmake_cmd,
                cwd=build_dir,
                capture_output=True,
                text=True,
                timeout=timeout
            )

            cmake_success = cmake_result.returncode == 0

            # Build make command
            make_cmd = ["make"]
            make_cmd.extend(default_make_options)

            # Execute make only if CMake succeeded
            make_result = None
            if cmake_success:
                logger.info(f"Running make: {' '.join(make_cmd)}")
                make_result = subprocess.run(
                    make_cmd,
                    cwd=build_dir,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                # Create dummy make result for consistency
                make_result = subprocess.CompletedProcess(
                    args=make_cmd,
                    returncode=-1,
                    stdout="",
                    stderr="CMake failed, make not executed"
                )

            # Find executable files
            executable_files = self._find_executable_files(build_dir)

            compile_success = cmake_success and (make_result.returncode == 0 if make_result else False)

            # Combine stdout and stderr
            combined_stdout = f"=== CMake Output ===\n{cmake_result.stdout}\n"
            combined_stderr = f"=== CMake Errors ===\n{cmake_result.stderr}\n"
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
            logger.error(f"csmgvt compilation timeout in project: {project_path}")
            return self._compile_error_result("Compilation timeout")
        except FileNotFoundError as e:
            logger.error(f"CMakeLists.txt not found: {e}")
            return self._compile_error_result(str(e))
        except Exception as e:
            logger.exception(f"Unexpected csmgvt compilation error: {e}")
            return self._compile_error_result(f"Unexpected error: {str(e)}")

    def _find_executable_files(self, build_dir: str) -> List[str]:
        """
        Find executable files in build directory.

        Args:
            build_dir: Build directory path

        Returns:
            List of executable file names
        """
        executable_files = []
        bin_dir = os.path.join(build_dir, "bin")
        search_dirs = [bin_dir] if os.path.exists(bin_dir) else [build_dir]

        for search_dir in search_dirs:
            if os.path.exists(search_dir):
                for file in os.listdir(search_dir):
                    file_path = os.path.join(search_dir, file)
                    if os.path.isfile(file_path) and os.access(file_path, os.X_OK):
                        executable_files.append(file)

        return executable_files

    def _compile_error_result(self, error_message: str) -> Dict[str, any]:
        """
        Create a standardized error result for compilation failures.

        Args:
            error_message: Error message to include

        Returns:
            Dictionary with error result
        """
        return {
            "compile_success": False,
            "compile_stdout": "",
            "compile_stderr": error_message,
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
        compile: Optional[bool] = None,
        log_library: str = None,
        cmake_options: List[str] = None,
        additional_args: List[str] = None,
        force: bool = False
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
            compile: Whether to compile the project after tool execution (for ldp tool).
                None: use configuration default (enabled: true),
                True: always compile,
                False: never compile
            log_library: Logging library to use for compilation (log4cplus, zlog, lttng)
            cmake_options: Additional CMake options for compilation
            additional_args: Additional command line arguments
            force: Force overwrite existing files (adds -f flag for ldp, csmgvt, mscigt)

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

        # Add force flag if enabled (for ldp, csmgvt, mscigt)
        if force and tool_id in ['ldp', 'csmgvt', 'mscigt']:
            cmd.append('-f')
            logger.info(f"Force overwrite enabled for tool {tool_id}")

        # Add verbose flag based on tool's verbose_type
        verbose_type = tool_config.get('verbose_type', 'boolean')
        if verbose_type == 'boolean':
            # Boolean flag: just add -v without value
            cmd.append('-v')
        else:
            # Integer type: add -v with value
            cmd.extend(['-v', str(verbose)])

        if additional_args:
            cmd.extend(additional_args)

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

            # Handle compilation for LDP and csmgvt separately
            compile_result = {}
            final_success = success

            if success:
                if tool_id == 'ldp':
                    compile_result = self._handle_ldp_compilation(
                        project_path, project_name, compile, log_library, cmake_options
                    )
                    # For LDP: tool success + make success = overall success
                    #         tool success + make failure = overall error
                    if compile_result:
                        final_success = compile_result.get('compile_success', False)

                elif tool_id == 'csmgvt':
                    compile_result = self._handle_csmgvt_compilation(
                        project_path, compile
                    )
                    # For csmgvt: same logic as LDP
                    if compile_result:
                        final_success = compile_result.get('compile_success', False)

            # Prepare result dictionary
            result_dict = {
                'success': final_success,
                'tool': tool_id,
                'project_name': project_name,
                'project_path': project_path,
                'project_file': project_file,
                'generated_files': generated_files,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'return_code': result.returncode,
                'message': self._get_message_for_tool(result.returncode, tool_id, compile_result)
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
