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
  - the CARD POOL: the laptop orchestrator is its one writer (engine 11-05). Each pass pops at most POOL_MAX cards from
    engine/current/data/card-pool.json {version, cards, at}, adds them to the runners under REFILL_MIN open cards (one
    queue file per runner, deduped by card id), and writes the pool back without them ONLY while its version matches - a
    mismatch means the laptop refilled meanwhile, so it is re-read and never overwritten. No pool: the refill says so and
    issues no card (the laptop keeps generating and writing it);
  - one heartbeat line per pass to engine/current/data/fleet-loop-log.jsonl (time from date -u).

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
HUB_AGENT = os.environ.get('FLEET_LOOP_AGENT') or 'engine'   # the name the hub token belongs to (X-CC-Agent must match it)
QUEUE_REL = 'engine/current/fleet/queue-r%s.md'
LOG_REL = 'engine/current/data/fleet-loop-log.jsonl'   # the hub writes artifacts under <owner>/current/<type>/
POOL_RELS = ('engine/current/data/card-pool.json', 'engine/current/state/card-pool.json')   # data first: the hub's real type
POOL_MAX = 24                                      # cards one pass moves at most (the brief's batch)
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
    req.add_header('X-CC-Agent', HUB_AGENT)
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
                     {'type': 'data', 'slug': 'fleet-loop-log', 'title': "the fleet loop's passes", 'ext': 'jsonl',
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
        body = (fetch(QUEUE_REL % r) or {}).get('body') or ''
        try:                              # a read that fails is NOT 0 cards: it is an error the pass reports (a 401 said "0" once)
            doc = json.loads(body) if body.strip() else {}
        except ValueError:
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


def read_pool(fetch):
    """('ready', {version, cards, at}) when the laptop's card pool is there - the laptop orchestrator is its ONE writer
    (engine 11-05, FLEET-LOOP-GH: card generation stays on the laptop; this job only pops) - else ('waits', {}). The two
    rels: the hub writes <owner>/current/<type>/, so data comes first, the brief's state path second."""
    for rel in POOL_RELS:
        doc = fetch(rel) or {}
        body = doc.get('body') or ''
        if isinstance(body, dict):
            pool = body
        else:
            try:
                pool = json.loads(body) if body.strip() else {}
            except ValueError:
                pool = {}
        if isinstance(pool, dict) and isinstance(pool.get('cards'), list):
            return 'ready', pool
    return 'waits', {}


def _bump(v):
    try:
        return int(v) + 1
    except (TypeError, ValueError):
        return 1


def plan_refill(opens, pool, kept):
    """{runner: [cards]}: the kept runners under REFILL_MIN open cards, in kept order, each brought up to REFILL_MIN at
    most, POOL_MAX cards in all, taken from the pool in its own order. A card with no id is skipped: it cannot be deduped."""
    cards = [c for c in (pool.get('cards') or []) if isinstance(c, dict) and c.get('id')]
    out, take = {}, 0
    for r in kept:
        want = min(REFILL_MIN - opens.get(r, 0), POOL_MAX - take)
        if want <= 0:
            continue
        got = cards[take:take + want]
        if not got:
            break
        out[r] = got
        take += len(got)
        if take >= POOL_MAX:
            break
    return out


def queue_add(fetch, post, runner, cards):
    """Add cards to a runner's queue file (the room's own shape) and return how many were really added: a card whose id is
    already open there is skipped, so a pass that runs twice adds nothing twice. shortcut: the queue file has no version,
    so a laptop write landing between this read and this write is lost; the next pass re-reads it (upgrade: a version on
    the queue file, as the pool has)."""
    body = (fetch(QUEUE_REL % runner) or {}).get('body') or ''
    try:
        q = json.loads(body) if body.strip() else {}
    except ValueError:
        q = {}
    have = {t.get('id') for t in (q.get('tasks') or []) if isinstance(t, dict)}
    new = [c for c in cards if c.get('id') not in have]
    if not new:
        return 0
    tasks = [t for t in (q.get('tasks') or []) if isinstance(t, dict)]
    tasks += [dict(c, room=c.get('room') or q.get('room') or 'fetch', added=c.get('added') or now_iso()) for c in new]
    post('fleet', 'queue-r%s' % runner, {'schema': 'b4l.fleet_queue/1', 'runner': str(runner), 'room': q.get('room') or 'fetch',
                                         'at': now_iso(), 'tasks': tasks})
    return len(new)


def pop_pool(fetch, post, opens, kept):
    """Move cards from the laptop's pool into the runners' queues (this job is the queues' only writer when the pool is
    live) -> (moved, one heartbeat sentence). The pool is written back without the moved cards ONLY while its version
    still matches; a mismatch means the laptop refilled meanwhile, so it is re-read and never overwritten - the moved ids
    stay in the pool and the next pass finds them already queued (queue_add dedupes), so nothing is queued twice."""
    kind, pool = read_pool(fetch)
    if kind != 'ready':
        return 0, 'waits (no pool: the laptop generator writes it)'
    plan = plan_refill(opens, pool, kept)
    if not plan:
        why = 'no card held' if not (pool.get('cards') or []) else 'every kept runner is at %d or more open' % REFILL_MIN
        return 0, 'pool v%s: %s' % (pool.get('version'), why)
    moved = sum(queue_add(fetch, post, r, cards) for r, cards in plan.items())
    moved_ids = {c.get('id') for cards in plan.values() for c in cards}
    kind2, again = read_pool(fetch)
    if kind2 != 'ready' or str(again.get('version')) != str(pool.get('version')):
        return moved, 'pool v%s: %d queued; the pool changed meanwhile (v%s): not written back, next pass re-reads' % (
            pool.get('version'), moved, again.get('version'))
    keep = [c for c in (pool.get('cards') or []) if isinstance(c, dict) and c.get('id') not in moved_ids]
    out = dict(pool, version=_bump(pool.get('version')), cards=keep, at=now_iso(),
               popped={'at': now_iso(), 'cards': sorted(moved_ids), 'n': moved})
    post('data', 'card-pool', out)
    return moved, 'pool v%s -> v%s: %d card(s) to %d runner(s), %d left' % (
        pool.get('version'), out['version'], moved, len(plan), len(keep))


def one_pass(deps, dry=False):
    """one pass -> the heartbeat line (a dict). deps: fetch, log, gh_runs, gh_dispatch, post, kept, now, pass_no. Never raises:
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
        under = [r for r, n in opens.items() if n < REFILL_MIN]
        if dry:
            kind, pool = read_pool(deps['fetch'])
            line['refill'] = ('ready: pool v%s, %d card(s), %d runner(s) under %d (dry: nothing moved)'
                              % (pool.get('version'), len(pool.get('cards') or []), len(under), REFILL_MIN)) if kind == 'ready' \
                else 'waits (no pool: the laptop generator writes it)'
        else:
            _, note = pop_pool(deps['fetch'], deps['post'], opens, kept)
            line['refill'] = '%s; %d runner(s) under %d' % (note, len(under), REFILL_MIN)
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
             'pool': None, 'posts': [], 'logs': []}

    def fetch(rel):
        if rel in POOL_RELS:
            return {'body': json.dumps(world['pool'])} if world['pool'] else {}
        m = re.match(r'engine/current/fleet/queue-r(.+)\.md$', rel)
        if m:
            return {'body': json.dumps(world['queues'].get(m.group(1), {}))} if m.group(1) in world['queues'] else {}
        return {}

    def post(kind, slug, obj):
        world['posts'].append(kind + '/' + slug)
        if kind == 'fleet' and slug.startswith('queue-r'):
            world['queues'][slug[len('queue-r'):]] = obj
        elif kind == 'data' and slug == 'card-pool':
            world['pool'] = obj
        return {'ok': True}

    sent = []

    def gh_dispatch(r):
        sent.append(r)
        return 'run %d' % (900 + len(sent))

    deps = {'fetch': fetch, 'post': post, 'log': world['logs'].append,
            'gh_runs': lambda: [{'databaseId': 7, 'status': 'in_progress', 'displayTitle': 'agentfleet room [2]'}],
            'gh_dispatch': gh_dispatch, 'kept': lambda: kept, 'now': 1000.0, 'pass_no': 1}
    line = one_pass(deps)
    check('pass: a runner with cards and no window is dispatched; the one a live run holds is not',
          [s['runner'] for s in line['dispatched']] == ['0'] and sent == ['0'])
    check('pass: with no card pool the refill says it waits and no queue is written',
          line['refill'].startswith('waits') and not any('queue' in p for p in world['posts']))
    world['pool'] = {'version': 1, 'cards': [{'id': 'POOL1', 'title': 'a card'}], 'at': now_iso()}
    line2 = one_pass(deps)
    check('pass: a card in the pool goes to the runner under 20 open cards, and the pool is written back without it',
          'POOL1' in [t['id'] for t in world['queues']['0']['tasks']] and world['pool']['version'] == 2
          and not world['pool']['cards'] and 'pool v1 -> v2' in line2['refill'])
    world['pool'] = {'version': 5, 'cards': [{'id': 'POOL1', 'title': 'a card'}], 'at': now_iso()}
    line2b = one_pass(deps)
    check('pool: a card the laptop publishes again (a pop it never saw) is not queued twice - the queue dedupes it',
          world['pool']['version'] == 6 and not world['pool']['cards'] and '0 card(s)' in line2b['refill']
          and [t['id'] for t in world['queues']['0']['tasks']].count('POOL1') == 1)
    reads = {'n': 0}

    def fetch_moves(rel):
        if rel in POOL_RELS:
            reads['n'] += 1
            return {'body': json.dumps({'version': reads['n'], 'cards': [{'id': 'POOL2', 'title': 'b'}], 'at': now_iso()})}
        return fetch(rel)

    before = json.dumps(world['pool'])
    moved2, note2 = pop_pool(fetch_moves, post, {'0': 0}, ['0'])
    check('pool: a version that moves between its read and its write is never overwritten (re-read, the card stays)',
          'not written back' in note2 and 'POOL2' in [t['id'] for t in world['queues']['0']['tasks']]
          and json.dumps(world['pool']) == json.dumps(json.loads(before)))
    world['pool'] = {'version': 9, 'cards': [], 'at': now_iso()}
    moved3, note3 = pop_pool(fetch, post, {'0': 0}, ['0'])
    check('pool: an empty pool moves nothing and says so', moved3 == 0 and 'no card held' in note3)
    before_logs, before_posts = len(world['logs']), len(world['posts'])
    dry_line = one_pass(deps, dry=True)
    check('pass --dry: nothing dispatched, nothing logged, no queue and no pool written',
          dry_line['dispatched'][0].get('dry') and len(world['logs']) == before_logs and len(world['posts']) == before_posts)
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
            'post': lambda kind, slug, body: http_json(HUB + '/api/artifacts', token,
                                                       {'type': kind, 'slug': slug, 'title': 'the fleet loop (%s)' % kind,
                                                        'body': json.dumps(body, ensure_ascii=False)}),
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
