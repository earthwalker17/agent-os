// Seed data for Aegis Launch Control
import { LaunchMission, Task, Risk, Workstream, Dependency, Milestone, Scenario, TeamMember } from '../types';

// Team Members
export const teamMembers: TeamMember[] = [
  { id: 'tm1', name: 'Sarah Chen', role: 'Mission Director', workstream: 'mission-command', avatarColor: '#3B82F6' },
  { id: 'tm2', name: 'Marcus Rodriguez', role: 'Chief Engineer', workstream: 'engineering', avatarColor: '#8B5CF6' },
  { id: 'tm3', name: 'Dr. Aisha Patel', role: 'Systems Lead', workstream: 'systems', avatarColor: '#EC4899' },
  { id: 'tm4', name: 'James O\'Connor', role: 'Operations Manager', workstream: 'operations', avatarColor: '#10B981' },
  { id: 'tm5', name: 'Lisa Kim', role: 'Compliance Officer', workstream: 'regulatory', avatarColor: '#F59E0B' },
  { id: 'tm6', name: 'David Zhang', role: 'Safety Director', workstream: 'safety', avatarColor: '#EF4444' },
  { id: 'tm7', name: 'Emma Williams', role: 'Propulsion Engineer', workstream: 'engineering', avatarColor: '#8B5CF6' },
  { id: 'tm8', name: 'Alex Turner', role: 'Integration Specialist', workstream: 'systems', avatarColor: '#EC4899' },
  { id: 'tm9', name: 'Raj Krishnan', role: 'Test Engineer', workstream: 'operations', avatarColor: '#10B981' },
  { id: 'tm10', name: 'Nina Volkov', role: 'Range Coordinator', workstream: 'operations', avatarColor: '#10B981' },
];

// Main Mission
export const missions: LaunchMission[] = [
  {
    id: 'mission-1',
    name: 'Aegis Orbital Deployment',
    code: 'AEGIS-001',
    status: 'active',
    launchDate: '2024-12-15T14:30:00Z',
    progress: 67,
    budget: 125000000,
    budgetUsed: 89500000,
    teamSize: 47,
    description: 'Primary satellite constellation deployment mission for global communications network'
  }
];

// Workstreams
export const workstreams: Workstream[] = [
  {
    id: 'engineering',
    name: 'Engineering & Design',
    description: 'Vehicle systems, propulsion, and structural engineering',
    lead: 'Marcus Rodriguez',
    color: '#8B5CF6',
    progress: 82,
    taskCount: 8,
    completedTaskCount: 6
  },
  {
    id: 'systems',
    name: 'Systems Integration',
    description: 'Payload integration, avionics, and software systems',
    lead: 'Dr. Aisha Patel',
    color: '#EC4899',
    progress: 71,
    taskCount: 7,
    completedTaskCount: 5
  },
  {
    id: 'operations',
    name: 'Launch Operations',
    description: 'Range operations, ground systems, and mission control',
    lead: 'James O\'Connor',
    color: '#10B981',
    progress: 58,
    taskCount: 9,
    completedTaskCount: 4
  },
  {
    id: 'regulatory',
    name: 'Regulatory & Compliance',
    description: 'FAA licensing, environmental compliance, safety approvals',
    lead: 'Lisa Kim',
    color: '#F59E0B',
    progress: 45,
    taskCount: 6,
    completedTaskCount: 2
  },
  {
    id: 'safety',
    name: 'Safety & Risk',
    description: 'Flight safety, range safety, and hazard analysis',
    lead: 'David Zhang',
    color: '#EF4444',
    progress: 63,
    taskCount: 5,
    completedTaskCount: 3
  },
  {
    id: 'mission-command',
    name: 'Mission Command',
    description: 'Mission planning, timeline coordination, stakeholder management',
    lead: 'Sarah Chen',
    color: '#3B82F6',
    progress: 75,
    taskCount: 6,
    completedTaskCount: 4
  }
];

