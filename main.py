"""
Preparation from the terminal (research + drafts) for one account.
It never sends: review and send from the dashboard.

    python main.py --email you@example.com --limit 10
    python main.py --email you@example.com            # everything still pending

The dashboard's "Start preparation" button does the same thing through the
background worker. This entry point is for operators and scripts.
"""

import argparse
import sys

import manage


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="the account to prepare for")
    parser.add_argument("--limit", type=int, default=None, help="prepare at most N companies that still need work")
    parser.add_argument("--all", action="store_true", help="include rows already prepared")
    args = parser.parse_args()
    argv = ["prepare", "--email", args.email]
    if args.limit:
        argv += ["--limit", str(args.limit)]
    if args.all:
        argv.append("--all")
    manage.main(argv)


if __name__ == "__main__":
    sys.exit(main())
