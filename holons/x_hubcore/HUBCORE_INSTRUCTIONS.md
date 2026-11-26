# x_APP_RECREATION Agent

Purpose:
Design and guide implementation of x_APP_RECREATION, an open-source, local-first holonic application platform.

Core Principles:
- Local-first
- Modular
- Portable
- Observable
- Event-driven
- API-first
- Model-agnostic
- Storage-agnostic
- Human and machine usable
- Offline capable
- Federation capable

Architecture:

x_Hub (UX)
x_Floor (Operations UI)
↓
x_HubCore (coordination kernel)
↓
Holons | Workers | Connectors | Storage | Models
↓
Event Bus
↓
APIs | SFTP | External Systems

HubCore Responsibilities:
- Service discovery
- Holon registration
- Task routing
- Event routing
- Capability discovery
- Worker management
- Connector management
- Model routing
- Storage abstraction
- Configuration
- Observability

Holon Model:
A Holon is both an independent application and a participant in a larger ecosystem.

Example Holons:
PFEP, MRP, BOM, OTD, QMS, CC, EO, IFR, WIP, Wiki, ProcessStudio, QUIPU, ERP, SFTP.

Every Holon publishes:
- Identity
- Version
- Capabilities
- Events
- Interfaces
- Dependencies
- Permissions

Core Objects:

Holon:
- holon_id
- version
- capabilities

Task:
- task_id
- source
- target
- operation
- payload
- state

Result:
- task_id
- producer
- status
- payload
- evidence

Event:
- event_id
- type
- producer
- timestamp
- payload

Artifact:
- artifact_id
- type
- uri
- hash

Event Architecture:
Applications communicate through events rather than direct coupling.

Example events:
- inventory.updated
- part.updated
- supplier.updated
- bom.changed
- order.created
- shortage.detected
- quality.issue.created
- shipment.received
- model.updated
- dataset.updated

Data Architecture:

Storage Classes:
- SQLite for transactional state
- Parquet for analytical/history data
- DuckDB for analytics across datasets
- JSON for manifests and configuration

Data Ownership:
Each Holon owns its own authoritative state.
Cross-Holon interaction occurs through:
- Events
- Tasks
- APIs
- Artifacts

Connector Framework:

All external integrations follow:
- Open
- Read
- Write
- Health
- Close

Connector Types:
- SFTP
- REST
- SQL
- PostgreSQL
- Oracle
- SharePoint
- Graph
- S3
- Azure Blob

Connector behavior must remain independent of HubCore.

ERP Abstraction:
Provide adapter-based ERP integration.

Examples:
- SQL Server
- Oracle
- SAP
- Dynamics
- SyteLine
- Generic ODBC

Separate read and mutation capabilities.

Workers:
Workers dynamically register capabilities.

Examples:
- Forecasting
- Simulation
- Optimization
- Analysis

Worker Isolation Options:
- Native process
- Docker
- Podman
- Kubernetes
- WASM/WASI
- Remote execution

Model Architecture:

Use x_ModelGateway.

Supported Providers:
- llama.cpp
- ONNX Runtime
- Ollama
- vLLM
- Transformers
- OpenAI-compatible endpoints
- Azure OpenAI-compatible endpoints
- Custom runtimes

System must remain model-neutral.

QUIPU:
Optional intelligence Holon providing:
- Inference
- Simulation
- Optimization
- Memory
- Semantic reasoning

QUIPU is a participant, not the control plane.

MESH Federation:

Nodes exchange:
- Identity
- Capabilities
- Models
- Holons
- Dataset descriptors
- Protocol versions

Supports multi-node federation and optional federated learning.

Knowledge Layer:

WIP:
NEW → QUEUED → ACTIVE → BLOCKED → REVIEW → DONE → FAILED

Wiki:
WIP → Result → Review → Knowledge

Knowledge entries retain:
- Revision
- Sources
- Owner
- Timestamp
- References

Process Studio:
Portable workflow engine.

Workflows orchestrate:
- Holons
- Workers
- Models
- Connectors

ToolForge:
Extension framework for reusable tools.

Provider Studio:
Configures external providers without code modifications.

Plugin Architecture:

Plugin Types:
- Connector
- Model
- Holon
- Worker
- Workflow
- Visualization
- Storage

Plugins declare:
- Name
- Version
- Type
- Permissions
- Dependencies
- Entrypoint
- Capabilities

Capability Registry:

Everything discoverable through capability metadata.

System should avoid hard-coded dependencies between components.

API Surface:

/holons
/tasks
/results
/events
/artifacts
/workers
/models
/connectors
/workflows
/storage
/system

SDK Targets:
- Python
- TypeScript
Future:
- C#
- Rust
- Go
- Java

CLI Concepts:
- init
- start
- stop
- holon management
- worker management
- connector management
- task management
- mesh diagnostics

Deployment Profiles:
1. Single-user local
2. Developer workstation
3. Containerized
4. Distributed Kubernetes

Observability:
- OpenTelemetry
- Structured JSON logging
- Metrics
- Tracing
- Health endpoints

Security:
- Authentication providers
- Role/capability permissions
- Connector permissions
- Filesystem scoping
- Network scoping
- Secret references
- Plugin isolation
- Artifact hashing
- Audit logging

Secrets:
Use references only.
Never store secrets directly in source control.

Testing:
- Unit
- Integration
- Contract
- Connector
- Holon
- Model
- Mesh
- Workflow
- Security
- End-to-end

Versioning:
Semantic Versioning (MAJOR.MINOR.PATCH).

Architectural Rule:
x_APP_RECREATION provides capabilities.
Deployment owners provide authority and governance.

Primary Goal:
Enable independent Holons, Workers, Models, and Connectors to collaborate through standard contracts so new components can be added without modifying HubCore.

Definition of v1:
A clean installation can:
- Discover Holons
- Submit and route tasks
- Persist state
- Generate artifacts
- Publish events
- Trigger downstream workflows
- Query data
- Connect through SFTP
- Invoke local or remote models
- Execute multi-Holon workflows
- Restart without data loss
- Add new Holons without changing HubCore