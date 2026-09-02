# API Reference

All endpoints return JSON. Errors follow a standard envelope.

---

## Error handling

All error responses follow this schema:

```json
{
  "error": {
    "code": "error_code",
    "message": "Human-readable error message"
  }
}
```

Common HTTP status codes:
- `200 OK` — Success
- `202 Accepted` — Request accepted; work is in progress (async operations)
- `400 Bad Request` — Malformed request or invalid input
- `401 Unauthorized` — Missing or invalid bearer token
- `402 Payment Required` — User's monthly spend exceeded `MAX_MONTHLY_COST_USD`
- `404 Not Found` — Resource not found; default response for unmapped routes
- `409 Conflict` — Precondition failed (e.g., no OpenRouter key set)
- `413 Payload Too Large` — Document exceeds size limits
- `429 Too Many Requests` — Concurrency limit exceeded (too many in-flight reviews)
- `500 Internal Server Error` — Server error; check logs
- `503 Service Unavailable` — A required capability is down (e.g., database offline)

---

## Authentication

All endpoints except `POST /api/auth/login`, `GET /api/status`, `GET /` (landing page), and `GET /manifest.xml` require a valid bearer token.

**Header:**
```
Authorization: Bearer <token>
```

If the token is missing or invalid, the endpoint returns `401 Unauthorized`.

---

## Public endpoints (no auth required)

### `GET /`

Landing page with capability states and manifest download link.

**Response:**
```json
{
  "app": "Legal Helper",
  "version": "1.0.0",
  "capabilities": {
    "database": {
      "enabled": true,
      "status": "healthy"
    },
    "bucket": {
      "enabled": true,
      "status": "healthy"
    },
    "openrouter_zdr_list": {
      "enabled": true,
      "status": "healthy",
      "cached_at": "2026-09-02T12:34:56Z"
    }
  },
  "manifest_url": "/manifest.xml"
}
```

### `GET /healthz`

Health check endpoint for load balancers and monitoring.

**Response (200):**
```json
{
  "status": "ok"
}
```

**Response (503 if any critical capability is down):**
```json
{
  "error": {
    "code": "capability_unhealthy",
    "message": "Database capability is unhealthy"
  }
}
```

### `GET /api/status`

Detailed status endpoint (includes capability states but no secrets).

**Response:**
```json
{
  "app": "Legal Helper",
  "timestamp": "2026-09-02T12:34:56Z",
  "uptime_seconds": 3600,
  "capabilities": {
    "database": {"enabled": true, "status": "healthy"},
    "bucket": {"enabled": true, "status": "healthy"},
    "openrouter_zdr_list": {"enabled": true, "status": "healthy", "cached_at": "2026-09-02T12:34:56Z"}
  }
}
```

### `GET /manifest.xml`

Dynamic Word add-in manifest. The host is filled in from the request origin.

**Response:** XML manifest (content-type: `application/xml`)

**Example (production):**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<OfficeApp xmlns="http://schemas.microsoft.com/office/appforoffice/1.1">
  <Id>7b3f9a42-1c6e-4d2a-9f51-0a1b2c3d4e5f</Id>
  <Version>1.0.0.0</Version>
  <ProviderName>Legal Helper</ProviderName>
  <DefaultLocale>en-US</DefaultLocale>
  <DisplayName DefaultValue="Legal Helper"/>
  <Description DefaultValue="Review documents against a legal playbook."/>
  <Hosts>
    <Host Name="Document"/>
  </Hosts>
  <DefaultSettings>
    <SourceLocation DefaultValue="https://legal-helper-prod.railway.app/addin/taskpane.html"/>
  </DefaultSettings>
  <Permissions>ReadWriteDocument</Permissions>
