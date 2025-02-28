#! /usr/bin/env python
"""
A script that provides:
1. Ability to grab binaries where possible from LLVM.
2. Ability to download binaries from MongoDB cache for clang-format.
3. Validates clang-format is the right version.
4. Has support for checking which files are to be checked.
5. Supports validating and updating a set of files to the right coding style.
"""
from __future__ import print_function, absolute_import

import Queue
import difflib
import glob as _glob
import itertools
import os
import os.path
import re
import shutil
import string
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib
from distutils import spawn
from optparse import OptionParser
from multiprocessing import cpu_count


##############################################################################
#
# Constants for clang-format
#
#

# Expected version of clang-format
CLANG_FORMAT_VERSION = "3.6.0"

# Name of clang-format as a binary
CLANG_FORMAT_PROGNAME = "clang-format"

# URL location of the "cached" copy of clang-format to download
# for users which do not have clang-format installed
CLANG_FORMAT_HTTP_LINUX_CACHE = "https://s3.amazonaws.com/boxes.10gen.com/build/clang-format-rhel55.tar.gz"

# URL on LLVM's website to download the clang tarball
CLANG_FORMAT_SOURCE_URL_BASE = string.Template("http://llvm.org/releases/$version/clang+llvm-$version-$llvm_distro.tar.xz")

# Path in the tarball to the clang-format binary
CLANG_FORMAT_SOURCE_TAR_BASE = string.Template("clang+llvm-$version-$tar_path/bin/" + CLANG_FORMAT_PROGNAME)

# Copied from python 2.7 version of subprocess.py
# Exception classes used by this module.
class CalledProcessError(Exception):
    """This exception is raised when a process run by check_call() or
    check_output() returns a non-zero exit status.
    The exit status will be stored in the returncode attribute;
    check_output() will also store the output in the output attribute.
    """
    def __init__(self, returncode, cmd, output=None):
        self.returncode = returncode
        self.cmd = cmd
        self.output = output
    def __str__(self):
        return ("Command '%s' returned non-zero exit status %d with output %s" %
            (self.cmd, self.returncode, self.output))


# Copied from python 2.7 version of subprocess.py
def check_output(*popenargs, **kwargs):
    r"""Run command with arguments and return its output as a byte string.

    If the exit code was non-zero it raises a CalledProcessError.  The
    CalledProcessError object will have the return code in the returncode
    attribute and output in the output attribute.

    The arguments are the same as for the Popen constructor.  Example:

    >>> check_output(["ls", "-l", "/dev/null"])
    'crw-rw-rw- 1 root root 1, 3 Oct 18  2007 /dev/null\n'

    The stdout argument is not allowed as it is used internally.
    To capture standard error in the result, use stderr=STDOUT.

    >>> check_output(["/bin/sh", "-c",
    ...               "ls -l non_existent_file ; exit 0"],
    ...              stderr=STDOUT)
    'ls: non_existent_file: No such file or directory\n'
    """
    if 'stdout' in kwargs:
        raise ValueError('stdout argument not allowed, it will be overridden.')
    process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
    output, unused_err = process.communicate()
    retcode = process.poll()
    if retcode:
        cmd = kwargs.get("args")
        if cmd is None:
            cmd = popenargs[0]
        raise CalledProcessError(retcode, cmd, output)
    return output

def callo(args):
    """Call a program, and capture its output
    """
    return check_output(args)

# From https://github.com/mongodb/mongo/blob/master/buildscripts/resmokelib/utils/globstar.py
_GLOBSTAR = "**"

def iglob(globbed_pathname):
    """
    Emit a list of pathnames matching the 'globbed_pathname' pattern.

    In addition to containing simple shell-style wildcards a la fnmatch,
    the pattern may also contain globstars ("**"), which is recursively
    expanded to match zero or more subdirectories.
    """

    parts = _split_path(globbed_pathname)
    parts = _canonicalize(parts)

    index = _find_globstar(parts)
    if index == -1:
        for pathname in _glob.iglob(globbed_pathname):
            # Normalize 'pathname' so exact string comparison can be used later.
            yield os.path.normpath(pathname)
        return

    # **, **/, or **/a
    if index == 0:
        expand = _expand_curdir

    # a/** or a/**/ or a/**/b
    else:
        expand = _expand

    prefix_parts = parts[:index]
    suffix_parts = parts[index + 1:]

    prefix = os.path.join(*prefix_parts) if prefix_parts else os.curdir
    suffix = os.path.join(*suffix_parts) if suffix_parts else ""

    for (kind, path) in expand(prefix):
        if not suffix_parts:
            yield path

        # Avoid following symlinks to avoid an infinite loop
        elif suffix_parts and kind == "dir" and not os.path.islink(path):
            path = os.path.join(path, suffix)
            for pathname in iglob(path):
                yield pathname


