#!/usr/bin/env node
/**
 * Streaming static file server for testing the Silksong WebGL port locally or
 * via remote preview. Supports GET, HEAD, HTTP Range requests, Content-Length,
 * and standard WebGL/WebM MIME types without buffering large files in memory.
 */

import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PORT = Number(process.env.PORT || 8080);
const HOST = process.env.HOST || '0.0.0.0';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.mjs': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.xml': 'application/xml; charset=utf-8',
  '.wasm': 'application/wasm',
  '.webm': 'video/webm',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.bundle': 'application/octet-stream',
  '.unityweb': 'application/octet-stream',
  '.bin': 'application/octet-stream',
  '.hash': 'text/plain; charset=utf-8',
};

function mimeFor(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  return MIME[ext] || 'application/octet-stream';
}

const server = http.createServer((req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, HEAD, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Range, Content-Type, If-None-Match, If-Modified-Since');
  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  let urlPath = '/';
  try {
    urlPath = decodeURIComponent(new URL(req.url || '/', 'http://localhost').pathname);
  } catch {
    res.writeHead(400);
    res.end('Bad Request');
    return;
  }
  if (urlPath === '/') urlPath = '/index.html';

  const resolved = path.resolve(ROOT, '.' + urlPath);
  if (resolved !== ROOT && !resolved.startsWith(ROOT + path.sep)) {
    res.writeHead(403);
    res.end('Forbidden');
    return;
  }

  fs.stat(resolved, (err, stat) => {
    if (err || !stat.isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('Not Found');
      return;
    }

    const total = stat.size;
    const commonHeaders = {
      'Content-Type': mimeFor(resolved),
      'Accept-Ranges': 'bytes',
      'Cache-Control': urlPath === '/index.html' || urlPath.endsWith('.js')
        ? 'no-cache'
        : 'public, max-age=3600',
    };

    const range = req.headers.range;
    if (range && range.startsWith('bytes=')) {
      const [startStr, endStr] = range.slice(6).split('-');
      const start = startStr ? parseInt(startStr, 10) : 0;
      const end = endStr ? Math.min(total - 1, parseInt(endStr, 10)) : total - 1;
      if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || start > end || start >= total) {
        res.writeHead(416, { 'Content-Range': `bytes */${total}` });
        res.end();
        return;
      }
      res.writeHead(206, {
        ...commonHeaders,
        'Content-Range': `bytes ${start}-${end}/${total}`,
        'Content-Length': String(end - start + 1),
      });
      if (req.method === 'HEAD') {
        res.end();
        return;
      }
      fs.createReadStream(resolved, { start, end }).pipe(res);
      return;
    }

    res.writeHead(200, {
      ...commonHeaders,
      'Content-Length': String(total),
    });
    if (req.method === 'HEAD') {
      res.end();
      return;
    }
    fs.createReadStream(resolved).pipe(res);
  });
});

server.listen(PORT, HOST, () => {
  console.log(`Silksong web port server listening on http://${HOST}:${PORT}`);
});
