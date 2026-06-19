// API service for fetching data from backend with fallback to static data
import {
  LaunchMission,
  Task,
  Risk,
  Workstream,
  Scenario,
} from '../types';
import {
  missions as staticMissions,
  tasks as staticTasks,
  risks as staticRisks,
  workstreams as staticWorkstreams,
  scenarios as staticScenarios,
} from '../data/seedData';

const API_BASE_URL = 'http://localhost:3001/api';
const USE_API = true; // Set to false to always use static data

// Generic fetch wrapper with fallback
async function fetchWithFallback<T>(
  endpoint: string,
  fallbackData: T
): Promise<T> {
  if (!USE_API) {
    return fallbackData;
  }

  try {
    const response = await fetch(`${API_BASE_URL}${endpoint}`, {
      method: 'GET',
      headers: {
        'Content-Type': 'application/json',
      },
    });

    if (!response.ok) {
      console.warn(`API request failed: ${endpoint}, using fallback data`);
      return fallbackData;
    }

    return await response.json();
  } catch (error) {
    console.warn(`API unavailable: ${endpoint}, using fallback data`, error);
    return fallbackData;
  }
}

// API functions
export const api = {
  // Get all missions
  async getMissions(): Promise<LaunchMission[]> {
    return fetchWithFallback('/missions', staticMissions);
  },

  // Get all workstreams
  async getWorkstreams(): Promise<Workstream[]> {
    return fetchWithFallback('/workstreams', staticWorkstreams);
  },

  // Get all tasks
  async getTasks(): Promise<Task[]> {
    return fetchWithFallback('/tasks', staticTasks);
  },

  // Get all risks
  async getRisks(): Promise<Risk[]> {
    return fetchWithFallback('/risks', staticRisks);
  },

  // Get all scenarios
  async getScenarios(): Promise<Scenario[]> {
    return fetchWithFallback('/scenarios', staticScenarios);
  },

  // Apply a scenario
  async applyScenario(scenarioId: string): Promise<{
    success: boolean;
    scenario: Scenario;
    message: string;
    timestamp: string;
  }> {
    if (!USE_API) {
      const scenario = staticScenarios.find((s) => s.id === scenarioId);
      if (!scenario) {
        throw new Error('Scenario not found');
      }
      return {
        success: true,
        scenario,
        message: `Scenario "${scenario.name}" applied (simulated)`,
        timestamp: new Date().toISOString(),
      };
    }

    try {
      const response = await fetch(
        `${API_BASE_URL}/scenarios/${scenarioId}/apply`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
        }
      );

      if (!response.ok) {
        throw new Error('Failed to apply scenario');
      }

      return await response.json();
    } catch (error) {
      console.warn('API unavailable, using simulated scenario application');
      const scenario = staticScenarios.find((s) => s.id === scenarioId);
      if (!scenario) {
        throw new Error('Scenario not found');
      }
      return {
        success: true,
        scenario,
        message: `Scenario "${scenario.name}" applied (simulated)`,
        timestamp: new Date().toISOString(),
      };
    }
  },

  // Update task
  async updateTask(taskId: string, updates: Partial<Task>): Promise<Task> {
    if (!USE_API) {
      const task = staticTasks.find((t) => t.id === taskId);
      if (!task) {
        throw new Error('Task not found');
      }
      return { ...task, ...updates };
    }

    try {
      const response = await fetch(`${API_BASE_URL}/tasks/${taskId}`, {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(updates),
      });

      if (!response.ok) {
        throw new Error('Failed to update task');
      }

      return await response.json();
    } catch (error) {
      console.warn('API unavailable, task update simulated');
      const task = staticTasks.find((t) => t.id === taskId);
      if (!task) {
        throw new Error('Task not found');
      }
      return { ...task, ...updates };
    }
  },

  // Health check
  async healthCheck(): Promise<{
    status: string;
    timestamp: string;
    service: string;
  }> {
    try {
      const response = await fetch(`${API_BASE_URL}/health`);
      if (!response.ok) {
        throw new Error('Health check failed');
      }
      return await response.json();
    } catch (error) {
      return {
        status: 'unavailable',
        timestamp: new Date().toISOString(),
        service: 'Aegis Launch Control API (offline)',
      };
    }
  },
};
