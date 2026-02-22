# 🏭 Multi-Agent Decentralized Smart Factory (SPADE)



A **decentralized smart factory management system** built with **SPADE**.  
This simulation models a factory floor where **machines, robots/workers, suppliers, and maintenance crews** collaborate **peer-to-peer** to schedule production tasks, manage resources, and handle equipment failures **in real time**, **without a central production controller**.

> ℹ️ This project includes a `SupervisorAgent` whose role is to **inject production orders (create jobs)** into the environment.  
> The **allocation/negotiation** of jobs, supply requests, maintenance dispatching, and job transfers remains **decentralized** through agent communication and the **Contract Net Protocol (CNP)**.

---

## ✨ Key Features

### 🤖 Agent Types
- **Machine Agents**
  - Execute production tasks (multi-step jobs / pipelines)
  - Monitor internal state and simulate failures
  - Request raw materials when needed
  - Ask for maintenance and/or reallocate work upon disruptions

- **Robot / Worker Agents**
  - Transport materials between machines
  - Support job transfer logistics (moving jobs between machines when required)
  - Adapt to workload and operational bottlenecks

- **Maintenance Crew Agents**
  - Handle breakdowns and preventive maintenance requests
  - Limited by availability and repair time
  - Negotiate requests via Contract Net Protocol

- **Supply Agents**
  - Manage raw material inventory and deliveries
  - Negotiate deliveries with machines to ensure timely supply
  - May fail requests if stock/availability is insufficient

### 🔁 Decentralized Coordination
- No central scheduler: agents coordinate **peer-to-peer**
- Task delegation and service requests are negotiated using **Contract Net Protocol (CNP)**
- Agents reroute tasks and balance workload under failures or delays

### 🌪️ Dynamic Environment
- Random machine breakdowns
- Supply delays / shortages
- Sudden changes in production demand (injected by the supervisor)

### 📈 Performance Metrics
- Production throughput (jobs completed)
- Downtime due to failures (downtime ticks)
- Resource/service utilization (machines, supplies, maintenance)
- Responsiveness to disruptions (repairs started/finished, transfers)
- Collaboration effectiveness (successful negotiations vs failures)

### 👀 Visualization
- Real-time simulation display using **Matplotlib**
- Shows factory layout, agent states, job progress, and aggregated metrics

---

## 🧠 Architecture Overview

### Main Components
- **FactoryEnvironment**  
  Shared simulation state & global counters (metrics).
- **Agents (SPADE)**  
  Independent entities interacting through XMPP messages.
- **Protocols (CNP + message channels)**  
  Contract Net Protocol is used for:
  - job allocation
  - supply requests
  - maintenance dispatch
  - job transfer and logistics

### High-Level Message Channels (indicative)
- `job-dispatch` — job allocation negotiation  
- `supply-cnp` — raw material supply negotiation  
- `maintenance-cnp` — maintenance request negotiation  
- `transfer-cnp` — machine-to-machine job transfer negotiation  
- `transport-cnp` — robot logistics negotiation  
- `job-transfer` — final job-transfer confirmation/execution  

## 📊 Metrics Tracked

The environment keeps global counters such as:
- **Jobs:** `jobs_created`, `jobs_completed`, `jobs_lost`
- **Supplies:** `supply_requests_ok`, `supply_requests_failed`
- **Failures & Maintenance:** `machine_failures`, `repairs_started`, `repairs_finished`, `downtime_ticks`
- **Resilience:** `jobs_transferred`


