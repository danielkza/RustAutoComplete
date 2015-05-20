import re
import time
import sys
import os
import os.path
import platform
import subprocess
import threading
import json
from functools import partial
from subprocess import Popen, PIPE, check_output
from collections import OrderedDict

import sublime
import sublime_plugin


class Settings:
    _instance = None
    KEYS = ["racer", "cargo", "search_paths"]

    def __init__(self):
        self.package_settings = sublime.load_settings("RustAutoComplete.sublime-settings")
        for key in self.KEYS:
            self.package_settings.add_on_change(key, self.settings_changed)
        
        self.settings_changed()

    def settings_changed(self):
        self.racer_bin = self.package_settings.get("racer", "racer")
        self.cargo_bin = self.package_settings.get("cargo", "cargo")
        self.search_paths = self.package_settings.get("search_paths", [])

    def unload(self):
        for key in self.KEYS:
            self.package_settings.clear_on_change(key)

    @classmethod
    def _init(cls):
        if not cls._instance:
            cls._instance = cls()

    @classmethod
    def get(cls):
        cls._init()
        return cls._instance

    @classmethod
    def c_unload(cls):
        if cls._instance:
            cls._instance.unload()
            cls._instance = None

def plugin_loaded():
    Settings._init()


def plugin_unloaded():
    Settings.c_unload()


class Result:
    def __init__(self, parts):
        self.completion = parts[0]
        self.snippet = parts[1]
        self.row = int(parts[2])
        self.column = int(parts[3])
        self.path = parts[4]
        self.type = parts[5]
        self.context = parts[6]


class RacerThread(threading.Thread):
    def __init__(self, cmd, view, location, callback=None, timeout=5000):
        super().__init__()

        self.content = view.substr(sublime.Region(0, view.size()))
        self.file_name = view.file_name()
        self.row, self.col = view.rowcol(location)
        self.row += 1
        self.callback = callback
        self.timeout = timeout

        self.src_path = RustProjectDirWatcher.get().get_view_src_path(view)
        self.with_snippet = False
        self.results = None

        self._start_process(cmd)

    def _start_process(self, cmd):
        cmd_list = [cmd] if isinstance(cmd, str) else cmd
        if cmd_list[0] == "complete-with-snippet":
            self.with_snippet = True

        cmd_list.insert(0, Settings.get().racer_bin)
        cmd_list.extend([str(self.row), str(self.col), '/dev/stdin'])

        env = self._get_racer_environment()
        print('racer:' + ' '.join(cmd_list))
        self.process = Popen(cmd_list, stdin=PIPE, stdout=PIPE, stderr=PIPE,
                             env=env)

    def _get_racer_environment(self):
        env = os.environ.copy()
        # Keep what was already in the environment
        search_paths = filter(None, env.get('RUST_SRC_PATH', '').split(':'))
        # Append from settings
        search_paths = list(search_paths) + Settings.get().search_paths
        # Try to get the path for the current project
        if self.src_path:
            search_paths.append(self.src_path)
        # Expand tilde for home
        search_paths = map(os.path.expanduser, search_paths)
        # We need to preserve the order but remove duplicates. Abuse an
        # OrderedDict for it
        search_paths = list(OrderedDict.fromkeys(search_paths))
        print(search_paths)
        env['RUST_SRC_PATH'] = ':'.join(search_paths)

        return env

    def kill(self):
        if self.process:
            if not self.process.poll():
                self.process.kill()

            self.process = None

    def set_results(self, results):
        self.results = results
        if self.callback:
            self.callback(self.results)

        self.process = None

    def run(self):
        sublime.set_timeout_async(self.kill, self.timeout)
        (output, err) = self.process.communicate(self.content.encode('utf-8'))

        exit_code = self.process and self.process.returncode
        results = []

        if exit_code == 0:
            # Parse output
            match_string = "MATCH "

            for byte_line in output.splitlines():
                line = byte_line.decode("utf-8")
                if not line.startswith(match_string):
                    continue

                if self.with_snippet:
                    parts = line[len(match_string):].split(';', 7)
                else:
                    parts = line[len(match_string):].split(',', 6)
                    parts.insert(1, "")

                result = Result(parts)
                if result.path == self.file_name:
                    continue

                if result.path == '/dev/stdin':
                    result.path = self.file_name

                results.append(result)
        else:
            print("racer failed (code {0}): ".format(exit_code), output, err)

        self.set_results(results)

    def results(self):
        self.join()
        return self.results

