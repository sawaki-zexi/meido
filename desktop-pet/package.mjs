import { cp, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
const root = path.dirname(fileURLToPath(import.meta.url));
if (process.platform !== 'win32') throw new Error('Local desktop pet distribution currently requires Windows');
createRequire(import.meta.url)('electron'); // Electron 44 installs its binary on demand.
const release = path.join(root, 'release');
await mkdir(release); // Do not replace an existing possibly running distribution.
await cp(path.join(root, '../node_modules/electron/dist'), release, { recursive: true });
await rename(path.join(release, 'electron.exe'), path.join(release, 'MeidoPet.exe'));
const application = path.join(release, 'resources/app');
await mkdir(application, { recursive: true });
await cp(path.join(root, 'dist'), application, { recursive: true });
await writeFile(path.join(application, 'package.json'), JSON.stringify({ name: 'meido-desktop-pet', version: '0.1.0', main: 'main.cjs' }));
await writeFile(path.join(release, 'SHIORI-LICENSE'), await readFile(path.join(root, '../third_party/shiori-agent/LICENSE')));
await writeFile(path.join(release, 'SHIORI-NOTICE.md'), await readFile(path.join(root, '../third_party/shiori-agent/README.md')));
for (const dependency of ['react', 'react-dom']) {
  await writeFile(path.join(release, `${dependency}-LICENSE`), await readFile(path.join(root, `../node_modules/${dependency}/LICENSE`)));
}
console.log(`Local desktop pet distribution: ${release}`);
