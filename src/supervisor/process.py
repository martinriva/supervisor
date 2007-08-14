##############################################################################
#
# Copyright (c) 2007 Agendaless Consulting and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE
#
##############################################################################

import asyncore
import os
import time
import errno
import shlex
import StringIO
import traceback
import signal

from supervisor.states import ProcessStates
from supervisor.states import getProcessStateDescription

from supervisor.options import decode_wait_status
from supervisor.options import signame
from supervisor.options import ProcessException

from supervisor.dispatchers import EventListenerStates
from supervisor.events import getEventNameByType
from supervisor.events import EventBufferOverflowEvent
from supervisor.events import notify
from supervisor.events import subscribe
from supervisor import events


class Subprocess:

    """A class to manage a subprocess."""

    # Initial state; overridden by instance variables

    pid = 0 # Subprocess pid; 0 when not running
    config = None # ProcessConfig instance
    state = None # process state code
    listener_state = None # listener state code
    laststart = 0 # Last time the subprocess was started; 0 if never
    laststop = 0  # Last time the subprocess was stopped; 0 if never
    delay = 0 # If nonzero, delay starting or killing until this time
    administrative_stop = 0 # true if the process has been stopped by an admin
    system_stop = 0 # true if the process has been stopped by the system
    killing = 0 # flag determining whether we are trying to kill this proc
    backoff = 0 # backoff counter (to startretries)
    dispatchers = None # asnycore output dispatchers (keyed by fd)
    pipes = None # map of channel name to file descriptor #
    exitstatus = None # status attached to dead process by finsh()
    spawnerr = None # error message attached by spawn() if any
    
    def __init__(self, config):
        """Constructor.

        Argument is a ProcessConfig instance.
        """
        self.config = config
        self.dispatchers = {}
        self.pipes = {}
        self.state = ProcessStates.STOPPED

    def removelogs(self):
        for dispatcher in self.dispatchers.values():
            if dispatcher.readable():
                dispatcher.removelogs()

    def reopenlogs(self):
        for dispatcher in self.dispatchers.values():
            if dispatcher.readable():
                dispatcher.reopenlogs()

    def drain(self):
        for dispatcher in self.dispatchers.values():
            if dispatcher.readable():
                dispatcher.handle_read_event()
            if dispatcher.writable():
                dispatcher.handle_write_event()
                
    def write(self, chars):
        if not self.pid or self.killing:
            raise IOError(errno.EPIPE, "Process already closed")
        stdin_fd = self.pipes['stdin']
        if stdin_fd is not None:
            dispatcher = self.dispatchers[stdin_fd]
            dispatcher.input_buffer += chars

    def get_execv_args(self):
        """Internal: turn a program name into a file name, using $PATH,
        make sure it exists / is executable, raising a ProcessException
        if not """
        commandargs = shlex.split(self.config.command)

        program = commandargs[0]

        if "/" in program:
            filename = program
            try:
                st = self.config.options.stat(filename)
            except OSError:
                st = None
            
        else:
            path = self.config.options.get_path()
            filename = None
            st = None
            for dir in path:
                filename = os.path.join(dir, program)
                try:
                    st = self.config.options.stat(filename)
                except OSError:
                    filename = None
                else:
                    break

        # check_execv_args will raise a ProcessException if the execv
        # args are bogus, we break it out into a separate options
        # method call here only to service unit tests
        self.config.options.check_execv_args(filename, commandargs, st)

        return filename, commandargs

    def change_state(self, new_state):
        old_state = self.state
        if new_state is old_state:
            return
        event_type = events.getProcessStateChangeEventType(old_state, new_state)
        notify(event_type(self))
        self.state = new_state

    def _assertInState(self, *states):
        if self.state not in states:
            current_state = getProcessStateDescription(self.state)
            allowable_states = ' '.join(map(getProcessStateDescription, states))
            raise AssertionError('Assertion failed for %s: %s not in %s' %  (
                self.config.name, current_state, allowable_states))

    def record_spawnerr(self, msg):
        now = time.time()
        self.spawnerr = msg
        self.config.options.logger.critical("spawnerr: %s" % msg)
        self.backoff = self.backoff + 1
        self.delay = now + self.backoff

    def spawn(self):
        """Start the subprocess.  It must not be running already.

        Return the process id.  If the fork() call fails, return None.
        """
        pname = self.config.name
        options = self.config.options

        if self.pid:
            msg = 'process %r already running' % pname
            options.logger.critical(msg)
            return

        self.killing = 0
        self.spawnerr = None
        self.exitstatus = None
        self.system_stop = 0
        self.administrative_stop = 0
        
        self.laststart = time.time()

        self._assertInState(ProcessStates.EXITED, ProcessStates.FATAL,
                            ProcessStates.BACKOFF, ProcessStates.STOPPED)

        self.change_state(ProcessStates.STARTING)

        try:
            filename, argv = self.get_execv_args()
        except ProcessException, what:
            self.record_spawnerr(what.args[0])
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)
            return

        try:
            self.dispatchers, self.pipes = self.config.make_dispatchers(self)
        except OSError, why:
            code = why[0]
            if code == errno.EMFILE:
                # too many file descriptors open
                msg = 'too many open files to spawn %r' % pname
            else:
                msg = 'unknown error: %s' % errno.errorcode.get(code, code)
            self.record_spawnerr(msg)
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)
            return

        try:
            pid = options.fork()
        except OSError, why:
            code = why[0]
            if code == errno.EAGAIN:
                # process table full
                msg  = 'Too many processes in process table to spawn %r' % pname
            else:
                msg = 'unknown error: %s' % errno.errorcode.get(code, code)

            self.record_spawnerr(msg)
            self._assertInState(ProcessStates.STARTING)
            self.change_state(ProcessStates.BACKOFF)
            options.close_parent_pipes(self.pipes)
            options.close_child_pipes(self.pipes)
            return

        if pid != 0:
            # Parent
            self.pid = pid
            options.close_child_pipes(self.pipes)
            options.logger.info('spawned: %r with pid %s' % (pname, pid))
            self.spawnerr = None
            self.delay = time.time() + self.config.startsecs
            options.pidhistory[pid] = self
            return pid
        
        else:
            # Child
            try:
                # prevent child from receiving signals sent to the
                # parent by calling os.setpgrp to create a new process
                # group for the child; this prevents, for instance,
                # the case of child processes being sent a SIGINT when
                # running supervisor in foreground mode and Ctrl-C in
                # the terminal window running supervisord is pressed.
                # Presumably it also prevents HUP, etc received by
                # supervisord from being sent to children.
                options.setpgrp()
                options.dup2(self.pipes['child_stdin'], 0)
                options.dup2(self.pipes['child_stdout'], 1)
                if self.config.redirect_stderr:
                    options.dup2(self.pipes['child_stdout'], 2)
                else:
                    options.dup2(self.pipes['child_stderr'], 2)
                for i in range(3, options.minfds):
                    options.close_fd(i)
                # sending to fd 1 will put this output in the log(s)
                msg = self.set_uid()
                if msg:
                    uid = self.config.uid
                    s = 'supervisor: error trying to setuid to %s ' % uid
                    options.write(1, s)
                    options.write(1, "(%s)\n" % msg)
                try:
                    env = os.environ.copy()
                    if self.config.environment is not None:
                        env.update(self.config.environment)
                    options.execve(filename, argv, env)
                except OSError, why:
                    code = why[0]
                    options.write(1, "couldn't exec %s: %s\n" % (
                        argv[0], errno.errorcode.get(code, code)))
                except:
                    (file, fun, line), t,v,tbinfo = asyncore.compact_traceback()
                    error = '%s, %s: file: %s line: %s' % (t, v, file, line)
                    options.write(1, "couldn't exec %s: %s\n" % (filename,
                                                                      error))
            finally:
                options._exit(127)

    def stop(self):
        """ Administrative stop """
        self.drain()
        self.administrative_stop = 1
        return self.kill(self.config.stopsignal)

    def fatal(self):
        self.delay = 0
        self.backoff = 0
        self.system_stop = 1
        self._assertInState(ProcessStates.BACKOFF)
        self.change_state(ProcessStates.FATAL)

    def kill(self, sig):
        """Send a signal to the subprocess.  This may or may not kill it.

        Return None if the signal was sent, or an error message string
        if an error occurred or if the subprocess is not running.
        """
        now = time.time()
        options = self.config.options
        if not self.pid:
            msg = ("attempted to kill %s with sig %s but it wasn't running" %
                   (self.config.name, signame(sig)))
            options.logger.debug(msg)
            return msg
        try:
            options.logger.debug('killing %s (pid %s) with signal %s'
                                 % (self.config.name,
                                    self.pid,
                                    signame(sig)))
            # RUNNING -> STOPPING
            self.killing = 1
            self.delay = now + self.config.stopwaitsecs
            self._assertInState(ProcessStates.RUNNING,ProcessStates.STARTING)
            self.change_state(ProcessStates.STOPPING)
            options.kill(self.pid, sig)
        except (AssertionError, NotImplementedError):
            # AssertionError is raised above, NotImplementedError potentially
            # raised by change_state
            raise
        except:
            io = StringIO.StringIO()
            traceback.print_exc(file=io)
            tb = io.getvalue()
            msg = 'unknown problem killing %s (%s):%s' % (self.config.name,
                                                          self.pid, tb)
            options.logger.critical(msg)
            self.change_state(ProcessStates.UNKNOWN)
            self.pid = 0
            self.killing = 0
            self.delay = 0
            return msg
            
        return None

    def finish(self, pid, sts):
        """ The process was reaped and we need to report and manage its state
        """
        self.drain()

        es, msg = decode_wait_status(sts)

        now = time.time()
        self.laststop = now
        processname = self.config.name

        tooquickly = now - self.laststart < self.config.startsecs
        badexit = not es in self.config.exitcodes
        expected = not (tooquickly or badexit)

        if self.killing:
            # likely the result of a stop request
            # implies STOPPING -> STOPPED
            self.killing = 0
            self.delay = 0
            self.exitstatus = es
            msg = "stopped: %s (%s)" % (processname, msg)
            self._assertInState(ProcessStates.STOPPING)
            self.change_state(ProcessStates.STOPPED)
        elif expected:
            # this finish was not the result of a stop request, but
            # was otherwise expected
            # implies RUNNING -> EXITED
            self.delay = 0
            self.backoff = 0
            self.exitstatus = es
            msg = "exited: %s (%s)" % (processname, msg + "; expected")
            if self.state == ProcessStates.STARTING:
                # XXX I dont know under which circumstances this happens,
                # but in the wild, there is a transition that subverts
                # the RUNNING state (directly from STARTING to EXITED),
                # so we perform the transition here.
                self.change_state(ProcessStates.RUNNING)
            self._assertInState(ProcessStates.RUNNING)
            self.change_state(ProcessStates.EXITED)
        else:
            # the program did not stay up long enough or exited with
            # an unexpected exit code
            self.exitstatus = None
            self.backoff = self.backoff + 1
            self.delay = now + self.backoff
            if tooquickly:
                self.spawnerr = (
                    'Exited too quickly (process log may have details)')
                self._assertInState(ProcessStates.STARTING)
                self.change_state(ProcessStates.BACKOFF)
            elif badexit:
                self.spawnerr = 'Bad exit code %s' % es
                self._assertInState(ProcessStates.RUNNING)
                self.change_state(ProcessStates.EXITED)
            msg = "exited: %s (%s)" % (processname, msg + "; not expected")

        self.config.options.logger.info(msg)

        self.pid = 0
        self.config.options.close_parent_pipes(self.pipes)
        self.pipes = {}
        self.dispatchers = {}

    def set_uid(self):
        if self.config.uid is None:
            return
        msg = self.config.options.dropPrivileges(self.config.uid)
        return msg

    def __cmp__(self, other):
        # sort by priority
        return cmp(self.config.priority, other.config.priority)

    def __repr__(self):
        return '<Subprocess at %s with name %s in state %s>' % (
            id(self),
            self.config.name,
            getProcessStateDescription(self.get_state()))

    def get_state(self):
        return self.state

    def transition(self):
        now = time.time()

        state = self.get_state()

        # we need to transition processes between BACKOFF ->
        # FATAL and STARTING -> RUNNING within here
        logger = self.config.options.logger

        if state == ProcessStates.BACKOFF:
            if self.backoff > self.config.startretries:
                # BACKOFF -> FATAL if the proc has exceeded its number
                # of retries
                self.fatal()
                msg = ('entered FATAL state, too many start retries too '
                       'quickly')
                logger.info('gave up: %s %s' % (self.config.name, msg))

        elif state == ProcessStates.STARTING:
            if now - self.laststart > self.config.startsecs:
                # STARTING -> RUNNING if the proc has started
                # successfully and it has stayed up for at least
                # proc.config.startsecs,
                self.delay = 0
                self.backoff = 0
                msg = (
                    'entered RUNNING state, process has stayed up for '
                    '> than %s seconds (startsecs)' % self.config.startsecs)
                logger.info('success: %s %s' % (self.config.name, msg))
                self._assertInState(ProcessStates.STARTING)
                self.change_state(ProcessStates.RUNNING)
        
