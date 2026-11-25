const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("neoagentDesktop", {
  platform: process.platform,
  pickWorkspace: () => ipcRenderer.invoke("neoagent:pickWorkspace"),
});
