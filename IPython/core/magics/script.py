"""Magic functions for running cells in various scripts."""

# Copyright (c) IPython Development Team.
# Distributed under the terms of the Modified BSD License.

import asyncio
import atexit
import errno
import functools
import os
import signal
import sys
import time
from contextlib import contextmanager
from subprocess import CalledProcessError

from traitlets import Dict, List, default

from IPython.core import magic_arguments
from IPython.core.magic import Magics, cell_magic, line_magic, magics_class
from IPython.lib.backgroundjobs import BackgroundJobManager
from IPython.utils.process import arg_split

#-----------------------------------------------------------------------------
# Magic implementation classes
#-----------------------------------------------------------------------------

def script_args(f):
    """single decorator for adding script args"""
    args = [
        magic_arguments.argument(
            '--out', type=str,
            help="""The variable in which to store stdout from the script.
            If the script is backgrounded, this will be the stdout *pipe*,
            instead of the stderr text itself and will not be auto closed.
            """
        ),
        magic_arguments.argument(
            '--err', type=str,
            help="""The variable in which to store stderr from the script.
            If the script is backgrounded, this will be the stderr *pipe*,
            instead of the stderr text itself and will not be autoclosed.
            """
        ),
        magic_arguments.argument(
            '--bg', action="store_true",
            help="""Whether to run the script in the background.
            If given, the only way to see the output of the command is
            with --out/err.
            """
        ),
        magic_arguments.argument(
            '--proc', type=str,
            help="""The variable in which to store Popen instance.
            This is used only when --bg option is given.
            """
        ),
        magic_arguments.argument(
            '--no-raise-error', action="store_false", dest='raise_error',
            help="""Whether you should raise an error message in addition to 
            a stream on stderr if you get a nonzero exit code.
            """
        )
    ]
    for arg in args:
        f = arg(f)
    return f


@contextmanager
def safe_watcher():
    if sys.platform == "win32":
        yield
        return

    from asyncio import SafeChildWatcher

    policy = asyncio.get_event_loop_policy()
    old_watcher = policy.get_child_watcher()
    if isinstance(old_watcher, SafeChildWatcher):
        yield
        return

    loop = asyncio.get_event_loop()
    try:
        watcher = asyncio.SafeChildWatcher()
        watcher.attach_loop(loop)
        policy.set_child_watcher(watcher)
        yield
    finally:
        watcher.close()
        policy.set_child_watcher(old_watcher)


def dec_safe_watcher(fun):
    @functools.wraps(fun)
    def _inner(*args, **kwargs):
        with safe_watcher():
            return fun(*args, **kwargs)

    return _inner


