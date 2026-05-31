// Generates the README hero terminal image and the social-preview card as SVGs.
// Run with: node assets/gen_images.js  (then convert to PNG with rsvg-convert).
const fs = require("fs");
const path = require("path");
const OUT = __dirname;

const C = {
  bg: "#0d1117",
  bg2: "#161b22",
  border: "#30363d",
  fg: "#c9d1d9",
  gray: "#8b949e",
  cyan: "#79c0ff",
  green: "#3fb950",
  accent: "#58a6ff",
  yellow: "#e3b341",
};

const esc = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// ---------- Hero terminal (real `jp --help` surface) ----------
function hero() {
  const W = 760;
  const padX = 26;
  const top = 64; // below title bar
  const lh = 23;
  const fs = 14.5;
  const mono = "'SF Mono','Menlo','DejaVu Sans Mono',monospace";

  // [text, color] or [name, desc] for command rows
  const lines = [];
  const text = (s, color = C.fg) => lines.push({ type: "t", s, color });
  const cmd = (name, desc) => lines.push({ type: "c", name, desc });
  const blank = () => lines.push({ type: "b" });

  lines.push({ type: "prompt" });
  blank();
  text("usage: jp [-h] [-q] [--no-color] [-V] <command> ...", C.gray);
  blank();
  text("git-like safe sync between a local folder and a remote JupyterHub.");
  blank();
  text("commands:", C.gray);
  cmd("clone", "clone a remote Jupyter folder into a new local dir");
  cmd("init", "initialize a .jp workspace in the current directory");
  cmd("login", "save a named API-token credential");
  cmd("pull", "download remote changes  (deletes are opt-in)");
  cmd("push", "upload local changes     (deletes are opt-in)");
  cmd("status", "show local/remote sync status (read-only)");
  cmd("diff", "show unified diffs of changed files (read-only)");
  cmd("rm", "delete a path on the remote (gated)");
  cmd("terminal", "open the remote machine's shell in this terminal");
  cmd("doctor", "diagnose config, credentials and connectivity");

  const H = top + lines.length * lh + 22;
  const rows = lines
    .map((ln, i) => {
      const y = top + i * lh + fs;
      if (ln.type === "b") return "";
      if (ln.type === "prompt") {
        return `<text x="${padX}" y="${y}" font-family="${mono}" font-size="${fs}"><tspan fill="${C.green}">$ </tspan><tspan fill="${C.fg}" font-weight="600">jp --help</tspan></text>`;
      }
      if (ln.type === "t") {
        return `<text x="${padX}" y="${y}" font-family="${mono}" font-size="${fs}" fill="${ln.color}">${esc(ln.s)}</text>`;
      }
      // command row: padded name (cyan) + desc (gray)
      const name = ln.name.padEnd(9, " ");
      return `<text x="${padX}" y="${y}" font-family="${mono}" font-size="${fs}" xml:space="preserve"><tspan fill="${C.cyan}">  ${esc(name)}</tspan><tspan fill="${C.gray}">${esc(ln.desc)}</tspan></text>`;
    })
    .join("\n    ");

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
  <defs>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="8" stdDeviation="16" flood-color="#000" flood-opacity="0.35"/>
    </filter>
  </defs>
  <rect width="${W}" height="${H}" rx="12" fill="${C.bg}" stroke="${C.border}" stroke-width="1" filter="url(#shadow)"/>
  <rect width="${W}" height="40" rx="12" fill="${C.bg2}"/>
  <rect y="28" width="${W}" height="12" fill="${C.bg2}"/>
  <circle cx="22" cy="20" r="6" fill="#ff5f56"/>
  <circle cx="42" cy="20" r="6" fill="#ffbd2e"/>
  <circle cx="62" cy="20" r="6" fill="#27c93f"/>
  <text x="${W / 2}" y="25" text-anchor="middle" font-family="'SF Pro Text','Helvetica Neue',Arial,sans-serif" font-size="13" fill="${C.gray}">jp — git-like JupyterHub sync</text>
  <line x1="0" y1="40" x2="${W}" y2="40" stroke="${C.border}" stroke-width="1"/>
  ${rows}
</svg>`;
  fs_write("hero.svg", svg);
}

// ---------- Social preview (1280x640) ----------
function social() {
  const W = 1280;
  const H = 640;
  const sans = "'SF Pro Display','Helvetica Neue',Arial,sans-serif";
  const mono = "'SF Mono','Menlo','DejaVu Sans Mono',monospace";

  const chip = (x, label) =>
    `<g transform="translate(${x},470)">
      <rect width="${28 + label.length * 11}" height="40" rx="20" fill="${C.bg2}" stroke="${C.border}"/>
      <text x="${14 + (label.length * 11) / 2}" y="26" text-anchor="middle" font-family="${mono}" font-size="17" fill="${C.cyan}">${esc(label)}</text>
    </g>`;

  // command pills row
  const cmds = ["clone", "push", "pull", "status", "diff"];
  let px = 90;
  const pills = cmds
    .map((c2) => {
      const w = 30 + c2.length * 12;
      const g = `<g transform="translate(${px},388)">
        <rect width="${w}" height="38" rx="8" fill="#1f2630" stroke="${C.border}"/>
        <text x="${w / 2}" y="25" text-anchor="middle" font-family="${mono}" font-size="17" fill="${C.fg}">${esc("jp " + c2)}</text>
      </g>`;
      px += w + 14;
      return g;
    })
    .join("\n  ");

  const feats = ["Zero dependencies", "Safe by default", "Pure Python 3.9+"];
  let fx = 90;
  const featRow = feats
    .map((f, i) => {
      const g = `<g transform="translate(${fx},470)">
        <circle cx="6" cy="20" r="5" fill="${C.green}"/>
        <text x="20" y="26" font-family="${sans}" font-size="20" fill="${C.gray}">${esc(f)}</text>
      </g>`;
      fx += 26 + f.length * 11 + 40;
      return g;
    })
    .join("\n  ");

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0d1117"/>
      <stop offset="1" stop-color="#161b22"/>
    </linearGradient>
    <linearGradient id="title" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#58a6ff"/>
      <stop offset="1" stop-color="#79c0ff"/>
    </linearGradient>
  </defs>
  <rect width="${W}" height="${H}" fill="url(#bg)"/>
  <rect x="0" y="0" width="10" height="${H}" fill="${C.accent}"/>

  <text x="86" y="200" font-family="${mono}" font-size="120" font-weight="700" fill="url(#title)">jpsync</text>
  <text x="92" y="262" font-family="${sans}" font-size="34" fill="${C.fg}">A git-like CLI to sync local folders with a remote JupyterHub</text>
  <text x="92" y="312" font-family="${sans}" font-size="24" fill="${C.gray}">Edit on your laptop · run on the remote GPUs · pull results back — no SSH needed</text>

  ${pills}
  ${featRow}

  <text x="90" y="588" font-family="${mono}" font-size="24" fill="${C.gray}">$ <tspan fill="${C.green}">pipx install</tspan> <tspan fill="${C.fg}">jpsync</tspan></text>
  <text x="${W - 86}" y="588" text-anchor="end" font-family="${sans}" font-size="22" fill="${C.border}">github.com/pehqge/jpsync</text>
</svg>`;
  fs_write("social-preview.svg", svg);
}

function fs_write(name, content) {
  fs.writeFileSync(path.join(OUT, name), content);
  console.log("wrote", name);
}

hero();
social();
