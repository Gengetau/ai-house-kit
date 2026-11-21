"""Validate the canonical catalogue and emit/check its generated public projection."""
import argparse
import json
from pathlib import Path
from .catalog import load, public_vocabulary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    data = load()
    rendered = json.dumps(public_vocabulary(data), ensure_ascii=False, indent=2) + "\n"
    if args.projection:
        if args.check:
            if not args.projection.exists() or args.projection.read_text(encoding="utf-8") != rendered:
                print("projection differs from canonical catalogue"); return 1
        else:
            args.projection.write_text(rendered, encoding="utf-8")
    print(json.dumps({"status": "valid", "version": data["vocab_version"], "activities": len(data["activities"]),
                      "public_locations": len(data["vocabulary"]["locations"]), "p1a_actions": len(data["p1a"]["action_types"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
