# Aegis Launch Control API

Lightweight Express.js backend for Aegis Launch Control.

## Overview

This is an optional backend API that provides RESTful endpoints for managing mission data. The frontend application works with or without this API — it automatically falls back to static data when the API is unavailable.

## Features

- File-based JSON data storage (no database required)
- RESTful API endpoints for missions, tasks, risks, workstreams, and scenarios
- CORS enabled for local development
- Scenario simulation endpoint
- Task update functionality

## Running the Server

From the project root:

```bash
# Run API server only
npm run dev:server

# Run both frontend and API
npm run dev:all
```

The server runs on `http://localhost:3001`

## API Endpoints

### Health Check
```
GET /api/health
```

### Missions
```
GET /api/missions
```

### Workstreams
```
GET /api/workstreams
```

### Tasks
```
GET /api/tasks
PATCH /api/tasks/:id
```

### Risks
```
GET /api/risks
```

### Scenarios
```
GET /api/scenarios
POST /api/scenarios/:id/apply
```

## Data Storage

All data is stored as JSON files in the `server/data/` directory:

- `missions.json` - Mission information
- `workstreams.json` - Workstream definitions
- `tasks.json` - Task list with dependencies
- `risks.json` - Risk register
- `scenarios.json` - Launch scenarios for simulation

Modify these files directly to update the data served by the API.

## Architecture

- **No database**: Uses simple JSON file storage
- **Stateless**: Each request reads from files
- **Development-focused**: Designed for local development and demos
- **Production-ready alternative**: For production use, replace file storage with a real database (PostgreSQL, MongoDB, etc.)

## Error Handling

The API returns appropriate HTTP status codes:
- `200` - Success
- `404` - Resource not found
- `500` - Server error

All errors include a JSON response with an `error` field describing the issue.