// Tasks (30-40 across workstreams)
export const tasks: Task[] = [
  // Engineering & Design (8 tasks)
  {
    id: 'task-eng-1',
    title: 'First Stage Propulsion System Validation',
    description: 'Complete static fire test series for main engines',
    workstream: 'engineering',
    status: 'completed',
    priority: 'critical',
    assignee: 'Marcus Rodriguez',
    dueDate: '2024-10-15',
    completedDate: '2024-10-12',
    estimatedHours: 120,
    actualHours: 115,
    dependencies: [],
    tags: ['propulsion', 'testing', 'critical-path']
  },
  {
    id: 'task-eng-2',
    title: 'Second Stage Structural Load Testing',
    description: 'Verify structural integrity under maximum expected loads',
    workstream: 'engineering',
    status: 'completed',
    priority: 'high',
    assignee: 'Emma Williams',
    dueDate: '2024-10-20',
    completedDate: '2024-10-18',
    estimatedHours: 80,
    actualHours: 92,
    dependencies: ['task-eng-1'],
    tags: ['structural', 'testing']
  },
  {
    id: 'task-eng-3',
    title: 'Fairing Acoustic Testing',
    description: 'Validate payload fairing acoustic protection systems',
    workstream: 'engineering',
    status: 'completed',
    priority: 'high',
    assignee: 'Marcus Rodriguez',
    dueDate: '2024-10-25',
    completedDate: '2024-10-24',
    estimatedHours: 60,
    actualHours: 58,
    dependencies: [],
    tags: ['fairing', 'testing']
  },
  {
    id: 'task-eng-4',
    title: 'Propellant Loading System Certification',
    description: 'Complete certification of automated propellant loading procedures',
    workstream: 'engineering',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'Emma Williams',
    dueDate: '2024-11-10',
    estimatedHours: 100,
    actualHours: 67,
    dependencies: ['task-eng-1'],
    tags: ['propulsion', 'ground-systems', 'critical-path']
  },
  {
    id: 'task-eng-5',
    title: 'Thrust Vector Control Calibration',
    description: 'Fine-tune TVC system for optimal flight control',
    workstream: 'engineering',
    status: 'in-progress',
    priority: 'high',
    assignee: 'Marcus Rodriguez',
    dueDate: '2024-11-12',
    estimatedHours: 75,
    actualHours: 48,
    dependencies: ['task-eng-1'],
    tags: ['propulsion', 'control-systems']
  },
  {
    id: 'task-eng-6',
    title: 'Flight Termination System Validation',
    description: 'Verify FTS activation and safing procedures',
    workstream: 'engineering',
    status: 'completed',
    priority: 'critical',
    assignee: 'Emma Williams',
    dueDate: '2024-10-30',
    completedDate: '2024-10-29',
    estimatedHours: 90,
    actualHours: 88,
    dependencies: [],
    tags: ['safety', 'fts', 'critical-path']
  },
  {
    id: 'task-eng-7',
    title: 'Aerodynamic Stability Analysis',
    description: 'Complete wind tunnel testing and CFD validation',
    workstream: 'engineering',
    status: 'completed',
    priority: 'medium',
    assignee: 'Marcus Rodriguez',
    dueDate: '2024-10-05',
    completedDate: '2024-10-03',
    estimatedHours: 65,
    actualHours: 70,
    dependencies: [],
    tags: ['aerodynamics', 'analysis']
  },
  {
    id: 'task-eng-8',
    title: 'Stage Separation Mechanism Testing',
    description: 'Validate stage separation pyrotechnics and timing',
    workstream: 'engineering',
    status: 'completed',
    priority: 'critical',
    assignee: 'Emma Williams',
    dueDate: '2024-10-22',
    completedDate: '2024-10-21',
    estimatedHours: 85,
    actualHours: 82,
    dependencies: ['task-eng-2'],
    tags: ['staging', 'pyrotechnics', 'critical-path']
  },

  // Systems Integration (7 tasks)
  {
    id: 'task-sys-1',
    title: 'Payload Interface Compatibility Check',
    description: 'Verify mechanical and electrical interfaces with customer payload',
    workstream: 'systems',
    status: 'completed',
    priority: 'critical',
    assignee: 'Dr. Aisha Patel',
    dueDate: '2024-10-10',
    completedDate: '2024-10-08',
    estimatedHours: 95,
    actualHours: 102,
    dependencies: [],
    tags: ['payload', 'integration', 'critical-path']
  },
  {
    id: 'task-sys-2',
    title: 'Avionics Integration Testing',
    description: 'Complete end-to-end avionics system validation',
    workstream: 'systems',
    status: 'completed',
    priority: 'critical',
    assignee: 'Alex Turner',
    dueDate: '2024-10-18',
    completedDate: '2024-10-17',
    estimatedHours: 110,
    actualHours: 118,
    dependencies: ['task-sys-1'],
    tags: ['avionics', 'integration', 'critical-path']
  },
  {
    id: 'task-sys-3',
    title: 'Flight Software Version 2.1 Deployment',
    description: 'Deploy and validate flight software update with navigation improvements',
    workstream: 'systems',
    status: 'completed',
    priority: 'high',
    assignee: 'Dr. Aisha Patel',
    dueDate: '2024-10-28',
    completedDate: '2024-10-26',
    estimatedHours: 70,
    actualHours: 75,
    dependencies: ['task-sys-2'],
    tags: ['software', 'navigation']
  },
  {
    id: 'task-sys-4',
    title: 'Telemetry System End-to-End Test',
    description: 'Validate telemetry data flow from vehicle to ground station',
    workstream: 'systems',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'Alex Turner',
    dueDate: '2024-11-08',
    estimatedHours: 85,
    actualHours: 52,
    dependencies: ['task-sys-2', 'task-ops-3'],
    tags: ['telemetry', 'communications', 'critical-path']
  },
  {
    id: 'task-sys-5',
    title: 'Power Distribution System Verification',
    description: 'Test power system redundancy and load balancing',
    workstream: 'systems',
    status: 'completed',
    priority: 'high',
    assignee: 'Dr. Aisha Patel',
    dueDate: '2024-10-15',
    completedDate: '2024-10-14',
    estimatedHours: 60,
    actualHours: 63,
    dependencies: [],
    tags: ['power', 'electrical']
  },
  {
    id: 'task-sys-6',
    title: 'Payload Encapsulation Rehearsal',
    description: 'Practice payload installation and fairing closure procedures',
    workstream: 'systems',
    status: 'in-progress',
    priority: 'high',
    assignee: 'Alex Turner',
    dueDate: '2024-11-15',
    estimatedHours: 95,
    actualHours: 44,
    dependencies: ['task-sys-1', 'task-eng-3'],
    tags: ['payload', 'procedures']
  },
  {
    id: 'task-sys-7',
    title: 'GPS/INS Navigation System Calibration',
    description: 'Calibrate and validate hybrid navigation system',
    workstream: 'systems',
    status: 'todo',
    priority: 'medium',
    assignee: 'Dr. Aisha Patel',
    dueDate: '2024-11-20',
    estimatedHours: 55,
    dependencies: ['task-sys-3'],
    tags: ['navigation', 'gps']
  },

  // Launch Operations (9 tasks)
  {
    id: 'task-ops-1',
    title: 'Launch Pad Readiness Assessment',
    description: 'Complete facility inspection and readiness certification',
    workstream: 'operations',
    status: 'completed',
    priority: 'critical',
    assignee: 'James O\'Connor',
    dueDate: '2024-10-08',
    completedDate: '2024-10-07',
    estimatedHours: 80,
    actualHours: 85,
    dependencies: [],
    tags: ['ground-systems', 'facilities', 'critical-path']
  },
  {
    id: 'task-ops-2',
    title: 'Mission Control Console Configuration',
    description: 'Set up and test all mission control stations',
    workstream: 'operations',
    status: 'completed',
    priority: 'high',
    assignee: 'Raj Krishnan',
    dueDate: '2024-10-12',
    completedDate: '2024-10-11',
    estimatedHours: 65,
    actualHours: 68,
    dependencies: [],
    tags: ['mission-control', 'ground-systems']
  },
  {
    id: 'task-ops-3',
    title: 'Ground Station Network Validation',
    description: 'Test tracking stations and data relay network',
    workstream: 'operations',
    status: 'completed',
    priority: 'critical',
    assignee: 'Nina Volkov',
    dueDate: '2024-10-20',
    completedDate: '2024-10-19',
    estimatedHours: 90,
    actualHours: 94,
    dependencies: ['task-ops-2'],
    tags: ['tracking', 'communications', 'critical-path']
  },
  {
    id: 'task-ops-4',
    title: 'Launch Countdown Procedure Dry Run',
    description: 'Execute full countdown rehearsal without propellant loading',
    workstream: 'operations',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'James O\'Connor',
    dueDate: '2024-11-18',
    estimatedHours: 140,
    actualHours: 72,
    dependencies: ['task-ops-1', 'task-eng-4'],
    tags: ['countdown', 'rehearsal', 'critical-path']
  },
  {
    id: 'task-ops-5',
    title: 'Weather Monitoring System Setup',
    description: 'Deploy and test weather sensors and forecasting tools',
    workstream: 'operations',
    status: 'in-progress',
    priority: 'high',
    assignee: 'Nina Volkov',
    dueDate: '2024-11-10',
    estimatedHours: 50,
    actualHours: 31,
    dependencies: [],
    tags: ['weather', 'monitoring']
  },
  {
    id: 'task-ops-6',
    title: 'Range Safety Coordination',
    description: 'Coordinate with Eastern Range for launch window and safety zones',
    workstream: 'operations',
    status: 'completed',
    priority: 'critical',
    assignee: 'James O\'Connor',
    dueDate: '2024-10-25',
    completedDate: '2024-10-23',
    estimatedHours: 75,
    actualHours: 80,
    dependencies: [],
    tags: ['range-safety', 'coordination', 'critical-path']
  },
  {
    id: 'task-ops-7',
    title: 'Emergency Response Drill',
    description: 'Conduct full-scale emergency response exercise',
    workstream: 'operations',
    status: 'todo',
    priority: 'high',
    assignee: 'Raj Krishnan',
    dueDate: '2024-11-22',
    estimatedHours: 110,
    dependencies: ['task-safety-4'],
    tags: ['emergency', 'safety', 'drill']
  },
  {
    id: 'task-ops-8',
    title: 'Propellant Storage Tank Inspection',
    description: 'Complete annual inspection of cryogenic propellant tanks',
    workstream: 'operations',
    status: 'todo',
    priority: 'medium',
    assignee: 'Nina Volkov',
    dueDate: '2024-11-25',
    estimatedHours: 60,
    dependencies: [],
    tags: ['propellant', 'inspection']
  },
  {
    id: 'task-ops-9',
    title: 'Crew Training Certification',
    description: 'Complete final certification for all launch crew positions',
    workstream: 'operations',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'James O\'Connor',
    dueDate: '2024-11-28',
    estimatedHours: 120,
    actualHours: 75,
    dependencies: ['task-ops-4'],
    tags: ['training', 'certification', 'critical-path']
  },

  // Regulatory & Compliance (6 tasks)
  {
    id: 'task-reg-1',
    title: 'FAA Launch License Application',
    description: 'Submit complete launch license application to FAA/AST',
    workstream: 'regulatory',
    status: 'completed',
    priority: 'critical',
    assignee: 'Lisa Kim',
    dueDate: '2024-09-30',
    completedDate: '2024-09-28',
    estimatedHours: 150,
    actualHours: 162,
    dependencies: [],
    tags: ['faa', 'licensing', 'critical-path']
  },
  {
    id: 'task-reg-2',
    title: 'Environmental Impact Assessment',
    description: 'Complete environmental review and mitigation plan',
    workstream: 'regulatory',
    status: 'completed',
    priority: 'high',
    assignee: 'Lisa Kim',
    dueDate: '2024-10-05',
    completedDate: '2024-10-04',
    estimatedHours: 100,
    actualHours: 105,
    dependencies: [],
    tags: ['environmental', 'compliance']
  },
  {
    id: 'task-reg-3',
    title: 'FCC Frequency Coordination',
    description: 'Obtain FCC approval for telemetry and communication frequencies',
    workstream: 'regulatory',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'Lisa Kim',
    dueDate: '2024-11-05',
    estimatedHours: 80,
    actualHours: 58,
    dependencies: ['task-sys-4'],
    tags: ['fcc', 'spectrum', 'critical-path']
  },
  {
    id: 'task-reg-4',
    title: 'Orbital Debris Assessment',
    description: 'Submit orbital debris mitigation plan and analysis',
    workstream: 'regulatory',
    status: 'todo',
    priority: 'high',
    assignee: 'Lisa Kim',
    dueDate: '2024-11-12',
    estimatedHours: 70,
    dependencies: ['task-reg-1'],
    tags: ['debris', 'orbital']
  },
  {
    id: 'task-reg-5',
    title: 'Insurance Policy Finalization',
    description: 'Finalize third-party liability and property insurance',
    workstream: 'regulatory',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'Lisa Kim',
    dueDate: '2024-11-20',
    estimatedHours: 95,
    actualHours: 42,
    dependencies: ['task-reg-1'],
    tags: ['insurance', 'liability', 'critical-path']
  },
  {
    id: 'task-reg-6',
    title: 'Export Control Compliance Review',
    description: 'Complete ITAR and export control documentation',
    workstream: 'regulatory',
    status: 'todo',
    priority: 'medium',
    assignee: 'Lisa Kim',
    dueDate: '2024-11-15',
    estimatedHours: 65,
    dependencies: [],
    tags: ['itar', 'export-control']
  },

  // Safety & Risk (5 tasks)
  {
    id: 'task-safety-1',
    title: 'Flight Safety Analysis Report',
    description: 'Complete comprehensive flight safety analysis and risk assessment',
    workstream: 'safety',
    status: 'completed',
    priority: 'critical',
    assignee: 'David Zhang',
    dueDate: '2024-10-10',
    completedDate: '2024-10-09',
    estimatedHours: 130,
    actualHours: 138,
    dependencies: [],
    tags: ['flight-safety', 'analysis', 'critical-path']
  },
  {
    id: 'task-safety-2',
    title: 'Hazard Analysis and Critical Control Points',
    description: 'Identify and document all mission hazards with control measures',
    workstream: 'safety',
    status: 'completed',
    priority: 'critical',
    assignee: 'David Zhang',
    dueDate: '2024-10-18',
    completedDate: '2024-10-16',
    estimatedHours: 110,
    actualHours: 115,
    dependencies: ['task-safety-1'],
    tags: ['hazard-analysis', 'safety', 'critical-path']
  },
  {
    id: 'task-safety-3',
    title: 'Range Safety Plan Approval',
    description: 'Obtain Range Safety Officer approval for launch operations',
    workstream: 'safety',
    status: 'completed',
    priority: 'critical',
    assignee: 'David Zhang',
    dueDate: '2024-10-25',
    completedDate: '2024-10-24',
    estimatedHours: 85,
    actualHours: 82,
    dependencies: ['task-safety-2', 'task-ops-6'],
    tags: ['range-safety', 'approval', 'critical-path']
  },
  {
    id: 'task-safety-4',
    title: 'Emergency Egress System Validation',
    description: 'Test and certify emergency evacuation procedures and systems',
    workstream: 'safety',
    status: 'in-progress',
    priority: 'high',
    assignee: 'David Zhang',
    dueDate: '2024-11-08',
    estimatedHours: 75,
    actualHours: 51,
    dependencies: ['task-ops-1'],
    tags: ['emergency', 'egress', 'safety']
  },
  {
    id: 'task-safety-5',
    title: 'Probabilistic Risk Assessment Update',
    description: 'Update PRA model with latest test data and operational changes',
    workstream: 'safety',
    status: 'in-progress',
    priority: 'medium',
    assignee: 'David Zhang',
    dueDate: '2024-11-18',
    estimatedHours: 90,
    actualHours: 38,
    dependencies: ['task-safety-1'],
    tags: ['pra', 'risk-assessment']
  },

  // Mission Command (6 tasks)
  {
    id: 'task-cmd-1',
    title: 'Mission Objectives Definition',
    description: 'Finalize primary and secondary mission success criteria',
    workstream: 'mission-command',
    status: 'completed',
    priority: 'critical',
    assignee: 'Sarah Chen',
    dueDate: '2024-09-20',
    completedDate: '2024-09-18',
    estimatedHours: 60,
    actualHours: 62,
    dependencies: [],
    tags: ['planning', 'objectives', 'critical-path']
  },
  {
    id: 'task-cmd-2',
    title: 'Integrated Master Schedule Update',
    description: 'Update master schedule with latest milestone dates',
    workstream: 'mission-command',
    status: 'completed',
    priority: 'high',
    assignee: 'Sarah Chen',
    dueDate: '2024-10-15',
    completedDate: '2024-10-14',
    estimatedHours: 45,
    actualHours: 48,
    dependencies: [],
    tags: ['scheduling', 'planning']
  },
  {
    id: 'task-cmd-3',
    title: 'Stakeholder Readiness Review',
    description: 'Conduct readiness review with customer and key stakeholders',
    workstream: 'mission-command',
    status: 'completed',
    priority: 'critical',
    assignee: 'Sarah Chen',
    dueDate: '2024-10-30',
    completedDate: '2024-10-28',
    estimatedHours: 55,
    actualHours: 60,
    dependencies: ['task-cmd-1'],
    tags: ['stakeholders', 'review', 'critical-path']
  },
  {
    id: 'task-cmd-4',
    title: 'Launch Readiness Review',
    description: 'Executive-level go/no-go decision meeting preparation',
    workstream: 'mission-command',
    status: 'in-progress',
    priority: 'critical',
    assignee: 'Sarah Chen',
    dueDate: '2024-12-05',
    estimatedHours: 80,
    actualHours: 35,
    dependencies: ['task-cmd-3', 'task-ops-9'],
    tags: ['lrr', 'go-no-go', 'critical-path']
  },
  {
    id: 'task-cmd-5',
    title: 'Mission Press Kit Development',
    description: 'Prepare comprehensive media materials and fact sheets',
    workstream: 'mission-command',
    status: 'completed',
    priority: 'medium',
    assignee: 'Sarah Chen',
    dueDate: '2024-11-01',
    completedDate: '2024-10-30',
    estimatedHours: 50,
    actualHours: 52,
    dependencies: [],
    tags: ['communications', 'media']
  },
  {
    id: 'task-cmd-6',
    title: 'Contingency Planning Workshop',
    description: 'Develop response plans for off-nominal scenarios',
    workstream: 'mission-command',
    status: 'in-progress',
    priority: 'high',
    assignee: 'Sarah Chen',
    dueDate: '2024-11-15',
    estimatedHours: 70,
    actualHours: 42,
    dependencies: ['task-safety-5'],
    tags: ['contingency', 'planning']
  }
];

