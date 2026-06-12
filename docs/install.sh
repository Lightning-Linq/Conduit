#!/usr/bin/env bash
# =============================================================================
# Conduit — One-Command Installer
# Lightning Payment Rails for AI Agents
#
# Usage:
#   curl -sSL lightninglinq.ai/install.sh | bash
#
# What this does:
#   1. Checks your system (macOS or Linux)
#   2. Installs prerequisites (Python 3.11+, PostgreSQL, Redis)
#   3. Clones the Conduit repo
#   4. Sets up a Python virtual environment
#   5. Walks you through LND node connection
#   6. Generates your .env config
#   7. Initializes the database
#   8. Optionally wires up Claude Desktop MCP integration
#   9. Starts the server
# =============================================================================
set -euo pipefail

# --- Colors & Helpers --------------------------------------------------------
BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
ORANGE='\033[38;5;208m'
PURPLE='\033[0;35m'
NC='\033[0m'

CONDUIT_DIR="$HOME/conduit"
REPO_URL="https://github.com/Lightning-Linq/conduit.git"

print_logo() {
    echo ""
    echo -e "${ORANGE}  ╔═══════════════════════════════════════════════╗${NC}"
    echo -e "${ORANGE}  ║                                               ║${NC}"
    echo -e "${ORANGE}  ║${NC}   ${BOLD}⚡ C O N D U I T${NC}                            ${ORANGE}║${NC}"
    echo -e "${ORANGE}  ║${NC}   ${DIM}Lightning Payment Rails for AI Agents${NC}       ${ORANGE}║${NC}"
    echo -e "${ORANGE}  ║                                               ║${NC}"
    echo -e "${ORANGE}  ╚═══════════════════════════════════════════════╝${NC}"
    echo ""
}

info()    { echo -e "${CYAN}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[  OK]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail()    { echo -e "${RED}[FAIL]${NC} $1"; }
step()    { echo -e "\n${BOLD}${PURPLE}→ $1${NC}"; }
ask()     { echo -en "${YELLOW}[????]${NC} $1"; }

confirm() {
    ask "$1 [Y/n] "
    read -r response
    case "$response" in
        [nN][oO]|[nN]) return 1 ;;
        *) return 0 ;;
    esac
}

# --- Detect OS ---------------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Darwin*) OS="macos" ;;
        Linux*)  OS="linux" ;;
        *)       fail "Unsupported OS: $(uname -s). Conduit supports macOS and Linux."; exit 1 ;;
    esac

    if [[ "$OS" == "linux" ]]; then
        if command -v apt-get &>/dev/null; then
            PKG_MANAGER="apt"
        elif command -v dnf &>/dev/null; then
            PKG_MANAGER="dnf"
        elif command -v pacman &>/dev/null; then
            PKG_MANAGER="pacman"
        else
            fail "No supported package manager found (apt, dnf, or pacman)."
            exit 1
        fi
    fi
}

# --- Check / Install Prerequisites ------------------------------------------
check_python() {
    step "Checking Python 3.11+"

    # Check common Python commands
    for cmd in python3.11 python3.12 python3.13 python3; do
        if command -v "$cmd" &>/dev/null; then
            PY_VERSION=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+')
            PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
            PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
            if [[ "$PY_MAJOR" -ge 3 && "$PY_MINOR" -ge 11 ]]; then
                PYTHON_CMD="$cmd"
                success "Found $($cmd --version)"
                return 0
            fi
        fi
    done

    warn "Python 3.11+ not found."
    if confirm "Install Python 3.11?"; then
        install_python
    else
        fail "Python 3.11+ is required. Install it and re-run this script."
        exit 1
    fi
}

