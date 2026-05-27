# Backend

TypeScript + Express + Mongoose API for the `Research_Project` MongoDB database
(the same database visible in MongoDB Compass at `localhost:27017`).

The frontend is Next.js + TypeScript, so this service uses TypeScript as well
to keep one language across the stack.

## Stack

- **Runtime:** Node.js (>= 18)
- **Language:** TypeScript
- **Framework:** Express 4
- **Database:** MongoDB 8.x via Mongoose 8
- **Validation:** Zod
- **Security:** Helmet + CORS

## Getting started

```bash
cd backend
copy .env.example .env   # (PowerShell) — or `cp .env.example .env` on bash
npm install
npm run dev              # starts the API on http://localhost:4000
```

Useful scripts:

```bash
npm run dev        # tsx watch -> live reload
npm run build      # compile TypeScript to dist/
npm run start      # run compiled output (after build)
npm run typecheck  # tsc --noEmit
```

## Environment variables

Copy `.env.example` to `.env` and adjust if needed.

| Key             | Default                       | Description                              |
| --------------- | ----------------------------- | ---------------------------------------- |
| `PORT`          | `4000`                        | HTTP port                                |
| `NODE_ENV`      | `development`                 | `development` / `production` / `test`    |
| `CORS_ORIGIN`   | `http://localhost:3000`       | Allowed origin(s), comma-separated or `*` |
| `MONGO_URI`     | `mongodb://localhost:27017`   | Mongo connection string                  |
| `MONGO_DB_NAME` | `Research_Project`            | Mongo database name                      |

## Repository layout

The parent `script/` folder contains only:

```
script/
  frontend/     # Next.js UI
  backend/      # Express API + Python extraction/report tooling
```

Python scripts, report PDFs, extracted JSON, and related data directories live
in `backend/` (same folder as this API). Run them from `backend/`, for example:

```bash
cd backend
python Data_retrive.py --apikey sk-...
python Extract_newly_updated.py
```

## Project layout

```
backend/
  app/
    config/
      env.ts          # typed env loader (dotenv)
      db.ts           # mongoose connection helpers
    models/
      User.ts         # users collection schema
    services/
      userService.ts  # CRUD + business rules
    controllers/
      userController.ts
    routes/
      index.ts        # mounts /health, /users
      userRoutes.ts
    middleware/
      errorHandler.ts # 404 + central error handler
    utils/
      logger.ts
      asyncHandler.ts
      httpError.ts
    app.ts            # express app factory
    server.ts         # entry point
  package.json
  tsconfig.json
  .env.example
```

## Data model

The `users` collection mirrors the documents shown in MongoDB Compass:

```jsonc
{
  "_id":         "ObjectId(...)",
  "user_id":     "USR_001",
  "first_name":  "Admin",
  "last_name":   "User",
  "email":       "admin@example.com",
  "password":    "AdminPass123",   // never returned by the API
  "user_status": "active",          // active | inactive | suspended
  "created_at":  "2026-05-26T03:56:09.906Z",
  "updated_at":  "2026-05-26T03:56:09.906Z",
  "last_login":  null
}
```

`created_at` and `updated_at` are managed automatically by Mongoose timestamps.
The API never returns the `password` field.

## REST API

Base URL: `http://localhost:4000/api`

| Method | Path                    | Description                                |
| ------ | ----------------------- | ------------------------------------------ |
| GET    | `/health`               | Service + DB health                        |
| GET    | `/users`                | List users (`page`, `limit`, `status`, `search`) |
| POST   | `/users`                | Create a user                              |
| GET    | `/users/:id`            | Get by Mongo `_id` or by `user_id` (`USR_001`) |
| PATCH  | `/users/:id`            | Partial update                             |
| DELETE | `/users/:id`            | Remove a user                              |
| POST   | `/users/:id/login`      | Stamp `last_login = now`                   |

### Examples

Create a user:

```bash
curl -X POST http://localhost:4000/api/users \
  -H "Content-Type: application/json" \
  -d '{
    "first_name": "Jane",
    "last_name":  "Doe",
    "email":      "jane@example.com",
    "password":   "Secret123"
  }'
```

List active users:

```bash
curl "http://localhost:4000/api/users?status=active&page=1&limit=25"
```

Look up by business id:

```bash
curl http://localhost:4000/api/users/USR_001
```

## Notes

- `user_id` is auto-generated as `USR_001`, `USR_002`, ... if the client does
  not send one. Email and `user_id` are unique.
- All errors are returned as JSON with `error`, `message`, and (when relevant)
  `details` fields. Validation errors return HTTP 400 with the Zod error tree.
- Connect the Next.js frontend by setting its API base URL to
  `http://localhost:4000/api`.
