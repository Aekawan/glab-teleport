#!/usr/bin/env node
"use strict";
// npm launcher: finds Python >= 3.9 and runs the glab_teleport package with the user's terminal attached.
const { spawnSync } = require("child_process");
const path = require("path");

const lib = path.join(__dirname, "..", "lib");
const candidates = process.env.GLAB_TELEPORT_PYTHON
  ? [process.env.GLAB_TELEPORT_PYTHON]
  : ["python3", "/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "python"];

const python = candidates.find((py) => {
  const r = spawnSync(py, ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"], { stdio: "ignore" });
  return r.status === 0;
});

if (!python) {
  console.error("glab-teleport requires Python 3.9 or newer.");
  console.error("  macOS:  xcode-select --install   (or: brew install python)");
  console.error("  Linux:  install python3 with your package manager");
  console.error("  Custom: GLAB_TELEPORT_PYTHON=/path/to/python3 glab-teleport");
  process.exit(1);
}

process.on("SIGINT", () => {}); // let Python handle Ctrl-C and restore the terminal
const env = { ...process.env, PYTHONPATH: [lib, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter), PYTHONDONTWRITEBYTECODE: "1" };
const r = spawnSync(python, ["-m", "glab_teleport", ...process.argv.slice(2)], { stdio: "inherit", env });
if (r.error) {
  console.error(`Could not start ${python}: ${r.error.message}`);
  process.exit(1);
}
process.exit(r.status === null ? 1 : r.status);
