// Type definitions for Aegis Launch Control

export interface LaunchMission {
  id: string;
  name: string;
  code: string;
  status: 'planning' | 'active' | 'completed' | 'delayed' | 'critical';
  launchDate: string;
  progress: number;
  budget: number;
  budgetUsed: number;
  teamSize: number;
  description: string;
}

export interface Task {
  id: string;
  title: string;
  description: string;
  workstream: string;
  status: 'todo' | 'in-progress' | 'blocked' | 'completed';
  priority: 'low' | 'medium' | 'high' | 'critical';
  assignee: string;
  dueDate: string;
  completedDate?: string;
  estimatedHours: number;
  actualHours?: number;
  dependencies: string[];
  tags: string[];
}

export interface Workstream {
  id: string;
  name: string;
  description: string;
  lead: string;
  color: string;
  progress: number;
  taskCount: number;
  completedTaskCount: number;
}

export interface Risk {
  id: string;
  title: string;
  description: string;
  category: 'technical' | 'schedule' | 'budget' | 'regulatory' | 'external';
  severity: 'low' | 'medium' | 'high' | 'critical';
  probability: number; // 0-100
  impact: number; // 0-100
  mitigation: string;
  status: 'identified' | 'monitoring' | 'mitigated' | 'accepted';
  owner: string;
  identifiedDate: string;
}

export interface Dependency {
  id: string;
  fromTaskId: string;
  toTaskId: string;
  type: 'blocking' | 'related' | 'finish-to-start' | 'start-to-start';
  description?: string;
}

export interface Milestone {
  id: string;
  name: string;
  description: string;
  date: string;
  status: 'upcoming' | 'at-risk' | 'completed' | 'missed';
  criticalPath: boolean;
  workstreams: string[];
}

export interface Scenario {
  id: string;
  name: string;
  description: string;
  type: 'nominal' | 'optimistic' | 'pessimistic' | 'contingency';
  launchDate: string;
  probability: number;
  assumptions: string[];
  impacts: {
    budget: number;
    timeline: number; // days delta
    risk: 'low' | 'medium' | 'high';
  };
}

export interface TeamMember {
  id: string;
  name: string;
  role: string;
  workstream: string;
  avatarColor: string;
}
