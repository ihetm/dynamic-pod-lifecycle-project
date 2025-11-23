# Dynamic Pod Lifecycle System (Kubernetes + AWS EKS)

This project dynamically creates Kubernetes pods based on user requests. Each request produces a session-based worker pod, routes traffic through a gateway, and cleans up pods automatically using a custom operator.

This architecture is designed for **on-demand compute**, isolation per request/session, scaling up pods dynamically, and auto-reaping idle workloads.

---

## 🚀 Features

| Feature | Description |
|---------|------------|
| **Dynamic pod creation** | Creates worker pods on demand when requests arrive |
| **Session-based routing** | Each request is tied to a dedicated `pod-session-*` |
| **Operator for lifecycle** | Custom operator manages pod creation/deletion |
| **Redis state management** | Stores mapping `session_id → pod_name` |
| **API gateway entrypoint** | Public-facing entrypoint to trigger sessions |
| **Router service** | Forwards traffic to appropriate worker pod |
| **Runs on AWS EKS** | Supports autoscaling + ALB ingress |

---

## 📂 Components

| Component | Role |
|----------|------|
| `api-gateway` | Receives `/process?session_id=<id>` and triggers routing |
| `router` | Forwards traffic to session worker pod |
| `session-operator` | Creates and deletes session pods dynamically |
| `pod-session-*` | Dynamic worker pods |
| `redis` | Tracks session routing metadata |