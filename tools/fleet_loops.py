#!/usr/bin/env python3
"""fleet_loops.py: list, pause, resume, set the loop time of, or stop the scheduled compute loops of a fleet (ENG-73).

  list                          every scheduled workflow: state, cron, loop time, runs at work
  pause   <workflow>            disable the workflow (its schedule and respawn stop; a run already at work finishes)
  resume  <workflow>            enable the workflow again
  every   <minutes> [<workflow>]  set the loop time: the file .github/loop_every_min (60 to 1440, whole hours); the
                                loop time is one number for the whole fleet, so <workflow> is only named in the output
  stop    <workflow>            cancel the runs of the workflow that are at work (each run id printed)

Options: --fleet fleet1 (default) or fleet2; --by <name> (who, in the change log; default engine_helper);
--ref <branch> (read and write this branch instead of the default, for a test); --json (one JSON object on stdout).
Exit codes: 0 done; 1 refused (the output is {"error": "..."}); 2 it failed.

Every write goes to the change log (one JSON line: who, what, before, after). Fleet owner/name and the write token's file
live in a local config file, not in this public repo. Writes use the partner token from that file, read by code and never
printed. Reads use the default gh login. A workflow is named by its file, for example donate.yml.
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys

# the fleets' owner/name and the write token's file live in a local file, not in this public repo (the privacy scan):
# one JSON object, {"fleet1": "<owner>/<repo>", "fleet2": "<owner>/<repo>", "token_file": "<path>"}.
# A fleet with no entry is refused; a write needs token_file.
CONFIG_FILE = os.path.expanduser('~/B4L_work/engine_helper/loops_config.json')
CONFIG = {}
if os.path.exists(CONFIG_FILE):
    CONFIG = json.load(open(CONFIG_FILE, encoding='utf-8'))
FLEETS = {k: v for k, v in CONFIG.items() if k != 'token_file'}
TOKEN_FILE = os.path.expanduser(CONFIG.get('token_file', ''))
LOG = os.path.expanduser('~/B4L_work/engine_helper/quality/fleet_loops_changelog.jsonl')
LOOP_FILE = '.github/loop_every_min'
DEFAULT_EVERY = 360
RUN_AT_WORK = ('in_progress', 'queued', 'pending', 'waiting')


class Refused(Exception):
    """the verb was refused (a bad input or an unknown fleet): exit 1 with an error sentence"""


class Failed(Exception):
    """GitHub or gh failed: exit 2"""


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


def repo_of(fleet):
    if fleet not in FLEETS:
        raise Refused('fleet %r has no repository in the local config (known: %s); its owner and name are asked on the board'
                      % (fleet, ', '.join(sorted(FLEETS)) or 'none'))
    return FLEETS[fleet]


def gh(args, write=False):
    env = dict(os.environ)
    if write:
        if not TOKEN_FILE or not os.path.exists(TOKEN_FILE):
            raise Failed('no write token: token_file in the local config is missing')
        env['GH_TOKEN'] = open(TOKEN_FILE, encoding='utf-8').read().strip()
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
    n, why = check_every(text)
    return (n if n is not None else DEFAULT_EVERY), d['sha']


def workflows(repo, ref):
    """{file name: {id, state, scheduled, cron}} of the fleet's workflows (state from GitHub; the schedule from the file)"""
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
    if not w['state'].startswith('active') or not w['cron']:
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


def record(fleet, repo, wf, w, every, runs, now):
    """the lead's loop record (site's SITE-73 shape): one shape for every fleet. account is not measured (null)."""
    return {'id': '-/%s/%s' % (repo, wf), 'fleet': fleet, 'account': None, 'repo': repo, 'workflow': wf,
            'state': state_of(w, runs), 'every_min': every, 'run_min': w.get('run_min'),
            'last_start': last_started(repo, wf), 'next_start': next_start(w, every, now),
            'running_now': len(runs), 'minutes_month': month_minutes(repo, w['id'])}


def cmd_list(a, repo):
    every, _sha = read_every(repo, a.ref)
    now = datetime.datetime.now(datetime.timezone.utc)
    loops = []
    for wf, w in sorted(workflows(repo, a.ref).items()):
        if w['scheduled']:
            loops.append(record(a.fleet, repo, wf, w, every, runs_at_work(repo, wf), now))
    return {'loops': loops}


def one(a, repo, wf, state_change=None):
    """the before and after records of one scheduled workflow"""
    ws = workflows(repo, a.ref)
    if wf not in ws:
        raise Refused('%s is not a workflow of %s' % (wf, a.fleet))
    return ws[wf]


