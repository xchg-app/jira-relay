"""jira-relay command line."""
import argparse
import json
import signal
import sys
import time

from . import __version__, admin, config
from .gateway import service
from .jira import JiraError
from .tracker import hub, ops, outbounds
from .tracker.poll import log
from .tracker.state import lock

# commands that write to the state and the hub: never concurrently with the poll cycle (run takes the lock itself)
LOCKED = {'add', 'to', 'subtasks', 'rm', 'poll'}


def snapshot_mode(args) -> str:
    if getattr(args, 'dry_run', False):
        return 'dry'
    if getattr(args, 'no_snapshot', False):
        return 'none'
    return 'send' if getattr(args, 'yes', False) else 'ask'


def cmd_show(cfg: config.Config, args) -> int:
    ops.show(cfg, args.key, args.with_subtasks, args.raw)
    return 0


def owner(args):
    return (args.by, '') if getattr(args, 'by', None) else None


def cmd_add(cfg: config.Config, args) -> int:
    ops.add(cfg, args.key, args.addresses, args.with_subtasks, snapshot_mode(args), owner(args))
    return 0


def cmd_to(cfg: config.Config, args) -> int:
    if args.action == 'rm':
        ops.remove_address(cfg, args.key, args.address)
    else:
        ops.add_address(cfg, args.key, args.address, snapshot_mode(args), owner(args))
    return 0


def cmd_subtasks(cfg: config.Config, args) -> int:
    ops.subtasks(cfg, args.key, args.mode == 'on', snapshot_mode(args))
    return 0


def cmd_rm(cfg: config.Config, args) -> int:
    ops.untrack(cfg, args.key)
    return 0


def cmd_ls(cfg: config.Config, args) -> int:
    ops.listing(cfg)
    return 0


def cmd_poll(cfg: config.Config, args) -> int:
    return ops.poll_once(cfg, args.dry_run)


def cmd_run(cfg: config.Config, args) -> int:
    """Poll cycle for the container: Jira/hub errors do not kill the process, the next cycle retries."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log(f'jira-relay {__version__} started: polling every {args.interval or cfg.interval} s')
    while True:
        started = time.monotonic()
        try:
            cfg = config.load()  # admin commands change the config file while the service runs
            with lock(cfg.state_dir):
                ops.poll_once(cfg, dry_run=False)
        except (config.ConfigError, JiraError, hub.HubError, OSError) as e:
            log(f'cycle failed: {e}')
        time.sleep(max(1.0, (args.interval or cfg.interval) - (time.monotonic() - started)))


def cmd_agent(cfg: config.Config, args) -> int:
    """Agent request from bin/jira (JSON on stdin) -> JSON reply on stdout. Does not block the poll cycle.

    --user is a registered login, resolved by the caller (never taken from the request JSON)."""
    try:
        reply = service.serve(cfg, args.user, json.load(sys.stdin))
    except ValueError as e:
        reply = {'ok': False, 'error': f'request is not JSON: {e}', 'version': __version__}
    json.dump(reply, sys.stdout, ensure_ascii=False)
    return 0 if reply['ok'] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='jira-relay', description='Relay Jira issue changes to outbounds and agent '
                                                                    'actions to Jira')
    parser.add_argument('--version', action='version', version=f'jira-relay {__version__}')
    sub = parser.add_subparsers(dest='command', required=True)

    show = sub.add_parser('show', help='render an issue snapshot without sending anything')
    show.add_argument('key', nargs='?')
    show.add_argument('--with-subtasks', action='store_true', help='also snapshots of subtasks')
    show.add_argument('--raw', metavar='FILE', help='read the issue JSON from a file instead of Jira')
    show.set_defaults(handler=cmd_show)

    add = sub.add_parser('add', help='track an issue: DEMO-42 team:@backend [me:alice ...]')
    add.add_argument('key')
    add.add_argument('addresses', nargs='+', metavar='address',
                     help='hub[:@project][:person]; hub alone means everyone in the hub')
    add.add_argument('--with-subtasks', action='store_true', help='with subtasks; new ones are picked up automatically')
    add.add_argument('--by', metavar='LOGIN', help='the registered user who watches it (polling uses their token)')
    choice = add.add_mutually_exclusive_group()
    choice.add_argument('--yes', action='store_true', help='send the snapshot without asking')
    choice.add_argument('--no-snapshot', action='store_true', help='do not send the snapshot')
    choice.add_argument('--dry-run', action='store_true', help='print the snapshots, send and record nothing')
    add.set_defaults(handler=cmd_add)

    subtasks = sub.add_parser('subtasks', help='pick up subtasks of an already tracked issue: DEMO-41 on')
    subtasks.add_argument('key')
    subtasks.add_argument('mode', choices=('on', 'off'))
    choice = subtasks.add_mutually_exclusive_group()
    choice.add_argument('--yes', action='store_true', help='send snapshots of existing subtasks without asking')
    choice.add_argument('--no-snapshot', action='store_true', help='do not send snapshots of existing subtasks')
    choice.add_argument('--dry-run', action='store_true', help='print the snapshots, send and record nothing')
    subtasks.set_defaults(handler=cmd_subtasks)

    to = sub.add_parser('to', help='addresses of a tracked issue: DEMO-42 add me:alice')
    to.add_argument('key')
    to.add_argument('action', choices=('add', 'rm'))
    to.add_argument('address')
    to.add_argument('--by', metavar='LOGIN', help='add: the registered user who watches it')
    choice = to.add_mutually_exclusive_group()
    choice.add_argument('--yes', action='store_true', help='send snapshots to the new address without asking')
    choice.add_argument('--no-snapshot', action='store_true', help='do not send snapshots: only changes from now on')
    choice.add_argument('--dry-run', action='store_true', help='print the snapshots, send and record nothing')
    to.set_defaults(handler=cmd_to)

    rm = sub.add_parser('rm', help='stop tracking an issue (and its subtasks)')
    rm.add_argument('key')
    rm.set_defaults(handler=cmd_rm)

    sub.add_parser('ls', help='what is tracked').set_defaults(handler=cmd_ls)

    poll = sub.add_parser('poll', help='poll Jira once and publish changes (for cron)')
    poll.add_argument('--dry-run', action='store_true', help='print the notes, send and record nothing')
    poll.set_defaults(handler=cmd_poll)

    run = sub.add_parser('run', help='poll in a loop (the container process)')
    run.add_argument('--interval', type=int, help='seconds between cycle starts (default: interval in the config)')
    run.set_defaults(handler=cmd_run)

    admin.add_parsers(sub)

    agent_cmd = sub.add_parser('agent', help='agent request from bin/jira: JSON on stdin, JSON reply on stdout')
    agent_cmd.add_argument('--user', default='', help='the registered login the request is from')
    agent_cmd.set_defaults(handler=cmd_agent)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'show' and not (args.key or args.raw):
        parser.error('show: an issue key or --raw FILE is required')
    try:
        cfg = config.load()
        admin.check_args(args)
        if args.command in LOCKED:
            with lock(cfg.state_dir):
                return args.handler(cfg, args)
        return args.handler(cfg, args)
    except (config.ConfigError, JiraError, hub.HubError, ops.TrackerError, outbounds.OutboundError, OSError) as e:
        print(f'jira-relay: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
