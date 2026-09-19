#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { resolve, join, dirname } from 'node:path';
import { homedir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const USAGE = `Usage:
  npx graphitect skill export [--host codex|claude-code] [--global] [--output SKILL_DIRECTORY] [--force]
  npx graphitect build <path> [-o report.html]
  npx graphitect --help
`;

function exportSkill(args) {
  let host = null;
  let output = null;
  let isGlobal = false;
  let force = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--host') {
      host = args[++i];
    } else if (args[i] === '--output') {
      output = args[++i];
    } else if (args[i] === '--global') {
      isGlobal = true;
    } else if (args[i] === '--force') {
      force = true;
    }
  }

  let destination = null;
  if (output) {
    destination = resolve(process.cwd(), output);
  } else if (host === 'codex') {
    destination = isGlobal
      ? join(homedir(), '.agents', 'skills', 'graphitect')
      : resolve(process.cwd(), '.agents', 'skills', 'graphitect');
  } else if (host === 'claude-code') {
    destination = isGlobal
      ? join(homedir(), '.claude', 'skills', 'graphitect')
      : resolve(process.cwd(), '.claude', 'skills', 'graphitect');
  } else {
    console.error('error: --host (codex|claude-code) or --output is required');
    process.exit(2);
  }

  if (existsSync(destination) && !force) {
    console.error(`error: skill destination already exists: ${destination} (use --force to replace it)`);
    process.exit(2);
  }

  const skillRoot = resolve(__dirname, '..', 'graphitect', 'skill');
  const skillMdPath = join(skillRoot, 'SKILL.md');
  const openaiYamlPath = join(skillRoot, 'agents', 'openai.yaml');

  if (!existsSync(skillMdPath)) {
    console.error(`error: internal skill template missing at ${skillMdPath}`);
    process.exit(1);
  }

  mkdirSync(join(destination, 'agents'), { recursive: true });
  writeFileSync(join(destination, 'SKILL.md'), readFileSync(skillMdPath, 'utf8'), 'utf8');
  if (existsSync(openaiYamlPath)) {
    writeFileSync(join(destination, 'agents', 'openai.yaml'), readFileSync(openaiYamlPath, 'utf8'), 'utf8');
  }

  console.log(`Exported Graphitect skill to ${destination}`);
  process.exit(0);
}

const args = process.argv.slice(2);

if (args.length === 0 || args.includes('--help') || args.includes('-h')) {
  console.log(USAGE);
  process.exit(0);
}

if (args[0] === 'skill' && args[1] === 'export') {
  exportSkill(args.slice(2));
} else {
  const child = spawn('graphitect', args, { stdio: 'inherit', shell: true });
  child.on('error', () => {
    console.error('Graphitect Python CLI not found on PATH. Install it with: pip install graphitect (or uv tool install graphitect)');
    process.exit(1);
  });
  child.on('close', (code) => {
    process.exit(code ?? 0);
  });
}