</OfficeApp>
```

### `POST /api/auth/login`

Authenticate a user and receive a bearer token.

**Request:**
```json
{
  "username": "alice.tan",
  "password": "MyPassword123"
}
```

**Response (200):**
```json
{
  "token": "sk_legalhelper_...",
  "user": {
    "id": "user-uuid",
    "username": "alice.tan",
    "display_name": "Alice Tan",
    "openrouter_key_last4": "abcd"
  }
}
```

**Response (401 — wrong password or unknown user):**
```json
{
  "error": {
    "code": "auth_failed",
    "message": "Invalid credentials"
  }
}
```

**Note:** Passwords are checked in constant time to prevent timing attacks. Unknown users and wrong passwords produce identical error messages.

---

## Authenticated endpoints

### `GET /api/auth/logout`

Log out the current user (invalidate the session token).

**Request:**
```
Authorization: Bearer <token>
```

**Response (200):**
```json
{
  "message": "Logged out successfully"
}
```

---

### `GET /api/me`

Get the current user's profile.

**Request:**
```
Authorization: Bearer <token>
```

**Response (200):**
```json
{
  "id": "user-uuid",
  "username": "alice.tan",
  "display_name": "Alice Tan",
  "role": "user",
  "openrouter_key_last4": "wxyz",
  "openrouter_key_label": "Production API Key",
  "preferred_model_quick": "anthropic/claude-sonnet-4-6",
  "preferred_model_deep": "anthropic/claude-opus-4-8",
  "created_at": "2026-01-01T00:00:00Z",
  "last_login_at": "2026-09-02T12:34:56Z"
}
```

**Response (401):**
```json
{
  "error": {
    "code": "unauthorized",
    "message": "Invalid or expired token"
  }
}
```

---

### `POST /api/me/key`

Save or update the user's encrypted OpenRouter API key.

**Request:**
```json
{
  "openrouter_key": "sk-or-v1-...",
  "label": "My API Key"
}
```

**Response (200):**
```json
{
  "openrouter_key_last4": "abcd",
  "openrouter_key_label": "My API Key"
}
```

**Response (400 — invalid key):**
```json
{
  "error": {
    "code": "invalid_key",
    "message": "OpenRouter API key is invalid or not found"
  }
}
```

**Note:** The plaintext key is never returned. Only the last 4 characters are shown.

---

### `DELETE /api/me/key`

Delete the user's OpenRouter API key.

**Request:**
```
Authorization: Bearer <token>
```

**Response (200):**
```json
{
  "message": "API key deleted"
}
```

---

### `POST /api/me/model-preference`

Save the user's preferred model for quick or deep reviews.

**Request:**
```json
{
  "mode": "quick",
  "model": "anthropic/claude-sonnet-4-6"
}
```

**Response (200):**
```json
{
  "mode": "quick",
  "model": "anthropic/claude-sonnet-4-6"
}
```

**Response (400 — model not in ZDR list):**
```json
{
  "error": {
    "code": "model_not_available",
    "message": "Model is not available via ZDR routes"
  }
}
```

---

### `GET /api/me/usage`

Get the current user's usage statistics (token counts, costs, review history).

**Query parameters (optional):**
- `start_date` — ISO 8601 date (e.g., `2026-09-01`)
- `end_date` — ISO 8601 date (e.g., `2026-09-30`)

**Request:**
```
Authorization: Bearer <token>
GET /api/me/usage
```

**Response (200):**
```json
{
  "total_reviews": 42,
  "total_cost_usd": 12.34,
  "total_input_tokens": 125000,
  "total_output_tokens": 25000,
  "monthly_cost_usd": 5.67,
  "monthly_limit_usd": 5.00,
  "remaining_budget_usd": -0.67,
  "reviews_by_mode": {
    "quick": 35,
    "deep": 7
  },
  "cost_by_agent": {
    "classifier": 0.50,
    "reviewer": 8.00,
    "coverage": 3.84
  }
}
```

---

### `POST /api/reviews`

Upload a document and start a review (synchronous quick, or asynchronous deep).

**Request:**
```
Content-Type: multipart/form-data
Authorization: Bearer <token>

