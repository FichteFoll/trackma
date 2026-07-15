# This file is part of Trackma.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import argparse
import os
import re
import shlex
import subprocess
import sys
import textwrap
from operator import itemgetter  # Used for sorting list

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion, FuzzyCompleter, WordCompleter
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.patch_stdout import patch_stdout

from trackma import messenger
from trackma import utils
from trackma.accounts import AccountManager
from trackma.engine import Engine

_COLOR_RESET = '\033[0m'
_COLOR_ENGINE = '\033[0;32m'
_COLOR_DATA = '\033[0;33m'
_COLOR_API = '\033[0;34m'
_COLOR_TRACKER = '\033[0;35m'
_COLOR_ERROR = '\033[0;31m'
_COLOR_FATAL = '\033[1;31m'

_COLOR_AIRING = '\033[0;34m'
_COLOR_BEHIND = '\033[0;31m'


def _display_width(text):
    return get_cwidth(text)


def _truncate_display(text, max_width):
    if _display_width(text) <= max_width:
        return text

    result = ''
    width = 0
    for char in text:
        char_width = get_cwidth(char)
        if width + char_width > max_width:
            break
        result += char
        width += char_width
    return result


def _prompt_input(message, *, password=False, default=''):
    try:
        return PromptSession().prompt(f'{message} ', default=default, is_password=password)
    except EOFError:
        return None


def _prompt_yes_no(message, *, title='Trackma'):
    try:
        answer = PromptSession().prompt(HTML(f'<b>{title}</b> {message} [y/N] '))
    except EOFError:
        return False

    return answer.strip().lower() in {'y', 'yes'}


def _prompt_choice(title, text, values):
    if not values:
        return None

    choices = []
    lookup = {}
    for value, label in values:
        choice = f'{value}: {label}'
        choices.append(choice)
        lookup[choice] = value

    session = PromptSession()
    try:
        selected = session.prompt(
            HTML(f'<b>{title}</b> {text} '),
            completer=FuzzyCompleter(WordCompleter(choices, ignore_case=True, sentence=True)),
            complete_style=CompleteStyle.COLUMN,
            complete_while_typing=True,
        )
    except EOFError:
        return None

    selected = selected.strip()
    if selected in lookup:
        return lookup[selected]

    for value, _label in values:
        if selected == str(value) or selected.startswith(f'{value}:'):
            return value

    return None


class TrackmaCompleter(Completer):
    def __init__(self, cli):
        self.cli = cli

    def _command_names(self):
        names = []
        for name in dir(self.cli):
            if name.startswith('do_') and not name.startswith('do__'):
                names.append(name[3:])
        names.extend(['help'])
        return sorted(set(names))

    def _show_titles(self):
        if not self.cli.engine:
            return []
        try:
            showlist = self.cli.engine.filter_list(self.cli.filter_num)
            return [show['title'] for show in showlist]
        except utils.TrackmaError:
            return []

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()

        if text.endswith(' '):
            parts.append('')

        current = document.get_word_before_cursor()
        command_names = self._command_names()

        if not parts:
            for name in command_names:
                yield Completion(name, start_position=-len(current))
            return

        cmd = parts[0]
        if len(parts) == 1 and not text.endswith(' '):
            for name in command_names:
                if name.startswith(current):
                    yield Completion(name, start_position=-len(current))
            return

        if cmd == 'help':
            for name in command_names:
                if name.startswith(current):
                    yield Completion(name, start_position=-len(current))
            return

        if cmd in {'filter'}:
            if not self.cli.engine:
                return
            for value in self.cli.engine.mediainfo['statuses_dict'].values():
                candidate = value.lower().replace(' ', '')
                if candidate.startswith(current.lower()):
                    yield Completion(candidate, start_position=-len(current))
            return

        if cmd in self.cli.show_title_commands and len(parts) <= 2:
            prefix = current
            title_doc = Document(text=prefix, cursor_position=len(prefix))
            title_completer = FuzzyCompleter(WordCompleter(self._show_titles(), ignore_case=True))
            for completion in title_completer.get_completions(title_doc, complete_event):
                yield Completion(
                    shlex.quote(completion.text),
                    start_position=completion.start_position,
                    display=completion.display_text,
                )


