#!/usr/bin/env node

/**
 * Conduit Setup CLI
 *
 * Detects installed AI clients, asks for wallet configuration,
 * and writes MCP server config to the right places.
 *
 * Usage:
 *   npx conduit-setup
 *   # or
 *   node cli/index.mjs
 */

import { existsSync, readFileSync, writeFileSync, mkdirSync } from "fs";
import { join, dirname, resolve } from "path";
import { homedir, platform } from "os";
import { execSync } from "child_process";
import { createInterface } from "readline";
import { randomBytes } from "crypto";

// ── Colors ──────────────────────────────────────────────────────────

const C = {
  reset: "\x1b[0m",
  bold: "\x1b[1m",
  dim: "\x1b[2m",
  orange: "\x1b[38;5;208m",
  purple: "\x1b[38;5;141m",
  green: "\x1b[32m",
  red: "\x1b[31m",
  cyan: "\x1b[36m",
  yellow: "\x1b[33m",
};

const log = (msg) => console.log(msg);
const info = (label, value) =>
  log(`  ${C.dim}${label}:${C.reset} ${value}`);
const success = (msg) => log(`${C.green}  ✓${C.reset} ${msg}`);
const warn = (msg) => log(`${C.yellow}  !${C.reset} ${msg}`);
const fail = (msg) => log(`${C.red}  ✗${C.reset} ${msg}`);

// ── Readline helper ─────────────────────────────────────────────────

const rl = createInterface({ input: process.stdin, output: process.stdout });

function ask(question) {
  return new Promise((resolve) => {
    rl.question(`${C.orange}?${C.reset} ${question} `, (answer) => {
      resolve(answer.trim());
    });
  });
}

async function choose(question, options) {
  log(`\n${C.orange}?${C.reset} ${question}`);
  options.forEach((opt, i) => {
    log(`  ${C.bold}${i + 1}${C.reset}  ${opt.label}${C.dim} — ${opt.desc}${C.reset}`);
  });
  while (true) {
    const answer = await ask(`Choose [1-${options.length}]:`);
    const idx = parseInt(answer, 10) - 1;
    if (idx >= 0 && idx < options.length) return options[idx].value;
    log(`  ${C.red}Invalid choice. Enter a number 1-${options.length}.${C.reset}`);
  }
}

// ── Client detection ────────────────────────────────────────────────

const HOME = homedir();
const IS_MAC = platform() === "darwin";
const IS_WIN = platform() === "win32";

const CLIENTS = [
  {
    name: "Claude Desktop",
    configPath: IS_MAC
      ? join(HOME, "Library", "Application Support", "Claude", "claude_desktop_config.json")
      : IS_WIN
        ? join(process.env.APPDATA || "", "Claude", "claude_desktop_config.json")
        : join(HOME, ".config", "claude", "claude_desktop_config.json"),
    detected: false,
  },
  {
    name: "Cursor",
    configPath: join(HOME, ".cursor", "mcp.json"),
    detected: false,
  },
  {
    name: "Windsurf",
    configPath: join(HOME, ".codeium", "windsurf", "mcp_config.json"),
    detected: false,
  },
  {
    name: "VS Code (Copilot)",
    configPath: join(HOME, ".vscode", "mcp.json"),
    detected: false,
  },
];

function detectClients() {
  for (const client of CLIENTS) {
    // Check if config file or parent directory exists
    const dir = dirname(client.configPath);
    if (existsSync(dir) || existsSync(client.configPath)) {
      client.detected = true;
    }
  }
  return CLIENTS.filter((c) => c.detected);
}

// ── Conduit detection ───────────────────────────────────────────────

function findConduit() {
  // Check if we're inside the Conduit repo
  const cwd = process.cwd();
  const candidates = [
    cwd,
    join(cwd, ".."),
    join(HOME, "Conduit"),
    join(HOME, "Desktop", "Conduit"),
    join(HOME, "Desktop", "Claude", "Conduit - Lightning Payment Rails for AI Agents"),
    join(HOME, "projects", "Conduit"),
    join(HOME, "Projects", "Conduit"),
    join(HOME, "code", "Conduit"),
  ];

  for (const dir of candidates) {
    const marker = join(dir, "src", "conduit", "mcp_server.py");
    if (existsSync(marker)) return resolve(dir);
  }
  return null;
}

