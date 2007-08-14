import sys
import unittest

class EventSubscriptionNotificationTests(unittest.TestCase):
    def setUp(self):
        from supervisor import events
        events.callbacks[:] = []

    def tearDown(self):
        from supervisor import events
        events.callbacks[:] = []

    def test_subscribe(self):
        from supervisor import events
        events.subscribe(None, None)
        self.assertEqual(events.callbacks, [(None, None)])

    def test_clear(self):
        from supervisor import events
        events.callbacks[:] = [(None, None)]
        events.clear()
        self.assertEqual(events.callbacks, [])

    def test_notify_true(self):
        from supervisor import events
        L = []
        def callback(event):
            L.append(1)
        class DummyEvent:
            pass
        events.callbacks[:] = [(DummyEvent, callback)]
        events.notify(DummyEvent())
        self.assertEqual(L, [1])

    def test_notify_false(self):
        from supervisor import events
        L = []
        def callback(event):
            L.append(1)
        class DummyEvent:
            pass
        class AnotherEvent:
            pass
        events.callbacks[:] = [(AnotherEvent, callback)]
        events.notify(DummyEvent())
        self.assertEqual(L, [])

    def test_notify_via_subclass(self):
        from supervisor import events
        L = []
        def callback(event):
            L.append(1)
        class DummyEvent:
            pass
        class ASubclassEvent(DummyEvent):
            pass
        events.callbacks[:] = [(DummyEvent, callback)]
        events.notify(ASubclassEvent())
        self.assertEqual(L, [1])
        

class TestEventTypes(unittest.TestCase):
    def test_EventBufferOverflowEvent(self):
        from supervisor.events import EventBufferOverflowEvent
        inst = EventBufferOverflowEvent(1, 2)
        self.assertEqual(inst.group, 1)
        self.assertEqual(inst.event, 2)

    def test_ProcessCommunicationEvent(self):
        from supervisor.events import ProcessCommunicationEvent
        inst = ProcessCommunicationEvent(1, 2)
        self.assertEqual(inst.process, 1)
        self.assertEqual(inst.data, 2)

    def test_ProcessCommunicationStdoutEvent(self):
        from supervisor.events import ProcessCommunicationStdoutEvent
        inst = ProcessCommunicationStdoutEvent(1, 2)
        self.assertEqual(inst.process, 1)
        self.assertEqual(inst.data, 2)
        self.assertEqual(inst.channel, 'stdout')
        
    def test_ProcessCommunicationStderrEvent(self):
        from supervisor.events import ProcessCommunicationStderrEvent
        inst = ProcessCommunicationStderrEvent(1, 2)
        self.assertEqual(inst.process, 1)
        self.assertEqual(inst.data, 2)
        self.assertEqual(inst.channel, 'stderr')

    def test_ProcessStateChangeEvent(self):
        from supervisor.events import ProcessStateChangeEvent
        inst = ProcessStateChangeEvent(1)
        self.assertEqual(inst.process, 1)
        
class TestUtilityFunctions(unittest.TestCase):
    def test_getEventNameByType(self):
        from supervisor import events
        for name, value in events.EventTypes.__dict__.items():
            self.assertEqual(events.getEventNameByType(value), name)

    def _assertStateChange(self, old, new, expected):
        from supervisor.events import getProcessStateChangeEventType
        klass = getProcessStateChangeEventType(old, new)
        self.assertEqual(expected, klass)

    def test_getProcessStateChangeEventType_STOPPED_TO_STARTING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.STOPPED, ProcessStates.STARTING,
                                events.StartingFromStoppedEvent)
        
    def test_getProcessStateChangeEventType_STARTING_TO_RUNNING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.STARTING, ProcessStates.RUNNING,
                                events.RunningFromStartingEvent)

    def test_getProcessStateChangeEventType_STARTING_TO_BACKOFF(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.STARTING, ProcessStates.BACKOFF,
                                events.BackoffFromStartingEvent)

    def test_getProcessStateChangeEventType_BACKOFF_TO_STARTING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.BACKOFF, ProcessStates.STARTING,
                                events.StartingFromBackoffEvent)

    def test_getProcessStateChangeEventType_BACKOFF_TO_FATAL(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.BACKOFF, ProcessStates.FATAL,
                                events.FatalFromBackoffEvent)

    def test_getProcessStateChangeEventType_FATAL_TO_STARTING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.FATAL, ProcessStates.STARTING,
                                events.StartingFromFatalEvent)

    def test_getProcessStateChangeEventType_STARTING_TO_RUNNING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.STARTING, ProcessStates.RUNNING,
                                events.RunningFromStartingEvent)

    def test_getProcessStateChangeEventType_RUNNING_TO_EXITED(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.RUNNING, ProcessStates.EXITED,
                                events.ExitedFromRunningEvent)

    def test_getProcessStateChangeEventType_EXITED_TO_STARTING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.EXITED, ProcessStates.STARTING,
                                events.StartingFromExitedEvent)

    def test_getProcessStateChangeEventType_RUNNING_TO_STOPPING(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.RUNNING, ProcessStates.STOPPING,
                                events.StoppingFromRunningEvent)

    def test_getProcessStateChangeEventType_STOPPING_TO_STOPPED(self):
        from supervisor.states import ProcessStates
        from supervisor import events
        self._assertStateChange(ProcessStates.STOPPING, ProcessStates.STOPPED,
                                events.StoppedFromStoppingEvent)

def test_suite():
    return unittest.findTestCases(sys.modules[__name__])

if __name__ == '__main__':
    unittest.main(defaultTest='test_suite')