def cmd_pause(a, repo):
    before = one(a, repo, a.workflow)
    gh(['workflow', 'disable', a.workflow, '-R', repo], write=True)
    after = one(a, repo, a.workflow)
    every, _ = read_every(repo, a.ref)
    now = datetime.datetime.now(datetime.timezone.utc)
    runs = runs_at_work(repo, a.workflow)
    rb = record(a.fleet, repo, a.workflow, before, every, runs, now)
    ra = record(a.fleet, repo, a.workflow, after, every, runs, now)
    log({'by': a.by, 'fleet': a.fleet, 'cmd': 'pause', 'workflow': a.workflow, 'before': rb, 'after': ra})
    return {'ok': True, 'changed': [{'id': ra['id'], 'before': rb, 'after': ra}]}


def cmd_resume(a, repo):
    before = one(a, repo, a.workflow)
    gh(['workflow', 'enable', a.workflow, '-R', repo], write=True)
    after = one(a, repo, a.workflow)
    every, _ = read_every(repo, a.ref)
    now = datetime.datetime.now(datetime.timezone.utc)
    runs = runs_at_work(repo, a.workflow)
    rb = record(a.fleet, repo, a.workflow, before, every, runs, now)
    ra = record(a.fleet, repo, a.workflow, after, every, runs, now)
    log({'by': a.by, 'fleet': a.fleet, 'cmd': 'resume', 'workflow': a.workflow, 'before': rb, 'after': ra})
    return {'ok': True, 'changed': [{'id': ra['id'], 'before': rb, 'after': ra}]}


def cmd_every(a, repo):
    minutes, why = check_every(a.minutes)
    if minutes is None:
        raise Refused(why)
    old, sha = read_every(repo, a.ref)
    body = {'message': 'loop time %d min (was %d) by %s: set by fleet_loops every' % (minutes, old, a.by),
            'content': base64.b64encode(('%d\n' % minutes).encode()).decode(), 'sha': sha or ''}
    if a.ref:
        body['branch'] = a.ref
    args = ['api', '-X', 'PUT', 'repos/%s/contents/%s' % (repo, LOOP_FILE)]
    for k, v in body.items():
        if v != '':
            args += ['-f', '%s=%s' % (k, v)]
    res = json.loads(gh(args, write=True))
    commit = (res.get('commit') or {}).get('sha', '')
    changed = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for wf, w in sorted(workflows(repo, a.ref).items()):
        if w['scheduled']:
            runs = runs_at_work(repo, wf)
            rb = record(a.fleet, repo, wf, w, old, runs, now)
            ra = record(a.fleet, repo, wf, w, minutes, runs, now)
            changed.append({'id': ra['id'], 'before': rb, 'after': ra})
    log({'by': a.by, 'fleet': a.fleet, 'cmd': 'every', 'loop_time_min': {'before': old, 'after': minutes},
         'commit': commit, 'ref': a.ref or 'default', 'loops': [c['id'] for c in changed]})
    return {'ok': True, 'changed': changed, 'commit': commit}


def cmd_stop(a, repo):
    before = runs_at_work(repo, a.workflow)
    for rid in before:
        gh(['run', 'cancel', rid, '-R', repo], write=True)
    after = runs_at_work(repo, a.workflow)
    every, _ = read_every(repo, a.ref)
    w = one(a, repo, a.workflow)
    now = datetime.datetime.now(datetime.timezone.utc)
    rb = record(a.fleet, repo, a.workflow, w, every, before, now)
    ra = record(a.fleet, repo, a.workflow, w, every, after, now)
    log({'by': a.by, 'fleet': a.fleet, 'cmd': 'stop', 'workflow': a.workflow, 'before': rb, 'after': ra})
    return {'ok': True, 'changed': [{'id': ra['id'], 'before': rb, 'after': ra}]}


def run(a):
    repo = repo_of(a.fleet)
    if a.cmd == 'list':
        return cmd_list(a, repo)
    return {'pause': cmd_pause, 'resume': cmd_resume, 'every': cmd_every, 'stop': cmd_stop}[a.cmd](a, repo)


def main(argv=None):
    ap = argparse.ArgumentParser(description='fleet loops: list, pause, resume, every, stop (ENG-73)')
    ap.add_argument('--fleet', default='engine', help='engine (the free fleet, this repo), fleet2, ...')
    ap.add_argument('--by', default='engine_helper', help='who asks (the change log)')
    ap.add_argument('--ref', default='', help='a branch to read and write instead of the default (a test)')
    ap.add_argument('--json', action='store_true', help='print one JSON object on stdout')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    for name in ('pause', 'resume', 'stop'):
        sub.add_parser(name).add_argument('workflow')
    p = sub.add_parser('every')
    p.add_argument('minutes')
    p.add_argument('workflow', nargs='?', default='')
    a = ap.parse_args(argv)
    try:
        out = run(a)
        code = 0
    except Refused as e:
        out, code = {'error': str(e)}, 1
    except Failed as e:
        out, code = {'error': str(e)}, 2
    if a.json or code:
        print(json.dumps(out, sort_keys=True))
    else:
        print(json.dumps(out, indent=1, sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