function findPython(conduitPath) {
  // Check for venv python first
  const venvPython = join(conduitPath, ".venv", "bin", "python");
  if (existsSync(venvPython)) return venvPython;

  const venvPython3 = join(conduitPath, ".venv", "bin", "python3");
  if (existsSync(venvPython3)) return venvPython3;

  // Windows venv
  const venvPythonWin = join(conduitPath, ".venv", "Scripts", "python.exe");
  if (existsSync(venvPythonWin)) return venvPythonWin;

  // System python
  try {
    execSync("python3 --version", { stdio: "pipe" });
    return "python3";
  } catch {
    try {
      execSync("python --version", { stdio: "pipe" });
      return "python";
    } catch {
      return null;
    }
  }
}

// ── Config generation ───────────────────────────────────────────────

function buildMcpEntry(conduitPath, pythonPath) {
  return {
    command: pythonPath,
    args: ["-m", "conduit.mcp_server"],
    cwd: conduitPath,
    env: {
      PYTHONPATH: join(conduitPath, "src"),
    },
  };
}

function writeClientConfig(client, mcpEntry) {
  let config = {};

  // Read existing config if it exists
  if (existsSync(client.configPath)) {
    try {
      const raw = readFileSync(client.configPath, "utf-8");
      config = JSON.parse(raw);
    } catch {
      // Corrupt or empty — start fresh
      config = {};
    }
  } else {
    // Create parent directory
    mkdirSync(dirname(client.configPath), { recursive: true });
  }

  // Merge in conduit server
  if (!config.mcpServers) config.mcpServers = {};

  if (config.mcpServers.conduit) {
    return "exists"; // Already configured
  }

  config.mcpServers.conduit = mcpEntry;

  writeFileSync(client.configPath, JSON.stringify(config, null, 2) + "\n");
  return "written";
}

// ── .env setup ──────────────────────────────────────────────────────

async function setupEnv(conduitPath) {
  const envPath = join(conduitPath, ".env");
  const examplePath = join(conduitPath, ".env.example");

  if (existsSync(envPath)) {
    const content = readFileSync(envPath, "utf-8");
    if (content.includes("CONDUIT_API_KEY") && !content.includes("CHANGE-ME")) {
      return "exists";
    }
  }

  // Copy from example if needed
  if (!existsSync(envPath) && existsSync(examplePath)) {
    let template = readFileSync(examplePath, "utf-8");

    // Generate API key
    const apiKey = randomBytes(32).toString("base64url");
    template = template.replace(
      /CONDUIT_API_KEY=.*/,
      `CONDUIT_API_KEY=${apiKey}`
    );

    writeFileSync(envPath, template);
    return "created";
  }

  return "missing-template";
}

async function configureWallet(conduitPath) {
  const envPath = join(conduitPath, ".env");
  if (!existsSync(envPath)) return;

  let env = readFileSync(envPath, "utf-8");

  const walletType = await choose("How do you connect to Lightning?", [
    {
      value: "nwc",
      label: "NWC (Nostr Wallet Connect)",
      desc: "Paste a connection string from Alby, Primal, Zeus, etc.",
    },
    {
      value: "lnd",
      label: "LND Node",
      desc: "Direct gRPC to your own Lightning node",
    },
    {
      value: "skip",
      label: "Skip for now",
      desc: "I'll configure the wallet later",
    },
  ]);

  if (walletType === "nwc") {
    const nwcString = await ask(
      "Paste your NWC connection string (nostr+walletconnect://...):"
    );
    if (nwcString.startsWith("nostr+walletconnect://")) {
      // Update .env
      if (env.includes("NWC_CONNECTION_STRING=")) {
        env = env.replace(
          /NWC_CONNECTION_STRING=.*/,
          `NWC_CONNECTION_STRING=${nwcString}`
        );
      } else {
        env += `\nNWC_CONNECTION_STRING=${nwcString}\n`;
      }
      if (env.includes("WALLET_BACKEND=")) {
        env = env.replace(/WALLET_BACKEND=.*/, "WALLET_BACKEND=nwc");
      } else {
        env += `WALLET_BACKEND=nwc\n`;
      }
      writeFileSync(envPath, env);
      success("NWC wallet configured");
    } else {
      warn("That doesn't look like a valid NWC string. You can edit .env manually later.");
    }
  } else if (walletType === "lnd") {
    log(`\n  ${C.dim}Configure LND in ${envPath}:${C.reset}`);
    log(`  ${C.dim}  LND_HOST, LND_GRPC_PORT, LND_TLS_CERT_PATH, LND_MACAROON_PATH${C.reset}`);
    warn("Edit .env with your LND credentials, then restart your AI client.");
  }
}

// ── Main ────────────────────────────────────────────────────────────

