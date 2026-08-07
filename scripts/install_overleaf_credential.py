#!/usr/bin/env python3
"""Store the Overleaf git token once, so unattended runs can push.

Run this after `scripts/install_schedule.py`, or any time the token is rotated:

    python scripts/install_overleaf_credential.py

Get the URL from Overleaf → Menu → Git. The dialog shows the project URL and the
token separately; both are needed:

    https://git:olp_YOUR_TOKEN@git.overleaf.com/YOUR_PROJECT_ID

Three ways to supply it, in the order they are tried:

    ~/.config/publications/overleaf_git_url   write the URL there, then run this
    $OVERLEAF_GIT_URL                         exported, as CI does it
    a hidden prompt                           the interactive default

The file exists for when whoever runs this is not whoever has the token -- an
assistant, a script, a second machine. It is outside the repository so no `git
add -A` can reach it, and it is deleted once the token is in the credential store,
because the whole point is not to leave a token in plaintext. `--keep` overrides
that.

Whichever way it arrives, the URL goes straight into git's credential store: not
echoed, not written into the working tree, not passed on a command line.
"""

import argparse
import getpass
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import overleaf_auth

OVERLEAF_DIR = os.path.join(ROOT, "overleaf")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url-file", default=overleaf_auth.URL_FILE,
                        help="file holding the URL (default: %(default)s)")
    parser.add_argument("--keep", action="store_true",
                        help="do not delete the URL file after storing the token")
    args = parser.parse_args(argv)

    if overleaf_auth.is_inside(args.url_file, ROOT):
        print(f"Refusing to read {args.url_file}: it is inside the repository, "
              f"where `git add -A` would commit the token.\nMove it to "
              f"{overleaf_auth.URL_FILE} and run this again.")
        return 1

    # `to_delete` is set only for the file, because it is the only source that
    # leaves the token lying around after this runs.
    to_delete = None
    url = overleaf_auth.url_from_file(args.url_file)
    if url:
        print(f"Using the URL from {args.url_file}.")
        to_delete = None if args.keep else args.url_file
    else:
        url = overleaf_auth.url_from_env()
        if url:
            print(f"Using the URL from ${overleaf_auth.ENV_VAR}.")
    if not url:
        print("From Overleaf → Menu → Git. The dialog shows the project URL and "
              "the token\nseparately; the URL below needs both:\n"
              "  https://git:olp_YOUR_TOKEN@git.overleaf.com/YOUR_PROJECT_ID\n")
        try:
            url = getpass.getpass("Overleaf git URL (not echoed): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return 1
    if not url:
        print("Nothing entered.")
        return 1

    try:
        helper = overleaf_auth.store_credential(url, OVERLEAF_DIR)
    except ValueError as exc:
        print(f"Not stored: {overleaf_auth.redact(exc)}")
        return 1
    print(f"Stored via credential.helper '{helper}'.")

    # Prove it, rather than reporting success for having written something: a
    # token can be stored under the right host and still be revoked.
    problem = overleaf_auth.check_credential(OVERLEAF_DIR)
    if problem:
        print("\nStored, but Overleaf still refuses it:")
        print(f"  {problem.splitlines()[-1].strip()}")
        print("  A token that was revoked, or a URL whose project ID is not "
              "yours, both look like this.")
        if to_delete:
            print(f"  Left {to_delete} in place so you can correct it.")
        return 1
    print("Verified: git can reach the Overleaf project. Step 7 will push.")

    # Only now: a file deleted before the token was proven to work would leave
    # nothing to retry from.
    if to_delete:
        try:
            os.remove(to_delete)
            print(f"Removed {to_delete} — the token now lives only in the "
                  f"credential store.")
        except OSError as exc:
            print(f"Could not remove {to_delete} ({exc}). Delete it by hand: it "
                  f"holds the token in plaintext.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
