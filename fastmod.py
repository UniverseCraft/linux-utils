#!/usr/local/bin/python3.11 -S
"""\
fastmod: Multithreaded utility for recursively changing permissions.

Runs as a standalone script.
"""

import getpass
import grp
import os
import multiprocessing as mp
import pwd
import sys
import time

__copyright__ = "Copyright (c) 2023 Broadcom Corporation. All rights reserved."
__license__ = "Public Domain"
__version__ = "1.1.3"

# How many files to change per chmod/chgrp command.
DEFAULT_BLOCKSIZE = int(os.environ.get("FASTMOD_BLOCKSIZE", 128))

# How many worker processes to create.
DEFAULT_CORES = int(os.environ.get("FASTMOD_CORES", os.cpu_count() - 1))

# Preset to use if no flag or preset is specified.
DEFAULT_PRESET = os.environ.get("FASTMOD_PRESET", "baseline")

PRESETS = {
    "baseline": {
        "fil": "u+rw,g+r-w,o+r-w",
        "dir": "u+rwx,g+rxs-w,o+rx-w"
    },
    "group-allowed": {
        "fil": "ug+rw,o+r-w",
        "dir": "ug+rwx,g+s,o+rx-w"
    },
    "private": {
        "fil": "u+rw,go-rwx",
        "dir": "u+rwx,g-rwx,o-rwx"
    },
    "private-group": {
        "fil": "ug+rw,o-rwx",
        "dir": "ug+rwx,g+s,o-rwx"
    },
    "readonly": {
        "fil": "a-w,+t",
        "dir": "a-w,+t"
    }
}

PRIMARY_GROUP = grp.getgrgid(pwd.getpwnam(getpass.getuser()).pw_gid).gr_name

# MOCKUP FOR TESTING
# real_system = os.system
# def mock_system(cmd):
#     print(os.getpid(), cmd)
#     ret = real_system(cmd)
#     if ret != 0:
#         print(f"ERROR :: {cmd} failed with exit code {ret}")
# os.system = mock_system


def worker_main(queue, group, quiet, blocksize):
    """Entry point for worker process."""
    buffer = {}
    if quiet:
        chmod = "chmod -f"
        chgrp = f"chgrp -f {group}"
    else:
        chmod = "chmod"
        chgrp = f"chgrp {group}"
    while True:
        root, name, perms = queue.get()
        if name == ".":
            path = root
        else:
            path = f"{root}/{name}"

        if root is None:
            break

        buffer.setdefault(perms, set()).add(path)

        buffered = buffer[perms]
        if len(buffered) >= blocksize:
            joined_paths = ' '.join([f"'{b}'" for b in buffered])
            if group is not None:
                os.system(f"{chgrp} " + joined_paths)
            os.system(f"{chmod} {perms} " + joined_paths)
            buffer[perms].clear()
    for perms, buffered in buffer.items():
        if not buffered:
            continue
        joined_paths = ' '.join([f"'{b}'" for b in buffered])
        if group is not None:
            os.system(f"{chgrp} " + joined_paths)
        os.system(f"{chmod} {perms} " + joined_paths)


def show_help():
    """Displays help information."""
    print("fastmod: Multithreaded utility for recursively changing "
          "permissions.")
    print(f"v{__version__} {__copyright__}")
    print()
    print("Usage:")
    print("  fastmod PATH [FLAGS|PRESET] [-G<group>=<primary group>|-G] [-q]")
    print("               [-C<cores>] [-B<blocksize>]")
    print()
    print("Arguments:")
    print("  PATH is the path to change permissions of. If a directory, "
          "permissions are recursively changed.")
    print("  FLAGS is a chmod-style permission string, eg u+rx,g=rs,o+r-w,+t")
    print("  You can specify separate flags for files and directories with:")
    print("    file-perms:folder-perms     e.g. u+xs,g+x,o-w:g+s,o-w")
    print("  PRESET can be *one* of the presets below:")
    max_width = max(len(preset_name) for preset_name in PRESETS)
    fil_perms_width = max(len("File Permissions"),
                          max(len(perms["fil"]) for perms in PRESETS.values()))
    dir_perms_width = max(len("Folder Permissions"),
                          max(len(perms["dir"]) for perms in PRESETS.values()))
    print(f"    {'Preset Flag'.ljust(max_width+2)}    "
          f"{'File Permissions'.ljust(fil_perms_width)}    "
          f"{'Folder Permissions'.ljust(dir_perms_width)}")
    print(f"    {'-'*(max_width+2)}    {'-'*fil_perms_width}    "
          f"{'-'*dir_perms_width}")
    for preset_name, perms in PRESETS.items():
        print(f"    --{preset_name.ljust(max_width)}    "
              f"{perms['fil'].ljust(fil_perms_width)}    "
              f"{perms['dir'].ljust(dir_perms_width)}")
    print(f"  By default, the preset is {DEFAULT_PRESET}.")
    print("  Specify -G<group> to set group ownership, e.g. -Gusers.")
    print(f"  Specify -G to set group ownership to the user's primary group. "
          f"(Yours is: {PRIMARY_GROUP})")
    print("     Omit -G to keep group ownership as it is.")
    print("  If you specify group ownership with -G, this will take effect "
          "*before* permissions are applied.")
    print("  Specify -q to suppress most messages.")
    print(f"  Specify -C<cores> to set the number of worker processes to use. "
          f"Else, defaults to number available minus 1. ({DEFAULT_CORES})")
    print(f"  Specify -B<blocksize> to set the number of files changed per "
          f"batch. Else, defaults to {DEFAULT_BLOCKSIZE}.")
    print()
    print("Configuration:")
    print("  You can override defaults with these environment variables:")
    print("    FASTMOD_BLOCKSIZE, FASTMOD_CORES, FASTMOD_PRESET")
    print()
    print("Examples:")
    print("  fastmod .                            to set cwd to baseline "
          "perms (user read/write, group/others read-only)")
    print("  fastmod . --readonly -G              to set to all read-only "
          "perms and set group ownership to your primary")
    print("                                       group")
    print("  fastmod . --group-allowed -Gusers    to set cwd to user/group "
          "read/write, others read-only")
    print("                                       and set group ownership to "
          "'users'")
    print("  fastmod . a+x -G                     to give everyone execute "
          "permissions and set group ownership to your")
    print("                                       primary group")


