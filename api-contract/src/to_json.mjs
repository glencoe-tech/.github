// Read a YAML or JSON document (a path, or "-" for stdin) and print it as JSON.
//
// One parser for every document the action reads (contracts and policy), so the contract that is
// validated, linted and compared is the same parse. Fails on anything ambiguous: a syntax error,
// a duplicate key, more than one document in the stream, or an alias bomb.
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";

// The parser comes from the pinned toolchain install (install-toolchain.sh), named by the caller.
const toolchain = process.env.API_CONTRACT_TOOLCHAIN;
if (!toolchain) {
  process.stderr.write("API_CONTRACT_TOOLCHAIN is not set\n");
  process.exit(2);
}
const { parseAllDocuments } = createRequire(join(toolchain, "package.json"))("yaml");

const source = process.argv[2];
if (!source) {
  process.stderr.write("usage: to_json.mjs <file|->\n");
  process.exit(2);
}
const text = readFileSync(source === "-" ? 0 : source, "utf8");
const docs = parseAllDocuments(text, { uniqueKeys: true, prettyErrors: true });
if (!Array.isArray(docs) || docs.length !== 1) {
  process.stderr.write(`expected exactly one document, found ${Array.isArray(docs) ? docs.length : 0}\n`);
  process.exit(1);
}
const [doc] = docs;
if (doc.errors.length > 0) {
  for (const error of doc.errors) process.stderr.write(`${error.message}\n`);
  process.exit(1);
}
process.stdout.write(JSON.stringify(doc.toJS({ maxAliasCount: 100 })));