class ProcessGroupBase:
    def __init__(self, config):
        self.config = config
        self.processes = {}
        for pconfig in self.config.process_configs:
            self.processes[pconfig.name] = pconfig.make_process()
        

    def __cmp__(self, other):
        return cmp(self.config.priority, other.config.priority)

    def __repr__(self):
        return '<%s instance at %s named %s>' % (self.__class__, id(self),
                                                 self.config.name)

    def removelogs(self):
        for process in self.processes.values():
            process.removelogs()

    def reopenlogs(self):
        for process in self.processes.values():
            process.reopenlogs()

    def start_necessary(self):
        processes = self.processes.values()
        processes.sort() # asc by priority
        now = time.time()

        for p in processes:
            state = p.get_state()
            if state == ProcessStates.STOPPED and not p.laststart:
                if p.config.autostart:
                    # STOPPED -> STARTING
                    p.spawn()
            elif state == ProcessStates.EXITED:
                if p.config.autorestart:
                    # EXITED -> STARTING
                    p.spawn()
            elif state == ProcessStates.BACKOFF:
                if now > p.delay:
                    # BACKOFF -> STARTING
                    p.spawn()

    def stop_all(self):
        processes = self.processes.values()
        processes.sort()
        processes.reverse() # stop in desc priority order

        for proc in processes:
            state = proc.get_state()
            if state == ProcessStates.RUNNING:
                # RUNNING -> STOPPING
                proc.stop()
            elif state == ProcessStates.STARTING:
                # STARTING -> STOPPING
                proc.stop()
            elif state == ProcessStates.BACKOFF:
                # BACKOFF -> FATAL
                proc.fatal()

    def get_delay_processes(self):
        """ Processes which are starting or stopping """
        return [ x for x in self.processes.values() if x.delay ]

    def get_undead(self):
        """ Processes which we've attempted to stop but which haven't responded
        to a kill request within a given amount of time (stopwaitsecs) """
        now = time.time()
        processes = self.processes.values()
        undead = []

        for proc in processes:
            if proc.get_state() == ProcessStates.STOPPING:
                time_left = proc.delay - now
                if time_left <= 0:
                    undead.append(proc)
        return undead

    def kill_undead(self):
        for undead in self.get_undead():
            # kill processes which are taking too long to stop with a final
            # sigkill.  if this doesn't kill it, the process will be stuck
            # in the STOPPING state forever.
            self.config.options.logger.critical(
                'killing %r (%s) with SIGKILL' % (undead.config.name,
                                                  undead.pid))
            undead.kill(signal.SIGKILL)

    def get_dispatchers(self):
        dispatchers = {}
        for process in self.processes.values():
            dispatchers.update(process.dispatchers)
        return dispatchers

