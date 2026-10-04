import argparse
import os
from pathlib import Path
from aiohttp import web
from .app import create_app
from .config import Config


def main():
    parser = argparse.ArgumentParser(description="Free-only personal API router")
    parser.add_argument("--config", default="config/router.json")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--port", type=int, default=3100)
    parser.add_argument("--database", default="data/router.db")
    args = parser.parse_args()
    config = Config.load(args.config)
    if args.check:
        print(f"Configuration valid: {len(config.models)} routes; {sum(config.eligible(m) for m in config.models.values())} eligible. No API calls made.")
        return
    os.umask(0o077)
    Path(args.database).parent.mkdir(parents=True, exist_ok=True)
    web.run_app(create_app(config,database=args.database,config_path=args.config), host="127.0.0.1", port=args.port,
                access_log=None, handler_cancellation=True, print=None)


if __name__ == "__main__": main()