class Trackma_cmd:
    """
    Interactive CLI command runner.

    This class owns the prompt, account selection, engine lifecycle,
    command parsing, argument validation, and tab completion.

    Commands are dispatched by name from `execute_line()`
    and parsed via shlex, i.e. they use shell syntax.
    The first token is treated as the command name,
    and the dispatcher looks for a matching `do_<name>()` method.
    Aliases are implemented by adding a `do_<alias>()` method that
    delegates to the real command handler.

    `help` is generated from command docstrings.
    A command only appears in help if its `do_*` method has a docstring.
    The parser understands these metadata tags:
    `:name` for a display name or aliases,
    `:param` for required arguments,
    `:optparam` for optional arguments,
    `:usage` for a short usage string,
    and `:example` for example invocations.

    To add a new command,
    implement `do_<name>(self, args)`,
    add an entry to `needed_args` when the command should enforce
    a specific argument count,
    and document it with the tags above so it shows up in `help`.
    """
    engine = None
    filter_num = 1
    sort = 'title'
    sortedlist = []
    needed_args = {
        'altname':      (1, 2),
        'filter':       (0, 1),
        'sort':         1,
        'mediatype':    (0, 1),
        'info':         1,
        'search':       1,
        'add':          1,
        'del':          1,
        'delete':       1,
        'play':         (1, 2),
        'openfolder':   1,
        'rescan':       (0, 1),
        'update':       (1, 2),
        'score':        2,
        'status':       2,
    }

    def __init__(self, account_num=None, debug=False, interactive=True):
        if interactive:
            print('Trackma v'+utils.VERSION+'  Copyright (C) 2012-2026  z411')
            print(
                'This program comes with ABSOLUTELY NO WARRANTY; for details type `about\'')
            print('This is free software, and you are welcome to redistribute it')
            print('under certain conditions; see the COPYING file for details.')
            print()

        self.interactive = interactive
        self.debug = debug
        self.prompt = ''
        self.history_file = utils.to_cache_path('cli_history.txt')
        utils.make_dir(utils.to_cache_path())
        self.completer = FuzzyCompleter(TrackmaCompleter(self))
        self.session = PromptSession(
            history=FileHistory(self.history_file),
            completer=self.completer,
            complete_style=CompleteStyle.COLUMN,
        )
        self.show_title_commands = {
            'info', 'altname', 'update', 'play', 'score', 'status', 'delete', 'del', 'openfolder'
        }

        self.accountman = Trackma_accounts()
        self.account = None
        if account_num:
            try:
                self.account = self.accountman.get_account(account_num)
            except KeyError:
                print(f"Account {account_num} doesn't exist.")
            except ValueError:
                print(f"Account {account_num} must be numeric.")

        while self.account is None:
            self.account = self.accountman.select_account(False)

    def forget_account(self):
        self.accountman.set_default(None)

    def _update_prompt(self):
        self.prompt = HTML(
            '<ansigreen>{u}</ansigreen> '
            '[<ansiblue>{a}</ansiblue>] '
            '(<ansiyellow>{mt}</ansiyellow>) '
            '<ansimagenta>{s}</ansimagenta> >> '
        ).format(
            u=self.engine.get_userconfig('username'),
            a=self.engine.api_info['shortname'],
            mt=self.engine.api_info['mediatype'],
            s=self.engine.mediainfo['statuses_dict'][self.filter_num].lower().replace(' ', ''),
        )

    def _command_names(self):
        names = []
        for name in dir(self):
            if name.startswith('do_'):
                names.append(name[3:])
        names.append('help')
        return sorted(set(names))

    def _command_prompt(self):
        return self.prompt if self.prompt else HTML('<ansigreen>trackma</ansigreen> >> ')

    def _load_list(self, *args):
        showlist = self.engine.filter_list(self.filter_num)
        sortedlist = sorted(showlist, key=itemgetter(self.sort))
        self.sortedlist = list(enumerate(sortedlist, 1))

    def _get_show(self, title):
        # Attempt parsing list index
        # otherwise use title
        try:
            index = int(title)-1
            return self.sortedlist[index][1]
        except (ValueError, AttributeError, IndexError):
            return self.engine.get_show_info(title=title)

    def _ask_update(self, show, episode):
        if _prompt_yes_no(f"Should I update {show['title']} to episode {episode}?", title='Update show'):
            self.engine.set_episode(show['id'], episode)

    def _ask_add(self, show, episode):
        if _prompt_yes_no(f"Should I search for the show {show['title']}?", title='Add show'):
            self.do_add([show['title']])

    def start(self):
        """
        Initializes the engine

        Creates an Engine object and starts it.
        """

        if self.interactive:
            print('Initializing engine...')
        self.engine = Engine(self.account, self.messagehandler if self.interactive else None)
        if not self.interactive:
            self.engine.set_config("tracker_enabled", False)
            self.engine.set_config("library_autoscan", False)
            self.engine.set_config("use_hooks", False)

        self.engine.connect_signal('show_added', self._load_list)
        self.engine.connect_signal('show_deleted', self._load_list)
        self.engine.connect_signal('status_changed', self._load_list)
        self.engine.connect_signal('episode_changed', self._load_list)
        self.engine.connect_signal('prompt_for_update', self._ask_update)
        self.engine.connect_signal('prompt_for_add', self._ask_add)
        self.engine.start()

        # Start with default filter selected
        self.filter_num = self.engine.mediainfo['statuses'][0]
        self._load_list()

        if self.interactive:
            self._update_prompt()

            print()
            print("Ready. Type 'help' for a list of commands.")
            print("Press tab for autocompletion and up/down for command history.")
            self.do_filter(None)  # Show available filters
            print()
        else:
            # We set the message handler only after initializing
            # so we still receive the important messages but avoid
            # the initial spam.
            self.engine.set_message_handler(self.messagehandler)

    def run(self):
        if not self.interactive:
            return

        with patch_stdout():
            while True:
                try:
                    line = self.session.prompt(self._command_prompt())
                except EOFError:
                    print()
                    self.do_quit([])
                except KeyboardInterrupt:
                    if self.session.default_buffer.text:
                        continue
                    raise

                self.onecmd(line)

    def onecmd(self, line):
        return self.execute_line(line)

    def execute_line(self, line):
        parts = self._parse_command_line(line)
        if not parts:
            return None

        cmd, args = parts[0], parts[1:]
        if cmd == 'help':
            return self.do_help(args[0] if args else '')

        needed = self.needed_args.get(cmd, 0)
        if isinstance(needed, int):
            needed = (needed, needed)
        if not (needed[0] <= len(args) <= needed[1]):
            print("Incorrect number of arguments. See `help %s`" % cmd)
            return None

        func = getattr(self, 'do_' + cmd, None)
        if func is None:
            print(f'Unknown command: {cmd}')
            return None
        return func(args)

    def _parse_command_line(self, line):
        try:
            return shlex.split(line)
        except ValueError:
            return []

    def do_about(self, args):
        print("Trackma {}  by z411 (z411@omaera.org)".format(utils.VERSION))
        print("Trackma is an open source client for media tracking websites.")
        print("https://github.com/z411/trackma")
        print()
        print("This program is licensed under the GPLv3 and it comes with ABSOLUTELY NO WARRANTY.")
        print("Many contributors have helped to run this project; for more information see the AUTHORS file.")
        print("For more information about the license, see the COPYING file.")
        print()
        print("If you encounter any problems please report them in https://github.com/z411/trackma/issues")
        print()
        print("This is the CLI version of Trackma. To see available commands type `help'.")
        print("For other available interfaces please see the README file.")
        print()

    def do_help(self, arg):
        if arg:
            try:
                doc = getattr(self, 'do_' + arg).__doc__
                if doc:
                    (name, args, expl, usage, examples) = self._parse_doc(arg, doc)

                    print()
                    print(name)
                    for line in expl:
                        print("  {}".format(line))
                    if args:
                        print("\n  Arguments:")
                        for arg in args:
                            if arg[2]:
                                print("    {}: {}".format(arg[0], arg[1]))
                            else:
                                print("    {} (optional): {}".format(
                                    arg[0], arg[1]))
                    if usage:
                        print("\n  Usage: " + usage)
                    for example in examples:
                        print("  Example: " + example)
                    print()
                    return
            except AttributeError:
                pass

            print("No help available.")
            return
        else:
            CMD_LENGTH = 11
            ARG_LENGTH = 13

            (height, width) = utils.get_terminal_size()
            prev_width = CMD_LENGTH + ARG_LENGTH + 3

            tw = textwrap.TextWrapper()
            tw.width = width - 2
            tw.subsequent_indent = ' ' * prev_width

            print()
            print(" {0:>{1}} {2:{3}} {4}".format(
                'command', CMD_LENGTH,
                'args', ARG_LENGTH,
                'description'))
            print(" " + "-"*(min(prev_width+81, width-3)))

            names = self._command_names()
            for name in names:
                doc = getattr(self, 'do_' + name, None)
                if not doc or not doc.__doc__:
                    continue

                (cmd_name, args, expl, usage, examples) = self._parse_doc(name, doc.__doc__)

                line = " {0:>{1}} {2:{3}} {4}".format(
                    cmd_name, CMD_LENGTH,
                    '<' + ','.join(a[0] for a in args) + '>', ARG_LENGTH,
                    expl[0])
                print(tw.fill(line))

            print()
            print("Use `help <command>` for detailed information.")
            print()

    def do_account(self, args):
        """
        Switch to a different account.
        """

        account = self.accountman.select_account(True)
        if account is None:
            return

        self.account = account
        self.engine.reload(account=self.account)

        # Start with default filter selected
        self.filter_num = self.engine.mediainfo['statuses'][0]
        self._load_list()
        self._update_prompt()

    def do_filter(self, args):
        """
        Changes the filtering of list by status (shows current if empty).

        :optparam status Name of status to filter
        :usage filter [filter type]
        """
        # Query the engine for the available statuses
        # that the user can choose
        if args:
            try:
                self.filter_num = self._guess_status(args[0].lower())
                self._load_list()
                self._update_prompt()
            except KeyError:
                print("Invalid filter.")
        else:
            print("Available statuses: %s" % ', '.join(v.lower().replace(' ', '')
                                                       for v in self.engine.mediainfo['statuses_dict'].values()))

    def do_sort(self, args):
        """
        Change of the lists

        :param type Sort type; available types: id, title, my_progress, total, my_score
        :usage sort <sort type>
        """
        sorts = ('id', 'title', 'my_progress', 'total', 'my_score')
        if args[0] in sorts:
            self.sort = args[0]
            self._load_list()
        else:
            print("Invalid sort.")

    def do_mediatype(self, args):
        """
        Reloads engine with different mediatype (shows current if empty).
        Call with no arguments to see supported mediatypes.

        :optparam mediatype Mediatype name
        :usage mediatype [mediatype]
        """
        if args:
            if args[0] in self.engine.api_info['supported_mediatypes']:
                self.engine.reload(mediatype=args[0])

                # Start with default filter selected
                self.filter_num = self.engine.mediainfo['statuses'][0]
                self._load_list()
                self._update_prompt()
            else:
                print("Invalid mediatype.")
        else:
            print("Supported mediatypes: %s" % ', '.join(
                self.engine.api_info['supported_mediatypes']))

    def do_ls(self, args):
        self.do_list(args)

    def do_list(self, args):
        """
        Lists all shows available in the local list.

        :name list|ls
        """
        # Show the list in memory
        self._make_list(self.sortedlist)

    def do_info(self, args):
        """
        Gets detailed information about a local show.

        :param show Show index or title.
        :usage info <show index or title>
        """
        try:
            show = self._get_show(args[0])
            details = self.engine.get_show_details(show)
        except utils.TrackmaError as e:
            self.display_error(e)
            return

        print(show['title'])
        print("-" * len(show['title']))
        print(show['url'])
        print()
        altnames = self.engine.altnames()
        if altname := altnames.get(show['id']):
            print(f"Altname: {altname}")

        for line in details['extra']:
            print("%s: %s" % line)

    def do_search(self, args):
        """
        Does a regex search on shows in the local lists, including synonyms and altname.

        :param pattern Regex pattern to search for.
        :usage search <pattern>
        """
        compiled_pattern = re.compile(args[0], re.I)
        altnames = self.engine.altnames()

        def matches_show(show):
            titles = [show['title'], *show['aliases']]
            if altname := altnames.get(show['id']):
                titles.append(altname)
            return any(map(compiled_pattern.search, titles))

        sublist = [v for v in self.sortedlist if matches_show(v[1])]
        self._make_list(sublist)

    def do_add(self, args):
        """
        Search for a show in the remote service and add it.

        :param pattern Show criteria to search.
        :usage add <pattern>
        """
        try:
            entries = self.engine.search(args[0])
        except utils.TrackmaError as e:
            self.display_error(e)
            return

        for i, entry in enumerate(entries, start=1):
            print("%d: (%s) %s" % (i, entry['type'], entry['title']))
        choice = _prompt_choice(
            'Add show',
            'Choose show to add:',
            [(entry['id'], f"({entry['type']}) {entry['title']}") for entry in entries],
        )
        if choice is not None:
            show = next((entry for entry in entries if entry['id'] == choice), None)
            if show is None:
                print("Invalid show.")
                return

            # Tell the engine to add the show
            try:
                self.engine.add_show(show, self.filter_num)
            except utils.TrackmaError as e:
                self.display_error(e)

    def do_del(self, args):
        self.do_delete(args)

    def do_delete(self, args):
        """
        Deletes a show from the local list.

        :name delete|del
        :param show Show index or title.
        :usage delete <show index or title>
        """
        try:
            show = self._get_show(args[0])

            if _prompt_yes_no(f"Delete {show['title']}?", title='Delete show'):
                self.engine.delete_show(show)
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_rescan(self, args):
        """
        Re-scans the local library.

        :optparam path Base path for the scan. Defaults to all library folders if omitted.
        :usage rescan [path]
        """
        path = args[0] if args else None
        self.engine.scan_library(rescan=True, path=path)

    def do_random(self, args):
        """
        Starts the media player with a random new episode.
        """
        try:
            args = self.engine.play_random()
            utils.spawn_process(args)
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_tracker(self, args):
        """
        Shows information about the tracker, if it's running.

        :usage tracker
        """
        try:
            info = self.engine.tracker_status()
            print("- Tracker status -")

            if info:
                if info['state'] == utils.Tracker.NOVIDEO:
                    state = 'No video'
                elif info['state'] == utils.Tracker.PLAYING:
                    state = 'Playing'
                elif info['state'] == utils.Tracker.UNRECOGNIZED:
                    state = 'Unrecognized'
                elif info['state'] == utils.Tracker.NOT_FOUND:
                    state = 'Not found'
                elif info['state'] == utils.Tracker.IGNORED:
                    state = 'Ignored'
                else:
                    state = 'N/A'

                print("State: {}".format(state))
                print("Filename: {}".format(info['filename'] or 'N/A'))
                print("Timer: {}{}".format(
                    info['timer'] or 'N/A', ' [P]' if info['paused'] else ''))
                if info['show']:
                    (show, ep) = info['show']
                    print("Show: {}\nEpisode: {}".format(show['title'], ep))
                else:
                    print("Show: N/A")
            else:
                print("Not started")
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_play(self, args):
        """
        Starts the media player with the specified episode number (next if unspecified).

        :param show Episode index or title.
        :optparam ep Episode number. Assume next if not specified.
        :usage play <show index or title> [episode number]
        """
        try:
            episode = 0
            show = self._get_show(args[0])

            # If the user specified an episode, play it
            # otherwise play the next episode not watched yet
            if len(args) > 1:
                episode = args[1]

            args = self.engine.play_episode(show, episode)
            utils.spawn_process(args)
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_openfolder(self, args):
        """
        Opens the folder containing the show

        :param show Show index or name.
        :usage openfolder <show index or name>
        """

        try:
            show = self._get_show(args[0])
            filename = self.engine.get_episode_path(show)
            with open(os.devnull, 'wb') as DEVNULL:
                if sys.platform == 'darwin':
                    subprocess.Popen(["open",
                                      os.path.dirname(filename)], stdout=DEVNULL, stderr=DEVNULL)
                elif sys.platform == 'win32':
                    subprocess.Popen(["explorer",
                                      os.path.dirname(filename)], stdout=DEVNULL, stderr=DEVNULL)
                else:
                    subprocess.Popen(["xdg-open",
                                      os.path.dirname(filename)], stdout=DEVNULL, stderr=DEVNULL)
        except OSError:
            # xdg-open failed.
            self.display_error("Could not open folder.")
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_update(self, args):
        """
        Updates the progress of a show to the specified episode (next if unspecified).

        :param show Show index, title or filename (prepend with file:).
        :optparam ep Episode number (numeric).
        :usage update <show index or name> [episode number]
        :example update Toradora! 5
        :example update 6
        :example update file:filename.mkv
        """
        try:
            if args[0][:5] == "file:":
                (show, ep) = self.engine.get_show_info(filename=args[0][5:])
            else:
                (show, ep) = (self._get_show(args[0]), None)

            if len(args) > 1:
                self.engine.set_episode(show['id'], args[1])
            else:
                self.engine.set_episode(
                    show['id'], ep or show['my_progress']+1)
        except IndexError:
            print("Missing arguments.")
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_score(self, args):
        """
        Changes the score of a show.

        :param show Show index or name.
        :param score Score to set (numeric/decimal).
        :usage score <show index or name> <score>
        """
        try:
            show = self._get_show(args[0])
            self.engine.set_score(show['id'], args[1])
        except IndexError:
            print("Missing arguments.")
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_status(self, args):
        """
        Changes the status of a show.
        Use the command `filter` without arguments to see the available statuses.

        :param show Show index or name.
        :param status Status name. Use `filter` without args to list them.
        :usage status <show index or name> <status name>
        """
        try:
            _showtitle = args[0]
            _filter = args[1]
        except IndexError:
            print("Missing arguments.")
            return

        try:
            _filter_num = self._guess_status(_filter)
        except KeyError:
            print("Invalid filter.")
            return

        try:
            show = self._get_show(_showtitle)
            self.engine.set_status(show['id'], _filter_num)
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_altname(self, args):
        """
        Changes the alternative name of a show (removes if unspecified).
        Use the command 'altname' without arguments to clear the alternative
        name.

        :param show Show index or name
        :param alt  The alternative name. Use `altname` without alt to clear it
        :usage altname <show index or name> <alternative name>
        """
        try:
            show = self._get_show(args[0])
            altname = args[1] if len(args) > 1 else ''
            self.engine.altname(show['id'], altname)
        except IndexError:
            print("Missing arguments")
            return
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_send(self, args):
        """
        Sends queued changes to the remote service.
        """
        try:
            self.engine.list_upload()
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_retrieve(self, args):
        """
        Retrieves the remote list overwrites the local one.
        """
        try:
            if self.engine.get_queue():
                if _prompt_yes_no('There are unqueued changes. Overwrite local list?', title='Retrieve list'):
                    self.engine.list_download()
            else:
                self.engine.list_download()
            self._load_list()
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_clearqueue(self, args):
        """
        Undo all changes in queue.
        """
        try:
            self.engine.queue_clear()
        except utils.TrackmaError as e:
            self.display_error(e)

    def do_viewqueue(self, args):
        """
        List the queued changes.
        """
        queue = self.engine.get_queue()
        if queue:
            print("Queue:")
            for show in queue:
                print("- %s" % show['title'])
        else:
            print("Queue is empty.")

    def do_exit(self, args):
        self.do_quit(args)

    def do_quit(self, args):
        """
        Quits the program.

        :name quit|exit
        """
        try:
            self.engine.unload()
        except utils.TrackmaError as e:
            self.display_error(e)

        print('Bye!')
        sys.exit(0)

    def do_EOF(self, args):
        print()
        self.do_quit(args)

    def parse_args(self, arg):
        if arg:
            return shlex.split(arg)

        return []

    def execute(self, cmd, args, line):
        try:
            func = getattr(self, 'do_' + cmd)
        except AttributeError:
            print(f'Unknown command: {cmd}')
            return None

        try:
            needed = self.needed_args[cmd]
        except KeyError:
            needed = 0

        if isinstance(needed, int):
            needed = (needed, needed)

        if needed[0] <= len(args) <= needed[1]:
            return func(args)
        else:
            print("Incorrect number of arguments. See `help %s`" % cmd)

    def display_error(self, e):
        print("%s%s: %s%s" % (_COLOR_ERROR, type(e).__name__, e, _COLOR_RESET))

    def messagehandler(self, classname, msgtype, msg):
        """
        Handles and shows messages coming from
        the engine messenger to provide feedback.
        """
        color_escape = ''
        match classname:
            case 'Engine':
                color_escape = _COLOR_ENGINE
            case 'Data':
                color_escape = _COLOR_DATA
            case x if x.startswith('lib'):
                color_escape = _COLOR_API
            case x if x.startswith('Tracker'):
                color_escape = _COLOR_TRACKER

        color_reset = _COLOR_RESET if color_escape else ''

        if msgtype == messenger.TYPE_INFO:
            out = f"{color_escape}{classname}: {msg}{color_reset}"
        elif msgtype == messenger.TYPE_WARN:
            out = f"{color_escape}{classname} warning: {msg}{color_reset}"
        elif self.debug and msgtype == messenger.TYPE_DEBUG:
            out = f"[D] {color_escape}{classname}: {msg}{color_reset}"
        else:
            return  # Unrecognized message, don't show anything

        print_formatted_text(ANSI(out))

    def _guess_status(self, string):
        for k, v in self.engine.mediainfo['statuses_dict'].items():
            if string.lower() == v.lower().replace(' ', ''):
                return k
        raise KeyError

    def _parse_doc(self, cmd, doc):
        lines = doc.split('\n')
        name = cmd
        args = []
        expl = []
        usage = None
        examples = []

        for line in lines:
            line = line.strip()
            if line[:6] == ":param":
                args.append(line[7:].split(' ', 1) + [True])
            elif line[:9] == ":optparam":
                args.append(line[10:].split(' ', 1) + [False])
            elif line[:6] == ':usage':
                usage = line[7:]
            elif line[:5] == ':name':
                name = line[6:]
            elif line[:8] == ':example':
                examples.append(line[9:])
            elif line:
                expl.append(line)

        return (name, args, expl, usage, examples)

    def _make_list(self, showlist):
        """
        Helper function for printing a formatted show list
        """
        # Fixed column widths
        col_id_length = 7
        col_index_length = 6
        col_title_length = 5
        col_episodes_length = 9
        col_score_length = 6
        altnames = self.engine.altnames()

        # Calculate maximum width for the title column
        # based on the width of the terminal
        (height, width) = utils.get_terminal_size()
        max_title_length = width - col_id_length - \
            col_episodes_length - col_score_length - col_index_length - 5

        # Find the widest title so we can adjust the title column
        for index, show in showlist:
            title_length = _display_width(show['title'])
            if title_length > col_title_length:
                if title_length > max_title_length:
                    # Stop if we exceeded the maximum column width
                    col_title_length = max_title_length
                    break
                else:
                    col_title_length = title_length

        title_column_length = max_title_length

        # Print header
        print_formatted_text(ANSI("| {0:{1}} {2:{3}} {4:{5}} {6:{7}} |".format(
            'Index',    col_index_length,
            'Title',    title_column_length,
            'Progress', col_episodes_length,
            'Score',    col_score_length)))

        # List shows
        for index, show in showlist:
            if self.engine.mediainfo['has_progress']:
                episodes_str = "{0:3} / {1}".format(
                    show['my_progress'], show['total'] or '?')
            else:
                episodes_str = "-"

            # Get title (and alt. title) and if need be, truncate it
            title_str = show['title']
            if altnames.get(show['id']):
                title_str += " [{}]".format(altnames.get(show['id']))
            title_str = _truncate_display(title_str, max_title_length)
            title_display_width = _display_width(title_str)
            title_padding = '.' * (max_title_length - title_display_width)

            # Color title according to status
            if show['status'] == utils.Status.AIRING:
                estimate = utils.estimate_aired_episodes(show)
                if estimate and show['my_progress'] < estimate:
                    # User is behind the (estimated) aired episode
                    colored_title = _COLOR_BEHIND + title_str + _COLOR_RESET
                else:
                    colored_title = _COLOR_AIRING + title_str + _COLOR_RESET
            else:
                colored_title = title_str

            print_formatted_text(ANSI("| {0:^{1}} {2}{3} {4:{5}} {6:^{7}} |".format(
                index, col_index_length,
                colored_title,
                title_padding,
                episodes_str, col_episodes_length,
                show['my_score'], col_score_length)))

        # Print result count
        print('%d results' % len(showlist))
        print()


