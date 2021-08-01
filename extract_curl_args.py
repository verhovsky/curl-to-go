#!/usr/bin/env python3

from pathlib import Path
import warnings
import pickle
import sys
import subprocess
from collections import namedtuple
import itertools
from operator import itemgetter

# TODO: make this and the location of the repo relative to script location
# TODO: and command line args
OUTPUT_FILE = Path("./resources/js/curl-to-go.js")
if not OUTPUT_FILE.is_file():
    sys.exit(
        f"{OUTPUT_FILE} doesn't exist. You should run this script from curl-to-go/"
    )

PATH_TO_CURL_REPO = Path("../curl")
if not PATH_TO_CURL_REPO.is_dir():
    sys.exit(
        f"{PATH_TO_CURL_REPO} needs to be a git repo with cURL's source code. "
        "You can clone it with\n\n"
        "git clone https://github.com/curl/curl ../curl"
        # or modify the PATH_TO_CURL_REPO variable
    )


PARAMS_CACHE = Path("curl_params.pickle")
SHOULD_CACHE = True

JS_PARAMS_START = "BEGIN GENERATED CURL OPTIONS"
JS_PARAMS_END = "END GENERATED CURL OPTIONS"

# The first commit in cURL's git repo (from 1999)
# ae1912cb0d494b48d514d937826c9fe83ec96c4d
# has args defined in src/main.c, then in
# 49b79b76316248d5233d08006234933913faaa3b
# the arg definitions were moved to src/tool_getparam.c
FILENAMES = ["./src/main.c", "./src/tool_getparam.c"]

# Originally there were only two arg "types": TRUE/FALSE which signified
# whether the option expected a value or was a boolean (respectively).
# Then in
IMPLICIT_NO_COMMIT = "5abfdc0140df0977b02506d16796f616158bfe88"
# all boolean (i.e. FALSE "type") options got an implicit --no-OPTION.
# Then TRUE/FALSE was changed to ARG_STRING/ARG_BOOL.
# Then it was realized that not all options should have a --no-OPTION
# counterpart, so a new ARG_NONE type was added for those in
# 913c3c8f5476bd7bc4d8d00509396bd4b525b8fc

OPTS_START = "struct LongShort aliases[]= {"
OPTS_END = "};"

BOOL_TYPES = ["FALSE", "ARG_BOOL", "ARG_NONE"]
STR_TYPES = ["TRUE", "ARG_STRING", "ARG_FILENAME"]
DESC_TYPES = BOOL_TYPES + STR_TYPES

# TODO: merge these 2 dicts
DUPES = {
    ("krb", "krb4"): "krb",
    ("http-request", "request"): "request",
    ("ftp-ascii", "use-ascii"): "use-ascii",
    ("ftp-port", "ftpport"): "ftp-port",
    ("socks", "socks5"): "socks5",
    ("socks", "socks5ip"): "socks5",  # TODO: what?
    ("ftp-ssl", "ssl"): "ssl",
    ("ftp-ssl-reqd", "ssl-reqd"): "ssl-reqd",
    ("proxy-service-name", "socks5-gssapi-service"): "proxy-service-name",
}
NAME_SPECIAL_CASES = {
    "ftp-ssl": "ssl",
    "no-ftp-ssl": "ssl",
    "ftp-ssl-reqd": "ssl-reqd",
    "no-ftp-ssl-reqd": "ssl-reqd",
    "ftp-ascii": "use-ascii",
    "ftpport": "ftp-port",
    "krb4": "krb",
    "socks5-gssapi-service": "proxy-service-name",
    "socks5": None,
}


def flatten(l):
    return list(itertools.chain.from_iterable(l))


