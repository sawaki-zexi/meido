import { contextBridge, ipcRenderer } from 'electron';
contextBridge.exposeInMainWorld('pet', Object.freeze({
  initial: () => ipcRenderer.invoke('pet:initial'),
  ready: () => ipcRenderer.send('pet:ready'),
  close: () => ipcRenderer.send('pet:close'),
  receipt: (actionId: string, status: string, frames: number) => ipcRenderer.send('pet:receipt', { actionId, status, frames }),
  onAction: (callback: (value: { actionId: string; state: string }) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, value: { actionId: string; state: string }) => callback(value);
    ipcRenderer.on('pet:action', listener);
    return () => ipcRenderer.removeListener('pet:action', listener);
  },
}));