class RustProjectDirWatcher(sublime_plugin.EventListener):
    _instance = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.file_names_cache = {}

        RustProjectDirWatcher._instance = self

    @classmethod
    def get(cls):
        return cls._instance

    def _find_src_path_by_file(self, file_name):
        cargo_bin = Settings.get().cargo_bin
        if not cargo_bin:
            return None

        try:
            print('cargo locate-project')
            cargo_json = check_output([cargo_bin, 'locate-project'],
                                      cwd=os.path.dirname(file_name))
            cargo_json = json.loads(cargo_json.decode('utf-8'))
            src_path = os.path.dirname(cargo_json['root']) + '/src'
            if not os.path.isdir(src_path):
                src_path = ''
        except (subprocess.CalledProcessError, KeyError):
            src_path = ''
        except OSError:
            src_path = None

        return src_path

    def get_view_src_path(self, view):
        file_name = view.file_name()
        if not file_name:
            return None

        src_path = self.file_names_cache.get(file_name, None)
        if src_path is None:
            src_path = self._find_src_path_by_file(file_name)
            self.file_names_cache[file_name] = src_path

        return src_path

    def on_load(self, view):
        self.file_names_cache.pop(view.file_name(), None)

class RustAutocomplete(sublime_plugin.EventListener):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.completions_id = None
        self.completions = None

    def get_completions_id(self):
        view = sublime.active_window().active_view()
        pos = view.sel()[0].begin()
        (line, col) = view.rowcol(pos)
        return (view.id(), line, col)

    def on_query_completions(self, view, prefix, locations):
        # Check if this is a Rust source file. This check
        # relies on the Rust syntax formatting extension
        # being installed - https://github.com/jhasse/sublime-rust
        if not view.match_selector(locations[0], "source.rust"):
            return

        current_completions_id = self.get_completions_id()
        if self.completions_id and self.completions_id == current_completions_id:
            return (list(set(self.results)),
                    sublime.INHIBIT_WORD_COMPLETIONS | sublime.INHIBIT_EXPLICIT_COMPLETIONS)

        self.completions_id = None
        self.results = None
        try:
            racer = RacerThread("complete-with-snippet", view, locations[0],
                callback=partial(self.on_racer_results, current_completions_id))
            racer.start()
        except FileNotFoundError:
            print("Unable to find racer executable (check settings)")

        return None

    def on_racer_results(self, completions_id, raw_results):
        if self.get_completions_id() != completions_id:
            return

        results = []
        lalign = 0;
        ralign = 0;
        for result in raw_results:
            result.middle = "{0} ({1})".format(result.type,
                                               os.path.basename(result.path))
            lalign = max(lalign, len(result.completion) + len(result.middle))
            ralign = max(ralign, len(result.context))

        for result in raw_results:
            context = result.context
            result_desc = "{0} {1:>{3}} : {2:{4}}".format(
                result.completion, result.middle, result.context,
                lalign - len(result.completion), ralign)
            result_desc = result_desc.rstrip(' {')
            results.append((result_desc, result.snippet))

        self.completions_id = completions_id
        self.results = results

        sublime.active_window().active_view().run_command('auto_complete',{
            'disable_auto_insert': True,
            'api_completions_only': True,
            'next_completion_if_showing': True
        })

class RustGotoDefinitionCommand(sublime_plugin.TextCommand):
    @classmethod
    def result_description(cls, result):
        return "{0} - {1}".format(r.snippet, os.path.basename(r.path))

    def on_racer_results(self, results):
        def display_result(idx):
            if idx < 0:
                return

            result = results[idx]
            encoded_path = "{0}:{1}:{2}".format(result.path, result.row, result.column)
            self.view.window().open_file(encoded_path, sublime.ENCODED_POSITION)

        if len(results) == 1:
            display_result(0)
        else:
            choices = list(map(self.result_description, results))
            print(choices)
            sublime.active_window().show_quick_panel(choices, display_result)

    def run(self, edit):
        # Get the buffer location in correct format for racer
        location = self.view.sel()[0].begin()
        racer = RacerThread("find-definition", self.view, location,
                            callback=self.on_racer_result)
        racer.start()