@magics_class
class ScriptMagics(Magics):
    """Magics for talking to scripts
    
    This defines a base `%%script` cell magic for running a cell
    with a program in a subprocess, and registers a few top-level
    magics that call %%script with common interpreters.
    """
    script_magics = List(
        help="""Extra script cell magics to define
        
        This generates simple wrappers of `%%script foo` as `%%foo`.
        
        If you want to add script magics that aren't on your path,
        specify them in script_paths
        """,
    ).tag(config=True)
    @default('script_magics')
    def _script_magics_default(self):
        """default to a common list of programs"""
        
        defaults = [
            'sh',
            'bash',
            'perl',
            'ruby',
            'python',
            'python2',
            'python3',
            'pypy',
        ]
        if os.name == 'nt':
            defaults.extend([
                'cmd',
            ])
        
        return defaults
    
    script_paths = Dict(
        help="""Dict mapping short 'ruby' names to full paths, such as '/opt/secret/bin/ruby'
        
        Only necessary for items in script_magics where the default path will not
        find the right interpreter.
        """
    ).tag(config=True)
    
    def __init__(self, shell=None):
        super(ScriptMagics, self).__init__(shell=shell)
        self._generate_script_magics()
        self.job_manager = BackgroundJobManager()
        self.bg_processes = []
        atexit.register(self.kill_bg_processes)

    def __del__(self):
        self.kill_bg_processes()
    
    def _generate_script_magics(self):
        cell_magics = self.magics['cell']
        for name in self.script_magics:
            cell_magics[name] = self._make_script_magic(name)
    
    def _make_script_magic(self, name):
        """make a named magic, that calls %%script with a particular program"""
        # expand to explicit path if necessary:
        script = self.script_paths.get(name, name)
        
        @magic_arguments.magic_arguments()
        @script_args
        def named_script_magic(line, cell):
            # if line, add it as cl-flags
            if line:
                line = "%s %s" % (script, line)
            else:
                line = script
            return self.shebang(line, cell)
        
        # write a basic docstring:
        named_script_magic.__doc__ = \
        """%%{name} script magic
        
        Run cells with {script} in a subprocess.
        
        This is a shortcut for `%%script {script}`
        """.format(**locals())
        
        return named_script_magic
    
    @magic_arguments.magic_arguments()
    @script_args
    @cell_magic("script")
    @dec_safe_watcher
    def shebang(self, line, cell):
        """Run a cell via a shell command
        
        The `%%script` line is like the #! line of script,
        specifying a program (bash, perl, ruby, etc.) with which to run.
        
        The rest of the cell is run by that program.
        
        Examples
        --------
        ::
        
            In [1]: %%script bash
               ...: for i in 1 2 3; do
               ...:   echo $i
               ...: done
            1
            2
            3
        """

        async def _handle_stream(stream, stream_arg, file_object):
            while True:
                line = (await stream.readline()).decode("utf8")
                if not line:
                    break
                if stream_arg:
                    self.shell.user_ns[stream_arg] = line
                else:
                    file_object.write(line)
                    file_object.flush()

        async def _stream_communicate(process, cell):
            process.stdin.write(cell)
            process.stdin.close()
            stdout_task = asyncio.create_task(
                _handle_stream(process.stdout, args.out, sys.stdout)
            )
            stderr_task = asyncio.create_task(
                _handle_stream(process.stderr, args.err, sys.stderr)
            )
            await asyncio.wait([stdout_task, stderr_task])
            await process.wait()

        if sys.platform.startswith("win"):
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        loop = asyncio.get_event_loop()
        argv = arg_split(line, posix=not sys.platform.startswith("win"))
        args, cmd = self.shebang.parser.parse_known_args(argv)
        try:
            p = loop.run_until_complete(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
            )
        except OSError as e:
            if e.errno == errno.ENOENT:
                print("Couldn't find program: %r" % cmd[0])
                return
            else:
                raise
        
        if not cell.endswith('\n'):
            cell += '\n'
        cell = cell.encode('utf8', 'replace')
        if args.bg:
            self.bg_processes.append(p)
            self._gc_bg_processes()
            to_close = []
            if args.out:
                self.shell.user_ns[args.out] = p.stdout
            else:
                to_close.append(p.stdout)
            if args.err:
                self.shell.user_ns[args.err] = p.stderr
            else:
                to_close.append(p.stderr)
            self.job_manager.new(self._run_script, p, cell, to_close, daemon=True)
            if args.proc:
                self.shell.user_ns[args.proc] = p
            return
        
        try:
            loop.run_until_complete(_stream_communicate(p, cell))
        except KeyboardInterrupt:
            try:
                p.send_signal(signal.SIGINT)
                time.sleep(0.1)
                if p.returncode is not None:
                    print("Process is interrupted.")
                    return
                p.terminate()
                time.sleep(0.1)
                if p.returncode is not None:
                    print("Process is terminated.")
                    return
                p.kill()
                print("Process is killed.")
            except OSError:
                pass
            except Exception as e:
                print("Error while terminating subprocess (pid=%i): %s" % (p.pid, e))
            return
        if args.raise_error and p.returncode!=0:
            # If we get here and p.returncode is still None, we must have
            # killed it but not yet seen its return code. We don't wait for it,
            # in case it's stuck in uninterruptible sleep. -9 = SIGKILL
            rc = p.returncode or -9
            raise CalledProcessError(rc, cell)
    
    def _run_script(self, p, cell, to_close):
        """callback for running the script in the background"""
        p.stdin.write(cell)
        p.stdin.close()
        for s in to_close:
            s.close()
        p.wait()

    @line_magic("killbgscripts")
    def killbgscripts(self, _nouse_=''):
        """Kill all BG processes started by %%script and its family."""
        self.kill_bg_processes()
        print("All background processes were killed.")

    def kill_bg_processes(self):
        """Kill all BG processes which are still running."""
        if not self.bg_processes:
            return
        for p in self.bg_processes:
            if p.returncode is None:
                try:
                    p.send_signal(signal.SIGINT)
                except:
                    pass
        time.sleep(0.1)
        self._gc_bg_processes()
        if not self.bg_processes:
            return
        for p in self.bg_processes:
            if p.returncode is None:
                try:
                    p.terminate()
                except:
                    pass
        time.sleep(0.1)
        self._gc_bg_processes()
        if not self.bg_processes:
            return
        for p in self.bg_processes:
            if p.returncode is None:
                try:
                    p.kill()
                except:
                    pass
        self._gc_bg_processes()

    def _gc_bg_processes(self):
        self.bg_processes = [p for p in self.bg_processes if p.returncode is None]
