"""Apply or roll back the exact route used in the Synapse discovery demo."""

import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("apply", "rollback"))
    args = parser.parse_args()
    service = Path(__file__).resolve().parents[1] / "services/tasks"
    route = "\n\n" + (service / "routine_preferences.py.stage").read_text(encoding="utf-8")
    path = service / "main.py"
    source = path.read_text(encoding="utf-8")
    applied = source.endswith(route)
    if not applied and '@app.get("/routine-preferences"' in source:
        parser.error("The route is present but differs from the staged patch. Review main.py before continuing.")
    if args.operation == "apply" and not applied:
        path.write_text(source + route, encoding="utf-8", newline="\n")
    if args.operation == "rollback" and applied:
        path.write_text(source.removesuffix(route), encoding="utf-8", newline="\n")
    print(f"Tasks route /routine-preferences: {'present' if args.operation == 'apply' else 'absent'}")


if __name__ == "__main__":
    main()
