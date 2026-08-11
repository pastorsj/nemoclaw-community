// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
// Generate the immutable Hermes configuration baked into the sandbox image.

import { chmodSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

function main(): void {
  const model = process.env.NEMOCLAW_MODEL;
  const baseUrl = process.env.NEMOCLAW_INFERENCE_BASE_URL;
  if (!model || !baseUrl) {
    throw new Error("NEMOCLAW_MODEL and NEMOCLAW_INFERENCE_BASE_URL are required");
  }

  const config: Record<string, unknown> = {
    _config_version: 34,
    model: {
      default: model,
      provider: "custom",
      base_url: baseUrl,
    },
    terminal: {
      backend: "local",
      cwd: "/sandbox",
      timeout: 180,
    },
    agent: {
      max_turns: 30,
      reasoning_effort: "medium",
      verify_on_stop: false,
    },
    memory: {
      memory_enabled: true,
      user_profile_enabled: true,
    },
    skills: {
      creation_nudge_interval: 15,
    },
    display: {
      compact: false,
      tool_progress: "all",
      interim_assistant_messages: false,
    },
    approvals: {
      mode: "off",
      timeout: 60,
    },
    platforms: {
      api_server: {
        enabled: true,
        extra: {
          host: "127.0.0.1",
          port: 18642,
        },
      },
    },
    // Hermes owns Relay's provider, tool, and session lifecycle in-process.
    plugins: {
      enabled: ["observability/nemo_relay"],
    },
  };

  const configPath = join(homedir(), ".hermes", "config.yaml");
  writeFileSync(configPath, toYaml(config));
  chmodSync(configPath, 0o600);

  // This token protects the API inside the sandbox. The host-side OpenShell
  // forward is loopback-only; no public proxy is part of this recipe.
  const envPath = join(homedir(), ".hermes", ".env");
  writeFileSync(
    envPath,
    [
      "API_SERVER_HOST=127.0.0.1",
      "API_SERVER_PORT=18642",
      "API_SERVER_KEY=nemoclaw-internal",
    ].join("\n") + "\n",
  );
  chmodSync(envPath, 0o600);

  console.log(`[config] Wrote ${configPath} (model=${model})`);
}

function toYaml(obj: Record<string, unknown>, indent = 0): string {
  const pad = "  ".repeat(indent);
  let output = "";
  for (const [key, value] of Object.entries(obj)) {
    if (value === null || value === undefined) {
      output += `${pad}${key}: null\n`;
    } else if (Array.isArray(value)) {
      if (value.length === 0) {
        output += `${pad}${key}: []\n`;
      } else {
        output += `${pad}${key}:\n`;
        for (const item of value) {
          if (typeof item === "string") {
            output += `${pad}  - ${yamlString(item)}\n`;
          } else if (typeof item === "object" && item !== null) {
            output += `${pad}  -\n`;
            output += toYaml(item as Record<string, unknown>, indent + 2);
          } else {
            output += `${pad}  - ${String(item)}\n`;
          }
        }
      }
    } else if (typeof value === "object") {
      output += `${pad}${key}:\n`;
      output += toYaml(value as Record<string, unknown>, indent + 1);
    } else if (typeof value === "string") {
      output += `${pad}${key}: ${yamlString(value)}\n`;
    } else {
      output += `${pad}${key}: ${String(value)}\n`;
    }
  }
  return output;
}

function yamlString(value: string): string {
  if (/[:{}\[\],&*?|>!%@`#'\"]/.test(value) || value.includes("\n") || value.trim() !== value) {
    return JSON.stringify(value);
  }
  return value;
}

main();