install_python() {
    if [[ "$OS" == "macos" ]]; then
        if ! command -v brew &>/dev/null; then
            info "Installing Homebrew first..."
            /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        fi
        brew install python@3.11
        PYTHON_CMD="python3.11"
    else
        case "$PKG_MANAGER" in
            apt)
                sudo apt-get update
                sudo apt-get install -y python3.11 python3.11-venv python3.11-dev python3-pip
                ;;
            dnf)
                sudo dnf install -y python3.11 python3.11-devel
                ;;
            pacman)
                sudo pacman -Sy --noconfirm python
                ;;
        esac
        PYTHON_CMD="python3.11"
    fi
    success "Python installed: $($PYTHON_CMD --version)"
}

check_postgresql() {
    step "Checking PostgreSQL"

    if command -v psql &>/dev/null; then
        PG_VERSION=$(psql --version | grep -oE '[0-9]+\.[0-9]+' | head -1)
        success "Found PostgreSQL $PG_VERSION"
    else
        warn "PostgreSQL not found."
        if confirm "Install PostgreSQL?"; then
            install_postgresql
        else
            fail "PostgreSQL is required. Install it and re-run this script."
            exit 1
        fi
    fi
}

install_postgresql() {
    if [[ "$OS" == "macos" ]]; then
        brew install postgresql@16
        brew services start postgresql@16
    else
        case "$PKG_MANAGER" in
            apt)
                sudo apt-get update
                sudo apt-get install -y postgresql postgresql-contrib
                sudo systemctl start postgresql
                sudo systemctl enable postgresql
                ;;
            dnf)
                sudo dnf install -y postgresql-server postgresql-contrib
                sudo postgresql-setup --initdb
                sudo systemctl start postgresql
                sudo systemctl enable postgresql
                ;;
            pacman)
                sudo pacman -Sy --noconfirm postgresql
                sudo -u postgres initdb -D /var/lib/postgres/data
                sudo systemctl start postgresql
                sudo systemctl enable postgresql
                ;;
        esac
    fi
    success "PostgreSQL installed and running"
}

check_redis() {
    step "Checking Redis"

    if command -v redis-cli &>/dev/null; then
        if redis-cli ping &>/dev/null; then
            success "Redis is running"
        else
            warn "Redis is installed but not running."
            if confirm "Start Redis?"; then
                start_redis
            fi
        fi
    else
        warn "Redis not found."
        if confirm "Install Redis?"; then
            install_redis
        else
            fail "Redis is required. Install it and re-run this script."
            exit 1
        fi
    fi
}

install_redis() {
    if [[ "$OS" == "macos" ]]; then
        brew install redis
        brew services start redis
    else
        case "$PKG_MANAGER" in
            apt)
                sudo apt-get update
                sudo apt-get install -y redis-server
                sudo systemctl start redis-server
                sudo systemctl enable redis-server
                ;;
            dnf)
                sudo dnf install -y redis
                sudo systemctl start redis
                sudo systemctl enable redis
                ;;
            pacman)
                sudo pacman -Sy --noconfirm redis
                sudo systemctl start redis
                sudo systemctl enable redis
                ;;
        esac
    fi
    success "Redis installed and running"
}

start_redis() {
    if [[ "$OS" == "macos" ]]; then
        brew services start redis
    else
        sudo systemctl start redis-server 2>/dev/null || sudo systemctl start redis
    fi
    success "Redis started"
}

# --- Clone Repo --------------------------------------------------------------
clone_repo() {
    step "Downloading Conduit"

    if [[ -d "$CONDUIT_DIR" ]]; then
        warn "Directory $CONDUIT_DIR already exists."
        if confirm "Remove it and start fresh?"; then
            rm -rf "$CONDUIT_DIR"
        else
            info "Using existing directory."
            return 0
        fi
    fi

    git clone "$REPO_URL" "$CONDUIT_DIR"
    success "Cloned to $CONDUIT_DIR"
}

# --- Python Environment ------------------------------------------------------
setup_venv() {
    step "Setting up Python environment"

    cd "$CONDUIT_DIR"
    $PYTHON_CMD -m venv .venv
    source .venv/bin/activate
    pip install --upgrade pip -q
    pip install -e ".[dev]" -q
    success "Dependencies installed"
}

