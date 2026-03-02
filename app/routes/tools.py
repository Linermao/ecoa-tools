"""API routes for tools."""

from flask import Blueprint, request, jsonify, current_app
from werkzeug.exceptions import BadRequest, NotFound, InternalServerError

from app.utils.config import get_config
from app.utils.logger import get_logger, RequestContext
from app.services.executor import ToolExecutor, ProjectNotFoundError, ProjectFileNotFoundError

bp = Blueprint('tools', __name__, url_prefix='/api/tools')
logger = get_logger(__name__)
executor = ToolExecutor()
config = get_config()


@bp.route('/', methods=['GET'])
def list_tools():
    """
    Get list of all available tools.

    Returns:
        JSON response with tools information
    """
    with RequestContext(logger) as ctx:
        ctx.info("API: List tools requested")

        tools_config = config.tools
        tools_list = []

        for tool_id, tool_config in tools_config.items():
            tools_list.append({
                'id': tool_id,
                'name': tool_config.get('name', tool_id),
                'description': tool_config.get('description', ''),
                'category': tool_config.get('category', 'uncategorized'),
                'command': tool_config.get('command', ''),
                'parameters': tool_config.get('parameters', []),
                'output_types': tool_config.get('output_types', []),
                'example': tool_config.get('example', '')
            })

        ctx.info(f"Returning {len(tools_list)} tools")

        return jsonify({
            'success': True,
            'count': len(tools_list),
            'tools': tools_list
        })


@bp.route('/<tool_id>', methods=['GET'])
def get_tool(tool_id: str):
    """
    Get detailed information about a specific tool.

    Args:
        tool_id: Tool identifier

    Returns:
        JSON response with tool details
    """
    with RequestContext(logger) as ctx:
        ctx.info(f"API: Tool details requested: {tool_id}")

        tool_config = config.get_tool(tool_id)

        if not tool_config:
            ctx.warning(f"Tool not found: {tool_id}")
            raise NotFound(f"Tool not found: {tool_id}")

        tool_info = {
            'id': tool_id,
            'name': tool_config.get('name', tool_id),
            'description': tool_config.get('description', ''),
            'category': tool_config.get('category', 'uncategorized'),
            'command': tool_config.get('command', ''),
            'parameters': tool_config.get('parameters', []),
            'output_types': tool_config.get('output_types', []),
            'example': tool_config.get('example', '')
        }

        ctx.info(f"Returning tool details: {tool_id}")

        return jsonify({
            'success': True,
            'tool': tool_info
        })


@bp.route('/execute', methods=['POST'])
def execute_tool():
    """
    Execute a tool with an uploaded file.

    Request:
        multipart/form-data:
            - file: XML file to process
            - tool: Tool identifier (default: 'exvt')
            - verbose: Verbosity level (optional)

    Returns:
        JSON response with execution result

    Note: This endpoint is deprecated. Use /execute-project instead.
    """
    with RequestContext(logger) as ctx:
        ctx.info("API: Tool execution requested (file upload)")

        # Check if file is in request
        if 'file' not in request.files:
            ctx.warning("No file provided in request")
            raise BadRequest("No file provided")

        file = request.files['file']

        if file.filename == '':
            ctx.warning("Empty filename in request")
            raise BadRequest("Empty filename")

        # Get tool identifier
        tool_id = request.form.get('tool', 'exvt')
        ctx.info(f"Tool: {tool_id}, File: {file.filename}")

        # Validate tool exists
        if not config.get_tool(tool_id):
            ctx.warning(f"Invalid tool requested: {tool_id}")
            raise BadRequest(f"Invalid tool: {tool_id}")

        # Get verbose level
        try:
            verbose = int(request.form.get('verbose', config.verbose))
        except ValueError:
            verbose = config.verbose

        # Save uploaded file
        try:
            file_content = file.read()
            file_path = executor.save_uploaded_file(file_content, file.filename)
            ctx.info(f"File saved: {file_path}")
        except Exception as e:
            ctx.error(f"Failed to save uploaded file: {e}")
            raise InternalServerError(f"Failed to save file: {str(e)}")

        # Execute tool
        try:
            result = executor.execute(tool_id, file_path, verbose)

            if result['success']:
                ctx.info(f"Tool executed successfully, outputs: {len(result['output_files'])} files")
            else:
                ctx.error(f"Tool execution failed: {result['message']}")

            return jsonify({
                'success': result['success'],
                'tool': result['tool'],
                'input_file': file.filename,
                'output_files': result['output_files'],
                'message': result['message'],
                'stdout': result['stdout'],
                'stderr': result['stderr'],
                'return_code': result['return_code']
            })

        except ValueError as e:
            ctx.error(f"Invalid execution parameters: {e}")
            raise BadRequest(str(e))
        except Exception as e:
            ctx.exception(f"Unexpected error during execution: {e}")
            raise InternalServerError(f"Execution error: {str(e)}")


