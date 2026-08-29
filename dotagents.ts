#!/usr/bin/env node
// dotagents — pick skills from ./skills and symlink them into ~/.agents/skills

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

// DOTAGENTS_HOME allows redirecting the .agents root (mainly for testing).
const AGENTS_SKILLS_DIR = path.join(
  process.env.DOTAGENTS_HOME ?? os.homedir(),
  ".agents",
  "skills",
);

const BOLD = "\x1b[1m";
const DIM = "\x1b[2m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const CYAN = "\x1b[36m";
const RESET = "\x1b[0m";
const HIDE_CURSOR = "\x1b[?25l";
const SHOW_CURSOR = "\x1b[?25h";
let cursorHidden = false;

type LinkState = "linked" | "linked-elsewhere" | "conflict" | "missing";

interface SkillEntry {
  name: string;
  source: string;
  state: LinkState;
  checked: boolean;
}

function safeRealpath(p: string): string {
  try {
    return fs.realpathSync(p);
  } catch {
    return path.resolve(p);
  }
}

// Only direct children of the skills directory count as skills; no recursion.
function isSkillDir(dir: string): boolean {
  try {
    return fs.statSync(path.join(dir, "SKILL.md")).isFile();
  } catch {
    return false;
  }
}

function linkState(name: string, source: string): LinkState {
  const linkPath = path.join(AGENTS_SKILLS_DIR, name);
  let st: fs.Stats;
  try {
    st = fs.lstatSync(linkPath);
  } catch {
    return "missing";
  }
  if (!st.isSymbolicLink()) return "conflict";
  const raw = fs.readlinkSync(linkPath);
  const target = path.isAbsolute(raw) ? raw : path.resolve(path.dirname(linkPath), raw);
  return safeRealpath(target) === safeRealpath(source) ? "linked" : "linked-elsewhere";
}

function listSkills(skillsDir: string): SkillEntry[] {
  const dirents = fs.readdirSync(skillsDir, { withFileTypes: true });
  return dirents
    .map((d) => path.join(skillsDir, d.name))
    .filter(isSkillDir)
    .sort()
    .map((dir) => {
      const name = path.basename(dir);
      const state = linkState(name, dir);
      // Conflicts start checked so confirming surfaces an explicit skip warning.
      return { name, source: dir, state, checked: state === "linked" || state === "conflict" };
    });
}

function stateHint(state: LinkState): string {
  switch (state) {
    case "linked":
      return `  ${DIM}(linked)${RESET}`;
    case "linked-elsewhere":
      return `  ${YELLOW}(points elsewhere)${RESET}`;
    case "conflict":
      return `  ${RED}(exists, not a symlink)${RESET}`;
    default:
      return "";
  }
}

type Key = "up" | "down" | "space" | "toggle-all" | "enter" | "cancel" | "ctrl-c";

function parseKeys(chunk: Buffer): Key[] {
  const s = chunk.toString("utf8");
  const keys: Key[] = [];
  let i = 0;
  while (i < s.length) {
    if (s[i] === "\x1b") {
      const seq = s.slice(i, i + 3);
      if (seq === "\x1b[A" || seq === "\x1bOA") { keys.push("up"); i += 3; continue; }
      if (seq === "\x1b[B" || seq === "\x1bOB") { keys.push("down"); i += 3; continue; }
      keys.push("cancel"); // bare escape
      i += 1;
      continue;
    }
    switch (s[i]) {
      case "\r":
      case "\n":
        keys.push("enter"); break;
      case " ": keys.push("space"); break;
      case "\x03": keys.push("ctrl-c"); break;
      case "j": keys.push("down"); break;
      case "k": keys.push("up"); break;
      case "a": keys.push("toggle-all"); break;
      case "q": keys.push("cancel"); break;
    }
    i += 1;
  }
  return keys;
}

function promptSkills(entries: SkillEntry[]): Promise<SkillEntry[] | null> {
  const stdin = process.stdin;
  const stdout = process.stdout;

  return new Promise((resolve) => {
    let cursor = 0;
    let drawn = 0;
    let done = false;

    const render = () => {
      const selected = entries.filter((e) => e.checked).length;
      const lines = [
        `${BOLD}?${RESET} Select skills to symlink ${DIM}(${selected}/${entries.length} selected)${RESET}`,
      ];
      entries.forEach((entry, i) => {
        const pointer = i === cursor ? `${CYAN}❯${RESET} ` : "  ";
        const box = entry.checked ? `${GREEN}◉${RESET}` : `${DIM}◯${RESET}`;
        lines.push(`${pointer}${box} ${entry.name}${stateHint(entry.state)}`);
      });
      lines.push("");
      lines.push(`${DIM}  ↑/↓ move · space toggle · a all/none · enter confirm · esc cancel${RESET}`);

      if (drawn > 0) stdout.write(`\x1b[${drawn}A`);
      for (const line of lines) stdout.write(`\x1b[2K${line}\n`);
      drawn = lines.length;
    };

    const clearDrawn = () => {
      if (drawn > 0) {
        stdout.write(`\x1b[${drawn}A`);
        for (let i = 0; i < drawn; i++) stdout.write("\x1b[2K\n");
        stdout.write(`\x1b[${drawn}A`);
      }
      drawn = 0;
    };

    function onData(chunk: Buffer) {
      for (const key of parseKeys(chunk)) {
        if (done) return;
        switch (key) {
          case "up":
            cursor = Math.max(0, cursor - 1);
            break;
          case "down":
            cursor = Math.min(entries.length - 1, cursor + 1);
            break;
          case "space":
            entries[cursor].checked = !entries[cursor].checked;
            break;
          case "toggle-all": {
            const anyUnchecked = entries.some((e) => !e.checked);
            for (const e of entries) e.checked = anyUnchecked;
            break;
          }
          case "enter":
            finish(entries.filter((e) => e.checked), false);
            return;
          case "cancel":
          case "ctrl-c":
            finish(null, true);
            return;
        }
      }
      render();
    }

    const finish = (value: SkillEntry[] | null, clear: boolean) => {
      if (done) return;
      done = true;
      if (clear) clearDrawn();
      stdin.setRawMode(false);
      stdin.pause();
      stdin.off("data", onData);
      stdout.write(SHOW_CURSOR);
      cursorHidden = false;
      resolve(value);
    };

    stdin.setRawMode(true);
    stdin.resume();
    stdin.on("data", onData);
    stdout.write(HIDE_CURSOR);
    cursorHidden = true;
    render();
  });
}

interface Outcome {
  linked: string[];
  unlinked: string[];
  skipped: string[];
}

// Applies the full desired state: checked entries get a link, unchecked
// entries that currently point into this skills dir get unlinked.
function applySelection(entries: SkillEntry[]): Outcome {
  const outcome: Outcome = { linked: [], unlinked: [], skipped: [] };
  const needsDir = entries.some(
    (e) => e.checked && (e.state === "missing" || e.state === "linked-elsewhere"),
  );
  if (needsDir) fs.mkdirSync(AGENTS_SKILLS_DIR, { recursive: true });

  for (const entry of entries) {
    const linkPath = path.join(AGENTS_SKILLS_DIR, entry.name);
    try {
      if (entry.checked) {
        if (entry.state === "linked") continue;
        if (entry.state === "conflict") {
          outcome.skipped.push(`skipped ${entry.name}: ${linkPath} exists and is not a symlink`);
          continue;
        }
        if (entry.state === "linked-elsewhere") fs.unlinkSync(linkPath);
        fs.symlinkSync(safeRealpath(entry.source), linkPath);
        outcome.linked.push(entry.name);
      } else if (entry.state === "linked") {
        fs.unlinkSync(linkPath);
        outcome.unlinked.push(entry.name);
      }
    } catch (err) {
      outcome.skipped.push(`failed ${entry.name}: ${(err as Error).message}`);
    }
  }
  return outcome;
}

const HELP = `dotagents — manage agent skills via symlinks

Usage:
  dotagents skills [--dir <path>]

Commands:
  skills        Interactively choose which skills from the skills directory
                should be symlinked into ${AGENTS_SKILLS_DIR}.
                Linked skills are preselected; deselecting one removes its link.

Options:
  --dir <path>  Skills directory to scan (default: ./skills)
  -h, --help    Show this help

Only direct subdirectories of the skills directory containing a SKILL.md
are considered.

Environment:
  DOTAGENTS_HOME  Override the home directory that contains .agents
`;

async function skillsCmd(rest: string[]): Promise<number> {
  let dir = path.resolve("skills");
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (arg === "--dir") {
      const value = rest[++i];
      if (!value) {
        console.error("error: --dir requires a path");
        return 1;
      }
      dir = path.resolve(value);
    } else if (arg === "-h" || arg === "--help") {
      console.log(HELP);
      return 0;
    } else {
      console.error(`error: unknown option '${arg}'`);
      return 1;
    }
  }

  if (!process.stdin.isTTY) {
    console.error("error: `dotagents skills` needs an interactive terminal (TTY)");
    return 1;
  }

  let entries: SkillEntry[];
  try {
    entries = listSkills(dir);
  } catch {
    console.error(`error: cannot read skills directory ${dir}`);
    return 1;
  }
  if (entries.length === 0) {
    console.error(`error: no skills found in ${dir}`);
    console.error("       (expected direct subdirectories containing a SKILL.md)");
    return 1;
  }

  console.log(`${DIM}skills dir : ${dir}${RESET}`);
  console.log(`${DIM}link target: ${AGENTS_SKILLS_DIR}${RESET}`);

  const selected = await promptSkills(entries);
  if (!selected) {
    console.log("Cancelled, nothing changed.");
    return 0;
  }

  const outcome = applySelection(entries);
  for (const name of outcome.linked) {
    console.log(
      `  ${GREEN}✓ linked${RESET} ${BOLD}${name}${RESET} ${DIM}→ ${path.join(AGENTS_SKILLS_DIR, name)}${RESET}`,
    );
  }
  for (const name of outcome.unlinked) {
    console.log(`  ${YELLOW}- unlinked${RESET} ${name}`);
  }
  for (const msg of outcome.skipped) {
    console.log(`  ${RED}! ${msg}${RESET}`);
  }
  if (outcome.linked.length + outcome.unlinked.length + outcome.skipped.length === 0) {
    console.log("Nothing to do, links are already up to date.");
  }
  return 0;
}

function showHelp(): number {
  console.log(HELP);
  return 0;
}

async function main(): Promise<number> {
  const [cmd, ...rest] = process.argv.slice(2);
  switch (cmd) {
    case undefined:
    case "help":
    case "-h":
    case "--help":
      return showHelp();
    case "skills":
      return skillsCmd(rest);
    default:
      console.error(`error: unknown command '${cmd}'`);
      console.error("try 'dotagents --help'");
      return 1;
  }
}

process.on("exit", () => {
  if (cursorHidden) process.stdout.write(SHOW_CURSOR);
});

main().then(
  (code) => process.exit(code),
  (err) => {
    console.error(err);
    process.exit(1);
  },
);