def _split_path(pathname):
    """
    Return 'pathname' as a list of path components.
    """

    parts = []

    while True:
        (dirname, basename) = os.path.split(pathname)
        parts.append(basename)
        if pathname == dirname:
            parts.append(dirname)
            break
        if not dirname:
            break
        pathname = dirname

    parts.reverse()
    return parts


def _canonicalize(parts):
    """
    Return a copy of 'parts' with consecutive "**"s coalesced.
    Raise a ValueError for unsupported uses of "**".
    """

    res = []

    prev_was_globstar = False
    for p in parts:
        if p == _GLOBSTAR:
            # Skip consecutive **'s
            if not prev_was_globstar:
                prev_was_globstar = True
                res.append(p)
        elif _GLOBSTAR in p:  # a/b**/c or a/**b/c
            raise ValueError("Can only specify glob patterns of the form a/**/b")
        else:
            prev_was_globstar = False
            res.append(p)

    return res


def _find_globstar(parts):
    """
    Return the index of the first occurrence of "**" in 'parts'.
    Return -1 if "**" is not found in the list.
    """

    for (i, p) in enumerate(parts):
        if p == _GLOBSTAR:
            return i
    return -1


def _list_dir(pathname):
    """
    Return a pair of the subdirectory names and filenames immediately
    contained within the 'pathname' directory.

    If 'pathname' does not exist, then None is returned.
    """

    try:
        (_root, dirs, files) = os.walk(pathname).next()
        return (dirs, files)
    except StopIteration:
        return None  # 'pathname' directory does not exist


def _expand(pathname):
    """
    Emit tuples of the form ("dir", dirname) and ("file", filename)
    of all directories and files contained within the 'pathname' directory.
    """

    res = _list_dir(pathname)
    if res is None:
        return

    (dirs, files) = res

    # Zero expansion
    if os.path.basename(pathname):
        yield ("dir", os.path.join(pathname, ""))

    for f in files:
        path = os.path.join(pathname, f)
        yield ("file", path)

    for d in dirs:
        path = os.path.join(pathname, d)
        for x in _expand(path):
            yield x


def _expand_curdir(pathname):
    """
    Emit tuples of the form ("dir", dirname) and ("file", filename)
    of all directories and files contained within the 'pathname' directory.

    The returned pathnames omit a "./" prefix.
    """

    res = _list_dir(pathname)
    if res is None:
        return

    (dirs, files) = res

    # Zero expansion
    yield ("dir", "")

    for f in files:
        yield ("file", f)

    for d in dirs:
        for x in _expand(d):
            yield x

def get_llvm_url(version, llvm_distro):
    """Get the url to download clang-format from llvm.org
    """
    return CLANG_FORMAT_SOURCE_URL_BASE.substitute(
        version=version,
        llvm_distro=llvm_distro)

def get_tar_path(version, tar_path):
    """ Get the path to clang-format in the llvm tarball
    """
    return CLANG_FORMAT_SOURCE_TAR_BASE.substitute(
        version=version,
        tar_path=tar_path)

def extract_clang_format(tar_path):
    # Extract just the clang-format binary
    # On OSX, we shell out to tar because tarfile doesn't support xz compression
    if sys.platform == 'darwin':
         subprocess.call(['tar', '-xzf', tar_path, '*clang-format*'])
    # Otherwise we use tarfile because some versions of tar don't support wildcards without
    # a special flag
    else:
        tarfp = tarfile.open(tar_path)
        for name in tarfp.getnames():
            if name.endswith('clang-format'):
                tarfp.extract(name)
        tarfp.close()