# --- Database Setup -----------------------------------------------------------
setup_database() {
    step "Setting up database"

    # Try to create the conduit user and database
    if command -v createuser &>/dev/null; then
        # Check if user already exists
        if ! psql -U postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='conduit'" 2>/dev/null | grep -q 1; then
            createuser -U postgres conduit 2>/dev/null || sudo -u postgres createuser conduit 2>/dev/null || true
        fi
        if ! psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='conduit'" 2>/dev/null | grep -q 1; then
            createdb -U postgres -O conduit conduit 2>/dev/null || sudo -u postgres createdb -O conduit conduit 2>/dev/null || true
        fi
        psql -U postgres -c "ALTER USER conduit WITH PASSWORD 'conduit';" 2>/dev/null || \
            sudo -u postgres psql -c "ALTER USER conduit WITH PASSWORD 'conduit';" 2>/dev/null || true
        success "Database 'conduit' ready"
    else
        warn "Could not auto-configure database. You may need to create it manually:"
        echo -e "  ${DIM}createdb conduit${NC}"
        echo -e "  ${DIM}createuser conduit${NC}"
    fi
}

# --- LND Configuration -------------------------------------------------------
configure_lnd() {
    step "Lightning Node Configuration"

    echo ""
    echo -e "  Conduit connects to your LND node to send and receive"
    echo -e "  Lightning payments. You have a few options:"
    echo ""
    echo -e "  ${BOLD}1)${NC} ${GREEN}Connect to an existing LND node${NC}"
    echo -e "     ${DIM}You already run LND (Start9, Umbrel, standalone, etc.)${NC}"
    echo ""
    echo -e "  ${BOLD}2)${NC} ${YELLOW}Use testnet / simulated mode${NC}"
    echo -e "     ${DIM}No real node needed — great for trying Conduit out${NC}"
    echo ""
    echo -e "  ${BOLD}3)${NC} ${CYAN}Skip for now${NC}"
    echo -e "     ${DIM}Configure LND later by editing ~/.conduit/.env${NC}"
    echo ""

    ask "Choose [1/2/3]: "
    read -r lnd_choice

    case "$lnd_choice" in
        1) configure_lnd_existing ;;
        2) configure_lnd_testnet ;;
        3) configure_lnd_skip ;;
        *) configure_lnd_skip ;;
    esac
}

configure_lnd_existing() {
    echo ""
    info "We need a few details about your LND node."
    echo ""

    ask "LND host IP or hostname [localhost]: "
    read -r lnd_host
    LND_HOST="${lnd_host:-localhost}"

    ask "LND gRPC port [10009]: "
    read -r lnd_port
    LND_GRPC_PORT="${lnd_port:-10009}"

    ask "Path to TLS cert [~/.lnd/tls.cert]: "
    read -r lnd_tls
    LND_TLS_CERT_PATH="${lnd_tls:-$HOME/.lnd/tls.cert}"
    # Expand tilde
    LND_TLS_CERT_PATH="${LND_TLS_CERT_PATH/#\~/$HOME}"

    ask "Path to admin macaroon [~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon]: "
    read -r lnd_mac
    LND_MACAROON_PATH="${lnd_mac:-$HOME/.lnd/data/chain/bitcoin/mainnet/admin.macaroon}"
    LND_MACAROON_PATH="${LND_MACAROON_PATH/#\~/$HOME}"

    ask "Network (mainnet/testnet/regtest) [mainnet]: "
    read -r lnd_net
    LND_NETWORK="${lnd_net:-mainnet}"

    # Validate paths
    if [[ ! -f "$LND_TLS_CERT_PATH" ]]; then
        warn "TLS cert not found at $LND_TLS_CERT_PATH — you can fix this in .env later."
    else
        success "TLS cert found"
    fi

    if [[ ! -f "$LND_MACAROON_PATH" ]]; then
        warn "Macaroon not found at $LND_MACAROON_PATH — you can fix this in .env later."
    else
        success "Macaroon found"
    fi
}

