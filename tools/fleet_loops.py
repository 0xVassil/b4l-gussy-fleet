#!/usr/bin/env python3
"""fleet_loops.py: list, pause, resume, set the loop time of, or stop the scheduled compute loops of the fleets (ENG-73).

  list                               every scheduled loop of every fleet in the local config (the loop record, below)
  pause  <id|fleet> [--stop-running] disable the loop(s); with --stop-running also cancel the runs at work
  resume <id|fleet>                  enable the loop(s) again
  every  <id|fleet> <minutes>        set the loop time of the fleet: the file .github/loop_every_min (60 to 1440, whole
                                     hours; missing or bad = 360). The file is one number for the whole fleet.
  stop   <id>                        cancel the runs of one loop that are at work (each run id printed)

A loop id is "<account letter>/<repo name>/<workflow file>", for example "A/<repo name>/donate.yml". A fleet name is
the key in the local config. The old form "<file> --fleet <name>" still works for one workflow.

Options: --by <name> (who, in the change log; default engine_helper); --ref <branch> (read and write this branch, for a
test); --json (one JSON object on stdout; the default prints the same, indented).
Exit codes: 0 done; 1 refused (the output is {"error": "..."}); 2 it failed.

Every write goes to the change log (one JSON line: who, what, before, after). Fleet owner/name, the account letter and the
write token's file live in a local config file, not in this public repo. Writes use the partner token from that file,
read by code and never printed. Reads use the default gh login.
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys

# the local config (not in this public repo): one JSON object,
#   {"engine": {"repo": "<owner>/<name>", "account": "A"}, "fleet2": {...}, "token_file": "<path>"}
# A fleet with no entry is refused; a write needs token_file.
CONFIG_FILE = os.path.expanduser('~/B4L_work/engine_helper/loops_config.json')
CONFIG = {}
if os.path.exists(CONFIG_FILE):
    CONFIG = json.load(open(CONFIG_FILE, encoding='utf-8'))
LOG = os.path.expanduser('~/B4L_work/engine_helper/quality/fleet_loops_changelog.jsonl')
LOOP_FILE = '.github/loop_every_min'
DEFAULT_EVERY = 360
RUN_AT_WORK = ('in_progress', 'queued', 'pending', 'waiting')
ID_RE = re.compile(r'([A-Z])/([^/]+)/([^/]+\.ya?ml)')


class Refused(Exception):
    """the verb was refused (a bad input, an unknown fleet or id): exit 1 with an error sentence"""


class Failed(Exception):
    """GitHub or gh failed: exit 2"""


def fleets():
    """{fleet name: {repo, account}} from the local config (the token file is not a fleet)"""
    return {k: v for k, v in CONFIG.items() if isinstance(v, dict)}


def fleet_info(fleet):
    f = fleets()
    if fleet not in f:
        raise Refused('fleet %r has no repository in the local config (known: %s); its owner and name are asked on the board'
                      % (fleet, ', '.join(sorted(f)) or 'none'))
    if not f[fleet].get('account'):
        raise Refused('fleet %r has no account letter in the local config' % fleet)
    return f[fleet]


def repo_name(repo):
    return repo.split('/')[-1]


def loop_id(account, repo, wf):
    return '%s/%s/%s' % (account, repo_name(repo), wf)


def check_every(text):
    """the loop time in minutes: 60 to 1440, whole hours. Returns (minutes, '') or (None, reason)."""
    if not re.fullmatch(r'\d+', str(text)):
        return None, 'the loop time must be whole minutes, got %r' % (text,)
    n = int(text)
    if n < 60 or n > 1440 or n % 60:
        return None, 'the loop time %d must be a multiple of 60 from 60 to 1440 (the base tick is hourly)' % n
    return n, ''


def scheduled(workflow_text):
    """True when a workflow file has a schedule trigger (its cron runs the loop)"""
    return bool(re.search(r'(?m)^\s*schedule:\s*$', workflow_text))


def token_of(fleet):
    """the write token of ONE fleet: its own token_file in the local config. No fleet borrows another's token."""
    path = os.path.expanduser(fleet_info(fleet).get('token_file', ''))
    if not path or not os.path.exists(path):
        raise Failed('no write token for %s: its token_file is not set in the local config' % fleet)
    return open(path, encoding='utf-8').read().strip()


