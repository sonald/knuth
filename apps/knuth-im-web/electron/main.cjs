const path = require("node:path");
const { pathToFileURL } = require("node:url");

const {
  app,
  BrowserWindow,
  Menu,
  dialog,
  ipcMain,
  net,
  protocol,
  shell,
} = require("electron");

const { BackendManager } = require("./backend-manager.cjs");
const { SettingsStore } = require("./settings-store.cjs");

const APP_SCHEME = "knuth";
const DEV_SERVER_URL = process.env.ELECTRON_START_URL || "http://127.0.0.1:3000";
const APP_ROOT = path.join(__dirname, "..");
const WINDOW_ICON = path.join(__dirname, "assets", "icon.png");
const settingsStore = new SettingsStore({
  app,
  appRoot: APP_ROOT,
});
const backendManager = new BackendManager({
  app,
  appRoot: APP_ROOT,
  settingsStore,
});
let backendReady = null;
let debugWindow = null;

protocol.registerSchemesAsPrivileged([
  {
    scheme: APP_SCHEME,
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
    },
  },
]);

function outDir() {
  return path.join(APP_ROOT, "out");
}

function resolveOutFile(url) {
  const parsed = new URL(url);
  let pathname = decodeURIComponent(parsed.pathname);
  if (!pathname || pathname === "/") {
    pathname = "/index.html";
  }

  const root = outDir();
  const filePath = path.normalize(path.join(root, pathname));
  if (filePath !== root && !filePath.startsWith(root + path.sep)) {
    return null;
  }
  return filePath;
}

async function registerStaticProtocol() {
  protocol.handle(APP_SCHEME, (request) => {
    const filePath = resolveOutFile(request.url);
    if (filePath === null) {
      return new Response("Not found", { status: 404 });
    }
    return net.fetch(pathToFileURL(filePath).toString());
  });
}

// Per-environment URL for an in-app route. Dev serves Next routes directly;
// the packaged build serves the static export via the knuth:// protocol, where
// extensionless routes resolve to their exported ``<route>.html`` file.
function appRouteUrl(route) {
  const useDevServer = !app.isPackaged && process.env.ELECTRON_START_URL;
  if (useDevServer) {
    return route === "/" ? DEV_SERVER_URL : `${DEV_SERVER_URL}${route}`;
  }
  const file = route === "/" ? "index.html" : `${route.replace(/^\//, "")}.html`;
  return `${APP_SCHEME}://app/${file}`;
}

// Keep navigation inside the app; bounce anything else to the system browser.
function attachWindowGuards(window) {
  window.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    const allowed =
      url.startsWith(`${APP_SCHEME}://app`) || url.startsWith(DEV_SERVER_URL);
    if (!allowed) {
      event.preventDefault();
      void shell.openExternal(url);
    }
  });
  // CmdOrCtrl+Shift+E opens the event viewer from any window. The app menu is
  // disabled (setApplicationMenu(null)), so the accelerator lives here.
  window.webContents.on("before-input-event", (event, input) => {
    const mod = process.platform === "darwin" ? input.meta : input.control;
    if (mod && input.shift && (input.key || "").toLowerCase() === "e") {
      event.preventDefault();
      openEventViewer();
    }
  });
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1024,
    minHeight: 720,
    title: "Knuth IM",
    icon: WINDOW_ICON,
    backgroundColor: "#f7f4ed",
    titleBarStyle: process.platform === "darwin" ? "hiddenInset" : "default",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  attachWindowGuards(window);

  if (!app.isPackaged && process.env.ELECTRON_START_URL) {
    void window.loadURL(DEV_SERVER_URL);
    window.webContents.openDevTools({ mode: "detach" });
    return window;
  }

  void window.loadURL(appRouteUrl("/"));
  return window;
}

// The event viewer is a separate, reusable debug window so it can sit beside
// the chat. Calling again focuses the existing window instead of spawning more.
function openEventViewer() {
  if (debugWindow && !debugWindow.isDestroyed()) {
    if (debugWindow.isMinimized()) debugWindow.restore();
    debugWindow.focus();
    return debugWindow;
  }

  debugWindow = new BrowserWindow({
    width: 1180,
    height: 820,
    minWidth: 820,
    minHeight: 560,
    title: "Knuth · Event Viewer",
    icon: WINDOW_ICON,
    backgroundColor: "#f7f4ed",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  attachWindowGuards(debugWindow);
  debugWindow.on("closed", () => {
    debugWindow = null;
  });
  void debugWindow.loadURL(appRouteUrl("/debug"));
  if (!app.isPackaged && process.env.ELECTRON_START_URL) {
    debugWindow.webContents.openDevTools({ mode: "detach" });
  }
  return debugWindow;
}

app.whenReady().then(async () => {
  Menu.setApplicationMenu(null);
  backendReady = backendManager.start();
  ipcMain.handle("knuth:backend", async () => {
    if (backendReady !== null) {
      await backendReady;
    }
    return backendManager.connection();
  });
  ipcMain.handle("knuth:open-event-viewer", () => {
    openEventViewer();
  });
  ipcMain.handle("knuth:backend:restart", async () => {
    backendReady = backendManager.restart();
    await backendReady;
    return backendManager.connection();
  });
  ipcMain.handle("knuth:settings:get", () =>
    settingsStore.publicSettings(process.env, {
      allowEnvFallback: !app.isPackaged,
    }),
  );
  ipcMain.handle("knuth:settings:save", async (_event, input) => {
    const settings = settingsStore.save(input);
    backendReady = backendManager.restart();
    await backendReady;
    return {
      settings,
      backend: backendManager.connection(),
    };
  });
  ipcMain.handle("knuth:settings:clearChatgptAuth", async () => {
    settingsStore.clearChatgptAuth();
    backendReady = backendManager.restart();
    await backendReady;
    return {
      settings: settingsStore.publicSettings(process.env, {
        allowEnvFallback: !app.isPackaged,
      }),
      backend: backendManager.connection(),
    };
  });
  ipcMain.handle("knuth:settings:chooseWorkspace", async () => {
    const settings = settingsStore.publicSettings(process.env, {
      allowEnvFallback: !app.isPackaged,
    });
    const result = await dialog.showOpenDialog({
      title: "Choose workspace",
      defaultPath: settings.workspace,
      properties: ["openDirectory"],
    });
    if (result.canceled || !result.filePaths.length) {
      return null;
    }
    return result.filePaths[0];
  });
  if (app.isPackaged || !process.env.ELECTRON_START_URL) {
    await registerStaticProtocol();
  }
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  backendManager.stop();
});