def get_clang_format_from_llvm(llvm_distro, tar_path, dest_file):
    """Download clang-format from llvm.org, unpack the tarball,
    and put clang-format in the specified place
    """
    # Build URL
    url = get_llvm_url(CLANG_FORMAT_VERSION, llvm_distro)

    dest_dir = tempfile.gettempdir()
    temp_tar_file = os.path.join(dest_dir, "temp.tar.xz")

    # Download from LLVM
    print("Downloading clang-format %s from %s, saving to %s" % (CLANG_FORMAT_VERSION,
            url, temp_tar_file))
    urllib.urlretrieve(url, temp_tar_file)

    extract_clang_format(temp_tar_file)

    # Destination Path
    shutil.move(get_tar_path(CLANG_FORMAT_VERSION, tar_path), dest_file)

def get_clang_format_from_linux_cache(dest_file):
    """Get clang-format from mongodb's cache
    """
    # Get URL
    url = CLANG_FORMAT_HTTP_LINUX_CACHE

    dest_dir = tempfile.gettempdir()
    temp_tar_file = os.path.join(dest_dir, "temp.tar.xz")

    # Download the file
    print("Downloading clang-format %s from %s, saving to %s" % (CLANG_FORMAT_VERSION,
            url, temp_tar_file))
    urllib.urlretrieve(url, temp_tar_file)

    extract_clang_format(temp_tar_file)

    # Destination Path
    shutil.move("llvm/Release/bin/clang-format", dest_file)


class ClangFormat(object):
    """Class encapsulates finding a suitable copy of clang-format,
    and linting/formating an individual file
    """
    def __init__(self, path, cache_dir):
        if os.path.exists('/usr/bin/clang-format-3.6'):
            clang_format_progname = 'clang-format-3.6'
        else:
            clang_format_progname = CLANG_FORMAT_PROGNAME

        # Initialize clang-format configuration information
        if sys.platform.startswith("linux"):
              #"3.6.0/clang+llvm-3.6.0-x86_64-linux-gnu-ubuntu-14.04.tar.xz
            self.platform = "linux_x64"
            self.llvm_distro = "x86_64-linux-gnu-ubuntu"
            self.tar_path = "x86_64-linux-gnu"
        elif sys.platform == "win32":
            self.platform = "windows_x64"
            self.llvm_distro = "windows_x64"
            self.tar_path = None
            clang_format_progname += ".exe"
        elif sys.platform == "darwin":
             #"3.6.0/clang+llvm-3.6.0-x86_64-apple-darwin.tar.xz
            self.platform = "darwin_x64"
            self.llvm_distro = "x86_64-apple-darwin"
            self.tar_path = "x86_64-apple-darwin"

        self.path = None

        # Find Clang-Format now
        if path is not None:
            if os.path.isfile(path):
                self.path = path
            else:
                print("WARNING: Could not find clang-format %s" % (path))

        # Check the envionrment variable
        if "MONGO_CLANG_FORMAT" in os.environ:
            self.path = os.environ["MONGO_CLANG_FORMAT"]

            if self.path and not self._validate_version(warn=True):
                self.path = None

        # Check the users' PATH environment variable now
        if self.path is None:
            self.path = spawn.find_executable(clang_format_progname)

            if self.path and not self._validate_version(warn=True):
                self.path = None

        # If Windows, try to grab it from Program Files
        if sys.platform == "win32":
            win32bin = os.path.join(os.environ["ProgramFiles(x86)"], "LLVM\\bin\\clang-format.exe")
            if os.path.exists(win32bin):
                self.path = win32bin

        # Have not found it yet, download it from the web
        if self.path is None:
            if not os.path.isdir(cache_dir):
                os.makedirs(cache_dir)

            self.path = os.path.join(cache_dir, clang_format_progname)

            if not os.path.isfile(self.path):
                if sys.platform.startswith("linux"):
                    get_clang_format_from_linux_cache(self.path)
                elif sys.platform == "darwin":
                    get_clang_format_from_llvm(self.llvm_distro, self.tar_path, self.path)
                else:
                    print("ERROR: clang-format.py does not support downloading clang-format " +
                        " on this platform, please install clang-format " + CLANG_FORMAT_VERSION)

        # Validate we have the correct version
        self._validate_version()

        self.print_lock = threading.Lock()

    def _validate_version(self, warn=False):
        """Validate clang-format is the expected version
        """
        try:
            cf_version = callo([self.path, "--version"])
        except CalledProcessError:
            cf_version = "clang-format call failed."

        if warn:
            print("WARNING: clang-format found in path, but incorrect version found at " +
                    self.path + " with version: " + cf_version)

        return False

    def _lint(self, file_name, print_diff):
        """Check the specified file has the correct format
        """
        with open(file_name, 'rb') as original_text:
            original_file = original_text.read()

        # Get formatted file as clang-format would format the file
        formatted_file = callo([self.path, "--style=file", file_name])

        if original_file != formatted_file:
            if print_diff:
                original_lines = original_file.splitlines()
                formatted_lines = formatted_file.splitlines()
                result = difflib.unified_diff(original_lines, formatted_lines)

                # Take a lock to ensure diffs do not get mixed when printed to the screen
                with self.print_lock:
                    print("ERROR: Found diff for " + file_name)
                    print("To fix formatting errors, run %s --style=file -i %s" %
                            (self.path, file_name))
                    for line in result:
                        print(line.rstrip())

            return False

        return True

    def lint(self, file_name):
        """Check the specified file has the correct format
        """
        return self._lint(file_name, print_diff=True)

    def format(self, file_name):
        """Update the format of the specified file
        """
        if self._lint(file_name, print_diff=False):
            return True

        # Update the file with clang-format
        return not subprocess.call([self.path, "--style=file", "-i", file_name])


