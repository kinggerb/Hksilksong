#!/usr/bin/env node
/* Decode a .hkar container exactly the way the browser does.
 *
 * Mirrors the footer/index parsing in index.html and decodes the entries with
 * the same wasm decoder the page ships (xz-decompress.min.js in hk-unpack.js).
 *
 *   node tools/verify_hkar.js assets.hkar [--sample N]
 */
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const crypto = require('crypto');

const root = path.join(__dirname, '..');
const xz = require(path.join(root, 'xz-decompress.min.js'));
const XzReadableStream = (xz && xz.XzReadableStream) ||
  (globalThis['xz-decompress'] || {}).XzReadableStream;

const file = process.argv[2];
const sampleIdx = process.argv.indexOf('--sample');
const sample = sampleIdx > 0 ? parseInt(process.argv[sampleIdx + 1], 10) : 0;
const buf = fs.readFileSync(file);

// --- same as index.html:installArchive -------------------------------------
const fileSize = buf.length;
const foot = buf.subarray(fileSize - 32);
const magic = foot.subarray(0, 8).toString('utf8');
if (magic !== 'HKAR0001') throw new Error('bad magic ' + magic);
const count = foot.readUInt32LE(8);
const indexOff = Number(foot.readBigUInt64LE(16));
const indexLen = Number(foot.readBigUInt64LE(24));
const index = buf.subarray(indexOff, indexOff + indexLen);

const entries = {};
let p = 0;
for (let i = 0; i < count; i++) {
  const nameLen = index.readUInt16LE(p);
  const e = {
    codec: index[p + 2],
    off: Number(index.readBigUInt64LE(p + 8)),
    comp: Number(index.readBigUInt64LE(p + 16)),
    raw: Number(index.readBigUInt64LE(p + 24)),
    crc: index.readUInt32LE(p + 32),
  };
  p += 36;
  const name = index.subarray(p, p + nameLen).toString('utf8');
  p += nameLen;
  entries[name] = e;
}
// --------------------------------------------------------------------------

console.log(`${file}: ${(fileSize / 1048576).toFixed(2)} MB, ${count} entries, ` +
            `index ${indexLen} B at ${indexOff}`);

async function decode(entry) {
  const slice = buf.subarray(entry.off, entry.off + entry.comp);
  const src = new ReadableStream({
    start(c) { c.enqueue(new Uint8Array(slice)); c.close(); },
  });
  const out = new Response(new XzReadableStream(src));
  return Buffer.from(await out.arrayBuffer());
}

(async () => {
  const names = Object.keys(entries).slice(0, sample || count);
  let raw = 0, comp = 0, bad = 0;
  for (const name of names) {
    const e = entries[name];
    const t = Date.now();
    const dec = await decode(e);
    const ms = Date.now() - t;
    const problems = [];
    if (dec.length !== e.raw) problems.push(`size ${dec.length} != ${e.raw}`);
    if ((zlib.crc32 ? zlib.crc32(dec) : crc32(dec)) !== e.crc) problems.push('crc mismatch');
    if (dec.toString('latin1', 0, 8) !== 'UnityFS\0') problems.push('not a UnityFS bundle');
    raw += dec.length; comp += e.comp;
    if (problems.length) bad++;
    console.log(`  ${name.slice(0, 56).padEnd(58)} ${(e.comp / 1048576).toFixed(2)} -> ` +
                `${(dec.length / 1048576).toFixed(2)} MB  ${ms} ms  ` +
                (problems.length ? '<-- ' + problems.join('; ') : 'ok'));
  }
  console.log(`\nchecked ${names.length}: ${(comp / 1048576).toFixed(2)} MB -> ` +
              `${(raw / 1048576).toFixed(2)} MB, ${bad} problem(s)`);
  process.exit(bad ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(1); });

function crc32(b) {
  let c, crc = 0xFFFFFFFF;
  for (let i = 0; i < b.length; i++) {
    c = (crc ^ b[i]) & 0xFF;
    for (let k = 0; k < 8; k++) c = c & 1 ? (c >>> 1) ^ 0xEDB88320 : c >>> 1;
    crc = (crc >>> 8) ^ c;
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}
