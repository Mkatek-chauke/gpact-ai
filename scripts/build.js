const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const root = path.resolve(__dirname, "..");
const requiredFiles = [
  "app.py",
  "storage.py",
  "govza.py",
  "rag.py",
  "neon.ts",
  "vercel.json",
  "templates/index.html",
  "public/static/app.js",
  "public/static/styles.css",
  "data/documents.json",
];

for (const file of requiredFiles) {
  if (!fs.existsSync(path.join(root, file))) {
    console.error(`Build failed: required file is missing: ${file}`);
    process.exit(1);
  }
}

const corpus = JSON.parse(fs.readFileSync(path.join(root, "data/documents.json"), "utf8"));
if (!Array.isArray(corpus) || corpus.length === 0) {
  console.error("Build failed: the local bootstrap corpus is empty or invalid.");
  process.exit(1);
}

const js = spawnSync(process.execPath, ["--check", path.join(root, "public/static/app.js")], {
  encoding: "utf8",
});
if (js.status !== 0) {
  process.stderr.write(js.stderr);
  process.exit(js.status || 1);
}

console.log(`Build checks passed; ${corpus.length} bootstrap documents are available.`);
