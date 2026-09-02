// Railway Infrastructure as Code (IaC) for Legal Helper
// Deployed to Railway.app as a single project with three services: app, Postgres, bucket.
// This file mirrors the dashboard canvas; students may see it as a reference for "the same graph as code".
// See the plan's §7 for the manual click-path; this replaces the deprecated railway.json.

import { Project, Service, Database, Resource } from "@railway/sdk";

const project = new Project({
  name: "legal-helper",
});

// 1. PostgreSQL database (private network)
const postgres = new Database({
  name: "Postgres",
  projectId: project.id,
  databaseType: "postgres",
});

// 2. Document bucket (S3-compatible object store)
const bucket = new Resource({
  name: "documents",
  projectId: project.id,
  type: "bucket",
  // Bucket region is immutable after creation; select one near your classroom.
  // On the Railway dashboard, choose the region when creating the bucket.
  // Credentials (ENDPOINT, BUCKET, ACCESS_KEY_ID, SECRET_ACCESS_KEY, REGION)
  // are injected as environment variables to the app service.
});

// 3. FastAPI web service (app service)
const app = new Service({
  name: "legal-helper",
  projectId: project.id,
  // GitHub repo connected via Railway dashboard: SMKxx1/legal-helper, branch main
  // Dockerfile at repo root; Railway auto-detects and builds.
  repoId: process.env.RAILWAY_REPO_ID, // set via dashboard
  branch: "main",
});

// Environment variables for the app service (reference the Postgres and bucket)
// On the dashboard, set these under the app service's Variables section.
// The values starting with ${{ }} are Railway reference variables (interpolated at deploy time).
app.environmentVariables = {
  // Deployment
  APP_ENV: "prod",
  PORT: process.env.PORT || "8000",

  // Database (private network reference variable from Postgres)
  DATABASE_URL: `${{${postgres.id}.DATABASE_URL}}`,
  APP_SECRET_KEY: process.env.APP_SECRET_KEY || "", // must be set; see plan for generation

  // Bucket (S3 reference variables)
  S3_ENDPOINT: `${{${bucket.id}.ENDPOINT}}`,
  S3_BUCKET: `${{${bucket.id}.BUCKET}}`,
  S3_ACCESS_KEY_ID: `${{${bucket.id}.ACCESS_KEY_ID}}`,
  S3_SECRET_ACCESS_KEY: `${{${bucket.id}.SECRET_ACCESS_KEY}}`,
  S3_REGION: `${{${bucket.id}.REGION}}`,

  // Demo seed data (set to "true" for demo deployment to seed synthetic users)

  // Add-in manifest GUID (generate a new one via Python or uuidgen)
  ADDIN_ID: process.env.ADDIN_ID || "7b3f9a42-1c6e-4d2a-9f51-0a1b2c3d4e5f",

  // Model choices (via OpenRouter)
  MODEL_CLASSIFIER: "anthropic/claude-haiku-4-5",
  MODEL_QUICK: "anthropic/claude-sonnet-4-6",
  MODEL_DEEP: "anthropic/claude-opus-4-8",
  OPENROUTER_BASE_URL: "https://openrouter.ai/api/v1",

  // Deployment limits and timeouts
  PROVIDER_TIMEOUT_S: "150",
  REVIEW_CONCURRENCY: "2",
  MAX_UPLOAD_MB: "10",
  MAX_DOC_CHARS: "120000",
  MAX_MONTHLY_COST_USD: "5",
  MAX_DOCS_PER_USER: "20",
};

// Health check configuration (Railway dashboard Settings → Healthcheck path)
// Endpoint: /healthz  Method: GET
app.healthCheckPath = "/healthz";
app.healthCheckInterval = 10;

// Networking: public domain + HTTPS (generate via dashboard)
// Railway auto-provisions a public domain and TLS certificate.

export { project, postgres, bucket, app };