// Dependencies (8-10 cross-workstream dependencies)
export const dependencies: Dependency[] = [
  { id: 'dep-1', fromTaskId: 'task-eng-1', toTaskId: 'task-eng-2', type: 'blocking', description: 'Propulsion validation required before structural testing' },
  { id: 'dep-2', fromTaskId: 'task-eng-1', toTaskId: 'task-eng-4', type: 'blocking', description: 'Engine validation needed for loading system cert' },
  { id: 'dep-3', fromTaskId: 'task-eng-2', toTaskId: 'task-eng-8', type: 'finish-to-start', description: 'Structural validation before staging tests' },
  { id: 'dep-4', fromTaskId: 'task-sys-1', toTaskId: 'task-sys-2', type: 'blocking', description: 'Interface check before avionics integration' },
  { id: 'dep-5', fromTaskId: 'task-sys-2', toTaskId: 'task-sys-3', type: 'blocking', description: 'Avionics integration before software deployment' },
  { id: 'dep-6', fromTaskId: 'task-sys-2', toTaskId: 'task-sys-4', type: 'blocking', description: 'Avionics ready for telemetry testing' },
  { id: 'dep-7', fromTaskId: 'task-ops-3', toTaskId: 'task-sys-4', type: 'blocking', description: 'Ground stations operational for telemetry test' },
  { id: 'dep-8', fromTaskId: 'task-ops-1', toTaskId: 'task-ops-4', type: 'blocking', description: 'Pad readiness required for countdown rehearsal' },
  { id: 'dep-9', fromTaskId: 'task-safety-1', toTaskId: 'task-safety-2', type: 'finish-to-start', description: 'Safety analysis informs hazard identification' },
  { id: 'dep-10', fromTaskId: 'task-cmd-1', toTaskId: 'task-cmd-3', type: 'blocking', description: 'Objectives defined before stakeholder review' }
];

