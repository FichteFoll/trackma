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

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, NamedTuple

from trackma import utils
from trackma.messenger import Messenger
from trackma.parser import get_parser_class

OnPlaybackCallback = Callable[[bool], None]
OnTickCallback = Callable[[float | None], None]


class TrackerResolution(NamedTuple):
    state: utils.Tracker
    show: dict[str, Any] | None
    show_ep: int | None

    @classmethod
    def NO_VIDEO(cls) -> TrackerResolution:
        return cls(utils.Tracker.NOVIDEO, None, None)

    @classmethod
    def UNRECOGNIZED(cls) -> TrackerResolution:
        return cls(utils.Tracker.UNRECOGNIZED, None, None)

    def show_tuple(self) -> tuple[dict[str, Any], int] | None:
        if self.show is None or self.show_ep is None:
            return None
        return (self.show, self.show_ep)


OnStateCallback = Callable[[TrackerResolution, str | None], None]


class TrackerTimer:
    def __init__(self, paused: bool = False):
        self.started_at = time.time()
        self.paused_at = self.started_at if paused else None
        self.paused_total = 0.0

    @property
    def paused(self) -> bool:
        return self.paused_at is not None

    def pause(self) -> None:
        if self.paused_at is None:
            self.paused_at = time.time()

    def resume(self) -> None:
        if self.paused_at is not None:
            self.paused_total += time.time() - self.paused_at
            self.paused_at = None

    def elapsed(self) -> float:
        end_at = self.paused_at if self.paused_at is not None else time.time()
        return end_at - self.started_at - self.paused_total


