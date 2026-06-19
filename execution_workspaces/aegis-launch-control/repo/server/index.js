import express from 'express';
import cors from 'cors';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';
import { readFileSync, writeFileSync } from 'fs';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const app = express();
const PORT = 3001;

// Middleware
app.use(cors());
app.use(express.json());

// Helper to read JSON files
const readData = (filename) => {
  try {
    const filePath = join(__dirname, 'data', filename);
    const data = readFileSync(filePath, 'utf-8');
    return JSON.parse(data);
  } catch (error) {
    console.error(`Error reading ${filename}:`, error.message);
    return null;
  }
};

// Helper to write JSON files
const writeData = (filename, data) => {
  try {
    const filePath = join(__dirname, 'data', filename);
    writeFileSync(filePath, JSON.stringify(data, null, 2));
    return true;
  } catch (error) {
    console.error(`Error writing ${filename}:`, error.message);
    return false;
  }
};

// API Routes

// Get all missions
app.get('/api/missions', (req, res) => {
  const missions = readData('missions.json');
  if (missions) {
    res.json(missions);
  } else {
    res.status(500).json({ error: 'Failed to load missions' });
  }
});

// Get all workstreams
app.get('/api/workstreams', (req, res) => {
  const workstreams = readData('workstreams.json');
  if (workstreams) {
    res.json(workstreams);
  } else {
    res.status(500).json({ error: 'Failed to load workstreams' });
  }
});

// Get all tasks
app.get('/api/tasks', (req, res) => {
  const tasks = readData('tasks.json');
  if (tasks) {
    res.json(tasks);
  } else {
    res.status(500).json({ error: 'Failed to load tasks' });
  }
});

// Get all risks
app.get('/api/risks', (req, res) => {
  const risks = readData('risks.json');
  if (risks) {
    res.json(risks);
  } else {
    res.status(500).json({ error: 'Failed to load risks' });
  }
});

// Get all scenarios
app.get('/api/scenarios', (req, res) => {
  const scenarios = readData('scenarios.json');
  if (scenarios) {
    res.json(scenarios);
  } else {
    res.status(500).json({ error: 'Failed to load scenarios' });
  }
});

// Apply a scenario (simulates impact)
app.post('/api/scenarios/:id/apply', (req, res) => {
  const scenarios = readData('scenarios.json');
  const scenario = scenarios?.find(s => s.id === req.params.id);
  
  if (!scenario) {
    return res.status(404).json({ error: 'Scenario not found' });
  }

  // Simulate applying the scenario by returning the impacts
  res.json({
    success: true,
    scenario: scenario,
    message: `Scenario "${scenario.name}" applied successfully`,
    timestamp: new Date().toISOString()
  });
});

// Update task status
app.patch('/api/tasks/:id', (req, res) => {
  const tasks = readData('tasks.json');
  if (!tasks) {
    return res.status(500).json({ error: 'Failed to load tasks' });
  }

  const taskIndex = tasks.findIndex(t => t.id === req.params.id);
  if (taskIndex === -1) {
    return res.status(404).json({ error: 'Task not found' });
  }

  // Update task with provided fields
  tasks[taskIndex] = { ...tasks[taskIndex], ...req.body };
  
  if (writeData('tasks.json', tasks)) {
    res.json(tasks[taskIndex]);
  } else {
    res.status(500).json({ error: 'Failed to update task' });
  }
});

// Health check
app.get('/api/health', (req, res) => {
  res.json({ 
    status: 'ok', 
    timestamp: new Date().toISOString(),
    service: 'Aegis Launch Control API'
  });
});

// Start server
app.listen(PORT, () => {
  console.log(`🚀 Aegis Launch Control API running on http://localhost:${PORT}`);
  console.log(`📡 Endpoints available:`);
  console.log(`   - GET  /api/missions`);
  console.log(`   - GET  /api/workstreams`);
  console.log(`   - GET  /api/tasks`);
  console.log(`   - GET  /api/risks`);
  console.log(`   - GET  /api/scenarios`);
  console.log(`   - POST /api/scenarios/:id/apply`);
  console.log(`   - PATCH /api/tasks/:id`);
  console.log(`   - GET  /api/health`);
});
