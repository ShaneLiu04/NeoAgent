/**
 * NeoAgent Studio — Production Electron Main Process
 *
 * Spawns the embedded Python `neoagent.desktop` control-plane server on a free
 * loopback port, waits for `/api/health`, and mounts a security-hardened
 * BrowserWindow (`contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`).
 */

const { app, BrowserWindow, ipcMain, dialog, shell } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const net = require("net");
const path = require("path");

let pyBackend = null;
let mainWindow = null;

function findFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

function waitForHealth(url, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const check = () => {
      http
        .get(`${url}/api/health`, (res) => {
          if (res.statusCode === 200) resolve();
          else retry();
        })
        .on("error", retry);
    };
    const retry = () => {
      if (Date.now() > deadline) reject(new Error("Backend health check timed out"));
      else setTimeout(check, 150);
    };
    check();
  });
}

async function createStudioWindow() {
  const port = await findFreePort();
  const repoRoot = process.env.NEOAGENT_PROJECT_ROOT || path.resolve(__dirname, "..");
  const pyBin = process.env.NEOAGENT_PYTHON || "python";

  pyBackend = spawn(
    pyBin,
    ["-m", "neoagent.desktop", "--root", repoRoot, "--port", String(port), "--headless"],
    {
      cwd: repoRoot,
      env: { ...process.env, PYTHONPATH: path.resolve(__dirname, "..") },
      stdio: "ignore",
    }
  );

  const serverUrl = `http://127.0.0.1:${port}`;
  await waitForHealth(serverUrl);

  mainWindow = new BrowserWindow({
    width: 1460,
    height: 920,
    minWidth: 1080,
    minHeight: 700,
    title: "NeoAgent Studio",
    backgroundColor: "#0b0f19",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  await mainWindow.loadURL(serverUrl);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://") || url.startsWith("http://")) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });
}

ipcMain.handle("neoagent:pickWorkspace", async () => {
  const res = await dialog.showOpenDialog({ properties: ["openDirectory"] });
  return res.canceled ? null : res.filePaths[0];
});

app.whenReady().then(createStudioWindow);

app.on("window-all-closed", () => {
  if (pyBackend) {
    pyBackend.kill();
    pyBackend = null;
  }
  if (process.platform !== "darwin") app.quit();
});