class Trackma_accounts(AccountManager):
    def _get_id(self, index):
        if index < 1:
            raise IndexError

        return index

    def _request_oauth_code(self, selected_api, extra=None):
        if extra is None:
            extra = {}

        print('OAuth Authentication')
        print('--------------------')
        print('This website requires OAuth authentication.')
        print('Please go to the following URL with your browser,')
        print('follow the steps and paste the given PIN code here.')
        print()

        auth_url = selected_api[3]
        if selected_api[2] == utils.Login.OAUTH_PKCE:
            extra = dict(extra)
            extra['code_verifier'] = utils.oauth_generate_pkce()
            auth_url = auth_url % extra['code_verifier']

        print(auth_url)
        print()

        return _prompt_input('PIN:'), extra

    def select_account(self, bypass):
        if not bypass and self.get_default():
            return self.get_default()
        if self.get_default():
            self.set_default(None)

        while True:
            print('--- Accounts ---')
            self.list_accounts()
            key = _prompt_input(
                "Input account number ([r#]emember, [a]dd, [e]dit, [c]ancel, [d]elete, [q]uit):")
            if key is None:
                continue

            if key.lower() in {'c', 'cancel'}:
                return None

            if key.lower() == 'a':
                available_libs = ', '.join(sorted(utils.available_libs.keys()))

                print("--- Add account ---")
                api = _prompt_input('Enter API (%s):' % available_libs)
                extra = {}
                try:
                    selected_api = utils.available_libs[api]
                except KeyError:
                    print("Invalid API.")
                    continue

                if selected_api[2] == utils.Login.PASSWD:
                    username = _prompt_input('Enter username:')
                    password = _prompt_input('Enter password (no echo):', password=True)
                elif selected_api[2] in [utils.Login.OAUTH, utils.Login.OAUTH_PKCE]:
                    username = _prompt_input('Enter account name:')
                    password, extra = self._request_oauth_code(selected_api, extra)

                try:
                    self.add_account(username, password, api, extra)
                    print('Done.')
                except utils.AccountError as e:
                    print('Error: %s' % e)
            elif key.lower() == 'e':
                print("--- Edit account ---")
                account_id = self._select_account_id('Select account to edit:')
                if account_id is None:
                    continue
                account = self.get_account(account_id)

                selected_api = utils.available_libs[account['api']]
                username = account['username']
                api = account['api']
                extra = account.get('extra', {})

                if selected_api[2] == utils.Login.PASSWD:
                    password = _prompt_input(
                        'Enter new password (leave blank to keep current):',
                        password=True,
                    )
                    if not password:
                        password = account['password']
                else:
                    password, extra = self._request_oauth_code(selected_api, extra)

                try:
                    self.edit_account(account_id, username, password, api, extra)
                    print('Done.')
                except utils.AccountError as e:
                    print('Error: %s' % e)
            elif key.lower() == 'd':
                print("--- Delete account ---")
                account_id = self._select_account_id('Select account to delete:')
                if account_id is None:
                    continue
                account = self.get_account(account_id)
                try:
                    confirm = PromptSession().prompt(
                        HTML(
                            '<ansired>Delete account '
                            f"{account_id} ({account['username']})? [y/N]</ansired> "
                        )
                    )
                except EOFError:
                    confirm = ''

                if confirm.strip().lower() in {'y', 'yes'}:
                    self.delete_account(account_id)
                    print('Account %d deleted.' % account_id)
            elif key.lower() == 'q':
                sys.exit(0)
            else:
                try:
                    if key[0] == 'r':
                        key = key[1:]
                        remember = True
                    else:
                        remember = False

                    num = int(key)
                    account_id = self._get_id(num)
                    if remember:
                        self.set_default(account_id)

                    return self.get_account(account_id)
                except ValueError:
                    print("Invalid value.")
                except (IndexError, KeyError):
                    print("Account doesn't exist.")

    def _select_account_id(self, text):
        accounts = [
            (num, f"{num}: {account['username']} ({account['api']})")
            for num, account in self.get_accounts()
        ]
        if not accounts:
            print('No accounts.')
            return None

        selected = _prompt_choice('Trackma', text, accounts)
        return selected

    def list_accounts(self):
        accounts = self.get_accounts()

        print("Available accounts:")
        if accounts:
            for k, account in accounts:
                print("%i: %s (%s)" % (k, account['username'], account['api']))
        else:
            print("No accounts.")


