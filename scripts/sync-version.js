#!/usr/bin/env node
"use strict";
// Runs inside `npm version <patch|minor|major>` after package.json is bumped:
// copies the version into the Python package and stamps the CHANGELOG's "Unreleased" section.
const fs = require("fs");
const path = require("path");

const root = path.join(__dirname, "..");
const { version } = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));

const init = path.join(root, "lib", "glab_teleport", "__init__.py");
fs.writeFileSync(init, fs.readFileSync(init, "utf8").replace(/__version__ = "[^"]*"/, `__version__ = "${version}"`));

const log = path.join(root, "CHANGELOG.md");
let text = fs.readFileSync(log, "utf8");
const today = new Date().toISOString().slice(0, 10);
if (/^## \[Unreleased\]/m.test(text)) {
  text = text.replace(/^## \[Unreleased\].*$/m, `## [${version}] — ${today}`);
  fs.writeFileSync(log, text);
} else if (!text.includes(`## [${version}]`)) {
  console.warn(`! CHANGELOG.md has no "## [Unreleased]" or "## [${version}]" section — add release notes before publishing.`);
}
console.log(`✓ glab-teleport ${version}`);