def commits_that_changed(filename):
    lines = subprocess.run(
        [
            "git",
            "log",
            "--diff-filter=d",
            "--date-order",
            "--reverse",
            "--format=%H %at",  # full commit hash and author date time stamp
            "--date=iso-strict",
            "--",
            filename,
        ],
        cwd=PATH_TO_CURL_REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in lines.splitlines():
        commit_hash, timestamp = line.strip().split()
        yield commit_hash, int(timestamp)


warned_repeats = set()


def get_aliases(file_contents):
    lines = iter(file_contents.splitlines())
    aliases = {}
    for line in lines:
        if OPTS_START in line:
            break
    for line in lines:
        line = line.strip()
        if line.endswith(OPTS_END):
            break
        if not line.strip().startswith("{"):
            continue

        # main.c has comments on the same line
        letter, lname, desc = line.split("/*")[0].strip().strip("{},").split(",")

        letter = letter.strip().strip('"')
        lname = lname.strip().strip('"')
        desc = desc.strip()

        if desc not in DESC_TYPES:
            raise ValueError(f"unknown desc: {desc!r}")

        if 1 > len(letter) > 2:
            raise ValueError(f"letter form of --{lname} must be 1 or 2 characters long")

        alias = {"letter": letter, "lname": lname, "desc": desc}

        if lname in aliases and aliases[lname] != alias:
            if lname not in warned_repeats:
                warnings.warn(
                    f"{lname!r} repeated with different values: {aliases[lname]} vs. {alias} "
                )
                warned_repeats.add(lname)

        aliases[lname] = alias

    return list(aliases.values())


def get_explicit_aliases_over_time():
    """yields the command line arguments that appear in the source code over time"""
    implicit_no = False
    for filename in FILENAMES:
        for commit_hash, timestamp in commits_that_changed(filename):
            if commit_hash == IMPLICIT_NO_COMMIT:
                implicit_no = True

            contents = subprocess.run(
                ["git", "cat-file", "-p", f"{commit_hash}:{filename}"],
                cwd=PATH_TO_CURL_REPO,
                capture_output=True,
                check=True,
            ).stdout
            try:
                contents = contents.decode("utf-8")
            except UnicodeDecodeError:
                contents = contents.decode("latin1")
            aliases = get_aliases(contents)
            if not aliases:
                raise ValueError(
                    f"Failed to extract params from {commit_hash}:{filename}"
                )
            yield {"hash": commit_hash, "timestamp": timestamp}, aliases


def add_names(aliases):
    for alias in aliases:
        if alias["desc"] in BOOL_TYPES:
            if alias["lname"].startswith("no-"):
                alias["name"] = alias["lname"].removeprefix("no-")
            if alias["lname"].startswith("disable-"):
                alias["name"] = alias["lname"].removeprefix("disable-")
    return aliases


def simplify_desc(aliases, implicit_no):
    for alias in aliases:
        # unify desc and simplify
        alias["desc"] = {
            "FALSE": "ARG_BOOL" if implicit_no else "ARG_NONE",
            "TRUE": "ARG_STRING",
            "ARG_FILENAME": "ARG_STRING",
        }.get(alias["desc"], alias["desc"])
    return aliases


def group_same(aliases):
    """If both --option and --other-option have "oO" (for example) as their `letter`,
    add a "name" property with the main option's `lname`"""
    seen_letters = {}
    for alias in aliases:
        seen_letters.setdefault(alias["letter"], []).append(alias["lname"])
    dup_aliases = {}
    for letter, lnames in seen_letters.items():
        lnames = tuple(sorted(lnames))
        if len(lnames) > 1:
            if lnames not in DUPES:
                raise ValueError(
                    f"The options {lnames} are the same option, they have the "
                    f"same letter {letter!r}. Which one is the main one?"
                )
            dup_aliases[letter] = DUPES[lnames]

    for alias in aliases:
        if alias["letter"] in dup_aliases:
            if alias["lname"] != dup_aliases[alias["letter"]]:
                # name, not lname
                alias["name"] = dup_aliases[alias["letter"]]
    return aliases


def add___no(aliases):
    to_insert = []  # can't iterate aliases and insert stuff at the same time
    for idx, alias in enumerate(aliases):
        # The BOOL/NONE distinction is no longer important
        is_bool = alias["desc"] == "ARG_BOOL"
        if alias["desc"] == "ARG_NONE":
            alias["desc"] = "ARG_BOOL"

        if is_bool:
            no_alias = {**alias, "lname": "no-" + alias["lname"]}
            if "name" not in no_alias:
                no_alias["name"] = alias["lname"]
            no_alias["expand"] = False
            to_insert.append((idx + 1, no_alias))
    for i, no_alias in to_insert:
        aliases.insert(i, no_alias)

    return aliases


def special_case_names(aliases):
    for alias in aliases:
        if alias["lname"] in NAME_SPECIAL_CASES:
            alias["name"] = NAME_SPECIAL_CASES[alias["lname"]]
            if alias["name"] is None:
                del alias["name"]
    return aliases


# TODO: does raymond's code on stackoverflow count all evenly spaced
# sequences (e.g. [2, 4, 6, 8]) as a consecutive run?
# for _, g in itertools.groupby(enumerate(seq), lambda i_x: i_x[0] - i_x[1]):
#     result = list(map(itemgetter(1), g))
#     yield result[0], result[-1]
def consecutive_runs(seq):
    if not seq:
        return []

    runs = []
    run = [seq[0]]
    for prev, cur in zip(seq, seq[1:]):
        if cur - prev == 1:
            run.append(cur)
        elif cur - prev == 0:
            raise ValueError("overlapping range: {seq}")
        else:
            runs.append((run[0], run[-1]))
            run = [cur]
    runs.append((run[0], run[-1]))
    return runs


def find_deleted(aliases_over_time):
    hashes = [c["hash"] for c, _ in aliases_over_time]
    to_hash = {i: c for i, c in enumerate(hashes)}

    long_args = {}
    short_args = {}

    for commit_idx, (commit, aliases) in enumerate(aliases_over_time):
        for alias in aliases:
            # At which commits did this option (with this type) exist
            long_opt_key = tuple(
                {k: v for k, v in alias.items() if k != "letter"}.items()
            )
            short_opt_key = (alias["letter"], alias.get("name", alias["lname"]))
            long_args.setdefault(long_opt_key, []).append(commit_idx)
            if len(alias["letter"]) == 1:
                # don't add duplicates
                if "name" not in alias:
                    short_args.setdefault(short_opt_key, []).append(commit_idx)

    long_args = {k: consecutive_runs(v) for k, v in long_args.items()}
    short_args = {k: consecutive_runs(v) for k, v in short_args.items()}

    import pprint

    pprint.pprint(long_args)
    pprint.pprint(short_args)

    _long_args = [
        (k, v) for k, v in sorted(long_args.items(), key=lambda x: dict(x[0])["lname"])
    ]
    for g, l in itertools.groupby(_long_args, key=lambda x: dict(x[0])["lname"]):
        l = list(l)
        if len(l) > 2:
            print(g)
            for x in l:
                print(x)
            print()
        if len(l) == 2:
            # Used to set NAME_SPECIAL_CASES
            print(g)
            for x in l:
                print(x)
            print()
            # backdate the "name"s of renamed options
            # first, second = dict(l[0][0]), dict(l[1][0])
            # first_no_name = {k: v for k, v in first.items() if k != "name"}
            # second_no_name = {k: v for k, v in second.items() if k != "name"}
            # if first_no_name == second_no_name:
            #     pass

    _short_args = [(k, v) for k, v in sorted(short_args.items(), key=lambda x: x[0][0])]
    for g, l in itertools.groupby(_short_args, key=lambda x: x[0][0]):
        l = list(l)
        if len(l) > 1:
            print(g)
            for x in l:
                print(x)
            print()

    # --metalink became a boolean
    # 'metalink': [('string', [(14917, 15129)]), ('bool', [(15129, None)])],

    # this option was removed more than once
    # ('sasl-authzid', 'ARG_STRING', None)
    # This one just had the short option changed and put back I think
    # ('http1.0', '0')

    new_long_args = []
    new_short_args = {}
    for arg_data, commits in long_args.items():
        lifetimes = [
            (
                start if start > 0 else None,
                (end + 1) if ((end + 1) < len(aliases_over_time)) else None,
            )
            for start, end in commits
        ]

        arg_data = dict(arg_data)
        arg_data["type"] = arg_data.pop("desc").removeprefix("ARG_").lower()
        if "name" not in arg_data:
            # one arg had a trailing space
            name = arg_data["lname"].strip()
            if name != arg_data["lname"]:
                arg_data["name"] = name
        ends = [l[1] for l in lifetimes]
        if None not in ends:
            arg_data["deleted"] = to_hash[max(ends)]

        lname = arg_data["lname"]
        new_arg_data = {}
        # sort consistently
        for k in ["type", "name", "deleted", "expand"]:
            if k in arg_data:
                new_arg_data[k] = arg_data[k]
        new_long_args.append((lname, new_arg_data))

    for (short, long), commits in short_args.items():
        lifetimes = [
            (
                start if start > 0 else None,
                (end + 1) if ((end + 1) < len(aliases_over_time)) else None,
            )
            for start, end in commits
        ]

        # -N is short for --no-buffer
        if short == "N":
            long = "no-" + long

        arg_data = {"long": long}
        ends = [l[1] for l in lifetimes]
        deleted = None not in ends
        if deleted:
            arg_data["deleted"] = to_hash[max(ends)]

        if short in new_short_args:
            if new_short_args[short].get("deleted"):
                new_short_args[short] = arg_data
        else:
            new_short_args[short] = arg_data
    return new_long_args, new_short_args


def format_as_js(long_args, short_args):
    def as_js_dict(d, var_name):
        yield f"\tvar {var_name} = {{"
        for top_key, opt_dict in d.items():

            def quote(key):
                return key if key.isalpha() else repr(key)

            def val_to_js(val):
                if isinstance(val, str):
                    return repr(val)
                if isinstance(val, bool):
                    return str(val).lower()
                raise TypeError(f"can't convert values of type {type(val)} to JS")

            vals = [f"{quote(k)}: {val_to_js(v)}" for k, v in opt_dict.items()]

            yield f"\t\t{top_key!r}: {{{', '.join(vals)}}},"
        yield "\t}"

    def as_js_list(d, var_name):
        yield f"\tvar {var_name} = ["
        for top_key, opt_dict in d:

            def quote(key):
                return key if key.isalpha() else repr(key)

            def val_to_js(val):
                if isinstance(val, str):
                    return repr(val)
                if isinstance(val, bool):
                    return str(val).lower()
                raise TypeError(f"can't convert values of type {type(val)} to JS")

            vals = [f"{quote(k)}: {val_to_js(v)}" for k, v in opt_dict.items()]

            yield f"\t\t[{top_key!r}, {{{', '.join(vals)}}}],"
        yield "\t]"

    yield from as_js_list(long_args, "extractedLongOptions")
    yield from as_js_dict(short_args, "extractedShortOptions")


def on_git_master():
    output = subprocess.run(
        ["git", "status", "-uno"], cwd=PATH_TO_CURL_REPO, capture_output=True, text=True
    ).stdout.strip()
    return output.startswith("On branch master")


if __name__ == "__main__":
    if not on_git_master():
        sys.exit("not on curl's git master")

    # cache because this takes a few seconds
    if SHOULD_CACHE:
        if not PARAMS_CACHE.is_file():
            explicit_aliases_over_time = list(get_explicit_aliases_over_time())
            with open(PARAMS_CACHE, "wb") as f:
                pickle.dump(explicit_aliases_over_time, f)
        else:
            with open(PARAMS_CACHE, "rb") as f:
                explicit_aliases_over_time = pickle.load(f)
    else:
        explicit_aliases_over_time = list(get_explicit_aliases_over_time())

    aliases_over_time = []
    after_implicit_no = False
    for commit, aliases in explicit_aliases_over_time:
        if commit["hash"] == IMPLICIT_NO_COMMIT:
            after_implicit_no = True

        add_names(aliases)
        simplify_desc(aliases, after_implicit_no)

        # For example --ftp-ssl is a deprecated alias for --ssl. They are the same option.
        # Assume the one that comes second in the code is the real one.
        group_same(aliases)

        # Add --no-OPTION for ARG_BOOL options
        add___no(aliases)

        special_case_names(aliases)

        aliases_over_time.append((commit, aliases))

    # Find which options were deleted and which commit they were deleted in
    long_aliases, short_aliases = find_deleted(aliases_over_time)

    js_params_lines = format_as_js(long_aliases, short_aliases)

    new_lines = []
    with open(OUTPUT_FILE) as f:
        for line in f:
            new_lines.append(line)
            if JS_PARAMS_START in line:
                break
        else:
            raise ValueError(f"{'// ' + JS_PARAMS_START!r} not in {OUTPUT_FILE}")

        new_lines += [l + "\n" for l in js_params_lines]
        for line in f:
            if JS_PARAMS_END in line:
                new_lines.append(line)
                break
        else:
            raise ValueError(f"{'// ' + JS_PARAMS_END!r} not in {OUTPUT_FILE}")
        for line in f:
            new_lines.append(line)
    with open(OUTPUT_FILE, "w", newline="\n") as f:
        f.write("".join(new_lines))