def gh(args, write=None):
    """gh with the default login, or with the token of the fleet named in write (never printed)"""
    env = dict(os.environ)
    if write:
        env['GH_TOKEN'] = token_of(write)
    out = subprocess.run(['gh'] + args, capture_output=True, text=True, env=env, timeout=180)
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).replace(env.get('GH_TOKEN') or '\0', '<token>').strip()
        raise Failed('gh %s failed: %s' % (args[0], msg[-300:]))
    return out.stdout


def log(entry):
    entry['at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')


def ref_query(ref):
    return '?ref=%s' % ref if ref else ''


def read_every(repo, ref):
    """the loop time in minutes and the file's sha. A missing file is the default (360) with no sha."""
    try:
        d = json.loads(gh(['api', 'repos/%s/contents/%s%s' % (repo, LOOP_FILE, ref_query(ref))]))
    except Failed as e:
        if 'Not Found' in str(e):
            return DEFAULT_EVERY, None
        raise
    text = base64.b64decode(d['content']).decode('utf-8', 'replace').strip()
    n, _why = check_every(text)
    return (n if n is not None else DEFAULT_EVERY), d['sha']


def workflows(repo, ref):
    """{file name: {id, state, scheduled, cron, run_min}} of the repo's workflows (state from GitHub; schedule from the file)"""
    listed = json.loads(gh(['api', 'repos/%s/actions/workflows' % repo, '--jq', '.workflows']) or '[]')
    out = {}
    for w in listed:
        name = os.path.basename(w['path'])
        d = json.loads(gh(['api', 'repos/%s/contents/%s%s' % (repo, w['path'], ref_query(ref))]))
        body = base64.b64decode(d['content']).decode('utf-8', 'replace')
        caps = [int(x) for x in re.findall(r'timeout-minutes:\s*(\d+)', body)]
        out[name] = {'id': w['id'], 'state': w['state'], 'scheduled': scheduled(body),
                     'cron': re.findall(r"cron:\s*'([^']+)'", body),
                     'run_min': max(caps) if caps else None}      # the longest job cap: one run's limit
    return out


def runs_at_work(repo, name):
    out = gh(['run', 'list', '-R', repo, '--workflow', name, '--limit', '20', '--json', 'databaseId,status'])
    return [str(r['databaseId']) for r in json.loads(out or '[]') if r['status'] in RUN_AT_WORK]


def state_of(w, runs):
    """the lead's state: paused (disabled), running (a run at work), waiting (enabled, nothing at work)"""
    if not w['state'].startswith('active'):
        return 'paused'
    return 'running' if runs else 'waiting'


def next_start(w, every, now):
    """the next UTC start of the loop: the cron's minute in the next hour the loop time allows; None when paused"""
    if not w['state'].startswith('active') or not w['cron'] or every is None:
        return None
    minute = int(w['cron'][0].split()[0])
    step = every // 60
    t = now.replace(minute=minute, second=0, microsecond=0)
    if t <= now:
        t += datetime.timedelta(hours=1)
    for _ in range(24 * 60):
        if (t.hour % step) == 0:
            return t.strftime('%Y-%m-%dT%H:%M:%SZ')
        t += datetime.timedelta(hours=1)
    return None


def last_started(repo, name):
    """the start (UTC Z) of the workflow's latest run of any state, or None"""
    out = gh(['run', 'list', '-R', repo, '--workflow', name, '--limit', '1', '--json', 'createdAt'])
    rows = json.loads(out or '[]')
    return rows[0]['createdAt'] if rows else None


def month_minutes(repo, wf_id):
    """the billable minutes of this month for the workflow, from GitHub's timing endpoint. This repository is public:
    its minutes are reported as public; private is 0 (the repository has no private minutes)."""
    out = gh(['api', 'repos/%s/actions/workflows/%s/timing' % (repo, wf_id), '--jq',
              '[.billable[]?.total_ms] | add // 0'])
    total_ms = int(float(out.strip() or 0))
    return {'public': int(round(total_ms / 60000.0)), 'private': 0}


def record(fleet, account, repo, wf, w, every, runs, now):
    """the lead's loop record (site's SITE-73 shape): the same keys for every fleet"""
    return {'id': loop_id(account, repo, wf), 'fleet': fleet, 'account': account, 'repo': repo_name(repo), 'workflow': wf,
            'state': state_of(w, runs), 'every_min': every, 'run_min': w.get('run_min'),
            'last_start': last_started(repo, wf), 'next_start': next_start(w, every, now),
            'running_now': len(runs), 'minutes_month': month_minutes(repo, w['id'])}


