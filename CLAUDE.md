# ER:ON Project Guidelines

## Project Overview

ER:ON is an AI-assisted emergency department support system. The project aims to:

- Detect patient deterioration risk
- Monitor emergency-room patient status
- Reduce gaps in emergency medical records
- Support AI-generated predictions and record drafts

AI predictions are decision-support information for medical professionals. They must not be presented as confirmed diagnoses or autonomous medical decisions.

## Repository Structure

- `backend/`: FastAPI backend, SQLAlchemy models, API routers, and schemas
- `frontend/`: TanStack Start / React frontend created and maintained with Lovable
- `docker-compose.yml`: Local PostgreSQL and backend infrastructure
- `.env.example`: Environment-variable template

The main backend domain relationships are:

- A `Patient` can have multiple `Visit` records.
- A `Visit` can have multiple `Vital`, `Prediction`, and `Record` records.
- Backend request and response contracts are defined by the Pydantic schemas in `backend/app/schemas/`.

## Development Rules

- Make the smallest coherent change that satisfies the request.
- Work within the requested feature scope. Do not change unrelated functionality, UI, or behavior.
- If a change to a shared component, API schema, data model, or other cross-feature area is unavoidable, explain the reason and affected scope before making the change.
- Do not include unrelated improvements in the same feature change; propose them as separate work.
- Inspect the existing implementation and follow established patterns before introducing new abstractions.
- Keep frontend changes small and preserve compatibility with the Lovable workflow. Follow the more specific rules in `frontend/AGENTS.md` when working in the frontend.
- When a change affects a route, API contract, environment variable, or domain behavior, update the relevant documentation.
- Use `npm` as the standard frontend package manager.

## Backend Rules

- Keep API routers under `backend/app/api/`.
- Keep SQLAlchemy models under `backend/app/models/`.
- Keep Pydantic request and response schemas under `backend/app/schemas/`.
- Use dependency-injected database sessions for API handlers.
- Validate referenced parent resources before creating child resources.
- Treat backend schemas as the source of truth for API response structures and shared types.
- Preserve existing route prefixes and response shapes unless an API change is explicitly requested.
- Keep AI service integrations behind environment-configured URLs.

The database schema is still being designed. Changes to models, schemas, or database relationships must be coordinated with the team and reflected in the relevant documentation.

## Frontend and API Integration

- The frontend currently contains mock data. Do not assume that mock data is connected to the backend API.
- Frontend bed status, alerts, summaries, and similar dashboard values may currently be demo data rather than backend data.
- When API integration is added, migrate flows deliberately and do not silently mix mock and live data in the same user flow.
- Use backend Pydantic schemas and actual API responses as the basis for frontend types.
- Keep patient, visit, vital, prediction, and record identifiers consistent across frontend and backend.
- Handle loading, empty, error, and stale-data states when replacing mock data with API data.
- Do not edit `frontend/src/routeTree.gen.ts` manually.
- Reuse existing components and routing conventions where possible.

## Security and Data Handling

- Never commit `.env` files, secrets, credentials, or service tokens.
- Never commit real personally identifiable information or medical data.
- Use fictional or clearly marked sample data for local development.
- Do not expose environment-variable values in source code, logs, screenshots, or documentation.
- Treat AI-generated predictions and records as reviewable drafts or decision support until confirmed by an authorized medical professional.

## Local Development

Copy `.env.example` to a local environment file and provide the required values before starting the backend.

Start the database and backend containers:

```sh
docker compose up --build
```

Run the backend locally:

```sh
cd backend
uvicorn app.main:app --reload --port 8000
```

Run the frontend locally:

```sh
cd frontend
npm install
npm run dev
```

Use `npm` as the standard frontend package manager. Do not switch package managers or update lockfiles incidentally.

## Verification

Run the checks relevant to the changed area:

- Frontend changes: `npm run lint` and `npm run build` from `frontend/`
- Backend changes: verify `http://localhost:8100/health` and `http://localhost:8100/health/db` when using Docker Compose
- API integration changes: verify the affected API responses and the corresponding frontend flow
- Route changes: confirm existing routes still load

Do not claim a check passed unless it was actually run.

## Collaboration and Change Communication

- Before making a large structural change or an API contract change, agree on the change with the team.
- If the requested feature cannot be implemented without affecting another feature, explain the dependency, expected impact, and proposed approach before proceeding.
- Keep temporary mock behavior, placeholders, and known limitations explicit in the relevant code or documentation.
- Keep commits focused on the requested feature.