def parallel_process(items, func):
    """Run a set of work items to completion
    """
    try:
        cpus = cpu_count()
    except NotImplementedError:
        cpus = 1

    task_queue = Queue.Queue()

    # Use a list so that worker function will capture this variable
    pp_event = threading.Event()
    pp_result = [True]
    pp_lock = threading.Lock()

    def worker():
        """Worker thread to process work items in parallel
        """
        while not pp_event.is_set():
            try:
                item = task_queue.get_nowait()
            except Queue.Empty:
                # if the queue is empty, exit the worker thread
                pp_event.set()
                return

            try:
                ret = func(item)
            finally:
                # Tell the queue we finished with the item
                task_queue.task_done()

            # Return early if we fail, and signal we are done
            if not ret:
                with pp_lock:
                    pp_result[0] = False

                pp_event.set()
                return

    # Enqueue all the work we want to process
    for item in items:
        task_queue.put(item)

    # Process all the work
    threads = []
    for cpu in range(cpus):
        thread = threading.Thread(target=worker)

        thread.daemon = True
        thread.start()
        threads.append(thread)

    # Wait for the threads to finish
    # Loop with a timeout so that we can process Ctrl-C interrupts
    # Note: On Python 2.6 wait always returns None so we check is_set also,
    #  This works because we only set the event once, and never reset it
    while not pp_event.wait(1) and not pp_event.is_set():
        time.sleep(1)

    for thread in threads:
        thread.join()

    return pp_result[0]

def get_base_dir():
    """Get the base directory for mongo repo.
        This script assumes that it is running in buildscripts/, and uses
        that to find the base directory.
    """
    try:
        return subprocess.check_output(['git', 'rev-parse', '--show-toplevel']).rstrip()
    except:
        # We are not in a valid git directory. Use the script path instead.
        return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

def get_repos():
    """Get a list of Repos to check clang-format for
    """
    base_dir = get_base_dir()

    paths = [base_dir]

    return [Repo(p) for p in paths]


class Repo(object):
    """Class encapsulates all knowledge about a git repository, and its metadata
        to run clang-format.
    """
    def __init__(self, path):
        self.path = path

       # Get candidate files
        self.candidate_files = self._get_candidate_files()

        self.root = self._get_root()

    def _callgito(self, args):
        """Call git for this repository
        """
        # These two flags are the equivalent of -C in newer versions of Git
        # but we use these to support versions back to ~1.8
        return callo(['git', '--git-dir', os.path.join(self.path, ".git"),
                        '--work-tree', self.path] + args)

    def _get_local_dir(self, path):
        """Get a directory path relative to the git root directory
        """
        if os.path.isabs(path):
            return os.path.relpath(path, self.root)
        return path

    def get_candidates(self, candidates):
        """Get the set of candidate files to check by doing an intersection
        between the input list, and the list of candidates in the repository

        Returns the full path to the file for clang-format to consume.
        """
        # NOTE: Files may have an absolute root (i.e. leading /)

        if candidates is not None and len(candidates) > 0:
            candidates = [self._get_local_dir(f) for f in candidates]
            valid_files = list(set(candidates).intersection(self.get_candidate_files()))
        else:
            valid_files = list(self.get_candidate_files())

        # Get the full file name here
        valid_files = [os.path.normpath(os.path.join(self.root, f)) for f in valid_files]
        return valid_files

    def get_root(self):
        """Get the root directory for this repository
        """
        return self.root

    def _get_root(self):
        """Gets the root directory for this repository from git
        """
        gito = self._callgito(['rev-parse', '--show-toplevel'])

        return gito.rstrip()

    def get_candidate_files(self):
        """Get a list of candidate files
        """
        return self._get_candidate_files()

    def _get_candidate_files(self):
        """Query git to get a list of all files in the repo to consider for analysis
        """
        gito = self._callgito(["ls-files"])

        # This allows us to pick all the interesting files
        # in the mongo and mongo-enterprise repos
        file_list = [line.rstrip()
                for line in gito.splitlines() if "src" in line and
                    not "examples" in line and
                    not "third_party" in line]

        files_match = re.compile('\\.(h|hpp|cpp)$')

        file_list = [a for a in file_list if files_match.search(a)]

        return file_list