def every_of(fleet, ref):
    """the fleet's loop time: the file read on its repo, or None when the fleet has no loop-time gate (it is dispatched
    by its keepalive, not by a cron of its own)"""
    info = fleet_info(fleet)
    if not info.get('gate', True):
        return None, None
    return read_every(info['repo'], ref)


def loop_files(fleet, ref):
    """the loops of a fleet: its scheduled workflows, and the files the local config names (dispatch-driven loops)"""
    info = fleet_info(fleet)
    ws = workflows(info['repo'], ref)
    names = {n for n, w in ws.items() if w['scheduled']} | set(info.get('loops', []))
    return {n: ws[n] for n in sorted(names) if n in ws}


def fleet_loops(fleet, ref, now=None):
    """the loops of one fleet as records, with the fleet's loop time"""
    info = fleet_info(fleet)
    repo, account = info['repo'], info['account']
    now = now or datetime.datetime.now(datetime.timezone.utc)
    every, _sha = every_of(fleet, ref)
    return [record(fleet, account, repo, wf, w, every, runs_at_work(repo, wf), now)
            for wf, w in loop_files(fleet, ref).items()]


def resolve(target, default_fleet):
    """a target is a fleet name, a loop id, or (old form) a workflow file of --fleet. Returns (kind, fleet, workflow)."""
    if target in fleets():
        return 'fleet', target, None
    m = ID_RE.fullmatch(target)
    if m:
        letter, name, wf = m.groups()
        for fleet, info in fleets().items():
            if info.get('account') == letter and repo_name(info['repo']) == name:
                return 'loop', fleet, wf
        raise Refused('no loop has the id %s: no fleet in the local config has that account letter and repository' % target)
    if target.endswith(('.yml', '.yaml')):
        return 'loop', default_fleet, target
    raise Refused('%r is neither a fleet name nor a loop id (<letter>/<repo>/<file>)' % target)


def scoped_names(fleet, ref, kind, wf):
    """the scheduled workflow files a target means: all of the fleet's loops, or the one workflow"""
    names = sorted(loop_files(fleet, ref))
    if kind == 'fleet':
        return names
    if wf not in names:
        raise Refused('%s is not a scheduled loop of %s' % (wf, fleet))
    return [wf]


def toggle(a, target, enable):
    kind, fleet, wf = resolve(target, a.fleet)
    info = fleet_info(fleet)
    repo, account = info['repo'], info['account']
    verb = 'enable' if enable else 'disable'
    names = scoped_names(fleet, a.ref, kind, wf)
    now = datetime.datetime.now(datetime.timezone.utc)
    every, _ = every_of(fleet, a.ref)
    changed = []
    for name in names:
        before_runs = runs_at_work(repo, name)
        rb = record(fleet, account, repo, name, workflows(repo, a.ref)[name], every, before_runs, now)
        gh(['workflow', verb, name, '-R', repo], write=fleet)
        stopped = []
        if not enable and a.stop_running:
            for rid in before_runs:
                gh(['run', 'cancel', rid, '-R', repo], write=fleet)
                stopped.append(rid)
        after_runs = runs_at_work(repo, name)
        ra = record(fleet, account, repo, name, workflows(repo, a.ref)[name], every, after_runs, now)
        changed.append({'id': ra['id'], 'before': rb, 'after': ra})
        log({'by': a.by, 'fleet': fleet, 'cmd': 'resume' if enable else 'pause', 'workflow': name,
             'stop_running': bool(stopped), 'cancelled': stopped, 'before': rb, 'after': ra})
    return {'ok': True, 'changed': changed}


def cmd_list(a):
    loops = []
    for fleet in sorted(fleets()):
        loops += fleet_loops(fleet, a.ref)
    return {'loops': loops}


def cmd_pause(a):
    return toggle(a, a.target, enable=False)


def cmd_resume(a):
    return toggle(a, a.target, enable=True)


