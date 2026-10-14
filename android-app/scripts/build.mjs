import { copyFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { build } from 'esbuild';

const root = resolve(import.meta.dirname, '..');
const output = resolve(root, 'www');
await mkdir(output, { recursive: true });
await copyFile(resolve(root, 'src/index.html'), resolve(output, 'index.html'));
await copyFile(resolve(root, '../app/static/offline.js'), resolve(output, 'offline.js'));
await build({ entryPoints: [resolve(root, 'src/main.js')], outfile: resolve(output, 'app.js'), bundle: true, format: 'iife', platform: 'browser', target: 'es2022' });