class Config:
    """Effective configuration for this run.
    
    Pre-populated with defaults where applicable.
    """

    def __init__(self):
        self.path = None
        self.perms_fil = PRESETS[DEFAULT_PRESET]["fil"]
        self.perms_dir = PRESETS[DEFAULT_PRESET]["dir"]
        self.group = None
        self.set_group = False
        self.ncpus = DEFAULT_CORES
        self.blocksize = DEFAULT_BLOCKSIZE
        self.quiet = False


def parse_args(argv):
    """Returns Config object of parsed arguments."""
    config = Config()
    config.path = argv[1]
    if not os.path.exists(config.path):
        print(f"fastmod: no such path as '{config.path}'")
        sys.exit(1)

    for arg in argv[2:]:
        if arg.startswith("-G"):
            config.set_group = True
            if arg == "-G":
                config.group = PRIMARY_GROUP
            else:
                config.group = arg[2:]
        elif arg.startswith("-C"):
            config.ncpus = int(arg[2:])
        elif arg.startswith("-B"):
            config.blocksize = int(arg[2:])
        elif arg == "-q":
            config.quiet = True
        elif arg.startswith("--"):
            preset_name = arg[2:]
            if preset_name not in PRESETS:
                print(f"fastmod: preset '{preset_name}' does not exist")
                return None
            config.perms_fil = PRESETS[preset_name]["fil"]
            config.perms_dir = PRESETS[preset_name]["dir"]
        elif ":" in arg:
            try:
                config.perms_fil, config.perms_dir = arg.split(":")
            except ValueError:
                print("fastmod: specify multiple permission flags like "
                      "'file-perms|folder-perms'")
                print("e.g. 'u+xs,g+x,o-w:g+s,o-w'")
                return None
        else:
            config.perms_fil = arg
            config.perms_dir = arg

    return config


def fastmod_folder(config):
    """Runs fastmod recursively on a folder."""
    if not config.quiet:
        print(f"Using {config.ncpus} workers, block size {config.blocksize}")
    group_or_none = config.group if config.set_group else None
    queue = mp.Queue()
    workers = [
        mp.Process(target=worker_main,
                   args=(queue, group_or_none, config.quiet, config.blocksize))
        for _ in range(config.ncpus)
    ]
    for worker in workers:
        worker.start()
    dot = "."
    total = 0
    start = time.time()
    for root, _, files in os.walk(config.path):
        queue.put_nowait((root, dot, config.perms_dir))
        total += 1
        for file in files:
            queue.put_nowait((root, file, config.perms_fil))
            total += 1

    for _ in workers:
        queue.put_nowait((None, None, None))

    for worker in workers:
        worker.join()

    duration = time.time() - start

    print(f"Set permissions on {total} files in {duration:.03f} seconds "
          f"({duration/total:.05f} s/file; {total/duration:.01f} files/s)")


def fastmod_file(config):
    """Runs fastmod on a single file."""
    os.system(f"chmod {config.perms_fil} '{config.path}'")
    if config.set_group:
        os.system(f"chgrp {config.group} '{config.path}'")
    print("Done")


def main(argv):
    """Entry point for the application."""
    if not argv[1:]:
        show_help()
        return 1

    config = parse_args(argv)
    if config is None:
        return 1

    if not config.quiet:
        print(f"Setting file permissions:      {config.perms_fil}")
        print(f"Setting directory permissions: {config.perms_dir}")
        if config.set_group:
            print(f"Setting group ownership:       {config.group}")

    if os.path.isdir(config.path):
        fastmod_folder(config)
    else:
        fastmod_file(config)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
else:
    raise ImportError("fastmod is run as a script, not included as a module.")