import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { startStaticServer } from "./server.mjs";

const root = path.resolve("..");
const pg = fs.mkdtempSync("/tmp/hl-app-e2e-");
const processes = [];
let server;
let staticServer;

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: "inherit", ...options });
  if (result.status !== 0) throw new Error(`${command} ${args.join(" ")} failed`);
  return result.stdout?.toString().trim();
}

function background(command, args, options = {}) {
  const child = spawn(command, args, { stdio: "inherit", ...options });
  processes.push(child);
  return child;
}

function foreground(command, args, options = {}) {
  const child = background(command, args, options);
  return new Promise((resolve, reject) => {
    child.on("close", (code) => code === 0 ? resolve() : reject(new Error(`${command} ${args.join(" ")} failed`)));
  });
}

async function ready(url) {
  for (let index = 0; index < 60; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`service not ready: ${url}`);
}

try {
  run("npm", ["run", "build"], { cwd: path.join(root, "frontend") });
  run("initdb", ["-D", path.join(pg, "data"), "-U", "postgres", "--auth=trust", "--no-instructions"], { stdio: "ignore" });
  run("pg_ctl", ["-D", path.join(pg, "data"), "-l", path.join(pg, "postgres.log"), "-o", `-k ${pg} -p 55440 -c listen_addresses=''`, "start"]);
  const seed = JSON.parse(run(`${root}/.venv/bin/python`, [path.join(root, "frontend/e2e/seed.py"), pg], { encoding: "utf-8", stdio: ["ignore", "pipe", "inherit"] }));
  const env = {
    ...process.env,
    HL_APP_DATABASE_URL: seed.database_url,
    HL_APP_MIGRATION_DATABASE_URL: seed.database_url,
    HL_APP_AUTH_TOKEN: seed.token,
    HL_APP_SECURE_COOKIE: "false",
    HL_APP_TRUSTED_ORIGINS: "http://127.0.0.1:4173",
    HL_APP_HOST: "127.0.0.1",
    HL_APP_PORT: "8001",
    HL_APP_TRADER_BIN: seed.trader,
    HL_APP_PAIRS_CONFIG: seed.pairs,
    HL_APP_RESEARCH_REPO_PATH: seed.research,
    HL_APP_MMX_BIN: seed.mmx,
    HL_APP_MMX_MODEL: "fixture-model",
    HL_APP_COLLECTORS_ENABLED: "false",
    HL_APP_SCHEDULER_ENABLED: "false",
  };
  background(`${root}/.venv/bin/python`, ["-m", "hl_app"], { cwd: root, env });
  staticServer = await startStaticServer(4173);
  await ready("http://127.0.0.1:8001/api/v1/health");
  await foreground(path.join(root, "frontend/node_modules/.bin/playwright"), ["test", "-c", path.join(root, "frontend/e2e/playwright.config.mjs")], { cwd: root, env });
} finally {
  if (staticServer) staticServer.close();
  for (const child of processes) child.kill("SIGTERM");
  if (fs.existsSync(path.join(pg, "data"))) {
    spawnSync("pg_ctl", ["-D", path.join(pg, "data"), "stop"], { stdio: "ignore" });
  }
  fs.rmSync(pg, { recursive: true, force: true });
}
