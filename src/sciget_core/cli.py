"""The `sciget` command."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import webbrowser
from pathlib import Path

from sciget_core import __version__, engines, plan
from sciget_core.catalog import CatalogError
from sciget_core.engines import EngineError
from sciget_core.manager import Manager, SessionError


def _print_sessions(sessions) -> None:
    if not sessions:
        print("No sessions.")
        return
    print(f"{'ID':10} {'DOMAIN':12} {'ENGINE':10} {'STATE':9} {'PORT':6} DATA")
    for s in sessions:
        print(f"{s.id:10} {s.domain:12} {s.engine:10} {s.state:9} {s.port:<6} {s.data_dir or '-'}")


def cmd_domains(m: Manager, args) -> int:
    for d in m.domains().values():
        print(f"{d.name:12} {d.registry}:{d.default_version}")
        if d.description:
            print(f"{'':12} {d.description.splitlines()[0]}")
    return 0


def cmd_engines(m: Manager, args) -> int:
    for e in m.engines():
        status = "available" if e.available else "not found"
        extra = f" {e.version}" + (" rootless" if e.rootless else "") if e.available else ""
        print(f"{e.kind:10} {status}{extra}")
    return 0


def cmd_remotes(m: Manager, args) -> int:
    for url in m.domain(args.domain).remote_urls:
        print(url)
    return 0


def _request(args) -> dict:
    req = {
        "domain": args.domain,
        "engine": args.engine,
        "version": args.version,
        "data_dir": str(Path(args.data).expanduser()) if args.data else None,
        "cvmfs": args.cvmfs,
        "sudo": not args.no_sudo,
        "privileged": args.privileged,
        "mac_address": args.mac_address,
    }
    if args.gpu:
        req["gpu"] = True
    return {k: v for k, v in req.items() if v is not None}


def cmd_start(m: Manager, args) -> int:
    request = _request(args)
    if args.dry_run:
        _, launch, session = m.prepare(request)
        print(f"# engine={session.engine} cvmfs={session.cvmfs} port={session.port}")
        print(f"# JUPYTER_TOKEN is passed in the environment, not shown here.")
        print(shlex.join(session.argv))
        return 0
    session = m.start(request)
    if session.state != "ready":
        print(f"Session {session.id} failed: {session.error}", file=sys.stderr)
        print(f"See its output with: sciget logs {session.id}", file=sys.stderr)
        return 1
    print(f"Session {session.id} is ready:\n  {session.url}")
    if args.open:
        webbrowser.open(session.url)
    return 0


def cmd_list(m: Manager, args) -> int:
    sessions = m.list()
    if args.json:
        print(json.dumps([s.to_json(include_url=False) for s in sessions], indent=2))
    else:
        _print_sessions(sessions)
    return 0


def cmd_url(m: Manager, args) -> int:
    print(m.get(args.id).url)
    return 0


def cmd_open(m: Manager, args) -> int:
    session = m.get(args.id)
    if session.state != "ready":
        print(f"Session {session.id} is {session.state}, not ready.", file=sys.stderr)
        return 1
    webbrowser.open(session.url)
    return 0


def cmd_stop(m: Manager, args) -> int:
    targets = [s.id for s in m.list() if s.state != "stopped"] if args.all else args.ids
    if not targets:
        print("Nothing to stop. Give a session ID, or --all.")
        return 0 if args.all else 2
    for sid in targets:
        print(f"{m.stop(sid).id} stopped")
    return 0


def cmd_rm(m: Manager, args) -> int:
    for sid in args.ids:
        m.forget(sid)
    return 0


def cmd_logs(m: Manager, args) -> int:
    for line in m.logs(args.id, args.tail):
        print(line)
    return 0


def cmd_serve(m: Manager, args) -> int:
    from sciget_core.serve import serve

    serve(m, args.port, args.open)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sciget", description="Start and manage Sciget container sessions.")
    p.add_argument("--version", action="version", version=f"sciget-core {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("domains", help="list the available domains").set_defaults(func=cmd_domains)
    sub.add_parser("engines", help="show which container engines are installed").set_defaults(func=cmd_engines)
    r = sub.add_parser("remotes", help="list hosted servers for a domain")
    r.add_argument("--domain", default="neurodesk")
    r.set_defaults(func=cmd_remotes)

    s = sub.add_parser("start", help="start a session")
    s.add_argument("data", nargs="?", help="folder to mount at /data")
    s.add_argument("--domain", default="neurodesk")
    s.add_argument("--engine", choices=("auto", *engines.KINDS))
    s.add_argument("--version", dest="version", metavar="TAG", help="image tag (default: the domain's)")
    s.add_argument("--cvmfs", choices=plan.CVMFS_MODES,
                   help="host: use /cvmfs from this computer; container: mount it inside; off: download tools")
    s.add_argument("--gpu", action="store_true", help="give the session the NVIDIA GPUs")
    s.add_argument("--no-sudo", action="store_true", help="do not grant sudo inside the session")
    s.add_argument("--privileged", action="store_true",
                   help="run the container privileged, as upstream Sciget App does (not recommended)")
    s.add_argument("--mac-address", help="fixed MAC address, for software licensed to one")
    s.add_argument("--open", action="store_true", help="open JupyterLab in the browser when ready")
    s.add_argument("--dry-run", action="store_true", help="print the engine command and exit")
    s.set_defaults(func=cmd_start)

    ls = sub.add_parser("list", help="list sessions")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)
    for name, func, text in (("url", cmd_url, "print a session's JupyterLab address"),
                             ("open", cmd_open, "open a session in the browser")):
        x = sub.add_parser(name, help=text)
        x.add_argument("id")
        x.set_defaults(func=func)
    st = sub.add_parser("stop", help="stop sessions")
    st.add_argument("ids", nargs="*")
    st.add_argument("--all", action="store_true")
    st.set_defaults(func=cmd_stop)
    rm = sub.add_parser("rm", help="forget stopped sessions")
    rm.add_argument("ids", nargs="+")
    rm.set_defaults(func=cmd_rm)
    lg = sub.add_parser("logs", help="show a session's output")
    lg.add_argument("id")
    lg.add_argument("--tail", type=int, default=200)
    lg.set_defaults(func=cmd_logs)
    sv = sub.add_parser("serve", help="run the local web UI and API")
    sv.add_argument("--port", type=int, default=0)
    sv.add_argument("--open", action="store_true", help="open the UI in the browser")
    sv.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(Manager(), args)
    except (SessionError, EngineError, plan.PlanError, CatalogError) as exc:
        print(f"sciget: {exc}", file=sys.stderr)
        return 1
