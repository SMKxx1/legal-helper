// CLOUD config TEMPLATE for the Word add-in (served at /addin/config.js on the public Caddy
// domain). The add-in is served SAME-ORIGIN with the engine, so apiBase is "" (relative) and
// calls go to /v1 with the X-API-Key below — no extra CORS needed.
//
// DEPLOY: copy this to config.js and replace __ENGINE_API_KEY__ with the deployment's engine
// key (the value of ENGINE_API_KEY / a named ENGINE_SERVICE_KEYS secret set in Railway). Do NOT
// commit a real key — config.js with a real key is .gitignored.
window.AMP_CONFIG = {
  apiBase: "", // same-origin: the add-in is served by Caddy alongside /v1
  apiKey: "__ENGINE_API_KEY__", // X-API-Key for /v1; substitute at deploy time, never commit
};
