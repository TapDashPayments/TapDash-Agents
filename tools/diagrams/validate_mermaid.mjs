// Parse every ```mermaid block in the given markdown files; exit 1 if any fails.
//   npm ci --prefix tools/diagrams
//   node tools/diagrams/validate_mermaid.mjs $(git ls-files '*.md')
import { JSDOM } from "jsdom";
import fs from "node:fs";

const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.Element = dom.window.Element;
const { default: mermaid } = await import("mermaid");
mermaid.initialize({ startOnLoad: false });

let failed = 0;
let count = 0;
for (const file of process.argv.slice(2)) {
  const blocks = [...fs.readFileSync(file, "utf8").matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map((m) => m[1]);
  for (const [i, source] of blocks.entries()) {
    count++;
    try {
      const { diagramType } = await mermaid.parse(source);
      console.log(`ok    ${file} diagram ${i + 1} (${diagramType})`);
    } catch (error) {
      failed++;
      const message = String(error?.message ?? error).split("\n").slice(0, 4).join(" | ");
      console.log(`FAIL  ${file} diagram ${i + 1}: ${message}`);
    }
  }
}
console.log(`${count} diagram(s), ${failed} failed`);
process.exit(failed ? 1 : 0);