// Risks (5 categories with multiple items)
export const risks: Risk[] = [
  // Technical Risks
  {
    id: 'risk-tech-1',
    title: 'Propellant Loading Valve Reliability',
    description: 'New automated loading system has limited flight heritage; valve failure could cause scrub or delay',
    category: 'technical',
    severity: 'high',
    probability: 25,
    impact: 70,
    mitigation: 'Redundant valve design implemented; extensive ground testing ongoing; manual backup procedures in place',
    status: 'monitoring',
    owner: 'Emma Williams',
    identifiedDate: '2024-09-15'
  },
  {
    id: 'risk-tech-2',
    title: 'Telemetry Downlink Intermittency',
    description: 'Occasional signal dropouts observed during integration testing',
    category: 'technical',
    severity: 'medium',
    probability: 35,
    impact: 50,
    mitigation: 'Antenna gain optimization in progress; additional ground stations activated for redundancy',
    status: 'monitoring',
    owner: 'Alex Turner',
    identifiedDate: '2024-10-12'
  },
  {
    id: 'risk-tech-3',
    title: 'Upper Stage Battery Performance Degradation',
    description: 'Battery capacity showing 8% reduction in recent thermal cycling tests',
    category: 'technical',
    severity: 'medium',
    probability: 40,
    impact: 45,
    mitigation: 'Operating within safe margins; replacement battery on standby; thermal management review completed',
    status: 'mitigated',
    owner: 'Dr. Aisha Patel',
    identifiedDate: '2024-10-05'
  },
  {
    id: 'risk-tech-4',
    title: 'First Stage Engine Turbopump Vibration',
    description: 'Slightly elevated vibration levels in turbopump bearing during static fire',
    category: 'technical',
    severity: 'high',
    probability: 20,
    impact: 85,
    mitigation: 'Root cause analysis completed; bearing redesigned; replacement installed and tested successfully',
    status: 'mitigated',
    owner: 'Marcus Rodriguez',
    identifiedDate: '2024-09-28'
  },

  // Schedule Risks
  {
    id: 'risk-sched-1',
    title: 'Regulatory Approval Timeline Uncertainty',
    description: 'FCC frequency coordination taking longer than anticipated; could delay launch window',
    category: 'schedule',
    severity: 'critical',
    probability: 45,
    impact: 75,
    mitigation: 'Daily engagement with FCC staff; backup frequencies identified; executive escalation prepared',
    status: 'monitoring',
    owner: 'Lisa Kim',
    identifiedDate: '2024-10-20'
  },
  {
    id: 'risk-sched-2',
    title: 'Payload Delivery Delay',
    description: 'Customer payload shipment delayed by 2 weeks due to supplier issue',
    category: 'schedule',
    severity: 'high',
    probability: 60,
    impact: 65,
    mitigation: 'Revised integration schedule developed; parallel workstreams accelerated; launch window still achievable',
    status: 'monitoring',
    owner: 'Sarah Chen',
    identifiedDate: '2024-10-18'
  },
  {
    id: 'risk-sched-3',
    title: 'Range Scheduling Conflicts',
    description: 'Multiple launch providers competing for limited range time in December',
    category: 'schedule',
    severity: 'medium',
    probability: 50,
    impact: 55,
    mitigation: 'Primary and backup launch windows secured; range coordination ongoing weekly',
    status: 'monitoring',
    owner: 'James O\'Connor',
    identifiedDate: '2024-10-01'
  },

  // Budget Risks
  {
    id: 'risk-budget-1',
    title: 'Cost Overrun in Ground Systems Upgrades',
    description: 'Pad infrastructure improvements 15% over original estimate',
    category: 'budget',
    severity: 'medium',
    probability: 80,
    impact: 40,
    mitigation: 'Management reserve allocated; non-critical upgrades deferred to post-launch; total budget impact contained',
    status: 'mitigated',
    owner: 'Sarah Chen',
    identifiedDate: '2024-09-25'
  },
  {
    id: 'risk-budget-2',
    title: 'Insurance Premium Increase',
    description: 'Third-party liability insurance quotes 20% higher than budgeted',
    category: 'budget',
    severity: 'medium',
    probability: 70,
    impact: 35,
    mitigation: 'Negotiating with multiple providers; risk mitigation measures documented to reduce premium',
    status: 'monitoring',
    owner: 'Lisa Kim',
    identifiedDate: '2024-10-22'
  },

  // Regulatory Risks
  {
    id: 'risk-reg-1',
    title: 'Environmental Permit Conditions',
    description: 'New noise abatement requirements may restrict launch window timing',
    category: 'regulatory',
    severity: 'high',
    probability: 35,
    impact: 60,
    mitigation: 'Acoustic modeling updated; alternative launch times identified; stakeholder engagement ongoing',
    status: 'monitoring',
    owner: 'Lisa Kim',
    identifiedDate: '2024-10-08'
  },
  {
    id: 'risk-reg-2',
    title: 'Orbital Debris Assessment Review',
    description: 'FAA requesting additional analysis on post-mission disposal',
    category: 'regulatory',
    severity: 'medium',
    probability: 55,
    impact: 50,
    mitigation: 'Enhanced deorbit analysis in progress; engineering team validating disposal timeline',
    status: 'monitoring',
    owner: 'Lisa Kim',
    identifiedDate: '2024-10-28'
  },

  // External Risks
  {
    id: 'risk-ext-1',
    title: 'Weather Delay Probability',
    description: 'December historical weather patterns show 40% scrub rate at launch site',
    category: 'external',
    severity: 'medium',
    probability: 40,
    impact: 45,
    mitigation: 'Extended launch window secured; multiple backup dates available; real-time forecasting enhanced',
    status: 'accepted',
    owner: 'Nina Volkov',
    identifiedDate: '2024-09-10'
  },
  {
    id: 'risk-ext-2',
    title: 'Geopolitical Launch Range Access',
    description: 'Potential airspace restrictions due to international tensions',
    category: 'external',
    severity: 'low',
    probability: 15,
    impact: 80,
    mitigation: 'Monitoring geopolitical situation; alternative trajectory options analyzed; State Dept coordination active',
    status: 'monitoring',
    owner: 'Sarah Chen',
    identifiedDate: '2024-10-15'
  },
  {
    id: 'risk-ext-3',
    title: 'Supply Chain Disruption for Consumables',
    description: 'Helium and nitrogen supply concerns due to global shortage',
    category: 'external',
    severity: 'medium',
    probability: 30,
    impact: 55,
    mitigation: 'Six-month supply secured; backup suppliers identified; conservation measures implemented',
    status: 'mitigated',
    owner: 'James O\'Connor',
    identifiedDate: '2024-09-20'
  }
];

