#!/usr/bin/env python3
"""fleet_loop.py - the free fleet's keep-busy loop as a GitHub job (row FLEET-LOOP-GH, the admin 11-05: "put the loop on
github runners and let the superagent handle it").

One pass, every 10 minutes inside ONE long job (fleet_loop.yml):

  - every kept runner's open cards, read from the hub (engine/current/fleet/queue-r<N>.md - a queue file holds only its
    open tasks: a closed card moves to done-r<N>.md);
  - the partner repo's live and queued agentfleet runs, and which runners each one holds (its title: "agentfleet room [1, 2]";
    another lane's run holds no room runner, and a room run that names none is reported as '*' = every runner);
  - a kept runner with open cards and no live or queued window gets one:
    gh workflow run agentfleet.yml -f lane=room -f runners=[N];
  - the CARD REFILL waits on the one place both the laptop and this job must read (the brief's own rule): the hub card
    state engine/current/state/project-cards.json. Until it exists this pass issues no card and says so - a second
    generator without the shared state would issue cards a room already used;
  - one heartbeat line per pass to engine/current/state/fleet-loop-log.jsonl (time from date -u).

    python tools/fleet_loop.py --once [--dry]     one pass (--dry prints the plan and dispatches nothing)
    python tools/fleet_loop.py                    the job: a pass every 10 min until 5 h 45 m, then exit 0
    python tools/fleet_loop.py --selftest         the plants of this file in a temp world: no network, no gh

Env: B4L_CC_TOKEN (the hub; the fleet token), GITHUB_REPOSITORY (the partner repo owner/name), FLEET_ROSTER (a roster json,
when the job fetched the kit; else tools/fleet_kept.json beside this file). Only the standard library.
"""
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HUB = os.environ.get('B4L_CC_HUB') or 'https://bull4life.com/cc'
QUEUE_REL = 'engine/current/fleet/queue-r%s.md'
LOG_REL = 'engine/current/state/fleet-loop-log.jsonl'
CARD_STATE_REL = 'engine/current/state/project-cards.json'
ROSTER = os.environ.get('FLEET_ROSTER') or os.path.join(ROOT, 'compute', 'fleet_kit', 'room_lane', 'ai_fleet.json')
KEPT_FILE = os.path.join(HERE, 'fleet_kept.json')
DEFAULT_KEPT = ('0', '1', '2')
MIN_CARDS = 1                                      # a window with no card to run is a burn: never start one
REFILL_MIN = 20                                    # the brief: under 20 open cards on a kept runner asks for a refill
MAX_DISPATCH = 2                                   # one pass starts at most this many windows (a storm guard)
EVERY_S = 10 * 60
DEADLINE_S = 5 * 3600 + 45 * 60                    # the job exits by itself at 5 h 45 m (the 355 min timeout is its fence)
RUN_AT_WORK = ('in_progress', 'queued', 'pending', 'waiting')
RUN_RUNNERS = re.compile(r'\[([0-9][0-9,\s]*)\]')


def now_iso(t=None):
    """UTC seconds for a timestamp, a clock function or None (now). The job passes time.time itself, so both work."""
    t = t() if callable(t) else t
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() if t is None else t))