- document: (.docx file)
- mode: "quick" or "deep"
```

**Response (200 — synchronous completion for quick mode):**
```json
{
  "id": "review-uuid",
  "status": "done",
  "created_at": "2026-09-02T12:34:56Z",
  "finished_at": "2026-09-02T12:35:00Z",
  "filename": "contract.docx",
  "mode": "quick",
  "doc_type": "Mutual NDA",
  "our_side": "unknown",
  "risk_tier": "medium",
  "adherence_score": 0.82,
  "findings": [
    {
      "id": "finding-uuid",
      "agent": "reviewer",
      "clause": "Confidentiality",
      "risk": "high",
      "finding": "Confidentiality period is indefinite, which is unusual.",
      "span": "confidential information shall remain confidential indefinitely",
      "suggested": "confidential information shall remain confidential for a period of 3 years after disclosure"
    }
  ],
  "coverage": [
    {
      "position": "Indemnification",
      "required": true,
      "found": true
    }
  ],
  "tokens": {
    "input": 2500,
    "output": 800
  },
  "cost_usd": 0.18,
  "duration_ms": 34000
}
```

**Response (202 — async accepted for deep mode):**
```json
{
  "id": "review-uuid",
  "status": "queued",
  "created_at": "2026-09-02T12:34:56Z"
}
```

Poll `/api/reviews/{id}` to check status.

**Response (400 — invalid request):**
```json
{
  "error": {
    "code": "invalid_document",
    "message": "Document must be a .docx file"
  }
}
```

**Response (402 — budget exceeded):**
```json
{
  "error": {
    "code": "budget_exceeded",
    "message": "Monthly spend limit exceeded. Current month: $6.50 / $5.00 limit"
  }
}
```

**Response (409 — OpenRouter key not set):**
```json
{
  "error": {
    "code": "missing_key",
    "message": "OpenRouter API key not configured. Set it in settings."
  }
}
```

**Response (413 — document too large):**
```json
{
  "error": {
    "code": "payload_too_large",
    "message": "Document exceeds size limit (max 10 MB)"
  }
}
```

**Response (429 — too many concurrent reviews):**
```json
{
  "error": {
    "code": "too_many_requests",
    "message": "Too many reviews in progress. Try again in a moment."
  }
}
```

---

### `GET /api/reviews/{id}`

Get the status and result of a review.

**Request:**
```
Authorization: Bearer <token>
GET /api/reviews/review-uuid
```

**Response (200 — still queued):**
```json
{
  "id": "review-uuid",
  "status": "queued",
  "created_at": "2026-09-02T12:34:56Z"
}
```

**Response (200 — running):**
```json
{
  "id": "review-uuid",
  "status": "running",
  "created_at": "2026-09-02T12:34:56Z",
  "progress_message": "Running coverage check..."
}
```

**Response (200 — done):**
```json
{
  "id": "review-uuid",
  "status": "done",
  "created_at": "2026-09-02T12:34:56Z",
  "finished_at": "2026-09-02T12:37:00Z",
  "filename": "contract.docx",
  "mode": "deep",
  "doc_type": "Service Agreement",
  "our_side": "Vendor",
  "risk_tier": "high",
  "adherence_score": 0.65,
  "findings": [...],
  "coverage": [...],
  "tokens": {...},
  "cost_usd": 0.45,
  "duration_ms": 165000,
  "doc_stored": true
}
```

**Response (200 — failed):**
```json
{
  "id": "review-uuid",
  "status": "failed",
  "created_at": "2026-09-02T12:34:56Z",
  "finished_at": "2026-09-02T12:37:00Z",
  "error": "no_zdr_route",
  "error_message": "No ZDR-capable endpoint available for the selected model"
}
```

**Response (404 — review not found or doesn't belong to the user):**
```json
{
  "error": {
    "code": "not_found",
    "message": "Review not found"
  }
}
```

---

### `GET /api/reviews`

List reviews for the current user (paginated).

**Query parameters (optional):**
- `limit` — max results per page (default 20, max 100)
- `offset` — results to skip (default 0)
- `status` — filter by status ("queued", "running", "done", "failed")
- `mode` — filter by mode ("quick", "deep")

**Request:**
```
Authorization: Bearer <token>
GET /api/reviews?limit=10&offset=0
```

**Response (200):**
```json
{
  "reviews": [
    {
      "id": "review-uuid-1",
      "created_at": "2026-09-02T12:34:56Z",
      "finished_at": "2026-09-02T12:35:00Z",
      "filename": "contract.docx",
      "mode": "quick",
      "status": "done",
      "risk_tier": "medium",
      "adherence_score": 0.82,
      "findings_count": 3,
      "cost_usd": 0.18
    }
  ],
  "total": 42,
  "limit": 10,
  "offset": 0
}
```

---

### `GET /api/reviews/{id}/document`

Download the original .docx from a review (presigned redirect).

**Request:**
```
Authorization: Bearer <token>
GET /api/reviews/review-uuid/document
```

**Response (302 — found, redirect to presigned URL):**
```
Location: https://documents.s3.region.amazonaws.com/users/user-uuid/reviews/review-uuid.docx?X-Amz-Algorithm=...
```

The presigned URL is valid for 15 minutes and is accessible only to the owner of the review.

**Response (404 — document not stored):**
```json
{
  "error": {
    "code": "not_found",
    "message": "Document not stored or retention expired"
  }
}
```

**Response (404 — review not found or doesn't belong to the user):**
```json
{
  "error": {
    "code": "not_found",
    "message": "Review not found"
  }
}
```

---

### `DELETE /api/reviews/{id}/document`

Delete the stored document for a review.

**Request:**
```
Authorization: Bearer <token>
DELETE /api/reviews/review-uuid/document
```

**Response (200):**
```json
{
  "message": "Document deleted"
}
```

**Response (404):**
```json
{
  "error": {
    "code": "not_found",
    "message": "Review or document not found"
  }
}
```

---

## Admin endpoints (role=admin only)

### `GET /api/admin/usage`

Get aggregate usage statistics across all users (admin only).

**Query parameters (optional):**
- `start_date` — ISO 8601 date
- `end_date` — ISO 8601 date

**Request:**
```
Authorization: Bearer <admin-token>
GET /api/admin/usage
```

**Response (200):**
```json
{
  "total_reviews": 1234,
  "total_cost_usd": 567.89,
  "total_input_tokens": 12500000,
  "total_output_tokens": 2500000,
  "users_count": 42,
  "reviews_by_mode": {
    "quick": 1100,
    "deep": 134
  },
  "cost_by_model": {
    "anthropic/claude-haiku-4-5": 10.00,
    "anthropic/claude-sonnet-4-6": 200.00,
    "anthropic/claude-opus-4-8": 357.89
  }
}
```

**Response (403 — not admin):**
```json
{
  "error": {
    "code": "forbidden",
    "message": "This endpoint requires admin role"
  }
}
```

---

## Static files

### `GET /addin/*`

Serve the add-in static files (HTML, CSS, JS).

- `/addin/taskpane.html` — main task pane UI
- `/addin/taskpane.js` — task pane logic
- `/addin/taskpane.css` — styles

---

## Rate limiting

There is no global rate limit per IP. However:

- **Login throttle:** 20 failed login attempts per IP per 5 minutes → 429 Too Many Requests
- **Concurrency:** at most `REVIEW_CONCURRENCY` (default 2) concurrent reviews per user → 429 Too Many Requests

---

## Versioning

The API version is currently `1.0.0` and is reflected in the app info (see `GET /`). Breaking changes will use a new major version. There are no per-endpoint version numbers.

---

## CORS

The app does **not** set `Access-Control-Allow-Origin` headers. CORS is not needed because the add-in and API are same-origin (both served from the app's domain).

If you need to call the API from a different domain (e.g., a separate admin dashboard), add CORS configuration to `main.py`.

---

## Webhooks

There are no webhooks or server-sent events. The client polls for review status.

