// gussy_ctl.mjs - Gussy's agent's control panel for the Gussy fleet on the Bull4Life Command Center (CC).
// Built by engine 2026-10-04 (admin's order). Node 18+, no packages. Your CC identity is `gussy`.
//
// Token: one line `CC_AGENT_TOKEN=<your controller token>` in ~/.b4l/gussy_cc.env (create it yourself; never paste the
// token into a chat, a log, a commit or a file inside this repo: this repo is PUBLIC).
//
//   node tools/gussy_ctl.mjs whoami                    checks the token and the 20 goals (read-only)
//   node tools/gussy_ctl.mjs fleet                     every worker: open rows, done rows, the row it is on
//   node tools/gussy_ctl.mjs rows <NN>                 one worker's rows (NN = 01..20)
//   node tools/gussy_ctl.mjs add <NN|auto> "<rid> <task text>"       append a todo row (auto = the emptiest worker)
//   node tools/gussy_ctl.mjs addfirst <NN> "<rid> <task text>"       insert a row ahead of the open ones
//   node tools/gussy_ctl.mjs close <NN> <rid> "<note>"               close one open row by hand
//   node tools/gussy_ctl.mjs card <NN> <rid>           read the card a worker published for a row
//   node tools/gussy_ctl.mjs inbox [since_seq]          your DMs (dm:gussy)
//   node tools/gussy_ctl.mjs say <agent> "<text>"      DM an agent (admin or engine); 200 OK = delivered
//
// Row title rules (each from a real incident in engine's fleet):
//   - the title STARTS with its row id, e.g. `g001 [research] funding-rate skew as a crowding signal on 3m bars`;
//     a tag first made the worker loop for 4 hours.
//   - the row id is letters, digits, - or _, and unique among that worker's open rows.
//   - one row = one research card. Workers take the FIRST open row; a row in progress shows as `doing`.
import fs from 'node:fs';
import os from 'node:os';

const BASE = 'https://bull4life.com/cc';
const ME = 'gussy';
const ENVF = os.homedir() + '/.b4l/gussy_cc.env';
const N = 20;
const RID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/;

const token = () => {
  let t = '';
  try { t = (fs.readFileSync(ENVF, 'utf8').match(/^CC_AGENT_TOKEN=(.+)$/m) || [])[1] || ''; } catch { /* missing */ }
  if (!t.trim()) { console.error(`no token: put one line CC_AGENT_TOKEN=<token> in ${ENVF}`); process.exit(2); }
  return t.trim();
};
const H = () => ({ Authorization: 'Bearer ' + token(), 'X-CC-Agent': ME, 'Content-Type': 'application/json' });
const api = async (p, body) => {
  const r = await fetch(BASE + p, body ? { method: 'POST', headers: H(), body: JSON.stringify(body) } : { headers: H() });
  const t = await r.text();
  if (!r.ok) throw new Error(`${p.split('?')[0]} -> HTTP ${r.status} ${t.slice(0, 200)}`);
  return JSON.parse(t);
};
const worker = (nn) => {
  const n = Number(nn);
  if (!Number.isInteger(n) || n < 1 || n > N) throw new Error(`worker number must be 01..${N}, got ${nn}`);
  return `gussy_spacebunny${String(n).padStart(2, '0')}`;
};
const goal = async (w) => {
  const gid = 'g_' + w;
  return (await api('/api/goals?id=' + encodeURIComponent(gid))).goals?.find((g) => g.id === gid) ?? null;
};
const open = (g) => (g?.milestones ?? []).filter((m) => m.status === 'todo' || m.status === 'doing');
// POST /api/goal REPLACES the whole milestone list: always send the full list back, then re-read the count.
const save = async (g, ms) => {
  const body = { id: g.id, progress: Number(g.progress || 0), milestones: ms };
  for (const k of ['title', 'owner', 'target', 'status']) if (g[k]) body[k] = g[k];
  return api('/api/goal', body);
};
const titleProblem = (title, openTitles) => {
  const t = (title || '').trim();
  if (!t) return 'empty title';
  const rid = t.split(/\s+/)[0];
  if (!RID.test(rid)) return `title must START with its row id (letters/digits/-/_), got first word "${rid}"`;
  if (t.split(/\s+/).length < 2) return 'title has an id but no task text';
  if (openTitles.some((o) => (o || '').split(/\s+/)[0] === rid)) return `row id ${rid} is already open on this worker`;
  return null;
};

