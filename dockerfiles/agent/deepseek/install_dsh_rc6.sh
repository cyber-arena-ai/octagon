#!/bin/sh
set -eux

# npm's caret dependencies make a nominal rc.6 install resolve rc.8 children.
# Seed the dependency graph, then pin every rc.6 package that exists in the
# registry through npm overrides. Browser-only packages introduced after rc.6
# are left out of the headless closure.
rm -rf /tmp/dsh-rc6 /opt/dsh-rc6
mkdir -p /tmp/dsh-rc6 /opt/dsh-rc6

node <<'NODE'
const fs = require('fs');
const path = require('path');
const cp = require('child_process');
const root = '/usr/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai';
const names = new Set();
const extras = new Map();
function walk(dir) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) walk(p);
    else if (ent.name === 'package.json') {
      const pkg = JSON.parse(fs.readFileSync(p, 'utf8'));
      if (typeof pkg.name === 'string' && pkg.name.startsWith('@deepseek-ai/dsh-')) names.add(pkg.name);
      else if (typeof pkg.name === 'string' && pkg.name.startsWith('@deepseek-ai/')) extras.set(pkg.name, pkg.version);
    }
  }
}
walk(root);
const overrides = {};
const unavailable = [];
// The known-good image used the rc.6 CLI with an rc.8 plugin closure.  Pin
// every published dsh-* package to rc.8 when available; falling back to rc.6
// is allowed only for packages that have no rc.8 publication.
const compatibility = {};
// dsh-agent-tool-presentation rc.8 regresses native tool-call serialization
// (the emitted tool-call name becomes empty).  The known-good image retained
// this one package at rc.6, so keep that proven exception explicit.
const legacyRc6 = new Set(['@deepseek-ai/dsh-agent-tool-presentation']);
for (const name of [...names].sort()) {
  if (legacyRc6.has(name)) {
    overrides[name] = '0.1.0-rc.6';
    compatibility[name] = '0.1.0-rc.6';
    continue;
  }
  try {
    cp.execFileSync('npm', ['view', `${name}@0.1.0-rc.8`, 'version', '--json'], {
      stdio: ['ignore', 'pipe', 'ignore'],
    });
    compatibility[name] = '0.1.0-rc.8';
    overrides[name] = '0.1.0-rc.8';
  } catch (_) {
    try {
      cp.execFileSync('npm', ['view', `${name}@0.1.0-rc.6`, 'version', '--json'], {
        stdio: ['ignore', 'pipe', 'ignore'],
      });
      overrides[name] = '0.1.0-rc.6';
    } catch (_) {
      unavailable.push(name);
    }
  }
}
fs.writeFileSync('/tmp/dsh-rc6/package.json', JSON.stringify({
  private: true,
  dependencies: {
    '@deepseek-ai/dsh': '0.1.0-rc.6',
    ...Object.fromEntries(extras),
    ...compatibility,
  },
  overrides,
}, null, 2));
fs.writeFileSync('/tmp/dsh-rc6/unavailable.json', JSON.stringify(unavailable.sort()));
console.log(`DeepSeek rc.6 overrides: ${Object.keys(overrides).length}/${names.size}; unavailable=${unavailable.length}`);
NODE

cp /tmp/dsh-rc6/package.json /opt/dsh-rc6/package.json
cd /opt/dsh-rc6
npm install --legacy-peer-deps

# DeepSeek's OpenAI-compatible stream may send a valid tool name followed by
# an empty function.name delta.  dsh's assembler must preserve the earlier
# name; otherwise MCP calls become `unknown tool ""`.  Patch the vendored
# bundle at image build time so a clean image carries the contract without a
# runtime npm install.  The host manager repeats this idempotently for older
# cached images.
node <<'NODE'
const fs = require('fs');
const path = require('path');
const root = '/opt/dsh-rc6/node_modules';
const old = 'if (call.function?.name !== void 0) block.name = call.function.name;';
const replacement = 'if (call.function?.name) block.name = call.function.name;';
let matched = 0;
let patched = 0;
function walk(dir) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) walk(p);
    else if (p.endsWith('/dsh-llm-deepseek/lib/index.js')) {
      const text = fs.readFileSync(p, 'utf8');
      if (text.includes(old)) {
        fs.writeFileSync(p, text.replaceAll(old, replacement));
        patched += 1;
        matched += 1;
      } else if (text.includes(replacement)) {
        matched += 1;
      }
    }
  }
}
walk(root);
if (!matched) throw new Error('DeepSeek tool assembler bundle not found');
console.log(`DeepSeek tool assembler: matched=${matched} patched=${patched}`);
NODE

# Remove the seed install and expose only the exact-closure binary.
rm -rf /usr/lib/node_modules/@deepseek-ai/dsh /usr/bin/dsh
ln -s /opt/dsh-rc6/node_modules/.bin/dsh /usr/bin/dsh

node <<'NODE'
const fs = require('fs');
const path = require('path');
const root = '/opt/dsh-rc6/node_modules/@deepseek-ai';
const expected = '0.1.0-rc.8';
const expectedVersions = {
  '@deepseek-ai/dsh-agent-tool-presentation': '0.1.0-rc.6',
};
const allowed = new Set([
  ...JSON.parse(fs.readFileSync('/tmp/dsh-rc6/unavailable.json', 'utf8')),
  '@deepseek-ai/dsh-client-ui-primitives',
  '@deepseek-ai/dsh-client-ui-slots',
  '@deepseek-ai/dsh-client-web',
  ...Object.keys(JSON.parse(fs.readFileSync('/tmp/dsh-rc6/package.json', 'utf8')).dependencies)
    .filter(name => name.startsWith('@deepseek-ai/dsh-')),
]);
const seen = new Map();
function walk(dir) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) walk(p);
    else if (ent.name === 'package.json') {
      const pkg = JSON.parse(fs.readFileSync(p, 'utf8'));
      if (typeof pkg.name === 'string' && pkg.name.startsWith('@deepseek-ai/dsh-')) seen.set(pkg.name, pkg.version);
    }
  }
}
walk(root);
const bad = [...seen].filter(([name, version]) =>
  version !== (expectedVersions[name] || expected) && !allowed.has(name));
if (bad.length) throw new Error(`DeepSeek package closure drift: ${JSON.stringify(bad)}`);
if (!seen.size) throw new Error('DeepSeek package closure is empty');
const exceptions = Object.entries(expectedVersions)
  .map(([name, version]) => `${name}=${version}`).join(',');
console.log(`DeepSeek package closure: ${seen.size} packages; default=${expected}; exceptions=${exceptions}; unavailable=${[...allowed].join(',')}`);
NODE
