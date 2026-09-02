// Dev HTTPS server for the Word add-in (LOCAL testing).
//
// Serves THIS folder's add-in static files over HTTPS on :3000 using the Office dev cert (so Word
// trusts it) AND reverse-proxies /api, /healthz, /manifest.xml to the backend on :8000 — so the
// add-in (https://localhost:3000) reaches the API SAME-ORIGIN (no mixed-content, no CORS). The
// add-in's apiBase resolves to its own origin, so /api calls land here and get proxied to the backend.
//
// Run the backend first (`make run`, or uvicorn on :8000), then: node word-addin/dev-server.mjs
import https from 'node:https';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// ROOT is this script's own directory — the nda-review-cloud word-addin folder. Portable: no
// absolute path baked in, so it serves whichever checkout it lives in.
const ROOT = path.dirname(fileURLToPath(import.meta.url));
const HOME = process.env.HOME;
const CERT = process.env.ADDIN_CERT || `${HOME}/.office-addin-dev-certs/localhost.crt`;
const KEY = process.env.ADDIN_KEY || `${HOME}/.office-addin-dev-certs/localhost.key`;
const BACKEND_HOST = process.env.BACKEND_HOST || '127.0.0.1';
const BACKEND_PORT = Number(process.env.BACKEND_PORT || 8000);
const PORT = Number(process.env.ADDIN_PORT || 3000);
const PROXY = ['/api', '/healthz', '/manifest.xml'];
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml', '.json': 'application/json',
  '.ico': 'image/x-icon', '.map': 'application/json' };

for (const f of [CERT, KEY]) {
  if (!fs.existsSync(f)) {
    console.error(`Missing dev cert: ${f}\nInstall once with:  npx office-addin-dev-certs install`);
    process.exit(1);
  }
}

const server = https.createServer(
  { cert: fs.readFileSync(CERT), key: fs.readFileSync(KEY) },
  (req, res) => {
    const u = new URL(req.url, `https://localhost:${PORT}`);
    if (PROXY.some((p) => u.pathname === p || u.pathname.startsWith(p + '/'))) {
      const preq = http.request(
        { host: BACKEND_HOST, port: BACKEND_PORT, path: req.url, method: req.method,
          headers: { ...req.headers, host: `${BACKEND_HOST}:${BACKEND_PORT}` } },
        (pres) => { res.writeHead(pres.statusCode || 502, pres.headers); pres.pipe(res); });
      preq.on('error', (e) => { res.writeHead(502); res.end('proxy error: ' + e.message); });
      req.pipe(preq);
      return;
    }
    let p = decodeURIComponent(u.pathname);
    if (p === '/' || p === '') p = '/taskpane.html';
    const file = path.normalize(path.join(ROOT, p));
    if (!file.startsWith(ROOT)) { res.writeHead(403); res.end('forbidden'); return; }
    fs.readFile(file, (err, data) => {
      if (err) { res.writeHead(404); res.end('not found: ' + p); return; }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
      res.end(data);
    });
  });
// Fail cleanly (a clear message + non-zero exit) instead of crashing with an unhandled 'error'
// event if the port is already taken by another process we don't own.
server.on('error', (err) => {
  if (err.code === 'EADDRINUSE') {
    console.error(`Port ${PORT} is already in use — stop whatever is bound to it and retry ` +
      `(e.g.  lsof -ti tcp:${PORT} | xargs kill).`);
  } else {
    console.error('Add-in dev server error:', err.message);
  }
  process.exit(1);
});

server.listen(PORT, () => console.log(
  `Word add-in: https://localhost:${PORT}  (serving ${ROOT}  +  proxy /v1,/api,/healthz -> ${BACKEND_HOST}:${BACKEND_PORT})`));
