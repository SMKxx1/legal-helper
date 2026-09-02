// Default no-op config. The on-prem installer OVERWRITES this file (served at /addin/config.js)
// with the deployment's API base + engine key so the add-in is zero-config for end users:
//   window.AMP_CONFIG = { apiBase: "https://<APP_HOSTNAME>", apiKey: "<ENGINE_API_KEY>" };
window.AMP_CONFIG = window.AMP_CONFIG || {};