// Milestones (6-8 timeline milestones)
export const milestones: Milestone[] = [
  {
    id: 'milestone-1',
    name: 'Mission Authorization',
    description: 'FAA launch license received and mission formally authorized',
    date: '2024-09-30',
    status: 'completed',
    criticalPath: true,
    workstreams: ['regulatory', 'mission-command']
  },
  {
    id: 'milestone-2',
    name: 'Propulsion Qualification Complete',
    description: 'All engine static fire tests completed successfully',
    date: '2024-10-15',
    status: 'completed',
    criticalPath: true,
    workstreams: ['engineering']
  },
  {
    id: 'milestone-3',
    name: 'Systems Integration Verified',
    description: 'End-to-end avionics and payload integration validated',
    date: '2024-10-28',
    status: 'completed',
    criticalPath: true,
    workstreams: ['systems', 'engineering']
  },
  {
    id: 'milestone-4',
    name: 'Flight Safety Certification',
    description: 'Range Safety Officer approves flight safety package',
    date: '2024-10-25',
    status: 'completed',
    criticalPath: true,
    workstreams: ['safety', 'regulatory']
  },
  {
    id: 'milestone-5',
    name: 'Launch Rehearsal Complete',
    description: 'Wet dress rehearsal with full countdown to T-10 seconds',
    date: '2024-11-20',
    status: 'at-risk',
    criticalPath: true,
    workstreams: ['operations', 'engineering', 'systems']
  },
  {
    id: 'milestone-6',
    name: 'Launch Readiness Review',
    description: 'Executive go/no-go decision for launch campaign',
    date: '2024-12-05',
    status: 'upcoming',
    criticalPath: true,
    workstreams: ['mission-command', 'all']
  },
  {
    id: 'milestone-7',
    name: 'Payload Integration',
    description: 'Customer payload mated to upper stage and encapsulated',
    date: '2024-12-08',
    status: 'upcoming',
    criticalPath: true,
    workstreams: ['systems', 'operations']
  },
  {
    id: 'milestone-8',
    name: 'Launch Window Opens',
    description: 'Primary launch attempt - T-0 at 14:30 UTC',
    date: '2024-12-15',
    status: 'upcoming',
    criticalPath: true,
    workstreams: ['all']
  }
];