@bp.route('/execute-project', methods=['POST'])
def execute_in_project():
    """
    Execute a tool in a project directory.

    Request (JSON):
        {
            "project_name": "marx_brothers",
            "project_file": "marx_brothers.project.xml",
            "tool": "asctg",
            "checker": "ecoa-exvt",
            "config_file": "ecoa_config.xml",
            "verbose": 3,
            "compile": false,
            "log_library": "log4cplus",
            "cmake_options": ["-DLDP_LOG_USE=log4cplus"]
        }

    Parameters:
        - project_name (required): Project directory name
        - project_file (required): Project XML file name
        - tool (required): Tool ID to execute
        - checker (optional): Checker tool for validation (default: ecoa-exvt)
        - config_file (optional): Config file name (required for asctg)
        - verbose (optional): Verbosity level (default: 3)
        - compile (optional): Whether to compile project after tool execution (for ldp tool only, default: false)
        - log_library (optional): Logging library for compilation (log4cplus, zlog, lttng, default: log4cplus)
        - cmake_options (optional): Additional CMake options for compilation (array of strings)

    Returns:
        JSON response with execution result:
        {
            "success": true,
            "tool": "asctg",
            "project_name": "marx_brothers",
            "project_path": "/path/to/projects/marx_brothers",
            "project_file": "marx_brothers.project.xml",
            "generated_files": [...],
            "message": "...",
            "stdout": "...",
            "stderr": "...",
            "return_code": 0,
            "compile_success": false (optional, present if compile=true),
            "compile_stdout": "..." (optional),
            "compile_stderr": "..." (optional),
            "compile_return_code": -1 (optional),
            "executable_files": [] (optional),
            "cmake_dir": "" (optional),
            "build_dir": "" (optional)
        }
    """
    with RequestContext(logger) as ctx:
        ctx.info("API: Tool execution in project requested")

        # Get request data (support both JSON and form data)
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form.to_dict()

        # Get required parameters
        project_name = data.get('project_name')
        project_file = data.get('project_file')
        tool_id = data.get('tool', 'exvt')
        checker = data.get('checker')  # Optional checker parameter
        config_file = data.get('config_file')  # Optional config file (for asctg)

        # Get optional compilation parameters
        compile_param = data.get('compile')  # None if not provided
        log_library = data.get('log_library')
        cmake_options = data.get('cmake_options')

        # Validate log_library if provided
        valid_log_libraries = ["log4cplus", "zlog", "lttng"]
        if log_library is not None and log_library not in valid_log_libraries:
            ctx.warning(f"Invalid log_library: {log_library}")
            raise BadRequest(f"Invalid log_library: {log_library}. Must be one of {valid_log_libraries}")

        # Convert compile parameter to boolean if provided
        if compile_param is not None:
            if isinstance(compile_param, str):
                compile_param = compile_param.lower() in ('true', '1', 'yes')
            else:
                # For JSON boolean (true/false), it's already bool
                compile_param = bool(compile_param)

        # Validate cmake_options if provided
        if cmake_options is not None:
            if not isinstance(cmake_options, list):
                ctx.warning(f"Invalid cmake_options type: {type(cmake_options)}")
                raise BadRequest("cmake_options must be a list of strings")
            for opt in cmake_options:
                if not isinstance(opt, str):
                    ctx.warning(f"Invalid cmake_option type: {type(opt)}")
                    raise BadRequest("All cmake_options must be strings")

        # Validate parameters
        if not project_name:
            ctx.warning("Missing project_name")
            raise BadRequest("Missing required parameter: project_name")

        if not project_file:
            ctx.warning("Missing project_file")
            raise BadRequest("Missing required parameter: project_file")

        ctx.info(f"Project: {project_name}, File: {project_file}, Tool: {tool_id}, Checker: {checker or 'default'}, Config: {config_file or 'N/A'}, Compile: {compile_param}, LogLibrary: {log_library or 'default'}, CMakeOptions: {len(cmake_options) if cmake_options else 0}")

        # Warn if compile is requested for non-ldp tools
        if compile_param and tool_id != 'ldp':
            ctx.warning(f"Compilation requested for non-ldp tool: {tool_id}. Compilation will be ignored.")

        # Validate tool exists
        if not config.get_tool(tool_id):
            ctx.warning(f"Invalid tool requested: {tool_id}")
            raise BadRequest(f"Invalid tool: {tool_id}")

        # Get verbose level
        try:
            verbose = int(data.get('verbose', config.verbose))
        except ValueError:
            verbose = config.verbose

        # Execute tool in project directory
        try:
            result = executor.execute_in_project(
                tool_id, project_name, project_file, verbose, checker, config_file,
                compile=compile_param, log_library=log_library, cmake_options=cmake_options
            )

            if result['success']:
                ctx.info(f"Tool executed successfully, generated: {len(result['generated_files'])} files")
            else:
                ctx.error(f"Tool execution failed: {result['message']}")

            # Create base response
            response_data = {
                'success': result['success'],
                'tool': result['tool'],
                'project_name': result['project_name'],
                'project_path': result['project_path'],
                'project_file': result['project_file'],
                'generated_files': result['generated_files'],
                'message': result['message'],
                'stdout': result['stdout'],
                'stderr': result['stderr'],
                'return_code': result['return_code']
            }

            # Add compilation results if present
            if 'compile_success' in result:
                response_data.update({
                    'compile_success': result.get('compile_success', False),
                    'compile_stdout': result.get('compile_stdout', ''),
                    'compile_stderr': result.get('compile_stderr', ''),
                    'compile_return_code': result.get('compile_return_code', -1),
                    'executable_files': result.get('executable_files', []),
                    'cmake_dir': result.get('cmake_dir', ''),
                    'build_dir': result.get('build_dir', '')
                })

            return jsonify(response_data)

        except ProjectNotFoundError as e:
            ctx.error(f"Project not found: {e}")
            raise NotFound(str(e))
        except ProjectFileNotFoundError as e:
            ctx.error(f"Project file not found: {e}")
            raise NotFound(str(e))
        except ValueError as e:
            ctx.error(f"Invalid execution parameters: {e}")
            raise BadRequest(str(e))
        except Exception as e:
            ctx.exception(f"Unexpected error during execution: {e}")
            raise InternalServerError(f"Execution error: {str(e)}")


@bp.errorhandler(BadRequest)
@bp.errorhandler(NotFound)
@bp.errorhandler(InternalServerError)
def handle_error(e):
    """Handle HTTP errors and return JSON responses."""
    logger.error(f"HTTP Error: {e.__class__.__name__}: {e}")
    return jsonify({
        'success': False,
        'error': e.description
    }), e.code