def cmd_every(a):
    minutes, why = check_every(a.minutes)
    if minutes is None:
        raise Refused(why)
    kind, fleet, _wf = resolve(a.target, a.fleet)
    info = fleet_info(fleet)
    repo = info['repo']
    if not info.get('gate', True):
        raise Refused('fleet %s has no loop-time gate: its loops are dispatched by its keepalive (window set there), so there '
                      'is no loop time to set until its own gate lands' % fleet)
    old, sha = read_every(repo, a.ref)
    body = {'message': 'loop time %d min (was %d) by %s: set by fleet_loops every' % (minutes, old, a.by),
            'content': base64.b64encode(('%d\n' % minutes).encode()).decode(), 'sha': sha or ''}
    if a.ref:
        body['branch'] = a.ref
    args = ['api', '-X', 'PUT', 'repos/%s/contents/%s' % (repo, LOOP_FILE)]
    for k, v in body.items():
        if v != '':
            args += ['-f', '%s=%s' % (k, v)]
    res = json.loads(gh(args, write=fleet))
    commit = (res.get('commit') or {}).get('sha', '')
    now = datetime.datetime.now(datetime.timezone.utc)
    changed = []
    for wf, w in sorted(loop_files(fleet, a.ref).items()):
        runs = runs_at_work(repo, wf)
        rb = record(fleet, info['account'], repo, wf, w, old, runs, now)
        ra = record(fleet, info['account'], repo, wf, w, minutes, runs, now)
        changed.append({'id': ra['id'], 'before': rb, 'after': ra})
    log({'by': a.by, 'fleet': fleet, 'cmd': 'every', 'loop_time_min': {'before': old, 'after': minutes},
         'commit': commit, 'ref': a.ref or 'default', 'loops': [c['id'] for c in changed]})
    return {'ok': True, 'changed': changed, 'commit': commit}


def cmd_stop(a):
    kind, fleet, wf = resolve(a.target, a.fleet)
    if kind != 'loop':
        raise Refused('stop takes one loop id (<letter>/<repo>/<file>), not a fleet name')
    info = fleet_info(fleet)
    repo, account = info['repo'], info['account']
    now = datetime.datetime.now(datetime.timezone.utc)
    every, _ = every_of(fleet, a.ref)
    w = workflows(repo, a.ref).get(wf)
    if w is None:
        raise Refused('%s is not a workflow of %s' % (wf, fleet))
    before = runs_at_work(repo, wf)
    for rid in before:
        gh(['run', 'cancel', rid, '-R', repo], write=fleet)
    after = runs_at_work(repo, wf)
    rb = record(fleet, account, repo, wf, w, every, before, now)
    ra = record(fleet, account, repo, wf, w, every, after, now)
    log({'by': a.by, 'fleet': fleet, 'cmd': 'stop', 'workflow': wf, 'cancelled': before, 'before': rb, 'after': ra})
    return {'ok': True, 'changed': [{'id': ra['id'], 'before': rb, 'after': ra}]}


def run(a):
    return {'list': cmd_list, 'pause': cmd_pause, 'resume': cmd_resume, 'every': cmd_every, 'stop': cmd_stop}[a.cmd](a)


def main(argv=None):
    ap = argparse.ArgumentParser(description='fleet loops: list, pause, resume, every, stop (ENG-73)')
    ap.add_argument('--fleet', default='engine', help='the fleet a bare workflow file means (the old form)')
    ap.add_argument('--by', default='engine_helper', help='who asks (the change log)')
    ap.add_argument('--ref', default='', help='a branch to read and write instead of the default (a test)')
    ap.add_argument('--json', action='store_true', help='print one JSON object on stdout')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    p = sub.add_parser('pause')
    p.add_argument('target')
    p.add_argument('--stop-running', action='store_true', help='also cancel the runs at work')
    sub.add_parser('resume').add_argument('target')
    sub.add_parser('stop').add_argument('target')
    p = sub.add_parser('every')
    p.add_argument('args', nargs=2, help='<id|fleet> <minutes> (the old form <minutes> <id|file> also works)')
    a = ap.parse_args(argv)
    if a.cmd == 'every':
        first, second = a.args
        a.target, a.minutes = (second, first) if first.isdigit() else (first, second)
    try:
        out = run(a)
        code = 0
    except Refused as e:
        out, code = {'error': str(e)}, 1
    except Failed as e:
        out, code = {'error': str(e)}, 2
    print(json.dumps(out, sort_keys=True) if (a.json or code) else json.dumps(out, indent=1, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
