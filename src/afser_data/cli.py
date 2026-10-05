import argparse
import html
import json
import logging
import os
import signal
import threading
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .authentication import TokenRegistry
from .config import BridgeConfig, load_json, private_dir, private_write
from .http_bridge import BridgeServer
from .store import Store
from .sync import SyncManager


def make_invite(directory: Path, website: str, endpoint: str, label: str, days: int = 30) -> Path:
    site = urlsplit(website)
    api = urlsplit(endpoint)
    if not site.netloc or (site.scheme != "https" and not (site.scheme == "http" and site.hostname in {"localhost", "127.0.0.1"})):
        raise ValueError("website_requires_https")
    if not api.netloc or (api.scheme != "https" and not (api.scheme == "http" and api.hostname in {"localhost", "127.0.0.1"})):
        raise ValueError("endpoint_requires_https")
    if site.fragment or site.query or site.username or api.fragment or api.query or api.username:
        raise ValueError("invalid_invite_url")
    token = TokenRegistry(directory).create(label, days)
    link = website.rstrip("/") + "/#" + urlencode({"api": endpoint.rstrip("/"), "token": token})
    config = load_json(directory / "bridge.json", {})
    origin = f"{site.scheme}://{site.netloc}"
    config["origins"] = list(dict.fromkeys(["http://localhost:5173", "http://127.0.0.1:5173", *config.get("origins", ()), origin]))
    if api.scheme == "https":
        config["remoteHosts"] = list(dict.fromkeys([*config.get("remoteHosts", ()), api.hostname]))
    private_write(directory / "bridge.json", json.dumps(config, indent=2))
    output = directory / "invite.html"
    private_write(output, '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Private AFSER map invite</title><style>body{font:18px system-ui;max-width:650px;margin:60px auto;padding:20px}a{display:inline-block;padding:14px;background:#153f35;color:white;border-radius:8px}</style><h1>Your AFSER map access</h1><p>This private invite allows anonymous map access for ' + str(days) + ' days. Share the link only with the intended AFS volunteer. AFSER still handles its own login.</p><p><a rel="noreferrer" href="' + html.escape(link, quote=True) + '">Open the map</a></p><p>Access label: ' + html.escape(label) + '</p><p>Keep this local file private. Use revoke-all to invalidate all existing invites.</p></html>')
    return output


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Local private AFSER sync and anonymous map API")
    parser.add_argument("--data-dir", type=Path, help="private data directory excluded from Git")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="start loopback bridge and automatic source polling")
    serve.add_argument("--no-poll", action="store_true", help="serve latest local snapshot without scheduled sync")
    serve.add_argument("--skip-initial-sync", action="store_true", help="wait one polling interval before first scheduled sync")
    serve.add_argument("--frontend-dir", type=Path, help="serve only safe static frontend files from this directory")
    sub.add_parser("sync", help="run a complete atomic source sync; print aggregate counts only")
    sub.add_parser("status", help="print sanitized sync status")
    sub.add_parser("mcp", help="start the official MCP stdio server")
    sub.add_parser("reproject-cached", help="rebuild anonymous locations from existing caches without network access")
    export = sub.add_parser("export-public", help="explicitly export only anonymous active projections for static hosting")
    export.add_argument("--output", type=Path, required=True)
    invite = sub.add_parser("invite", help="write a private invite HTML file; never print its token")
    invite.add_argument("--website", required=True)
    invite.add_argument("--endpoint", default="http://127.0.0.1:8765")
    invite.add_argument("--label", default="AFS volunteer")
    invite.add_argument("--days", type=int, default=30)
    sub.add_parser("revoke-all", help="invalidate every map invite")
    args = parser.parse_args()
    directory = private_dir(args.data_dir)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.command == "invite":
            path = make_invite(directory, args.website, args.endpoint, args.label, args.days)
            print(f"Private invite written to {path}. Open this file locally to see its access link.")
            return
        if args.command == "revoke-all":
            TokenRegistry(directory).revoke_all()
            print("All map invites revoked.")
            return
        store = Store(directory)
        if args.command == "reproject-cached":
            from .cached_source import CachedSource
            result = SyncManager(store).run(CachedSource(load_json(directory / 'source.json', {}), directory))
            print(json.dumps(result))
            raise SystemExit(0 if result['ok'] else 1)
        if args.command == "export-public":
            from .public_export import export_public
            print(json.dumps(export_public(store, args.output)))
            return
        if args.command == "sync":
            result = SyncManager(store).run()
            print(json.dumps(result))
            raise SystemExit(0 if result["ok"] else 1)
        if args.command == "status":
            print(json.dumps(store.status()))
            return
        if args.command == "mcp":
            from .mcp_server import create_server

            create_server(store).run(transport="stdio")
            return
        config = BridgeConfig.load(directory)
        server = BridgeServer(store, config, frontend_dir=args.frontend_dir)
        stop = threading.Event()
        if not args.no_poll:
            threading.Thread(target=SyncManager(store).poll, args=(stop, config.poll_seconds, not args.skip_initial_sync), daemon=True).start()

        def shutdown(signum, frame):
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        logging.info("anonymous bridge listening on http://127.0.0.1:%s", config.port)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            stop.set()
            server.server_close()
    except (ValueError, OSError):
        # Do not leak token-bearing paths, source config or request values.
        logging.error("configuration or local file error; check the private setup")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