configure_lnd_testnet() {
    info "Setting up testnet/simulated mode."
    LND_HOST="localhost"
    LND_GRPC_PORT="10009"
    LND_TLS_CERT_PATH="~/.lnd/tls.cert"
    LND_MACAROON_PATH="~/.lnd/data/chain/bitcoin/testnet/admin.macaroon"
    LND_NETWORK="testnet"
    warn "You'll need a testnet LND node to make real payments."
    echo -e "  ${DIM}See: https://docs.lightning.engineering/lightning-network-tools/lnd/run-lnd${NC}"
}

configure_lnd_skip() {
    info "Skipping LND setup. Using placeholder values."
    LND_HOST="localhost"
    LND_GRPC_PORT="10009"
    LND_TLS_CERT_PATH="~/.lnd/tls.cert"
    LND_MACAROON_PATH="~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
    LND_NETWORK="mainnet"
}

# --- Generate .env ------------------------------------------------------------
generate_env() {
    step "Generating configuration"

    # Generate a random API key
    API_KEY=$(openssl rand -base64 32 | tr -d '=/+' | head -c 43)
    L402_SECRET=$(openssl rand -base64 32 | tr -d '=/+' | head -c 43)

    cat > "$CONDUIT_DIR/.env" << EOF
# =============================================================================
# Conduit Configuration — Generated by installer
# =============================================================================

# --- App ---
APP_NAME=Conduit
APP_ENV=development
DEBUG=false
API_HOST=0.0.0.0
API_PORT=8000

# --- PostgreSQL ---
DATABASE_URL=postgresql+asyncpg://conduit:conduit@localhost:5432/conduit

# --- Redis ---
REDIS_URL=redis://localhost:6379/0

# --- LND Node ---
LND_HOST=${LND_HOST}
LND_GRPC_PORT=${LND_GRPC_PORT}
LND_TLS_CERT_PATH=${LND_TLS_CERT_PATH}
LND_MACAROON_PATH=${LND_MACAROON_PATH}
LND_NETWORK=${LND_NETWORK}

# --- L402 Auth ---
L402_SECRET_KEY=${L402_SECRET}
L402_TOKEN_EXPIRY_SECONDS=3600

# --- Fees ---
TRANSACTION_FEE_PERCENT=1.5

# --- API Key Auth ---
CONDUIT_API_KEY=${API_KEY}

# --- Spending Limits (sats) ---
SPENDING_LIMIT_PER_PAYMENT_SATS=10000
SPENDING_LIMIT_HOURLY_SATS=50000
SPENDING_LIMIT_DAILY_SATS=200000
SPENDING_CONFIRM_ABOVE_SATS=5000
EOF

    success "Config written to $CONDUIT_DIR/.env"
    echo ""
    echo -e "  ${DIM}Your API key: ${NC}${BOLD}${API_KEY}${NC}"
    echo -e "  ${DIM}Save this — you'll need it to authenticate requests.${NC}"
}

# --- Run Migrations -----------------------------------------------------------
run_migrations() {
    step "Initializing database schema"

    cd "$CONDUIT_DIR"
    source .venv/bin/activate

    if [[ -d "alembic" ]]; then
        alembic upgrade head 2>/dev/null && success "Database migrations applied" || \
            warn "Could not run migrations — you may need to configure the database first."
    else
        warn "No alembic directory found. Skipping migrations."
    fi
}

