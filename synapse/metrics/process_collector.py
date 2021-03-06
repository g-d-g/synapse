# -*- coding: utf-8 -*-
# Copyright 2015, 2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Because otherwise 'resource' collides with synapse.metrics.resource
from __future__ import absolute_import

import os
import stat
from resource import getrusage, RUSAGE_SELF


TICKS_PER_SEC = 100
BYTES_PER_PAGE = 4096

HAVE_PROC_STAT = os.path.exists("/proc/stat")
HAVE_PROC_SELF_STAT = os.path.exists("/proc/self/stat")
HAVE_PROC_SELF_LIMITS = os.path.exists("/proc/self/limits")
HAVE_PROC_SELF_FD = os.path.exists("/proc/self/fd")

TYPES = {
    stat.S_IFSOCK: "SOCK",
    stat.S_IFLNK: "LNK",
    stat.S_IFREG: "REG",
    stat.S_IFBLK: "BLK",
    stat.S_IFDIR: "DIR",
    stat.S_IFCHR: "CHR",
    stat.S_IFIFO: "FIFO",
}

# Field indexes from /proc/self/stat, taken from the proc(5) manpage
STAT_FIELDS = {
    "utime": 14,
    "stime": 15,
    "starttime": 22,
    "vsize": 23,
    "rss": 24,
}


rusage = None
stats = {}
fd_counts = None

# In order to report process_start_time_seconds we need to know the
# machine's boot time, because the value in /proc/self/stat is relative to
# this
boot_time = None
if HAVE_PROC_STAT:
    with open("/proc/stat") as _procstat:
        for line in _procstat:
            if line.startswith("btime "):
                boot_time = int(line.split()[1])


def update_resource_metrics():
    global rusage
    rusage = getrusage(RUSAGE_SELF)

    if HAVE_PROC_SELF_STAT:
        global stats
        with open("/proc/self/stat") as s:
            line = s.read()
            # line is PID (command) more stats go here ...
            raw_stats = line.split(") ", 1)[1].split(" ")

            for (name, index) in STAT_FIELDS.iteritems():
                # subtract 3 from the index, because proc(5) is 1-based, and
                # we've lost the first two fields in PID and COMMAND above
                stats[name] = int(raw_stats[index - 3])

    global fd_counts
    fd_counts = _process_fds()


def _process_fds():
    counts = {(k,): 0 for k in TYPES.values()}
    counts[("other",)] = 0

    # Not every OS will have a /proc/self/fd directory
    if not HAVE_PROC_SELF_FD:
        return counts

    for fd in os.listdir("/proc/self/fd"):
        try:
            s = os.stat("/proc/self/fd/%s" % (fd))
            fmt = stat.S_IFMT(s.st_mode)
            if fmt in TYPES:
                t = TYPES[fmt]
            else:
                t = "other"

            counts[(t,)] += 1
        except OSError:
            # the dirh itself used by listdir() is usually missing by now
            pass

    return counts


def register_process_collector(process_metrics):
    # Legacy synapse-invented metric names

    resource_metrics = process_metrics.make_subspace("resource")

    resource_metrics.register_collector(update_resource_metrics)

    # msecs
    resource_metrics.register_callback("utime", lambda: rusage.ru_utime * 1000)
    resource_metrics.register_callback("stime", lambda: rusage.ru_stime * 1000)

    # kilobytes
    resource_metrics.register_callback("maxrss", lambda: rusage.ru_maxrss * 1024)

    process_metrics.register_callback("fds", _process_fds, labels=["type"])

    # New prometheus-standard metric names

    if HAVE_PROC_SELF_STAT:
        process_metrics.register_callback(
            "cpu_user_seconds_total",
            lambda: float(stats["utime"]) / TICKS_PER_SEC
        )
        process_metrics.register_callback(
            "cpu_system_seconds_total",
            lambda: float(stats["stime"]) / TICKS_PER_SEC
        )
        process_metrics.register_callback(
            "cpu_seconds_total",
            lambda: (float(stats["utime"] + stats["stime"])) / TICKS_PER_SEC
        )

        process_metrics.register_callback(
            "virtual_memory_bytes",
            lambda: int(stats["vsize"])
        )
        process_metrics.register_callback(
            "resident_memory_bytes",
            lambda: int(stats["rss"]) * BYTES_PER_PAGE
        )

        process_metrics.register_callback(
            "start_time_seconds",
            lambda: boot_time + int(stats["starttime"]) / TICKS_PER_SEC
        )

    if HAVE_PROC_SELF_FD:
        process_metrics.register_callback(
            "open_fds",
            lambda: sum(fd_counts.values())
        )

    if HAVE_PROC_SELF_LIMITS:
        def _get_max_fds():
            with open("/proc/self/limits") as limits:
                for line in limits:
                    if not line.startswith("Max open files "):
                        continue
                    # Line is  Max open files  $SOFT  $HARD
                    return int(line.split()[3])
            return None

        process_metrics.register_callback(
            "max_fds",
            lambda: _get_max_fds()
        )
