"""AL Language Server implementation for Microsoft Dynamics 365 Business Central."""

import logging
import os
import pathlib
import platform
import stat
import time
import zipfile
from pathlib import Path

import requests
from overrides import override

from solidlsp.language_servers.common import quote_windows_path
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.ls_logger import LanguageServerLogger
from solidlsp.lsp_protocol_handler.lsp_types import Definition, DefinitionParams, LocationLink
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings


class ALLanguageServer(SolidLanguageServer):
    """
    Language server implementation for AL (Microsoft Dynamics 365 Business Central).

    This implementation uses the AL Language Server from the VS Code AL extension
    (ms-dynamics-smb.al). The extension must be installed or available locally.

    Key Features:
    - Automatic download of AL extension from VS Code marketplace if not present
    - Platform-specific executable detection (Windows/Linux/macOS)
    - Special initialization sequence required by AL Language Server
    - Custom AL-specific LSP commands (al/gotodefinition, al/setActiveWorkspace)
    - File opening requirement before symbol retrieval
    """

    def __init__(
        self, config: LanguageServerConfig, logger: LanguageServerLogger, repository_root_path: str, solidlsp_settings: SolidLSPSettings
    ):
        """
        Initialize the AL Language Server.

        Args:
            config: Language server configuration
            logger: Logger instance for debugging
            repository_root_path: Root path of the AL project (must contain app.json)
            solidlsp_settings: Solid LSP settings

        Note:
            The initialization process will automatically:
            1. Check for AL extension in the resources directory
            2. Download it from VS Code marketplace if not found
            3. Extract and configure the platform-specific executable

        """
        # Setup runtime dependencies and get the language server command
        # This will download the AL extension if needed
        cmd = self._setup_runtime_dependencies(logger, config, solidlsp_settings)

        super().__init__(
            config,
            logger,
            repository_root_path,
            ProcessLaunchInfo(cmd=cmd, cwd=repository_root_path),
            "al",  # Language ID for LSP
            solidlsp_settings,
        )

    @classmethod
    def _download_al_extension(cls, logger: LanguageServerLogger, url: str, target_dir: str) -> bool:
        """
        Download and extract the AL extension from VS Code marketplace.

        The VS Code marketplace packages extensions as .vsix files (which are ZIP archives).
        This method downloads the VSIX file and extracts it to get the language server binaries.

        Args:
            logger: Logger for tracking download progress
            url: VS Code marketplace URL for the AL extension
            target_dir: Directory where the extension will be extracted

        Returns:
            True if successful, False otherwise

        Note:
            The download includes progress tracking and proper user-agent headers
            to ensure compatibility with the VS Code marketplace.

        """
        try:
            logger.log(f"Downloading AL extension from {url}", logging.INFO)

            # Create target directory for the extension
            os.makedirs(target_dir, exist_ok=True)

            # Download with proper headers to mimic VS Code marketplace client
            # These headers are required for the marketplace to serve the VSIX file
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/octet-stream, application/vsix, */*",
            }

            response = requests.get(url, headers=headers, stream=True, timeout=300)
            response.raise_for_status()

            # Save to temporary VSIX file (will be deleted after extraction)
            temp_file = os.path.join(target_dir, "al_extension_temp.vsix")
            total_size = int(response.headers.get("content-length", 0))

            logger.log(f"Downloading {total_size / 1024 / 1024:.1f} MB...", logging.INFO)

            with open(temp_file, "wb") as f:
                downloaded = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0 and downloaded % (10 * 1024 * 1024) == 0:  # Log progress every 10MB
                            progress = (downloaded / total_size) * 100
                            logger.log(f"Download progress: {progress:.1f}%", logging.INFO)

            logger.log("Download complete, extracting...", logging.INFO)

            # Extract VSIX file (VSIX files are just ZIP archives with a different extension)
            # This will extract the extension folder containing the language server binaries
            with zipfile.ZipFile(temp_file, "r") as zip_ref:
                zip_ref.extractall(target_dir)

            # Clean up temp file
            os.remove(temp_file)

            logger.log("AL extension extracted successfully", logging.INFO)
            return True

        except Exception as e:
            logger.log(f"Error downloading/extracting AL extension: {e}", logging.ERROR)
            return False

    @classmethod
    def _setup_runtime_dependencies(
        cls, logger: LanguageServerLogger, config: LanguageServerConfig, solidlsp_settings: SolidLSPSettings
    ) -> str:
        """
        Setup runtime dependencies for AL Language Server and return the command to start the server.

        This method handles the complete setup process:
        1. Checks for existing AL extension installations
        2. Downloads from VS Code marketplace if not found
        3. Configures executable permissions on Unix systems
        4. Returns the properly formatted command string

        The AL Language Server executable is located in different paths based on the platform:
        - Windows: bin/win32/Microsoft.Dynamics.Nav.EditorServices.Host.exe
        - Linux: bin/linux/Microsoft.Dynamics.Nav.EditorServices.Host
        - macOS: bin/darwin/Microsoft.Dynamics.Nav.EditorServices.Host
        """
        system = platform.system()

        # Find existing extension or download if needed
        extension_path = cls._find_al_extension(logger, solidlsp_settings)
        if extension_path is None:
            logger.log("AL extension not found on disk, attempting to download...", logging.INFO)
            extension_path = cls._download_and_install_al_extension(logger, solidlsp_settings)

        if extension_path is None:
            raise RuntimeError(
                "Failed to locate or download AL Language Server. Please either:\n"
                "1. Set AL_EXTENSION_PATH environment variable to the AL extension directory\n"
                "2. Install the AL extension in VS Code (ms-dynamics-smb.al)\n"
                "3. Ensure internet connection for automatic download"
            )

        # Build executable path based on platform
        executable_path = cls._get_executable_path(extension_path, system)

        if not os.path.exists(executable_path):
            raise RuntimeError(f"AL Language Server executable not found at: {executable_path}")

        # Prepare and return the executable command
        return cls._prepare_executable(executable_path, system, logger)

    @classmethod
    def _find_al_extension(cls, logger: LanguageServerLogger, solidlsp_settings: SolidLSPSettings) -> str | None:
        """
        Find AL extension in various locations.

        Search order:
        1. Environment variable (AL_EXTENSION_PATH)
        2. Default download location (~/.serena/ls_resources/al-extension)
        3. VS Code installed extensions

        Returns:
            Path to AL extension directory or None if not found

        """
        # Check environment variable
        env_path = os.environ.get("AL_EXTENSION_PATH")
        if env_path and os.path.exists(env_path):
            logger.log(f"Found AL extension via AL_EXTENSION_PATH: {env_path}", level=5)
            return env_path
        elif env_path:
            logger.log(f"AL_EXTENSION_PATH set but directory not found: {env_path}", logging.WARNING)

        # Check default download location
        default_path = os.path.join(cls.ls_resources_dir(solidlsp_settings), "al-extension", "extension")
        if os.path.exists(default_path):
            logger.log(f"Found AL extension in default location: {default_path}", level=5)
            return default_path

        # Search VS Code extensions
        vscode_path = cls._find_al_extension_in_vscode(logger)
        if vscode_path:
            logger.log(f"Found AL extension in VS Code: {vscode_path}", level=5)
            return vscode_path

        logger.log("AL extension not found in any known location", level=10)
        return None

    @classmethod
    def _download_and_install_al_extension(cls, logger: LanguageServerLogger, solidlsp_settings: SolidLSPSettings) -> str | None:
        """
        Download and install AL extension from VS Code marketplace.

        Returns:
            Path to installed extension or None if download failed

        """
        al_extension_dir = os.path.join(cls.ls_resources_dir(solidlsp_settings), "al-extension")

        # AL extension version - using latest stable version
        AL_VERSION = "latest"
        url = f"https://marketplace.visualstudio.com/_apis/public/gallery/publishers/ms-dynamics-smb/vsextensions/al/{AL_VERSION}/vspackage"

        logger.log(f"Downloading AL extension from: {url}", logging.INFO)

        if cls._download_al_extension(logger, url, al_extension_dir):
            extension_path = os.path.join(al_extension_dir, "extension")
            if os.path.exists(extension_path):
                logger.log("AL extension downloaded and installed successfully", logging.INFO)
                return extension_path
            else:
                logger.log(f"Download completed but extension not found at: {extension_path}", logging.ERROR)
        else:
            logger.log("Failed to download AL extension from marketplace", logging.ERROR)

        return None

    @classmethod
    def _get_executable_path(cls, extension_path: str, system: str) -> str:
        """
        Build platform-specific executable path.

        Args:
            extension_path: Path to AL extension directory
            system: Operating system name

        Returns:
            Full path to executable

        """
        if system == "Windows":
            return os.path.join(extension_path, "bin", "win32", "Microsoft.Dynamics.Nav.EditorServices.Host.exe")
        elif system == "Linux":
            return os.path.join(extension_path, "bin", "linux", "Microsoft.Dynamics.Nav.EditorServices.Host")
        elif system == "Darwin":
            return os.path.join(extension_path, "bin", "darwin", "Microsoft.Dynamics.Nav.EditorServices.Host")
        else:
            raise RuntimeError(f"Unsupported platform: {system}")

    @classmethod
    def _prepare_executable(cls, executable_path: str, system: str, logger: LanguageServerLogger) -> str:
        """
        Prepare the executable by setting permissions and handling path quoting.

        Args:
            executable_path: Path to the executable
            system: Operating system name
            logger: Logger instance

        Returns:
            Properly formatted command string

        """
        # Make sure executable has proper permissions on Unix-like systems
        if system in ["Linux", "Darwin"]:
            st = os.stat(executable_path)
            os.chmod(executable_path, st.st_mode | stat.S_IEXEC)
            logger.log(f"Set execute permission on: {executable_path}", level=10)

        logger.log(f"Using AL Language Server executable: {executable_path}", level=5)

        # The AL Language Server uses stdio for LSP communication by default
        # Use the utility function to handle Windows path quoting
        return quote_windows_path(executable_path)

    @classmethod
    def _get_language_server_command_fallback(cls, logger: LanguageServerLogger) -> str:
        """
        Get the command to start the AL language server.

        Returns:
            Command string to launch the AL language server

        Raises:
            RuntimeError: If AL extension cannot be found

        """
        # Check if AL extension path is configured via environment variable
        al_extension_path = os.environ.get("AL_EXTENSION_PATH")

        if not al_extension_path:
            # Try to find the extension in the current working directory
            # (for development/testing when extension is in the serena repo)
            cwd_path = Path.cwd()
            potential_extension = None

            # Look for ms-dynamics-smb.al-* directories
            for item in cwd_path.iterdir():
                if item.is_dir() and item.name.startswith("ms-dynamics-smb.al-"):
                    potential_extension = item
                    break

            if potential_extension:
                al_extension_path = str(potential_extension)
                logger.log(f"Found AL extension in current directory: {al_extension_path}", level=5)
            else:
                # Try to find in common VS Code extension locations
                al_extension_path = cls._find_al_extension_in_vscode(logger)

        if not al_extension_path:
            raise RuntimeError(
                "AL Language Server not found. Please either:\n"
                "1. Set AL_EXTENSION_PATH environment variable to the VS Code AL extension directory\n"
                "2. Install the AL extension in VS Code (ms-dynamics-smb.al)\n"
                "3. Place the extension directory in the current working directory"
            )

        # Determine platform-specific executable
        system = platform.system()
        if system == "Windows":
            executable = os.path.join(al_extension_path, "bin", "win32", "Microsoft.Dynamics.Nav.EditorServices.Host.exe")
        elif system == "Linux":
            executable = os.path.join(al_extension_path, "bin", "linux", "Microsoft.Dynamics.Nav.EditorServices.Host")
        elif system == "Darwin":
            executable = os.path.join(al_extension_path, "bin", "darwin", "Microsoft.Dynamics.Nav.EditorServices.Host")
        else:
            raise RuntimeError(f"Unsupported platform: {system}")

        # Verify executable exists
        if not os.path.exists(executable):
            raise RuntimeError(
                f"AL Language Server executable not found at: {executable}\nPlease ensure the AL extension is properly installed."
            )

        # Make sure executable has proper permissions on Unix-like systems
        if system in ["Linux", "Darwin"]:
            st = os.stat(executable)
            os.chmod(executable, st.st_mode | stat.S_IEXEC)

        logger.log(f"Using AL Language Server executable: {executable}", level=5)

        # The AL Language Server uses stdio for LSP communication (no --stdio flag needed)
        # Use the utility function to handle Windows path quoting
        return quote_windows_path(executable)

    @classmethod
    def _find_al_extension_in_vscode(cls, logger: LanguageServerLogger) -> str | None:
        """
        Try to find AL extension in common VS Code extension locations.

        Returns:
            Path to AL extension directory or None if not found

        """
        home = Path.home()
        possible_paths = []

        # Common VS Code extension paths
        if platform.system() == "Windows":
            possible_paths.extend(
                [
                    home / ".vscode" / "extensions",
                    home / ".vscode-insiders" / "extensions",
                    Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "extensions",
                    Path(os.environ.get("APPDATA", "")) / "Code - Insiders" / "User" / "extensions",
                ]
            )
        else:
            possible_paths.extend(
                [
                    home / ".vscode" / "extensions",
                    home / ".vscode-server" / "extensions",
                    home / ".vscode-insiders" / "extensions",
                ]
            )

        for base_path in possible_paths:
            if base_path.exists():
                logger.log(f"Searching for AL extension in: {base_path}", level=10)
                # Look for AL extension directories
                for item in base_path.iterdir():
                    if item.is_dir() and item.name.startswith("ms-dynamics-smb.al-"):
                        logger.log(f"Found AL extension at: {item}", level=5)
                        return str(item)

        return None

    @staticmethod
    def _get_initialize_params(repository_absolute_path: str) -> dict:
        """
        Returns the initialize params for the AL Language Server.
        """
        # Ensure we have an absolute path for URI generation
        repository_path = pathlib.Path(repository_absolute_path).resolve()
        root_uri = repository_path.as_uri()

        # AL requires extensive capabilities based on VS Code trace
        initialize_params = {
            "processId": os.getpid(),
            "rootPath": str(repository_path),
            "rootUri": root_uri,
            "capabilities": {
                "workspace": {
                    "applyEdit": True,
                    "workspaceEdit": {
                        "documentChanges": True,
                        "resourceOperations": ["create", "rename", "delete"],
                        "failureHandling": "textOnlyTransactional",
                        "normalizesLineEndings": True,
                    },
                    "configuration": True,
                    "didChangeWatchedFiles": {"dynamicRegistration": True},
                    "symbol": {"dynamicRegistration": True, "symbolKind": {"valueSet": list(range(1, 27))}},
                    "executeCommand": {"dynamicRegistration": True},
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "workspaceFolders": True,
                },
                "textDocument": {
                    "synchronization": {"dynamicRegistration": True, "willSave": True, "willSaveWaitUntil": True, "didSave": True},
                    "completion": {
                        "dynamicRegistration": True,
                        "contextSupport": True,
                        "completionItem": {
                            "snippetSupport": True,
                            "commitCharactersSupport": True,
                            "documentationFormat": ["markdown", "plaintext"],
                            "deprecatedSupport": True,
                            "preselectSupport": True,
                        },
                    },
                    "hover": {"dynamicRegistration": True, "contentFormat": ["markdown", "plaintext"]},
                    "definition": {"dynamicRegistration": True, "linkSupport": True},
                    "references": {"dynamicRegistration": True},
                    "documentHighlight": {"dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                        "hierarchicalDocumentSymbolSupport": True,
                    },
                    "codeAction": {"dynamicRegistration": True},
                    "formatting": {"dynamicRegistration": True},
                    "rangeFormatting": {"dynamicRegistration": True},
                    "rename": {"dynamicRegistration": True, "prepareSupport": True},
                },
                "window": {
                    "showMessage": {"messageActionItem": {"additionalPropertiesSupport": True}},
                    "showDocument": {"support": True},
                    "workDoneProgress": True,
                },
            },
            "trace": "verbose",
            "workspaceFolders": [{"uri": root_uri, "name": repository_path.name}],
        }

        return initialize_params

    @override
    def _start_server(self):
        """
        Starts the AL Language Server process and initializes it.

        This method sets up custom notification handlers for AL-specific messages
        before starting the server. The AL server sends various notifications
        during initialization and project loading that need to be handled.
        """

        # Set up event handlers
        def do_nothing(params):
            return

        def window_log_message(msg):
            self.logger.log(f"AL LSP: window/logMessage: {msg}", logging.INFO)

        def publish_diagnostics(params):
            # AL server publishes diagnostics during initialization
            uri = params.get("uri", "")
            diagnostics = params.get("diagnostics", [])
            self.logger.log(f"AL LSP: Diagnostics for {uri}: {len(diagnostics)} issues", logging.DEBUG)

        def handle_al_notifications(params):
            # AL server sends custom notifications during project loading
            self.logger.log("AL LSP: Notification received", logging.DEBUG)

        # Register handlers for AL-specific notifications
        # These notifications are sent by the AL server during initialization and operation
        self.server.on_notification("window/logMessage", window_log_message)  # Server log messages
        self.server.on_notification("textDocument/publishDiagnostics", publish_diagnostics)  # Compilation diagnostics
        self.server.on_notification("$/progress", do_nothing)  # Progress notifications during loading
        self.server.on_notification("al/refreshExplorerObjects", handle_al_notifications)  # AL-specific object updates

        # Start the server process
        self.logger.log("Starting AL Language Server process", logging.INFO)
        self.server.start()

        # Send initialize request
        initialize_params = self._get_initialize_params(self.repository_root_path)

        self.logger.log(
            "Sending initialize request from LSP client to AL LSP server and awaiting response",
            logging.INFO,
        )

        # Send initialize and wait for response
        resp = self.server.send_request("initialize", initialize_params)
        if resp is None:
            raise RuntimeError("AL Language Server initialization failed - no response")

        self.logger.log("AL Language Server initialized successfully", logging.INFO)

        # Send initialized notification
        self.server.send_notification("initialized", {})
        self.logger.log("Sent initialized notification", logging.INFO)

    @override
    def start(self) -> "ALLanguageServer":
        """
        Start the AL Language Server with special initialization.
        """
        # Call parent start method
        super().start()

        # AL-specific post-initialization
        self._post_initialize_al_workspace()

        # Note: set_active_workspace() can be called manually if needed for multi-workspace scenarios
        # We don't call it automatically to avoid issues during single-workspace initialization

        return self

    def _post_initialize_al_workspace(self) -> None:
        """
        Post-initialization setup for AL Language Server.

        The AL server requires additional setup after initialization:
        1. Set the active workspace - tells AL which project to work with
        2. Send workspace configuration - provides AL settings and paths
        3. Open app.json to trigger project loading - AL uses app.json to identify project structure
        4. Wait for project to be loaded - ensures symbols are indexed before operations

        This special initialization sequence is unique to AL and necessary for proper
        symbol resolution and navigation features.
        """
        # Give the server a moment to fully initialize
        time.sleep(1)

        # Send workspace configuration first
        # This tells AL about assembly paths, package caches, and code analysis settings
        try:
            self.server.send_notification(
                "workspace/didChangeConfiguration",
                {
                    "settings": {
                        "workspacePath": self.repository_root_path,
                        "alResourceConfigurationSettings": {
                            "assemblyProbingPaths": ["./.netpackages"],
                            "codeAnalyzers": [],
                            "enableCodeAnalysis": False,
                            "backgroundCodeAnalysis": "Project",
                            "packageCachePaths": ["./.alpackages"],
                            "ruleSetPath": None,
                            "enableCodeActions": True,
                            "incrementalBuild": False,
                            "outputAnalyzerStatistics": True,
                            "enableExternalRulesets": True,
                        },
                        "setActiveWorkspace": True,
                        "expectedProjectReferenceDefinitions": [],
                        "activeWorkspaceClosure": [self.repository_root_path],
                    }
                },
            )
            self.logger.log("Sent workspace configuration", level=5)
        except Exception as e:
            self.logger.log(f"Failed to send workspace config: {e}", level=3)

        # Check if app.json exists and open it
        # app.json is the AL project manifest file (similar to package.json for Node.js)
        # Opening it triggers AL to load the project and index all AL files
        app_json_path = Path(self.repository_root_path) / "app.json"
        if app_json_path.exists():
            try:
                with open(app_json_path, encoding="utf-8") as f:
                    app_json_content = f.read()

                # Use forward slashes for URI
                app_json_uri = app_json_path.as_uri()

                # Send textDocument/didOpen for app.json
                self.server.send_notification(
                    "textDocument/didOpen",
                    {"textDocument": {"uri": app_json_uri, "languageId": "json", "version": 1, "text": app_json_content}},
                )

                self.logger.log(f"Opened app.json: {app_json_uri}", level=5)
            except Exception as e:
                self.logger.log(f"Failed to open app.json: {e}", level=3)

        # Try to set active workspace (AL-specific custom LSP request)
        # This is important when multiple AL projects are open
        workspace_uri = Path(self.repository_root_path).resolve().as_uri()
        try:
            result = self.server.send_request(
                "al/setActiveWorkspace",
                {
                    "currentWorkspaceFolderPath": {"uri": workspace_uri, "name": Path(self.repository_root_path).name, "index": 0},
                    "settings": {
                        "workspacePath": self.repository_root_path,
                        "alResourceConfigurationSettings": {
                            "assemblyProbingPaths": ["./.netpackages"],
                            "codeAnalyzers": [],
                            "enableCodeAnalysis": False,
                            "backgroundCodeAnalysis": "Project",
                            "packageCachePaths": ["./.alpackages"],
                            "ruleSetPath": None,
                            "enableCodeActions": True,
                            "incrementalBuild": False,
                            "outputAnalyzerStatistics": True,
                            "enableExternalRulesets": True,
                        },
                        "setActiveWorkspace": True,
                        "expectedProjectReferenceDefinitions": [],
                        "activeWorkspaceClosure": [self.repository_root_path],
                    },
                },
                timeout=10,
            )
            self.logger.log(f"Set active workspace result: {result}", level=5)
        except Exception as e:
            # This is a custom AL request, might not be critical
            self.logger.log(f"Failed to set active workspace: {e}", level=3)

        # Wait for project to be loaded using AL's custom load check
        # The AL server needs time to parse all AL files and build its symbol index
        if not self._wait_for_project_load(timeout=30):
            # Even if not confirmed loaded, give it extra time to index
            # Some large projects may take longer to fully index
            self.logger.log("Project load not confirmed, waiting additional time for AL workspace indexing", level=5)
            time.sleep(5)
        else:
            # Even when loaded, give a bit more time for symbol indexing to complete
            # This ensures all symbols are available for navigation
            self.logger.log("Project loaded, waiting for symbol indexing", level=5)
            time.sleep(2)

    @override
    def is_ignored_dirname(self, dirname: str) -> bool:
        """
        Define AL-specific directories to ignore during file scanning.

        These directories contain generated files, dependencies, or cache data
        that should not be analyzed for symbols.

        Args:
            dirname: Directory name to check

        Returns:
            True if directory should be ignored

        """
        al_ignore_dirs = {
            ".alpackages",  # AL package cache - downloaded dependencies
            ".alcache",  # AL compiler cache - intermediate compilation files
            ".altemplates",  # AL templates - code generation templates
            ".snapshots",  # Test snapshots - test result snapshots
            "out",  # Compiled output - generated .app files
            ".vscode",  # VS Code settings - editor configuration
            "Reference",  # Reference assemblies - .NET dependencies
            ".netpackages",  # .NET packages - NuGet packages for AL
            "bin",  # Binary output - compiled binaries
            "obj",  # Object files - intermediate build artifacts
        }

        # Check parent class ignore list first, then AL-specific
        return super().is_ignored_dirname(dirname) or dirname in al_ignore_dirs

    @override
    def request_full_symbol_tree(self, within_relative_path: str | None = None, include_body: bool = False) -> list[dict]:
        """
        Override to handle AL's requirement of opening files before requesting symbols.

        The AL Language Server requires files to be explicitly opened via textDocument/didOpen
        before it can provide meaningful symbols. Without this, it only returns directory symbols.
        This is different from most language servers which can provide symbols for unopened files.

        This method:
        1. Scans the repository for all AL files (.al and .dal extensions)
        2. Opens each file with the AL server
        3. Requests symbols for each file
        4. Combines all symbols into a hierarchical tree structure
        5. Closes the files to free resources

        Args:
            within_relative_path: Restrict search to this file or directory path
            include_body: Whether to include symbol body content

        Returns:
            Full symbol tree with all AL symbols from opened files organized by directory

        """
        self.logger.log("AL: Starting request_full_symbol_tree with file opening", level=5)

        # Determine the root path for scanning
        if within_relative_path is not None:
            within_abs_path = os.path.join(self.repository_root_path, within_relative_path)
            if not os.path.exists(within_abs_path):
                raise FileNotFoundError(f"File or directory not found: {within_abs_path}")

            if os.path.isfile(within_abs_path):
                # Single file case - use parent class implementation
                _, root_nodes = self.request_document_symbols(within_relative_path, include_body=include_body)
                return root_nodes

            # Directory case - scan within this directory
            scan_root = Path(within_abs_path)
        else:
            # Scan entire repository
            scan_root = Path(self.repository_root_path)

        # For AL, we always need to open files to get symbols
        al_files = []

        # Walk through the repository to find all AL files
        for root, dirs, files in os.walk(scan_root):
            # Skip ignored directories
            dirs[:] = [d for d in dirs if not self.is_ignored_dirname(d)]

            # Find AL files
            for file in files:
                if file.endswith((".al", ".dal")):
                    file_path = Path(root) / file
                    # Use forward slashes for consistent paths
                    try:
                        relative_path = str(file_path.relative_to(self.repository_root_path)).replace("\\", "/")
                        al_files.append((file_path, relative_path))
                    except ValueError:
                        # File is outside repository root, skip it
                        continue

        self.logger.log(f"AL: Found {len(al_files)} AL files", level=5)

        if not al_files:
            self.logger.log("AL: No AL files found in repository", level=3)
            return []

        # Collect all symbols from all files
        all_file_symbols = []

        for file_path, relative_path in al_files:
            try:
                # Use our overridden request_document_symbols which handles opening
                self.logger.log(f"AL: Getting symbols for {relative_path}", level=8)
                all_syms, root_syms = self.request_document_symbols(relative_path, include_body=include_body)

                if root_syms:
                    # Create a file-level symbol containing the document symbols
                    file_symbol = {
                        "name": file_path.stem,  # Just the filename without extension
                        "kind": 1,  # File
                        "children": root_syms,
                        "location": {
                            "uri": file_path.as_uri(),
                            "relativePath": relative_path,
                            "absolutePath": str(file_path),
                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                        },
                    }
                    all_file_symbols.append(file_symbol)
                    self.logger.log(f"AL: Added {len(root_syms)} symbols from {relative_path}", level=8)
                elif all_syms:
                    # If we only got all_syms but not root, use all_syms
                    file_symbol = {
                        "name": file_path.stem,
                        "kind": 1,  # File
                        "children": all_syms,
                        "location": {
                            "uri": file_path.as_uri(),
                            "relativePath": relative_path,
                            "absolutePath": str(file_path),
                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                        },
                    }
                    all_file_symbols.append(file_symbol)
                    self.logger.log(f"AL: Added {len(all_syms)} symbols from {relative_path}", level=8)

            except Exception as e:
                self.logger.log(f"AL: Failed to get symbols for {relative_path}: {e}", level=5)

        if all_file_symbols:
            self.logger.log(f"AL: Returning symbols from {len(all_file_symbols)} files", level=5)

            # Group files by directory
            directory_structure = {}

            for file_symbol in all_file_symbols:
                rel_path = file_symbol["location"]["relativePath"]
                path_parts = rel_path.split("/")

                if len(path_parts) > 1:
                    # File is in a subdirectory
                    dir_path = "/".join(path_parts[:-1])
                    if dir_path not in directory_structure:
                        directory_structure[dir_path] = []
                    directory_structure[dir_path].append(file_symbol)
                else:
                    # File is in root
                    if "." not in directory_structure:
                        directory_structure["."] = []
                    directory_structure["."].append(file_symbol)

            # Build hierarchical structure
            result = []
            repo_path = Path(self.repository_root_path)
            for dir_path, file_symbols in directory_structure.items():
                if dir_path == ".":
                    # Root level files
                    result.extend(file_symbols)
                else:
                    # Create directory symbol
                    dir_symbol = {
                        "name": Path(dir_path).name,
                        "kind": 4,  # Package/Directory
                        "children": file_symbols,
                        "location": {
                            "relativePath": dir_path,
                            "absolutePath": str(repo_path / dir_path),
                            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}},
                        },
                    }
                    result.append(dir_symbol)

            return result
        else:
            self.logger.log("AL: No symbols found in any files", level=3)
            return []

    @override
    def request_document_symbols(self, relative_file_path: str, include_body: bool = False) -> tuple[list[dict], list[dict]]:
        """
        Override to handle AL's requirement of opening files before requesting symbols.
        Uses direct LSP request to ensure exact URI matching.

        The AL server requires an exact sequence:
        1. Open the file with textDocument/didOpen
        2. Wait for the server to process the file
        3. Request symbols with textDocument/documentSymbol
        4. Close the file with textDocument/didClose

        URI matching must be exact - any mismatch will result in empty symbols.

        Args:
            relative_file_path: Relative path to the file within the repository
            include_body: Whether to include the body of symbols

        Returns:
            Tuple of (all symbols including nested, root level symbols only)

        """
        self.logger.log(f"AL: Requesting document symbols for {relative_file_path}", level=5)

        # Convert relative path to absolute, handling both forward and backslashes
        relative_file_path = relative_file_path.replace("\\", "/")
        abs_path = Path(self.repository_root_path) / relative_file_path

        # Check if file exists
        if not abs_path.exists():
            self.logger.log(f"AL: File does not exist: {abs_path}", level=3)
            return ([], [])

        try:
            # Read file content
            with open(abs_path, encoding="utf-8") as f:
                content = f.read()

            # Create URI - ensure consistent format
            # Use pathlib to get proper URI format
            file_uri = pathlib.Path(abs_path).as_uri()

            self.logger.log(f"AL: Opening file with URI: {file_uri}", level=5)

            # Open the file first with exact URI we'll use for symbols
            self.server.send_notification(
                "textDocument/didOpen", {"textDocument": {"uri": file_uri, "languageId": "al", "version": 1, "text": content}}
            )

            self.logger.log("AL: File opened, requesting symbols immediately", level=5)

            # Now request symbols using direct LSP request with exact same URI
            self.logger.log(f"AL: Requesting symbols with URI: {file_uri}", level=5)

            try:
                # Direct LSP request to ensure URI matches exactly
                response = self.server.send_request("textDocument/documentSymbol", {"textDocument": {"uri": file_uri}}, timeout=5)

                self.logger.log(f"AL: Got symbol response: {response is not None}", level=5)

                if response:
                    # Process the response to match expected format
                    all_symbols = []
                    root_symbols = []

                    if isinstance(response, list):
                        # Response is a list of symbols
                        for symbol in response:
                            symbol_info = self._convert_lsp_symbol_to_solidlsp(symbol)
                            all_symbols.append(symbol_info)
                            root_symbols.append(symbol_info)

                            # Process children if hierarchical
                            if "children" in symbol:
                                self._process_child_symbols(symbol["children"], all_symbols)

                    self.logger.log(f"AL: Processed {len(all_symbols)} total symbols, {len(root_symbols)} root symbols", level=5)

                    # Close the file
                    try:
                        self.server.send_notification("textDocument/didClose", {"textDocument": {"uri": file_uri}})
                    except Exception:
                        pass

                    return (all_symbols, root_symbols)
                else:
                    self.logger.log("AL: No symbols returned from language server", level=3)

            except Exception as e:
                self.logger.log(f"AL: Error requesting symbols: {e}", level=3)

            # Close file even if symbol request failed
            try:
                self.server.send_notification("textDocument/didClose", {"textDocument": {"uri": file_uri}})
            except Exception:
                pass

            # If direct request failed, try parent method as fallback
            self.logger.log("AL: Falling back to parent method", level=5)
            return super().request_document_symbols(relative_file_path, include_body)

        except Exception as e:
            self.logger.log(f"AL: Error in request_document_symbols: {e}", level=3)
            return ([], [])

    def _convert_lsp_symbol_to_solidlsp(self, lsp_symbol: dict) -> dict:
        """Convert standard LSP symbol format to SolidLSP internal format."""
        # Extract basic info
        name = lsp_symbol.get("name", "")
        kind = lsp_symbol.get("kind", 0)

        # Convert range/location
        range_info = lsp_symbol.get("range", {})
        location = lsp_symbol.get("location", {})

        symbol_info = {
            "name": name,
            "kind": kind,
            "location": location if location else {"range": range_info, "uri": ""},  # Will be filled by parent
        }

        # Add detail if present
        if "detail" in lsp_symbol:
            symbol_info["detail"] = lsp_symbol["detail"]

        # Add children if present
        if lsp_symbol.get("children"):
            symbol_info["children"] = [self._convert_lsp_symbol_to_solidlsp(child) for child in lsp_symbol["children"]]

        return symbol_info

    def _process_child_symbols(self, children: list, all_symbols: list) -> None:
        """Recursively process child symbols."""
        for child in children:
            child_info = self._convert_lsp_symbol_to_solidlsp(child)
            all_symbols.append(child_info)

            if "children" in child:
                self._process_child_symbols(child["children"], all_symbols)

    # ===== Phase 1: Custom AL Command Implementations =====

    @override
    def _send_definition_request(self, definition_params: DefinitionParams) -> Definition | list[LocationLink] | None:
        """
        Override to use AL's custom gotodefinition command.

        AL Language Server uses 'al/gotodefinition' instead of the standard
        'textDocument/definition' request. This custom command provides better
        navigation for AL-specific constructs like table extensions, page extensions,
        and codeunit references.

        If the custom command fails, we fall back to the standard LSP method.
        """
        # Convert standard params to AL format (same structure, different method)
        al_params = {"textDocument": definition_params["textDocument"], "position": definition_params["position"]}

        try:
            # Use custom AL command instead of standard LSP
            response = self.server.send_request("al/gotodefinition", al_params)
            self.logger.log(f"AL gotodefinition response: {response}", logging.DEBUG)
            return response
        except Exception as e:
            self.logger.log(f"Failed to use al/gotodefinition, falling back to standard: {e}", logging.WARNING)
            # Fallback to standard LSP method if custom command fails
            return super()._send_definition_request(definition_params)

    def check_project_loaded(self) -> bool:
        """
        Check if AL project closure is fully loaded.

        Uses AL's custom 'al/hasProjectClosureLoadedRequest' to determine if
        the project and all its dependencies have been fully loaded and indexed.
        This is important because AL operations may fail or return incomplete
        results if the project is still loading.

        Returns:
            bool: True if project is loaded, False otherwise

        """
        if not hasattr(self, "server") or not self.server_started:
            self.logger.log("Cannot check project load - server not started", logging.DEBUG)
            return False

        try:
            response = self.server.send_request("al/hasProjectClosureLoadedRequest", {})
            # Response can be boolean directly, dict with 'loaded' field, or None
            if isinstance(response, bool):
                return response
            elif isinstance(response, dict):
                return response.get("loaded", False)
            elif response is None:
                # None typically means the project is still loading or the request isn't supported
                self.logger.log("Project load check returned None - project likely still loading", logging.DEBUG)
                return False
            else:
                self.logger.log(f"Unexpected response type for project load check: {type(response)}", logging.WARNING)
                return False
        except Exception as e:
            self.logger.log(f"Failed to check project load status: {e}", logging.WARNING)
            # Assume loaded if we can't check
            return True

    def _wait_for_project_load(self, timeout: int = 30) -> bool:
        """
        Wait for project to be fully loaded.

        Polls the AL server repeatedly to check if the project is loaded.
        This is necessary because AL project loading is asynchronous and can
        take significant time for large projects with many dependencies.

        Args:
            timeout: Maximum time to wait in seconds (default 30s)

        Returns:
            bool: True if project loaded within timeout, False otherwise

        """
        start_time = time.time()
        self.logger.log(f"Waiting for AL project to load (timeout: {timeout}s)...", logging.INFO)

        while time.time() - start_time < timeout:
            if self.check_project_loaded():
                elapsed = time.time() - start_time
                self.logger.log(f"AL project fully loaded after {elapsed:.1f}s", logging.INFO)
                return True
            time.sleep(0.5)

        self.logger.log(f"Timeout waiting for AL project to load after {timeout}s", logging.WARNING)
        return False

    def set_active_workspace(self, workspace_uri: str | None = None) -> None:
        """
        Set the active AL workspace.

        This is important when multiple workspaces exist to ensure operations
        target the correct workspace. The AL server can handle multiple projects
        simultaneously, but only one can be "active" at a time for operations
        like symbol search and navigation.

        This uses the custom 'al/setActiveWorkspace' LSP command.

        Args:
            workspace_uri: URI of workspace to set as active, or None to use repository root

        """
        if not hasattr(self, "server") or not self.server_started:
            self.logger.log("Cannot set active workspace - server not started", logging.DEBUG)
            return

        if workspace_uri is None:
            workspace_uri = Path(self.repository_root_path).resolve().as_uri()

        params = {"workspaceUri": workspace_uri}

        try:
            self.server.send_request("al/setActiveWorkspace", params)
            self.logger.log(f"Set active workspace to: {workspace_uri}", logging.INFO)
        except Exception as e:
            self.logger.log(f"Failed to set active workspace: {e}", logging.WARNING)
            # Non-critical error, continue operation