def http_json(url, token, payload=None, timeout=60):
    """one hub call: a JSON answer ({} on an empty body). A non-2xx raises with the status, never a silent {}."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='GET' if payload is None else 'POST')
    req.add_header('Authorization', 'Bearer ' + (token or ''))
    req.add_header('X-CC-Agent', 'fleet-loop')
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'replace')
    return json.loads(body) if body.strip() else {}


def hub_get(rel, token):
    """an artifact's body, or {} when it is not there (404). Any other failure raises: a blind pass is worse than a red one."""
    url = HUB + '/api/artifacts/read?rel=' + urllib.parse.quote(rel)
    try:
        return http_json(url, token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def hub_log(line, token):
    """the heartbeat: one JSON line appended to the log artifact. The hub keeps the newest copy; each line carries its own
    pass time, so the panel reads the last one."""
    prev = (hub_get(LOG_REL, token) or {}).get('body') or ''
    body = (prev if (not prev or prev.endswith('\n')) else prev + '\n') + json.dumps(line, sort_keys=True) + '\n'
    return http_json(HUB + '/api/artifacts', token,
                     {'type': 'data', 'slug': 'engine/current/state/fleet-loop-log', 'title': "the fleet loop's passes",
                      'body': body})


def kept_runners(roster=ROSTER, kept_file=KEPT_FILE):
    """the roster's kept list (the one list of the fleet's runners), else the small file beside this tool, else 0-2."""
    for path in (roster, kept_file):
        try:
            with open(path, encoding='utf-8') as f:
                got = json.load(f).get('kept')
            if isinstance(got, list) and got:
                return [str(x) for x in got]
        except (OSError, ValueError):
            continue
    return list(DEFAULT_KEPT)


def open_cards(fetch, kept):
    """{runner: open cards}. A queue file holds its OPEN tasks (a closed card moves to done-r<N>.md); a file that is not
    there is 0 cards, never a guess. The reads are independent and side by side (a hub read takes 1.5-7 s under load: 12 of
    them one after another would hold a pass for a minute and a half - fleet_queue.read_all found the same)."""
    def one(r):
        try:
            body = (fetch(QUEUE_REL % r) or {}).get('body') or ''
            doc = json.loads(body) if body.strip() else {}
        except (ValueError, OSError):
            doc = {}
        return r, len([t for t in (doc.get('tasks') or []) if isinstance(t, dict) and t.get('id')])

    workers = max(1, min(8, len(kept)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, kept))


def run_runners(title):
    """the runners a live run holds, from its title: "agentfleet room [1, 2]". Another lane's run holds no room runner; a
    ROOM run whose title names none holds the workflow's default matrix - reported as ['*'], because a hidden window is
    worse than a late one (the pass then dispatches nothing)."""
    if not (title or '').strip().lower().startswith('agentfleet room'):
        return []
    m = RUN_RUNNERS.search(title)
    if m:
        return [x.strip() for x in m.group(1).split(',') if x.strip()]
    return ['*']


def held_runners(rows, wf='agentfleet.yml'):
    """{runner: run id} of the partner repo's live and queued runs; '*' means a room run that names no runner and holds
    every one of them."""
    held = {}
    for row in rows or []:
        if (row.get('workflowName') or wf) not in (wf, 'agentfleet') or row.get('status') not in RUN_AT_WORK:
            continue
        for r in run_runners(row.get('displayTitle') or row.get('name') or ''):
            held.setdefault(r, str(row.get('databaseId') or ''))
    return held


def plan_pass(opens, held, kept):
    """[(runner, why)]: a kept runner with open cards and no live or queued window. At most MAX_DISPATCH a pass, the
    emptiest first (a runner whose queue is about to run dry is the one the fleet loses first). A run that holds every
    runner ('*') stops the pass: no window is started on a guess."""
    if '*' in held:
        return []
    due = [(r, 'open cards %d, no live or queued window' % opens.get(r, 0)) for r in kept
           if opens.get(r, 0) >= MIN_CARDS and r not in held]
    due.sort(key=lambda x: opens.get(x[0], 0))
    return due[:MAX_DISPATCH]


def refill_state(fetch):
    """('ready', the state) when the one hub card state both the laptop and this job read exists, else ('waits', {})."""
    doc = fetch(CARD_STATE_REL) or {}
    body = doc.get('body') or ''
    if isinstance(body, dict):
        state = body
    else:
        try:
            state = json.loads(body) if body.strip() else {}
        except ValueError:
            state = {}
    if not isinstance(state, dict) or not state.get('version'):
        return 'waits', {}
    return 'ready', state


def one_pass(deps, dry=False):
    """one pass -> the heartbeat line (a dict). deps: fetch, log, gh_runs, gh_dispatch, kept, now, pass_no. Never raises:
    a failing leg is reported in the line, because one bad minute must not end a 6-hour loop."""
    line = {'at': now_iso(deps.get('now')), 'pass': deps.get('pass_no'), 'dry': bool(dry)}
    started = time.time()
    try:
        kept = deps['kept']()
        line['kept'] = kept
    except Exception as e:                                                          # noqa: BLE001 - reported, not hidden
        line['error'] = 'kept: %s' % e
        kept = []
    try:
        opens = open_cards(deps['fetch'], kept)
        line['open'] = opens
    except Exception as e:                                                          # noqa: BLE001
        line['error'] = 'cards: %s' % e
        opens = {}
    blind = False
    try:
        held = held_runners(deps['gh_runs']())
        line['held'] = held
    except Exception as e:                                                          # noqa: BLE001
        line['error'] = 'runs: %s' % e
        blind = True            # a run list we cannot read may hide a live window: dispatch nothing this pass
    if blind:
        line['dispatched'] = []
        line['skip'] = 'no run list: nothing dispatched this pass'
    else:
        sent = []
        for runner, why in plan_pass(opens, held, kept):
            if dry:
                sent.append({'runner': runner, 'why': why, 'dry': True})
                continue
            try:
                sent.append({'runner': runner, 'why': why, 'run': deps['gh_dispatch'](runner)})
            except Exception as e:                                                  # noqa: BLE001
                sent.append({'runner': runner, 'why': why, 'error': str(e)[-200:]})
        line['dispatched'] = sent
    try:
        state_kind, state = refill_state(deps['fetch'])
        under = [r for r, n in opens.items() if n < REFILL_MIN]
        line['refill'] = '%s; %d kept runner(s) under %d open cards (the generator is engine\'s card_refill.py; this pass ' \
                         'issues no card)' % (state_kind, len(under), REFILL_MIN)
    except Exception as e:                                                          # noqa: BLE001
        line['refill'] = 'unknown: %s' % e
    if not dry:
        line['seconds'] = round(time.time() - started, 1)
        try:
            deps['log'](line)
        except Exception as e:                                                      # noqa: BLE001
            line['log_error'] = str(e)[-200:]
    return line


def run_loop(pass_fn, now_fn=time.time, sleep_fn=time.sleep, every=EVERY_S, deadline=DEADLINE_S, start=None):
    """a pass now and every `every` seconds until `deadline` seconds have passed, then return the pass count - the job's
    own end, so no run outlives its 355-minute fence."""
    t0 = now_fn() if start is None else start
    n = 0
    while True:
        n += 1
        pass_fn(n)
        spent = now_fn() - t0
        if spent + every > deadline:
            return n
        sleep_fn(min(every, deadline - spent))


# ------------------------------------------------------------------ the plants (a temp world, no network, no gh)

def selftest():
    checks = []

    def check(name, ok):
        checks.append((name, bool(ok)))
        print(('ok   ' if ok else 'FAIL ') + name)

    kept = ['0', '1', '2']
    check('plan: a kept runner with open cards and no window gets one; one with no card does not',
          plan_pass({'0': 24, '1': 3, '2': 0}, {}, kept) == [('1', 'open cards 3, no live or queued window'),
                                                            ('0', 'open cards 24, no live or queued window')])
    check('plan: a runner a live or queued run holds gets no second window',
          plan_pass({'0': 24, '1': 3}, {'1': '555'}, kept) == [('0', 'open cards 24, no live or queued window')])
    check('plan: at most %d windows a pass' % MAX_DISPATCH,
          plan_pass({r: 9 for r in ('0', '1', '2', '3')}, {}, ['0', '1', '2', '3']) ==
          [('0', 'open cards 9, no live or queued window'), ('1', 'open cards 9, no live or queued window')])
    check('runs: a room title names its runners; a room run with no runner holds every one ("*"); another lane holds none',
          run_runners('agentfleet room [1, 2]') == ['1', '2'] and run_runners('agentfleet room') == ['*']
          and run_runners('agentfleet bot [3]') == [] and run_runners('') == [])
    check('runs: only live or queued runs hold a runner; a run that holds every runner stops the pass on a guess',
          held_runners([{'databaseId': 1, 'status': 'in_progress', 'displayTitle': 'agentfleet room [1]'},
                        {'databaseId': 2, 'status': 'completed', 'displayTitle': 'agentfleet room [2]'},
                        {'databaseId': 3, 'status': 'queued', 'displayTitle': 'agentfleet room'},
                        {'databaseId': 4, 'status': 'in_progress', 'displayTitle': 'agentfleet bot [9]'}]) ==
          {'1': '1', '*': '3'} and plan_pass({'0': 24}, {'*': '3'}, ['0', '1']) == [])
    world = {'queues': {'0': {'tasks': [{'id': 'SI1'}, {'id': 'SI2'}]}, '1': {'tasks': []}, '2': {'tasks': [{'id': 'SI3'}]}},
             'state': None, 'posts': [], 'logs': []}

    def fetch(rel):
        if rel == CARD_STATE_REL:
            return {'body': world['state']} if world['state'] else {}
        m = re.match(r'engine/current/fleet/queue-r(.+)\.md$', rel)
        if m:
            return {'body': json.dumps(world['queues'].get(m.group(1), {}))} if m.group(1) in world['queues'] else {}
        return {}

    sent = []

    def gh_dispatch(r):
        sent.append(r)
        return 'run %d' % (900 + len(sent))

    deps = {'fetch': fetch, 'log': world['logs'].append,
            'gh_runs': lambda: [{'databaseId': 7, 'status': 'in_progress', 'displayTitle': 'agentfleet room [2]'}],
            'gh_dispatch': gh_dispatch, 'kept': lambda: kept, 'now': 1000.0, 'pass_no': 1}
    line = one_pass(deps)
    check('pass: a runner with cards and no window is dispatched; the one a live run holds is not',
          [s['runner'] for s in line['dispatched']] == ['0'] and sent == ['0'])
    check('pass: the refill waits on the hub card state and issues no card (no queue is ever posted)',
          line['refill'].startswith('waits') and not any('queue' in p for p in world['posts']))
    world['state'] = json.dumps({'version': 3, 'issued': ['A1']})
    line2 = one_pass(deps)
    check('pass: with the hub card state there the refill reads it (version) and still issues no card of its own',
          line2['refill'].startswith('ready') and not any('queue' in p for p in world['posts']))
    dry_line = one_pass(deps, dry=True)
    check('pass --dry: nothing dispatched, nothing logged',
          dry_line['dispatched'][0].get('dry') and len(world['logs']) == 2)
    call_line = one_pass(dict(deps, now=lambda: 1000.0, pass_no=5))
    check('pass: a clock given as a function (the job passes time.time) is read, not taken for a timestamp',
          call_line['at'] == now_iso(1000.0))
    boom = dict(deps, gh_dispatch=lambda r: (_ for _ in ()).throw(RuntimeError('gh refused')), pass_no=2)
    line3 = one_pass(boom)
    check('pass: a dispatch that fails is on the line and never ends the loop',
          line3['dispatched'][0].get('error') == 'gh refused' and bool(line3['at']))
    blind = dict(deps, gh_runs=lambda: (_ for _ in ()).throw(RuntimeError('gh list failed')), pass_no=3)
    line4 = one_pass(blind)
    check('pass: a run list we cannot read dispatches NOTHING (a hidden window is worse than a late one)',
          line4['dispatched'] == [] and 'no run list' in line4.get('skip', ''))
    no_log = dict(deps, log=lambda line: (_ for _ in ()).throw(RuntimeError('hub down')), pass_no=4)
    line5 = one_pass(no_log)
    check('pass: a heartbeat that cannot be written is named on the line, not raised', line5.get('log_error') == 'hub down')
    t0, slept, passes = 5000.0, [], []

    def fake_now():
        return t0 + sum(slept)

    n = run_loop(lambda k: passes.append((k, round(fake_now() - 5000.0))), now_fn=fake_now, sleep_fn=slept.append,
                 every=EVERY_S, deadline=DEADLINE_S)
    check('loop: a pass every %d s and the job ends by itself at 5 h 45 m (%d passes, last at %d s)'
          % (EVERY_S, n, passes[-1][1]), n == 35 and passes[-1][1] == 34 * EVERY_S and passes[-1][1] < DEADLINE_S)
    t0, slept, passes = 0.0, [], []
    got = run_loop(lambda k: passes.append(k), now_fn=fake_now, sleep_fn=slept.append, every=EVERY_S, deadline=EVERY_S * 2)
    check('loop: a deadline of two intervals runs the pass at the fence and sleeps no longer than it',
          got == 3 and sum(slept) == EVERY_S * 2)
    bad = [(n, ok) for n, ok in checks if not ok]
    print('fleet_loop selftest: %d checks, %d failed' % (len(checks), len(bad)))
    return 0 if not bad else 1


def gh(args, timeout=180):
    out = subprocess.run(['gh'] + args, capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError('gh %s: %s' % (' '.join(args[:2]), (out.stderr or out.stdout).strip()[-200:]))
    return out.stdout


def live_deps(token, repo):
    """the job's own reads and writes: the hub, gh's run list, a dispatch in this repo, the log artifact"""
    def runs():
        return json.loads(gh(['run', 'list', '-R', repo, '--workflow', 'agentfleet.yml', '--limit', '30',
                              '--json', 'databaseId,status,displayTitle,workflowName']) or '[]')

    def dispatch(runner):
        gh(['workflow', 'run', 'agentfleet.yml', '-R', repo, '-f', 'lane=room', '-f', 'runners=[%s]' % runner])
        return 'dispatched [%s]' % runner

    return {'fetch': lambda rel: hub_get(rel, token), 'log': lambda line: hub_log(line, token),
            'gh_runs': runs, 'gh_dispatch': dispatch, 'kept': kept_runners, 'now': time.time}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '--selftest' in argv:
        return selftest()
    token = os.environ.get('B4L_CC_TOKEN') or ''
    repo = os.environ.get('GITHUB_REPOSITORY') or ''
    if not token or not repo:
        print('fleet_loop: B4L_CC_TOKEN and GITHUB_REPOSITORY must be set (the job sets both)')
        return 2
    deps = live_deps(token, repo)
    if '--once' in argv:
        print(json.dumps(one_pass(dict(deps, pass_no=1), dry='--dry' in argv), sort_keys=True))
        return 0
    n = run_loop(lambda k: print(json.dumps(one_pass(dict(deps, pass_no=k)), sort_keys=True), flush=True),
                 every=float(os.environ.get('FLEET_LOOP_EVERY_S') or EVERY_S),
                 deadline=float(os.environ.get('FLEET_LOOP_DEADLINE_S') or DEADLINE_S))
    print('fleet_loop: %d pass(es), the job ends itself' % n)
    return 0


if __name__ == '__main__':
    sys.exit(main())