def expand_file_string(glob_pattern):
    """Expand a string that represents a set of files
    """
    return [os.path.abspath(f) for f in iglob(glob_pattern)]

def get_files_to_check(files):
    """Filter the specified list of files to check down to the actual
        list of files that need to be checked."""
    candidates = []

    # Get a list of candidate_files
    candidates = [expand_file_string(f) for f in files]
    candidates = list(itertools.chain.from_iterable(candidates))

    repos = get_repos()

    valid_files = list(itertools.chain.from_iterable([r.get_candidates(candidates) for r in repos]))

    return valid_files

def get_files_to_check_from_patch(patches):
    """Take a patch file generated by git diff, and scan the patch for a list of files to check.
    """
    candidates = []

    # Get a list of candidate_files
    check = re.compile(r"^diff --git a\/([a-z\/\.\-_0-9]+) b\/[a-z\/\.\-_0-9]+")

    lines = []
    for patch in patches:
        with open(patch, "rb") as infile:
            lines += infile.readlines()

    candidates = [check.match(line).group(1) for line in lines if check.match(line)]

    repos = get_repos()

    valid_files = list(itertools.chain.from_iterable([r.get_candidates(candidates) for r in repos]))

    return valid_files

def _get_build_dir():
    """Get the location of the scons' build directory in case we need to download clang-format
    """
    return os.path.join(get_base_dir(), "build")

def _lint_files(clang_format, files):
    """Lint a list of files with clang-format
    """
    clang_format = ClangFormat(clang_format, _get_build_dir())

    lint_clean = parallel_process([os.path.abspath(f) for f in files], clang_format.lint)

    if not lint_clean:
        print("ERROR: Code Style does not match coding style")
        sys.exit(1)

def lint_patch(clang_format, infile):
    """Lint patch command entry point
    """
    files = get_files_to_check_from_patch(infile)

    # Patch may have files that we do not want to check which is fine
    if files:
        _lint_files(clang_format, files)

def lint(clang_format, glob):
    """Lint files command entry point
    """
    files = get_files_to_check(glob)

    _lint_files(clang_format, files)

    return True

def _format_files(clang_format, files):
    """Format a list of files with clang-format
    """
    clang_format = ClangFormat(clang_format, _get_build_dir())

    format_clean = parallel_process([os.path.abspath(f) for f in files], clang_format.format)

    if not format_clean:
        print("ERROR: failed to format files")
        sys.exit(1)

def format_func(clang_format, glob):
    """Format files command entry point
    """
    files = get_files_to_check(glob)

    _format_files(clang_format, files)

def usage():
    """Print usage
    """
    print("clang-format.py supports 3 commands [ lint, lint-patch, format ]. Run "
            " <command> -? for more information")

def main():
    """Main entry point
    """
    parser = OptionParser()
    parser.add_option("-c", "--clang-format", type="string", dest="clang_format")

    (options, args) = parser.parse_args(args=sys.argv)

    if len(args) > 1:
        command = args[1]

        if command == "lint":
            lint(options.clang_format, args[2:])
        elif command == "lint-patch":
            lint_patch(options.clang_format, args[2:])
        elif command == "format":
            format_func(options.clang_format, args[2:])
        else:
            usage()
    else:
        usage()

if __name__ == "__main__":
    main()
