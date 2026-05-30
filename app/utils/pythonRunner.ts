import { ChildProcess, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { Request, Response } from "express";

export const PYTHON_END_SENTINEL = "__END__";

export type PythonRunState = {
  active: boolean;
  child: ChildProcess | null;
  history: string[];
  subscribers: Set<(line: string) => void>;
};

function getScriptDir(): string {
  const fromEnv = process.env.SCRIPT_DIR;
  if (fromEnv && fromEnv.trim().length > 0) {
    return path.resolve(fromEnv.trim());
  }
  return path.resolve(process.cwd(), "scripts");
}

export function pythonCommand(): string {
  if (process.env.PYTHON_BIN && process.env.PYTHON_BIN.trim().length > 0) {
    return process.env.PYTHON_BIN.trim();
  }
  return process.platform === "win32" ? "python" : "python3";
}

export function createPythonState(): PythonRunState {
  return {
    active: false,
    child: null,
    history: [],
    subscribers: new Set(),
  };
}

export function broadcast(state: PythonRunState, line: string) {
  state.history.push(line);
  if (state.history.length > 5000) {
    state.history.splice(0, state.history.length - 5000);
  }
  for (const sub of state.subscribers) {
    try {
      sub(line);
    } catch {
      /* ignore */
    }
  }
}

export function finalize(state: PythonRunState, code: number) {
  if (!state.active && state.subscribers.size === 0) return;
  if (state.active) {
    broadcast(state, JSON.stringify({ type: "exit", code }));
  }
  state.active = false;
  state.child = null;
  const subs = Array.from(state.subscribers);
  state.subscribers.clear();
  for (const sub of subs) {
    try {
      sub(PYTHON_END_SENTINEL);
    } catch {
      /* ignore */
    }
  }
}

export function spawnPythonProcess(
  state: PythonRunState,
  scriptName: string,
  args: string[],
  onClose?: () => void,
): void {
  if (state.active) return;
  state.active = true;
  state.history = [];

  const scriptDir = getScriptDir();
  const scriptPath = path.join(scriptDir, scriptName);
  const python = pythonCommand();
  const fullArgs = ["-u", scriptPath, ...args];

  let child: ChildProcess;
  try {
    child = spawn(python, fullArgs, {
      cwd: scriptDir,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    broadcast(
      state,
      JSON.stringify({ type: "error", message: `Failed to spawn python: ${msg}` }),
    );
    finalize(state, 1);
    return;
  }
  state.child = child;

  let stdoutBuf = "";
  let stderrBuf = "";

  child.stdout?.on("data", (chunk: Buffer) => {
    stdoutBuf += chunk.toString("utf8");
    let nl = stdoutBuf.indexOf("\n");
    while (nl !== -1) {
      const line = stdoutBuf.slice(0, nl).trim();
      stdoutBuf = stdoutBuf.slice(nl + 1);
      if (line.length > 0) broadcast(state, line);
      nl = stdoutBuf.indexOf("\n");
    }
  });

  child.stderr?.on("data", (chunk: Buffer) => {
    stderrBuf += chunk.toString("utf8");
    let nl = stderrBuf.indexOf("\n");
    while (nl !== -1) {
      const line = stderrBuf.slice(0, nl).trim();
      stderrBuf = stderrBuf.slice(nl + 1);
      if (line.length > 0) {
        broadcast(
          state,
          JSON.stringify({ type: "log", level: "stderr", message: line }),
        );
      }
      nl = stderrBuf.indexOf("\n");
    }
  });

  child.on("error", (err) => {
    broadcast(
      state,
      JSON.stringify({
        type: "error",
        message: `Failed to spawn python: ${err.message}`,
      }),
    );
    finalize(state, 1);
  });

  child.on("close", (code) => {
    if (stdoutBuf.trim().length > 0) broadcast(state, stdoutBuf.trim());
    if (stderrBuf.trim().length > 0) {
      broadcast(
        state,
        JSON.stringify({
          type: "log",
          level: "stderr",
          message: stderrBuf.trim(),
        }),
      );
    }
    finalize(state, code ?? 0);
    onClose?.();
  });
}

export function writeFilterFile(payload: unknown): string | null {
  try {
    const tmp = path.join(
      os.tmpdir(),
      `ambeon-filter-${Date.now()}-${process.pid}.json`,
    );
    fs.writeFileSync(tmp, JSON.stringify(payload), "utf8");
    return tmp;
  } catch {
    return null;
  }
}

export function streamNdjson(state: PythonRunState, res: Response) {
  res.status(200);
  res.setHeader("Content-Type", "application/x-ndjson; charset=utf-8");
  res.setHeader("Cache-Control", "no-store, no-transform");
  res.setHeader("X-Accel-Buffering", "no");

  for (const line of state.history) {
    res.write(line + "\n");
  }

  if (!state.active) {
    res.end();
    return;
  }

  const send = (line: string) => {
    if (line === PYTHON_END_SENTINEL) {
      state.subscribers.delete(send);
      res.end();
      return;
    }
    try {
      res.write(line + "\n");
    } catch {
      state.subscribers.delete(send);
    }
  };

  state.subscribers.add(send);
  res.on("close", () => {
    state.subscribers.delete(send);
  });
}

export function killPythonChild(state: PythonRunState) {
  if (state.child) {
    try {
      state.child.kill();
    } catch {
      /* ignore */
    }
  }
}
