{
  description = "ECOA Tools Development Environment & Backend API";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        # ECOA toolchain Python packages
        ecoaPackages = with python.pkgs; [
          # Core dependencies
          colorama
          lxml
          xsdata
          jinja2

          # Development tools
          setuptools
          wheel
          pip
        ];

        # Flask backend API packages
        apiPackages = with python.pkgs; [
          flask
          werkzeug
          pyyaml
          python-dotenv
        ];

        # ECOA tools to install from as6-tools directory
        ecoaTools = [
          "ecoa-toolset"
          "ecoa-exvt"
          "ecoa-csmgvt"
          "ecoa-mscigt"
          "ecoa-asctg"
          "ecoa-ldp"
        ];

        # ECOA tools mapping with descriptions
        toolDescriptions = {
          "ecoa-exvt" = "Validate ECOA XML files";
          "ecoa-csmgvt" = "Generate CSM test framework";
          "ecoa-mscigt" = "Generate module skeletons";
          "ecoa-asctg" = "Generate component tests";
          "ecoa-ldp" = "Generate platform code";
        };

      in
      {
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Python environment
            python
            python.pkgs.pip
            python.pkgs.venvShellHook

            # C/C++ build tools (for compiling generated code)
            gcc
            cmake
            gnumake

            # System dependencies
            apr
            cunit
            log4cplus
            pkg-config
            bison
            flex
          ] ++ ecoaPackages ++ apiPackages;

          # Virtual environment path
          VENV = ".venv";

          shellHook = ''
            echo "╔════════════════════════════════════════════════════════════╗"
            echo "║   ECOA Tools & Backend API Development Environment         ║"
            echo "╚════════════════════════════════════════════════════════════╝"
            echo ""

            # Create virtual environment if it doesn't exist
            if [ ! -d "$VENV" ]; then
              echo "📦 Creating virtual environment..."
              python -m venv "$VENV"
            fi

            # Activate virtual environment
            source "$VENV/bin/activate"

            # Upgrade pip
            echo "📦 Upgrading pip..."
            pip install --upgrade pip --quiet

            # Install ECOA tools in editable mode
            echo ""
            echo "📦 Installing ECOA tools in editable mode..."
            installed_tools=()
            for tool in ${builtins.toString ecoaTools}; do
              if [ -d "as6-tools/$tool" ]; then
                echo "  → Installing $tool"
                pip install -e "as6-tools/$tool" --no-build-isolation --no-deps --quiet && \
                  installed_tools+=("$tool")
              fi
            done

            echo ""
            echo "✅ Setup complete!"
            echo ""
            echo "Environment info:"
            echo "  Python: $(python --version)"
            echo "  GCC:    $(gcc --version | head -n1)"
            echo "  CMake:  $(cmake --version | head -n1)"
            echo "  Venv:   $VENV"
            echo ""

            # Set CMAKE_PREFIX_PATH for CMake to find libraries
            export CMAKE_PREFIX_PATH="${pkgs.zlog}:${pkgs.apr}:${pkgs.aprutil}:$CMAKE_PREFIX_PATH"

            # Add paths to PKG_CONFIG_PATH
            export PKG_CONFIG_PATH="${pkgs.apr.dev}/lib/pkgconfig:${pkgs.aprutil.dev}/lib/pkgconfig:${pkgs.zlog}/lib/pkgconfig:$PKG_CONFIG_PATH"

            echo "Available ECOA commands:"
            echo "  • ecoa-exvt    - Validate ECOA XML files"
            echo "  • ecoa-csmgvt  - Generate CSM test framework"
            echo "  • ecoa-mscigt  - Generate module skeletons"
            echo "  • ecoa-asctg   - Generate component tests"
            echo "  • ecoa-ldp     - Generate platform code"
            echo ""

            echo "Flask API commands:"
            echo "  • python main.py           - Start the API server"
            echo "  • curl /api/tools          - List available tools"
            echo "  • curl /api/tools/execute  - Execute a tool"
            echo ""

            echo "Example usage:"
            echo "  # ECOA tool"
            echo "  ecoa-exvt -p examples/marx_brothers/marx_brothers.project.xml"
            echo ""
            echo "  # Flask API"
            echo "  python main.py &"
            echo ""
          '';
        };
      }
    );
}