const [cmd, a1, a2, ...rest] = process.argv.slice(2);
try {
  if (cmd === 'whoami' || cmd === 'fleet') {
    let missing = 0;
    for (let i = 1; i <= N; i++) {
      const w = worker(i);
      const g = await goal(w);
      if (!g) { missing++; console.log(`${w}  GOAL MISSING (g_${w})`); continue; }
      const ms = g.milestones ?? [];
      const doing = ms.find((m) => m.status === 'doing');
      const line = `${w}  open ${open(g).length}  done ${ms.filter((m) => m.status === 'done').length}  blocked ${ms.filter((m) => m.status === 'blocked').length}`;
      console.log(cmd === 'fleet' ? `${line}${doing ? '  NOW: ' + String(doing.title).slice(0, 90) : ''}` : line);
    }
    console.log(missing ? `${missing} of ${N} goals missing: ask engine` : `token OK as ${ME}; ${N} of ${N} goals readable`);
  } else if (cmd === 'rows') {
    const g = await goal(worker(a1));
    if (!g) throw new Error('goal missing');
    for (const m of g.milestones ?? []) console.log(`[${m.status}] ${String(m.title).slice(0, 220)}`);
  } else if (cmd === 'add' || cmd === 'addfirst') {
    const title = (a2 || '').trim();
    let w;
    if (cmd === 'add' && a1 === 'auto') {
      let best = null;
      for (let i = 1; i <= N; i++) {
        const g = await goal(worker(i));
        if (g && (!best || open(g).length < best.n)) best = { w: worker(i), n: open(g).length };
      }
      if (!best) throw new Error('no goal readable');
      w = best.w;
    } else w = worker(a1);
    const g = await goal(w);
    if (!g) throw new Error(`goal g_${w} missing: ask engine`);
    const ms = [...(g.milestones ?? [])];
    const bad = titleProblem(title, open(g).map((m) => m.title));
    if (bad) throw new Error('refused: ' + bad);
    const row = { title, status: 'todo' };
    const n0 = ms.length;
    if (cmd === 'addfirst') {
      const i = ms.findIndex((m) => m.status === 'todo' || m.status === 'doing');
      ms.splice(i < 0 ? ms.length : i, 0, row);
    } else ms.push(row);
    await save(g, ms);
    const n1 = ((await goal(w))?.milestones ?? []).length;
    console.log(`${w}: rows ${n0} -> ${n1} ${n1 === n0 + 1 ? 'OK' : 'MISMATCH (re-run rows to check)'}`);
    if (n1 !== n0 + 1) process.exit(1);
  } else if (cmd === 'close') {
    const w = worker(a1);
    const g = await goal(w);
    const ms = [...(g?.milestones ?? [])];
    const hit = ms.filter((m) => String(m.title).split(/\s+/)[0] === a2 && (m.status === 'todo' || m.status === 'doing'));
    if (hit.length !== 1) throw new Error(`${hit.length} open rows have id ${a2}: nothing changed`);
    hit[0].status = 'done';
    hit[0].title = `${hit[0].title} || ${rest.join(' ') || 'closed by gussy'}`;
    await save(g, ms);
    console.log(`${w} ${a2} closed`);
  } else if (cmd === 'card') {
    const w = worker(a1);
    const r = await api('/api/artifacts/read?rel=' + encodeURIComponent(`${w}/current/research/${String(a2).toLowerCase()}.md`));
    console.log(r.body ?? '(empty)');
  } else if (cmd === 'inbox') {
    const since = Number(a1 || 0);
    const r = await api(`/api/messages?channel=${encodeURIComponent('dm:' + ME)}&since=${since}`);
    for (const m of r.messages ?? r ?? []) console.log(`[${m.seq ?? '?'} ${m.ts ?? m.at ?? ''} ${m.from ?? m.agent ?? '?'}] ${m.body}`);
  } else if (cmd === 'say') {
    if (!a1 || !a2) throw new Error('usage: say <agent> "<text>"');
    const r = await api('/api/message', { channel: 'dm:' + a1, body: [a2, ...rest].join(' ') });
    console.log(`-> dm:${a1}: 200 OK ${JSON.stringify(r).slice(0, 80)}`);
  } else {
    console.log(fs.readFileSync(new URL(import.meta.url)).toString().split('\n').filter((l) => l.startsWith('//')).map((l) => l.slice(3)).join('\n'));
  }
} catch (e) {
  console.error('gussy_ctl: ' + e.message);
  process.exit(1);
}
