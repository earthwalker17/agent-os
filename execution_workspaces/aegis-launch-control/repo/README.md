# Aegis Launch Control

A cinematic mission-control dashboard for managing complex product launches.

## Features

- **Mission Overview**: Key metrics and status indicators
- **Workstream Board**: Task tracking and progress monitoring
- **Dependency Map**: Critical path and dependencies visualization
- **Risk Radar**: Risk assessment and monitoring
- **Launch Timeline**: Countdown and milestone tracking
- **Scenario Simulator**: What-if analysis and planning
- **Optional Backend API**: Lightweight Express.js server with file-based data storage

## Tech Stack

- **Frontend**: React 18 + TypeScript
- **Build Tool**: Vite
- **Styling**: Tailwind CSS
- **Backend** (optional): Express.js + Node.js
- **Theme**: Dark mission-control aesthetic

## Getting Started

### Install Dependencies

```bash
npm install
```

### Development Options

#### Option 1: Frontend Only (Static Data)

Run just the frontend with bundled static data:

```bash
npm run dev
```

Open [http://127.0.0.1:5174](http://127.0.0.1:5174) in your browser.

#### Option 2: Full Stack (Frontend + Backend API)

Run both the frontend and backend API concurrently:

```bash
npm run dev:all
```

This starts:
- **Frontend**: [http://127.0.0.1:5174](http://127.0.0.1:5174)
- **Backend API**: [http://localhost:3001](http://localhost:3001)

The frontend automatically detects the API and uses it when available, falling back to static data if the API is unreachable.

### Backend API Only

To run just the backend API server:

```bash
npm run dev:server
```

### Build for Production

```bash
npm run build
```

### Preview Production Build

```bash
npm run preview
```

## Project Structure

```
src/
  ├── components/     # React components
  ├── data/          # Static seed data (fallback)
  ├── services/      # API service with automatic fallback
  ├── types/         # TypeScript type definitions
  ├── App.tsx        # Main application component
  ├── main.tsx       # Application entry point
  └── index.css      # Global styles and Tailwind imports

server/
  ├── data/          # JSON data files for backend
  │   ├── missions.json
  │   ├── workstreams.json
  │   ├── tasks.json
  │   ├── risks.json
  │   └── scenarios.json
  └── index.js       # Express server
```

## API Endpoints

When running the backend server (`npm run dev:all` or `npm run dev:server`), the following endpoints are available:

- `GET /api/health` - API health check
- `GET /api/missions` - Get all missions
- `GET /api/workstreams` - Get all workstreams
- `GET /api/tasks` - Get all tasks
- `GET /api/risks` - Get all risks
- `GET /api/scenarios` - Get all scenarios
- `POST /api/scenarios/:id/apply` - Apply a scenario simulation
- `PATCH /api/tasks/:id` - Update a task

## Data Management

The application uses a **graceful fallback** approach:

1. **API Available**: Fetches data from the backend API (`http://localhost:3001/api`)
2. **API Unavailable**: Automatically falls back to bundled static data from `src/data/seedData.ts`

You can modify data in two ways:

- **Frontend static data**: Edit `src/data/seedData.ts`
- **Backend data files**: Edit JSON files in `server/data/`

The API status indicator in the top-right shows whether the backend is connected:
- 🟢 **Green**: API Online
- 🟡 **Yellow**: Static Mode (fallback data)

## Development Notes

- The frontend runs on port `5174` to avoid conflicts with the Agent OS (which uses `5173`)
- The backend API runs on port `3001`
- CORS is enabled on the backend for local development
- All backend data is stored in JSON files for simplicity (no database required)

## License

MIT
