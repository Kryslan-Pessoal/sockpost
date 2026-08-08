"""Command line interface."""

from __future__ import annotations

import argparse
import sys

from . import __version__, client
from .config import load_settings

EPILOG = """\
examples:
  export SOCKPOST_ID=planner
  sockpost listen &                       # stream events for this channel
  sockpost send --to builder --text "run the build"
  sockpost unread                         # queued but unacknowledged messages
  sockpost status                         # who is connected, who answers

environment:
  SOCKPOST_ID         default channel id for --id / --from
  SOCKPOST_SOCKET     daemon socket path
  SOCKPOST_DB         queue database path
  SOCKPOST_AUTOSTART  set to 0 to never start the daemon implicitly
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sockpost",
        description="Local message bus for terminal agents.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version="sockpost %s" % __version__)
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p_listen = sub.add_parser("listen", help="stream events for a channel")
    p_listen.add_argument("--id", dest="channel", help="channel id to listen on")
    p_listen.add_argument("--manual-ack", action="store_true",
                          help="do not acknowledge automatically on delivery")
    p_listen.add_argument("--json", dest="as_json", action="store_true",
                          help="emit raw JSON events instead of key=value lines")

    p_send = sub.add_parser("send", help="queue a message for a channel")
    p_send.add_argument("--from", dest="sender", help="sender channel id")
    p_send.add_argument("--to", dest="recipient", required=True,
                        help="recipient channel id")
    p_send.add_argument("--text", "--message", dest="text", required=True,
                        help="message body")

    p_ack = sub.add_parser("ack", help="acknowledge a delivered message")
    p_ack.add_argument("msg_id", help="message id returned by send")
    p_ack.add_argument("--from", dest="sender",
                       help="channel acknowledging (must be the recipient)")

    p_unread = sub.add_parser("unread", help="list queued, unacknowledged messages")
    p_unread.add_argument("--id", dest="channel", help="only this recipient")

    sub.add_parser("status", help="connected channels and liveness")

    p_ping = sub.add_parser("ping", help="check which channels answer")
    p_ping.add_argument("--to", dest="targets", required=True,
                        help="comma separated channel ids")
    p_ping.add_argument("--timeout", type=float, default=5.0,
                        help="seconds to wait for a reply (default: 5)")

    p_wakeup = sub.add_parser(
        "wakeup", help="periodic heartbeat delivered to a channel")
    p_wakeup.add_argument("channel", nargs="?", help="channel id")
    p_wakeup.add_argument("interval", help="30s, 10min, 2h, 1d or off")

    p_daemon = sub.add_parser("daemon", help="run the daemon in the foreground")
    p_daemon.add_argument("--socket", help="override the socket path")
    p_daemon.add_argument("--db", help="override the queue database path")

    sub.add_parser("stop", help="stop the running daemon")

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2

    if args.command == "daemon":
        import os
        if args.socket:
            os.environ["SOCKPOST_SOCKET"] = args.socket
        if args.db:
            os.environ["SOCKPOST_DB"] = args.db
        from .daemon import run_daemon
        return run_daemon(load_settings())

    settings = load_settings()

    if args.command == "listen":
        return client.cmd_listen(settings, args.channel, args.manual_ack,
                                 args.as_json)
    if args.command == "send":
        return client.cmd_send(settings, args.sender, args.recipient, args.text)
    if args.command == "ack":
        return client.cmd_ack(settings, args.msg_id, args.sender)
    if args.command == "unread":
        return client.cmd_unread(settings, args.channel)
    if args.command == "status":
        return client.cmd_status(settings)
    if args.command == "ping":
        return client.cmd_ping(settings, args.targets, args.timeout)
    if args.command == "wakeup":
        return client.cmd_wakeup(settings, args.channel, args.interval)
    if args.command == "stop":
        return client.cmd_stop(settings)

    parser.print_help()
    return 2


def entrypoint() -> None:  # pragma: no cover - console script wrapper
    try:
        sys.exit(main())
    except BrokenPipeError:
        # A downstream 'head' or 'grep -q' closed the pipe; that is not an error.
        sys.exit(0)
    except KeyboardInterrupt:
        sys.exit(130)
