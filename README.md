# PMO - Process Manager Omni

A lightweight process manager inspired by PM2, but designed primarily for development environments.

## Features

- `start`, `stop`, and `restart` services, similar to PM2
- Simple YAML configuration with `extends` inheritance
- Real-time logs with highlight, optional merged stdout/stderr, optional timestamps
- Environment variable support with `${VAR}` / `${VAR:-default}` substitution
- Automatic `.env` file loading
- **Pipeline**: sequential task execution with `pmo pipeline` or YAML `pipeline:` field
- **Sweep**: parametric sweep via `pipeline_sweep:` — auto-generates sub-tasks from variable combinations
- Multi-machine support with hostname-specific directories (for shared NAS environments)

## Installation

```bash
pip install pmo
```

## Usage

### Quick Start

1. Create a `pmo.yml` file in your project:

```yaml
# Simple format, just like procfile
web-server: node server.js

# Detailed format
api-server:
  cmd: python api.py
  cwd: ./api
  env:
    NODE_ENV: development
```

2. Optional: Create a `.env` file for shared environment variables:

```
# This will apply to all services
DATABASE_URL=postgres://localhost:5432/mydb
DEBUG=true
```

3. Start your services:

```bash
pmo start all
```

4. List your services:

```bash
pmo ls
```

### Commands

```
pmo start    [all | service-name | service-id]
pmo stop     [all | service-name | service-id]
pmo restart  [all | service-name | service-id]
pmo logs     [all | service-name | service-id]
pmo flush    [all | service-name | service-id]
pmo status   [all | service-name | service-id]
pmo dry-run  [all | service-name | service-id]
pmo ls
pmo pipeline <task1> <task2> ... [--sleep N] [--flush] [--poll-interval N]
```

## Configuration

The `pmo.yml` file supports two formats:

1. **Simple**: `service-name: command`
2. **Detailed**:
   ```yaml
   service-name:
     cmd: command
     cwd: working directory (optional)
     env:
       KEY: value
   ```

### Service Options

| Field | Description |
|---|---|
| `cmd` | Command to run |
| `cwd` | Working directory (optional) |
| `env` | Environment variables (dict) |
| `extends` | Inherit config from another service |
| `merge_logs` | Merge stdout and stderr into one log file (default: false) |
| `log_with_timestamp` | Add timestamp to log filename (default: false) |
| `log_backup` | Backup old log files before starting (default: false) |

### Extends (Inheritance)

Services can inherit from a base service and override specific fields:

```yaml
base-server:
  cmd: python serve.py
  env:
    HOST: 0.0.0.0
    PORT: 8000

server-dev:
  extends: base-server
  env:
    PORT: 8001
    DEBUG: true
```

### Pipeline (Sequential Execution)

Run multiple tasks one after another. Each task is started, waited on until it
finishes, stopped (cleanup), then the next task starts.

**CLI usage:**

```bash
pmo pipeline task1 task2 task3 --sleep 10 --poll-interval 30
```

**YAML usage:**

```yaml
my_pipeline:
  pipeline: [task1, task2, task3]
  pipeline_sleep: 10          # seconds between tasks (default: 5)
  pipeline_flush: false       # flush logs before each task (default: false)
  pipeline_poll_interval: 30  # status poll interval in seconds (default: 10)
  merge_logs: true
  log_with_timestamp: true
```

Then simply: `pmo start my_pipeline`

> **Note:** `pipeline` tasks generate their own `cmd`. If the task definition
> already contains a `cmd` (directly or via `extends`), it is ignored with a
> warning. Other fields (`merge_logs`, `log_with_timestamp`, `env`, etc.) are
> kept normally.

### Pipeline Sweep (Parametric Sweep)

Automatically generate sub-tasks by sweeping over variable combinations.
Works with `extends` or standalone — as long as the task has a `cmd` (directly
or inherited).

**With extends:**

```yaml
serve_base:
  cmd: bash run.sh
  merge_logs: true
  log_with_timestamp: true
  env:
    TP: 1
    run_after_start: 1
    kill_after_run: 1

tp_sweep:
  extends: serve_base
  pipeline_sweep:
    TP: [1, 2, 4, 8]
  pipeline_sleep: 10
```

Sub-tasks are named after the extends target: `_serve_base__TP_1` ...
`_serve_base__TP_8`.

**Standalone (no extends):**

```yaml
tp_sweep:
  cmd: bash run.sh
  merge_logs: true
  log_with_timestamp: true
  env:
    run_after_start: 1
    kill_after_run: 1
  pipeline_sweep:
    TP: [1, 2, 4, 8]
  pipeline_sleep: 10
```

Sub-tasks are named after the task itself: `_tp_sweep__TP_1` ...
`_tp_sweep__TP_8`.

Each sub-task gets the corresponding env var override and a unique `exp_name`
for distinguishable log files.

**Multi-variable (cartesian product):**

```yaml
tp_async_sweep:
  extends: serve_base
  pipeline_sweep:
    TP: [1, 2, 4]
    async_sche: [0, 1]
  pipeline_sleep: 10
```

Generates 6 sub-tasks: `_serve_base__TP_1_async_sche_0`, ...,
`_serve_base__TP_4_async_sche_1`.

**Operations:**

```bash
pmo start tp_sweep    # run all combinations sequentially (background)
pmo logs tp_sweep     # view orchestrator progress
pmo ls                # see sweep + sub-task statuses
pmo stop tp_sweep     # interrupt (cleans up current sub-task)
```

### Multi-machine Support

PMO supports multiple machines sharing the same configuration through a shared filesystem (like NAS). Each machine stores its process information in a hostname-specific directory:

```
.pmo/
  hostname1/
    pids/
    logs/
  hostname2/
    pids/
    logs/
```

This allows processes on different machines to be managed separately even when sharing the same configuration files.

## Runtime Data

PMO manages runtime data in the `.pmo` directory with logs and PID files.

## License

MIT
