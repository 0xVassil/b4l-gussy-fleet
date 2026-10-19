#!/usr/bin/env python3
"""fleet_loops.py: list, pause, resume, set the loop time of, or stop the scheduled compute loops of a fleet (ENG-73).

  list                       every workflow of the fleet, its state, its cron, its loop time and its runs at work
  pause   <workflow>         disable the workflow (its schedule and its respawn stop; a run already at work finishes)
  resume  <workflow>         enable the workflow again
  every   <minutes> <workflow>  set the loop time (repo variable B4L_LOOP_EVERY_MIN): 60 to 1440, whole hours
  stop    <workflow>         cancel the runs of the workflow that are at work (each run id printed)

Every write is written to the change log (one JSON line each). Fleet names: --fleet fleet1 (default) or fleet2.
Writes use the fleet's partner token from its file, read by code and never printed. Reads use the default gh login.
A workflow is named by its file, for example donate.yml.
"""
import argparse
import base64
import datetime
import json
import os
import re
import subprocess
import sys

# the fleets' owner/name live in a local file, not in this public repo (the privacy scan): one JSON object,
# {"fleet1": "<owner>/<repo>", "fleet2": "<owner>/<repo>"}. A fleet with no entry refuses to run.
FLEETS_FILE = os.path.expanduser('~/B4L_work/engine_helper/fleet_loops_fleets.json')
FLEETS = json.load(open(FLEETS_FILE, encoding='utf-8')) if os.path.exists(FLEETS_FILE) else {}
TOKEN_FILE = os.path.expanduser('~/.b4l_partner_gussy_github.token')
LOG = os.path.expanduser('~/B4L_work/engine_helper/quality/fleet_loops_changelog.jsonl')
VAR = 'B4L_LOOP_EVERY_MIN'
DEFAULT_EVERY = 360


def check_every(text):
    """the loop time in minutes: 60 to 1440, whole hours. Returns (minutes, '') or (None, reason)."""
    if not re.fullmatch(r'\d+', str(text)):
        return None, 'the loop time must be whole minutes, got %r' % text
    n = int(text)
    if n < 60 or n > 1440 or n % 60:
        return None, 'the loop time %d must be a multiple of 60 from 60 to 1440 (the base tick is hourly)' % n
    return n, ''


def scheduled(workflow_text):
    """True when a workflow file has a schedule trigger (its cron runs the loop)"""
    return bool(re.search(r'(?m)^\s*schedule:\s*$', workflow_text))


def repo_of(fleet):
    if fleet not in FLEETS:
        sys.exit('fleet %r has no repository yet (known: %s). Ask the board for its owner/name.' % (fleet, ', '.join(FLEETS)))
    return FLEETS[fleet]


def gh(args, write=False, repo=None):
    env = dict(os.environ)
    if write:
        env['GH_TOKEN'] = open(TOKEN_FILE, encoding='utf-8').read().strip()
    out = subprocess.run(['gh'] + args, capture_output=True, text=True, env=env, timeout=180)
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).replace(env.get('GH_TOKEN', '\0'), '<token>').strip()
        sys.exit('gh %s failed: %s' % (args[0], msg[-300:]))
    return out.stdout


def log(entry):
    entry['at'] = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, sort_keys=True) + '\n')


def workflows(repo):
    """{file name: {state, scheduled, cron}} of the fleet's workflows"""
    listed = json.loads(gh(['api', 'repos/%s/actions/workflows' % repo, '--jq', '.workflows']) or '[]')
    out = {}
    for w in listed:
        name = os.path.basename(w['path'])
        text = gh(['api', 'repos/%s/contents/%s' % (repo, w['path']), '--jq', '.content']).strip()
        body = base64.b64decode(text).decode('utf-8', 'replace')
        cron = re.findall(r"cron:\s*'([^']+)'", body)
        out[name] = {'id': w['id'], 'state': w['state'], 'scheduled': scheduled(body), 'cron': cron}
    return out


def loop_time(repo):
    try:
        return gh(['variable', 'get', VAR, '-R', repo]).strip() or None
    except SystemExit:
        return None


def runs_at_work(repo, name):
    out = gh(['run', 'list', '-R', repo, '--workflow', name, '--limit', '20', '--json', 'databaseId,status,createdAt'])
    return [r for r in json.loads(out or '[]') if r['status'] in ('in_progress', 'queued', 'pending', 'waiting')]


def cmd_list(a):
    repo = repo_of(a.fleet)
    rows = workflows(repo)
    every = loop_time(repo) or '(unset: %d)' % DEFAULT_EVERY
    print('%s (%s): loop time %s minutes' % (a.fleet, repo, every))
    for name, w in sorted(rows.items()):
        if not w['scheduled']:
            continue
        at = runs_at_work(repo, name)
        print('  %-22s %-18s cron %-16s runs at work %d %s' % (name, w['state'], ','.join(w['cron']) or '-', len(at),
                                                                ' '.join(str(r['databaseId']) for r in at)))
    print('  (manual-only workflows are not scheduled and are not listed)')
    return 0


def cmd_pause(a):
    repo = repo_of(a.fleet)
    gh(['workflow', 'disable', a.workflow, '-R', repo], write=True)
    log({'fleet': a.fleet, 'cmd': 'pause', 'workflow': a.workflow})
    print('paused %s on %s (a run at work finishes; no new run starts)' % (a.workflow, a.fleet))
    return 0


def cmd_resume(a):
    repo = repo_of(a.fleet)
    gh(['workflow', 'enable', a.workflow, '-R', repo], write=True)
    log({'fleet': a.fleet, 'cmd': 'resume', 'workflow': a.workflow})
    print('resumed %s on %s' % (a.workflow, a.fleet))
    return 0


def cmd_every(a):
    minutes, why = check_every(a.minutes)
    if minutes is None:
        sys.exit(why)
    repo = repo_of(a.fleet)
    gh(['variable', 'set', VAR, '-b', str(minutes), '-R', repo], write=True)
    log({'fleet': a.fleet, 'cmd': 'every', 'workflow': a.workflow, 'minutes': minutes})
    print('loop time of %s on %s set to %d minutes (%d h); the hourly cron starts it in the hours due' % (
        a.workflow, a.fleet, minutes, minutes // 60))
    return 0


def cmd_stop(a):
    repo = repo_of(a.fleet)
    ids = [str(r['databaseId']) for r in runs_at_work(repo, a.workflow)]
    for rid in ids:
        gh(['run', 'cancel', rid, '-R', repo], write=True)
    log({'fleet': a.fleet, 'cmd': 'stop', 'workflow': a.workflow, 'runs': ids})
    print('stop %s on %s: cancelled %d run(s) %s' % (a.workflow, a.fleet, len(ids), ' '.join(ids) or '-'))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='fleet loops: list, pause, resume, every, stop (ENG-73)')
    ap.add_argument('--fleet', default='fleet1')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('list')
    for name in ('pause', 'resume', 'stop'):
        p = sub.add_parser(name)
        p.add_argument('workflow')
    p = sub.add_parser('every')
    p.add_argument('minutes')
    p.add_argument('workflow')
    a = ap.parse_args(argv)
    return {'list': cmd_list, 'pause': cmd_pause, 'resume': cmd_resume, 'every': cmd_every, 'stop': cmd_stop}[a.cmd](a)


if __name__ == '__main__':
    sys.exit(main())