// Scenarios (3-4 launch scenarios)
export const scenarios: Scenario[] = [
  {
    id: 'scenario-1',
    name: 'Nominal Mission',
    description: 'Best-case scenario with all systems performing as designed and favorable conditions',
    type: 'nominal',
    launchDate: '2024-12-15T14:30:00Z',
    probability: 65,
    assumptions: [
      'All regulatory approvals received on time',
      'No significant technical anomalies during final testing',
      'Weather within acceptable launch commit criteria',
      'Payload delivered and integrated on schedule',
      'Range availability confirmed for primary window'
    ],
    impacts: {
      budget: 0,
      timeline: 0,
      risk: 'low'
    }
  },
  {
    id: 'scenario-2',
    name: 'Optimistic Early Launch',
    description: 'Accelerated timeline if all activities complete ahead of schedule',
    type: 'optimistic',
    launchDate: '2024-12-12T14:30:00Z',
    probability: 15,
    assumptions: [
      'FCC frequency approval received 1 week early',
      'Launch rehearsal successful on first attempt',
      'No weather delays during final preparations',
      'Customer payload arrives early',
      'Range accommodates earlier launch date'
    ],
    impacts: {
      budget: -2500000,
      timeline: -3,
      risk: 'medium'
    }
  },
  {
    id: 'scenario-3',
    name: 'Delayed Launch - Technical Hold',
    description: 'Launch delayed due to technical issue requiring additional testing or repairs',
    type: 'pessimistic',
    launchDate: '2024-12-22T14:30:00Z',
    probability: 25,
    assumptions: [
      'Minor technical anomaly discovered during launch rehearsal',
      'Additional 5-7 days required for troubleshooting and retest',
      'Backup launch window available within same month',
      'Weather conditions remain favorable in extended window',
      'Regulatory approvals remain valid'
    ],
    impacts: {
      budget: 3800000,
      timeline: 7,
      risk: 'medium'
    }
  },
  {
    id: 'scenario-4',
    name: 'Major Delay - Regulatory/Weather',
    description: 'Significant delay due to regulatory hold or extended weather constraints',
    type: 'contingency',
    launchDate: '2025-01-15T14:30:00Z',
    probability: 10,
    assumptions: [
      'FCC frequency coordination requires additional review',
      'Extended period of unfavorable weather conditions',
      'Need to secure new launch window in January',
      'Additional range safety reviews required',
      'Payload thermal cycling limits approached'
    ],
    impacts: {
      budget: 12500000,
      timeline: 31,
      risk: 'high'
    }
  }
];
