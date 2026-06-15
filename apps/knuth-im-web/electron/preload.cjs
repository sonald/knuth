const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("knuthDesktop", {
  platform: process.platform,
  versions: {
    electron: process.versions.electron,
    chrome: process.versions.chrome,
    node: process.versions.node,
  },
  backend: () => ipcRenderer.invoke("knuth:backend"),
  restartBackend: () => ipcRenderer.invoke("knuth:backend:restart"),
  getSettings: () => ipcRenderer.invoke("knuth:settings:get"),
  saveSettings: (settings) => ipcRenderer.invoke("knuth:settings:save", settings),
  chooseWorkspace: () => ipcRenderer.invoke("knuth:settings:chooseWorkspace"),
});
