# 001: Pin the MCP Python SDK below version 2

Status: Accepted · Date: 2026-10-01

## Context
Version 2 of the MCP Python SDK renamed `FastMCP` to `MCPServer`, which broke the knowledge
base server and the gateway client on install.

## Decision
Pin `mcp<2` in `pyproject.toml` and keep the lock file committed, so every install uses the
same tested version.

## Consequences
- Builds are repeatable and the evaluation results can be reproduced.
- The project does not get MCP 2.x features or fixes until it migrates.
- Migration to 2.x is a planned task: update imports, re-run unit tests and the red-team evaluation.