def main():
    # Process args
    parser = argparse.ArgumentParser()
    parser.add_argument('-a', '--account', type=int,
                        help='Use specific account number.')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='Show debugging messages.')
    parser.add_argument(
        'cmd', nargs='?', help='Run the following command and exit. Will run in interactive mode if not specified. - will take in commands from stdin.')
    parser.add_argument('args', nargs=argparse.REMAINDER,
                        help='Arguments for the aforementioned command, if any.')
    args = parser.parse_args()

    # Boot Trackma CLI
    main_cmd = Trackma_cmd(args.account, args.debug,
                           interactive=args.cmd is None)
    try:
        main_cmd.start()
        if args.cmd:
            if args.cmd == '-':
                # Run commands from stdin
                for line in sys.stdin:
                    main_cmd.execute_line(line)
            else:
                # Run the specified command in the arguments
                main_cmd.execute_line(' '.join([shlex.quote(args.cmd), *[shlex.quote(arg) for arg in args.args]]))
        else:
            main_cmd.run()
    except utils.TrackmaFatal as e:
        main_cmd.forget_account()
        print("%s%s: %s%s" % (_COLOR_FATAL, type(e).__name__, e, _COLOR_RESET))
    except KeyboardInterrupt:
        if main_cmd.engine:
            try:
                main_cmd.engine.unload()
            except utils.TrackmaError:
                pass
        sys.exit(130)


if __name__ == '__main__':
    main()