class ProcessGroup(ProcessGroupBase):
    def transition(self):
        self.kill_undead()
        for proc in self.processes.values():
            proc.transition()

class EventListenerPool(ProcessGroupBase):
    def __init__(self, config):
        ProcessGroupBase.__init__(self, config)
        self.event_buffer = []
        for event_type in self.config.pool_events:
            subscribe(event_type, self._dispatchEvent)
        subscribe(events.EventRejectedEvent, self.handle_rejected)

    def handle_rejected(self, event):
        process = event.process
        procs = self.processes.values()
        if process in procs: # this is one of our processes
            # rebuffer the event
            self._bufferEvent(event.event)

    def transition(self):
        self.kill_undead()
        for proc in self.processes.values():
            proc.transition()
        if self.event_buffer:
            # resend the oldest buffered event (dont rebuffer though, maintain
            # order oldest (leftmost) to newest (rightmost) in list)
            event = self.event_buffer.pop(0)
            ok = self._dispatchEvent(event, buffer=False)
            if not ok:
                self.config.options.logger.log(self.config.options.TRACE,
                                               'Failed sending buffered event '
                                               '%s' % event)
                self.event_buffer.insert(0, event)

    def _eventEnvelope(self, event_type, payload):
        event_name = getEventNameByType(event_type)
        payload_len = len(payload)
        D = {'ver':'3.0',
             'len':payload_len,
             'event_name':event_name,
             'payload':payload}
        return 'SUPERVISORD%(ver)s %(event_name)s %(len)s\n%(payload)s' % D

    def _bufferEvent(self, event):
        if isinstance(event, EventBufferOverflowEvent):
            return # don't ever buffer EventBufferOverflowEvents
        if len(self.event_buffer) >= self.config.buffer_size:
            discarded_event = self.event_buffer.pop(0)
            notify(EventBufferOverflowEvent(self, discarded_event))
        self.config.options.logger.log(self.config.options.TRACE,
                                       'pool %s busy, buffering event %s' % (
                                           (self.config.name, event)))
        self.event_buffer.append(event)

    def _dispatchEvent(self, event, buffer=True):
        # events are required to be instances
        serializer = None
        event_type = event.__class__
        for klass, callback in serializers.items():
            if isinstance(event, klass):
                serializer = callback
        if serializer is None:
            # this is a system programming error, we must handle
            # all events
            raise NotImplementedError(etype)
        for process in self.processes.values():
            if process.listener_state == EventListenerStates.READY:
                payload = serializer(event)
                try:
                    envelope = self._eventEnvelope(event_type, payload)
                    process.write(envelope)
                except IOError, why:
                    if why[0] == errno.EPIPE:
                        continue
                process.listener_state = EventListenerStates.BUSY
                process.event = event
                return True

        if buffer:
            self._bufferEvent(event)
        return False

serializers = {}
def pcomm_event(event):
    return 'process_name: %s\nchannel: %s\n%s' % (
        event.process.config.name,
        event.channel,
        event.data)
serializers[events.ProcessCommunicationEvent] = pcomm_event

def overflow_event(event):
    name = event.group.config.name
    typ = getEventNameByType(event.event)
    return 'group_name: %s\nevent_type: %s' % (name, typ)
serializers[events.EventBufferOverflowEvent] = overflow_event

def proc_sc_event(event):
    return 'process_name: %s\n' % event.process.config.name

serializers[events.ProcessStateChangeEvent] = proc_sc_event

def supervisor_sc_event(event):
    return ''
serializers[events.SupervisorStateChangeEvent] = supervisor_sc_event

            
    