class TrackerBase:
    msg: Messenger
    active = True
    list = None
    last_resolution = None
    last_filename = None
    last_state = utils.Tracker.NOVIDEO
    last_updated = False
    last_close_queue: Callable[[], None] | None = None
    timer: TrackerTimer | None = None

    name = 'Tracker'

    signals = {
        'state': None,
        'detected': None,
        'playing': None,
        'removed': None,
        'update': None,
        'unrecognised': None,
    }

    def __init__(self, messenger, tracker_list, config, watch_dirs, redirections=None):
        self.msg = messenger.with_classname(self.name)
        self.msg.info('Initializing...')

        self.list = tracker_list
        self.config = config
        self.redirections = redirections
        # Reverse sorting for prefix matching
        self.watch_dirs = tuple(sorted(watch_dirs, reverse=True))
        self.wait_s = None

        self.timer = None
        self.parser_class = get_parser_class(self.msg, self.config['title_parser'])

        self.view_offset = None

        tracker_args = (config, watch_dirs)
        tracker_t = threading.Thread(target=self.observe, args=tracker_args)
        tracker_t.daemon = True

        self.msg.debug('Enabling tracker...')
        tracker_t.start()

    def set_message_handler(self, message_handler):
        """Changes the message handler function on the fly."""
        self.msg = message_handler.with_classname(self.name)

    def disable(self) -> None:
        self.msg.info('Unloading...')
        self.active = False

    def update_list(self, tracker_list):
        self.list = tracker_list

    def connect_signal(self, signal, callback):
        try:
            self.signals[signal] = callback
        except KeyError:
            raise utils.EngineFatal("Invalid signal.")

    def observe(self, _config, _watch_dirs, /) -> None:
        raise NotImplementedError

    def get_status(self):
        timer = None
        if self.timer is not None:
            timer = int(
                1
                + (self.wait_s or self.config['tracker_update_wait_s'])
                - self.timer.elapsed()
            )

        return {
            'state': self.last_state,
            'timer': timer,
            'viewOffset': self.view_offset,
            'paused': bool(self.timer and self.timer.paused),
            'show': self.last_resolution.show_tuple() if self.last_resolution else None,
            'filename': self.last_filename,
        }

    def _emit_signal(self, signal, *args):
        try:
            if callback := self.signals[signal]:
                callback(*args)
        except KeyError:
            raise Exception("Call to undefined signal.")

    def update_timer(self) -> None:
        if not self.timer or self.timer.paused or self.last_updated:
            return

        resolution = self.last_resolution
        if not resolution or not resolution.show or resolution.show_ep is None or resolution.state != utils.Tracker.PLAYING:
            self.timer = None
            return

        # This computes the elapsed time.
        status = self.get_status()
        self._emit_signal('state', status)

        if (status['timer'] or 0) <= 0:
            # Perform show update
            self.last_updated = True

            def emit_update() -> None:
                if resolution.state == utils.Tracker.PLAYING:
                    self._emit_signal('update', resolution.show, resolution.show_ep)
                elif resolution.state == utils.Tracker.NOT_FOUND:
                    self._emit_signal('unrecognised', resolution.show, resolution.show_ep)

            if self.config['tracker_update_close']:
                self.msg.info('Waiting for the player to close.')
                self.last_close_queue = emit_update
            else:
                emit_update()

    def _prepare_state_change(self) -> None:
        # Call when show or state is changed. Perform queued update if any.
        if self.last_close_queue:
            self.last_close_queue()
            self.last_close_queue = None

        self.timer = None

        # Signal that the "current" episode stopped playing.
        if self.last_resolution:
            last_show = self.last_resolution.show
            last_show_ep = self.last_resolution.show_ep
            if not last_show or last_show_ep is None:
                return
            if last_show['id']:
                self._emit_signal(
                    'playing', last_show['id'], False, last_show_ep)

    def pause_timer(self) -> None:
        if self.timer:
            self.timer.pause()

            self._emit_signal('state', self.get_status())

    def resume_timer(self) -> None:
        if self.timer:
            self.timer.resume()

            self._emit_signal('state', self.get_status())

    def update_show_if_needed(self, resolution: TrackerResolution, filename: str | None = None):
        self.last_filename = filename

        if resolution == self.last_resolution:
            self.update_timer()
            return

        show_tuple = resolution.show_tuple()
        if resolution.state == utils.Tracker.IGNORED:
            if self.last_resolution and show_tuple == self.last_resolution.show_tuple():
                self.timer = None
            # The state should be ignored.
            return

        elif show_tuple:
            # A new show/ep pair was found that should result in an update eventually.
            self._prepare_state_change()
            (show, episode) = show_tuple
            self._emit_signal('playing', show['id'], True, episode)

            if resolution.state == utils.Tracker.PLAYING:
                self.msg.info('Will update %s - %d' % (show['title'], episode))
            elif resolution.state == utils.Tracker.NOT_FOUND:
                self.msg.info('Will add %s - %d' % (show['title'], episode))
            else:
                self.msg.warn(f"Some state transition was not considered. {self.last_resolution} => {resolution}")

            self.last_resolution = resolution
            self.last_updated = False
            self.timer = TrackerTimer()
            self.update_timer()

        elif self.last_state != resolution.state:
            self._prepare_state_change()

            # React depending on state
            if resolution.state == utils.Tracker.NOVIDEO:  # No video is playing
                # Video didn't get to update phase before it was closed
                if self.last_state == utils.Tracker.PLAYING and not self.last_updated:
                    self.msg.info('Player was closed before update.')
            # There's a new video playing but the regex didn't recognize the format
            elif resolution.state == utils.Tracker.UNRECOGNIZED:
                self.msg.warn("Found video but the file name format couldn't be recognized.")
            elif resolution.state == utils.Tracker.NOT_FOUND:  # There's a new video playing but an associated show wasn't found
                self.msg.warn('Found player but show not in list.')

            self.last_resolution = None
            self.last_updated = False
            self.timer = None

        self.last_state = resolution.state
        self._emit_signal('state', self.get_status())

    def resolve_playing_show(self, filename: str | None) -> TrackerResolution:
        if not self.active:
            # Don't do anything if the Tracker is disabled
            return (utils.Tracker.NOVIDEO, None)

        if filename:
            if filename == self.last_filename:
                # It's the exact same filename, there's no need to do the processing again
                return (self.last_state, self.last_show_tuple)

            self.last_filename = filename
            self.msg.debug("Guessing filename: {}".format(filename))

            # Trim out watch dir
            if os.path.isabs(filename):
                for watch_prefix in self.watch_dirs:
                    if filename.startswith(watch_prefix):
                        filename = filename[len(watch_prefix):].lstrip(os.path.sep)
                        break

            # Invoke the parser to extract show title and episode.
            try:
                aie = self.parser_class(self.msg, filename)
                (show_title, show_ep) = (aie.getName(), aie.getEpisode())
            except Exception:
                self.msg.exception('Failed to parse filename', sys.exc_info())
                return (utils.Tracker.UNRECOGNIZED, None)
            if not show_title:
                # Format not recognized
                return (utils.Tracker.UNRECOGNIZED, None)

            playing_show = utils.guess_show(show_title, self.list)
            self.msg.debug("Show guess: {}: {} - {}".format(show_title, playing_show, show_ep))

            if playing_show:
                (redirected_show, redirected_ep) = utils.redirect_show(
                    (playing_show, show_ep), self.redirections, self.list)
                if (redirected_show, redirected_ep) != (playing_show, show_ep):
                    self.msg.debug("Redirected to: {} - {}".format(redirected_show, redirected_ep))
                    (playing_show, show_ep) = (redirected_show, redirected_ep)

                return (utils.Tracker.PLAYING, (playing_show, show_ep))
            else:
                # Show not in list
                if self.config['tracker_not_found_prompt']:
                    # Dummy show to search for
                    show = {'id': 0, 'title': show_title}
                    return (utils.Tracker.NOT_FOUND, (show, show_ep))
                else:
                    return (utils.Tracker.NOT_FOUND, None)
        else:
            # Show not in list
            if self.config['tracker_not_found_prompt']:
                # Dummy show to search for
                show = {'id': 0, 'title': show_title}
                return TrackerResolution(utils.Tracker.NOT_FOUND, show, show_ep)
            else:
                return TrackerResolution(utils.Tracker.NOT_FOUND, None, None)

    def _should_ignore(self, playing_show: Any, show_ep: int) -> bool:
        expected_ep = playing_show['my_progress'] + 1
        title = playing_show['title']
        if show_ep == playing_show['my_progress']:
            self.msg.warn(f"Playing the current episode of {title}. Ignoring.")
            return True
        elif show_ep < 1 or (playing_show['total'] and show_ep > playing_show['total']):
            self.msg.warn(f"Playing an invalid episode of {title}. Ignoring.")
            return True
        elif self.config['tracker_ignore_not_next'] and show_ep != expected_ep:
            self.msg.warn(
                f'Not playing the next episode of {title}'
                f' (expected: {expected_ep}, found: {show_ep}). Ignoring.',
            )
            return True
        return False