async function main() {
  log("");
  log(
    `${C.bold}${C.orange}  ⚡ Conduit Setup${C.reset}${C.dim} — Lightning Payment Rails for AI Agents${C.reset}`
  );
  log(`${C.dim}  by Lightning Linq${C.reset}`);
  log("");

  // Step 1: Find Conduit
  log(`${C.bold}  Step 1: Locate Conduit${C.reset}`);
  let conduitPath = findConduit();

  if (!conduitPath) {
    log("");
    warn("Conduit repo not found in common locations.");
    const customPath = await ask("Enter the path to your Conduit directory (or 'clone' to clone it):");

    if (customPath.toLowerCase() === "clone") {
      const cloneDir = await ask(`Clone to (default: ${join(HOME, "Conduit")}):`);
      const target = cloneDir || join(HOME, "Conduit");
      log(`\n  Cloning...`);
      try {
        execSync(`git clone https://github.com/Lightning-Linq/Conduit.git "${target}"`, {
          stdio: "inherit",
        });
        conduitPath = target;
        success(`Cloned to ${target}`);
      } catch {
        fail("Clone failed. Check your internet connection and try again.");
        rl.close();
        process.exit(1);
      }
    } else {
      conduitPath = resolve(customPath);
      if (!existsSync(join(conduitPath, "src", "conduit", "mcp_server.py"))) {
        fail(`${conduitPath} doesn't look like a Conduit installation.`);
        rl.close();
        process.exit(1);
      }
    }
  }

  success(`Found Conduit at ${conduitPath}`);
  log("");

  // Step 2: Find Python
  log(`${C.bold}  Step 2: Check Python${C.reset}`);
  const pythonPath = findPython(conduitPath);
  if (!pythonPath) {
    fail("Python not found. Install Python 3.11+ and create a venv:");
    log(`    cd "${conduitPath}"`);
    log("    python3 -m venv .venv && source .venv/bin/activate && pip install -e .");
    rl.close();
    process.exit(1);
  }
  success(`Python: ${pythonPath}`);
  log("");

  // Step 3: Setup .env
  log(`${C.bold}  Step 3: Configure environment${C.reset}`);
  const envResult = await setupEnv(conduitPath);
  if (envResult === "exists") {
    success(".env already configured");
  } else if (envResult === "created") {
    success(".env created with auto-generated API key");
  } else {
    warn(".env.example not found — create .env manually");
  }
  log("");

  // Step 4: Wallet configuration
  log(`${C.bold}  Step 4: Lightning wallet${C.reset}`);
  await configureWallet(conduitPath);
  log("");

  // Step 5: Detect and configure AI clients
  log(`${C.bold}  Step 5: Configure AI clients${C.reset}`);
  const detected = detectClients();

  if (detected.length === 0) {
    warn("No AI clients detected. Supported: Claude Desktop, Cursor, Windsurf, VS Code");
    log(`  ${C.dim}You can add Conduit manually — see docs/setup.md${C.reset}`);
  } else {
    const mcpEntry = buildMcpEntry(conduitPath, pythonPath);

    for (const client of detected) {
      const result = writeClientConfig(client, mcpEntry);
      if (result === "exists") {
        info(client.name, "already configured");
      } else {
        success(`${client.name} — config written to ${client.configPath}`);
      }
    }

    // Also offer to configure non-detected clients
    const undetected = CLIENTS.filter((c) => !c.detected);
    if (undetected.length > 0) {
      log("");
      const configMore = await ask(
        `Also configure ${undetected.map((c) => c.name).join(", ")}? [y/N]`
      );
      if (configMore.toLowerCase() === "y") {
        for (const client of undetected) {
          const result = writeClientConfig(client, mcpEntry);
          success(`${client.name} — config written`);
        }
      }
    }
  }

  // Done!
  log("");
  log(`${C.bold}${C.green}  ✓ Setup complete!${C.reset}`);
  log("");
  log(`  ${C.dim}Next steps:${C.reset}`);
  log(`  1. Restart your AI client (quit and reopen)`);
  log(`  2. Make sure PostgreSQL is running: ${C.cyan}brew services start postgresql@16${C.reset}`);
  log(`  3. Ask your AI: ${C.orange}"What's my Lightning wallet balance?"${C.reset}`);
  log("");
  log(`  ${C.dim}Docs: https://lightninglinq.com/conduit/docs${C.reset}`);
  log(`  ${C.dim}Repo: https://github.com/Lightning-Linq/Conduit${C.reset}`);
  log("");

  rl.close();
}

main().catch((err) => {
  console.error(`\n${C.red}Error: ${err.message}${C.reset}`);
  rl.close();
  process.exit(1);
});