# --- Claude Desktop MCP Integration ------------------------------------------
setup_claude_desktop() {
    step "Claude Desktop Integration"

    echo ""
    echo -e "  Conduit can integrate directly with Claude Desktop as an"
    echo -e "  MCP server, giving Claude access to all 26 Lightning tools."
    echo ""

    if ! confirm "Set up Claude Desktop integration?"; then
        info "Skipping Claude Desktop setup."
        return 0
    fi

    # Find Claude Desktop config
    if [[ "$OS" == "macos" ]]; then
        CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
    else
        CLAUDE_CONFIG="$HOME/.config/claude/claude_desktop_config.json"
    fi

    CONDUIT_MCP_ENTRY=$(cat << MCPEOF
{
    "conduit-lightning": {
        "command": "${CONDUIT_DIR}/.venv/bin/python",
        "args": ["-m", "conduit.mcp_server"],
        "cwd": "${CONDUIT_DIR}",
        "env": {
            "PYTHONPATH": "${CONDUIT_DIR}/src"
        }
    }
}
MCPEOF
)

    if [[ -f "$CLAUDE_CONFIG" ]]; then
        warn "Claude Desktop config exists at:"
        echo -e "  ${DIM}${CLAUDE_CONFIG}${NC}"
        echo ""
        echo -e "  Add this to the ${BOLD}\"mcpServers\"${NC} section:"
        echo ""
        echo -e "${DIM}${CONDUIT_MCP_ENTRY}${NC}"
        echo ""
        info "We won't modify your config automatically to avoid breaking existing servers."
    else
        if confirm "Create Claude Desktop config?"; then
            mkdir -p "$(dirname "$CLAUDE_CONFIG")"
            cat > "$CLAUDE_CONFIG" << CONFIGEOF
{
    "mcpServers": {
        "conduit-lightning": {
            "command": "${CONDUIT_DIR}/.venv/bin/python",
            "args": ["-m", "conduit.mcp_server"],
            "cwd": "${CONDUIT_DIR}",
            "env": {
                "PYTHONPATH": "${CONDUIT_DIR}/src"
            }
        }
    }
}
CONFIGEOF
            success "Claude Desktop config created"
            info "Restart Claude Desktop to load Conduit tools."
        fi
    fi
}

# --- Final Summary ------------------------------------------------------------
print_summary() {
    echo ""
    echo -e "${ORANGE}  ╔═══════════════════════════════════════════════╗${NC}"
    echo -e "${ORANGE}  ║${NC}   ${GREEN}${BOLD}Conduit is installed!${NC}                        ${ORANGE}║${NC}"
    echo -e "${ORANGE}  ╚═══════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  ${BOLD}Start the server:${NC}"
    echo -e "  ${DIM}  cd ~/conduit${NC}"
    echo -e "  ${DIM}  source .venv/bin/activate${NC}"
    echo -e "  ${DIM}  uvicorn conduit.api.main:app --reload${NC}"
    echo ""
    echo -e "  ${BOLD}Or start the MCP server for Claude:${NC}"
    echo -e "  ${DIM}  cd ~/conduit${NC}"
    echo -e "  ${DIM}  source .venv/bin/activate${NC}"
    echo -e "  ${DIM}  python -m conduit.mcp_server${NC}"
    echo ""
    echo -e "  ${BOLD}Useful paths:${NC}"
    echo -e "  ${DIM}  Config:     ~/conduit/.env${NC}"
    echo -e "  ${DIM}  Source:     ~/conduit/src/conduit/${NC}"
    echo -e "  ${DIM}  Tests:      cd ~/conduit && pytest${NC}"
    echo ""
    echo -e "  ${BOLD}Links:${NC}"
    echo -e "  ${DIM}  Docs:       https://lightninglinq.ai/docs${NC}"
    echo -e "  ${DIM}  GitHub:     https://github.com/Lightning-Linq/conduit${NC}"
    echo -e "  ${DIM}  API:        http://localhost:8000/docs${NC}"
    echo ""
    echo -e "  ${ORANGE}⚡${NC} ${BOLD}Happy building!${NC}"
    echo ""
}

# === Main ====================================================================
main() {
    print_logo

    info "This script will install Conduit and its dependencies."
    info "It will ask before installing anything."
    echo ""

    if ! confirm "Ready to begin?"; then
        echo "No worries — run this script again when you're ready."
        exit 0
    fi

    detect_os
    info "Detected: ${BOLD}${OS}${NC}"

    check_python
    check_postgresql
    check_redis
    clone_repo
    setup_venv
    setup_database
    configure_lnd
    generate_env
    run_migrations
    setup_claude_desktop
    print_summary
}

main "$@"
