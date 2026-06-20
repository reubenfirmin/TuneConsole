// Copy the pinned front-end libs from node_modules into the committed vendor dir.
// Run via `npm run sync-assets` after `npm install`/`npm update`. The destination files are
// checked into git so the app (and `uv build`) never need Node; npm is only for upgrades.
import { copyFileSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
const destDir = resolve(root, "src/yt_playlist/web/static/vendor");
const fontDir = resolve(destDir, "fonts");

// source (relative to node_modules) -> destination filename
const ASSETS = [
  ["htmx.org/dist/htmx.min.js", "htmx.min.js"],
  ["alpinejs/dist/cdn.min.js", "alpine.min.js"],
];

// self-hosted variable fonts (latin, weight axis) -> static/vendor/fonts/<dest>
const FONTS = [
  ["@fontsource-variable/fraunces/files/fraunces-latin-wght-normal.woff2", "fraunces.woff2"],
  ["@fontsource-variable/hanken-grotesk/files/hanken-grotesk-latin-wght-normal.woff2", "hanken-grotesk.woff2"],
  ["@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2", "jetbrains-mono.woff2"],
];

mkdirSync(destDir, { recursive: true });
mkdirSync(fontDir, { recursive: true });
for (const [src, name] of ASSETS) {
  const lib = src.split("/")[0];
  copyFileSync(resolve(root, "node_modules", src), resolve(destDir, name));
  console.log(`synced ${name}  <-  ${lib}@${pkg.dependencies[lib]}`);
}
for (const [src, name] of FONTS) {
  const lib = src.split("/").slice(0, 2).join("/");
  copyFileSync(resolve(root, "node_modules", src), resolve(fontDir, name));
  console.log(`synced fonts/${name}  <-  ${lib}@${pkg.dependencies[lib]}`);
}
