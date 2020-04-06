import itertools
import time
from unittest.mock import patch

import pytest

import dramatiq
from dramatiq import Message, Middleware
from dramatiq.errors import RateLimitExceeded, Retry
from dramatiq.middleware import CurrentMessage, SkipMessage

from .common import skip_on_pypy, worker


def test_actors_can_be_defined(stub_broker):
    # Given that I've decorated a function with @actor
    @dramatiq.actor
    def add(x, y):
        return x + y

    # I expect that function to become an instance of Actor
    assert isinstance(add, dramatiq.Actor)


def test_actors_can_be_declared_with_actor_class(stub_broker):
    # Given that I have a non-standard Actor class
    class ActorChild(dramatiq.Actor):
        pass

    # When I define an actor with that class
    @dramatiq.actor(actor_class=ActorChild)
    def add(x, y):
        return x + y

    # Then that actor should be an instance of ActorChild
    assert isinstance(add, ActorChild)


def test_actors_can_be_assigned_predefined_options(stub_broker):
    # Given that I have a stub broker with the retries middleware
    # If I define an actor with a max_retries number
    @dramatiq.actor(max_retries=32)
    def add(x, y):
        return x + y

    # I expect the option to persist
    assert add.options["max_retries"] == 32


def test_actors_cannot_be_assigned_arbitrary_options(stub_broker):
    # Given that I have a stub broker
    # If I define an actor with a nonexistent option
    # I expect it to raise a ValueError
    with pytest.raises(ValueError):
        @dramatiq.actor(invalid_option=32)
        def add(x, y):
            return x + y


def test_actors_can_be_named(stub_broker):
    # Given that I've decorated a function with @actor and named it explicitly
    @dramatiq.actor(actor_name="foo")
    def add(x, y):
        return x + y

    # I expect the returned function to have that name
    assert add.actor_name == "foo"


def test_actors_can_be_assigned_custom_queues(stub_broker):
    # Given that I've decorated a function with @actor and given it an explicit queue
    @dramatiq.actor(queue_name="foo")
    def foo():
        pass

    # I expect the returned function to use that queue
    assert foo.queue_name == "foo"


def test_actors_fail_given_invalid_queue_names(stub_broker):
    # If I define an actor with an invalid queue name
    # I expect a ValueError to be raised
    with pytest.raises(ValueError):
        @dramatiq.actor(queue_name="$2@!@#")
        def foo():
            pass


def test_actors_can_be_called(stub_broker):
    # Given that I have an actor
    @dramatiq.actor
    def add(x, y):
        return x + y

    # If I call it directly,
    # I expect it to run synchronously
    assert add(1, 2) == 3


def test_actors_can_be_sent_messages(stub_broker):
    # Given that I have an actor
    @dramatiq.actor
    def add(x, y):
        return x + y

    # If I send it a message,
    # I expect it to enqueue a message
    enqueued_message = add.send(1, 2)
    enqueued_message_data = stub_broker.queues["default"].get(timeout=1)
    assert enqueued_message == Message.decode(enqueued_message_data)


def test_actors_can_perform_work(stub_broker, stub_worker):
    # Given that I have a database
    database = {}

    # And an actor that can write data to that database
    @dramatiq.actor
    def put(key, value):
        database[key] = value

    # If I send that actor many async messages
    for i in range(100):
        assert put.send("key-%s" % i, i)

    # Then join on the queue
    stub_broker.join(put.queue_name)
    stub_worker.join()

    # I expect the database to be populated
    assert len(database) == 100


def test_actors_can_perform_work_with_kwargs(stub_broker, stub_worker):
    # Given that I have a database
    results = []

    # And an actor
    @dramatiq.actor
    def add(x, y):
        results.append(x + y)

    # If I send it a message with kwargs
    add.send(x=1, y=2)

    # Then join on the queue
    stub_broker.join(add.queue_name)
    stub_worker.join()

    # I expect the database to be populated
    assert results == [3]


def test_actors_retry_on_failure(stub_broker, stub_worker):
    # Given that I have a database
    failures, successes = [], []

    # And an actor that fails the first time it's called
    @dramatiq.actor(min_backoff=100, max_backoff=500)
    def do_work():
        if sum(failures) == 0:
            failures.append(1)
            raise RuntimeError("First failure.")
        else:
            successes.append(1)

    # If I send it a message
    do_work.send()

    # Then join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # I expect successes
    assert sum(successes) == 1


def test_actors_retry_a_max_number_of_times_on_failure(stub_broker, stub_worker):
    # Given that I have a database
    attempts = []

    # And an actor that fails every time
    @dramatiq.actor(max_retries=3, min_backoff=100, max_backoff=500)
    def do_work():
        attempts.append(1)
        raise RuntimeError("failure")

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect 4 attempts to have occurred
    assert sum(attempts) == 4


def test_actors_retry_for_a_max_time(stub_broker, stub_worker):
    # Given that I have a database
    attempts = []

    # And an actor that fails every time
    @dramatiq.actor(max_age=100, min_backoff=50, max_backoff=500)
    def do_work():
        attempts.append(1)
        raise RuntimeError("failure")

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect at least one attempt to have occurred
    assert sum(attempts) >= 1


def test_retry_exceptions_are_not_logged(stub_broker, stub_worker):
    # Given that I have an actor that raises Retry
    @dramatiq.actor(max_retries=0)
    def do_work():
        raise Retry()

    # And that I've mocked the logging class
    with patch("logging.Logger.error") as error_mock:
        # When I send that actor a message
        do_work.send()

        # And join on the queue
        stub_broker.join(do_work.queue_name)
        stub_worker.join()

        # Then no error should be logged
        error_messages = [args[0] for _, args, _ in error_mock.mock_calls]
        assert error_messages == []


def test_retry_exceptions_can_specify_a_delay(stub_broker, stub_worker):
    # Given that I have an actor that raises Retry
    attempts = 0
    timestamps = [time.monotonic()]

    @dramatiq.actor(max_retries=1)
    def do_work():
        nonlocal attempts
        attempts += 1
        timestamps.append(time.monotonic())
        if attempts == 1:
            raise Retry(delay=100)

    # When I send that actor a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then the actor should have been retried after 100ms
    assert 0.1 <= timestamps[-1] - timestamps[-2] < 0.15


@skip_on_pypy
def test_actors_can_be_assigned_time_limits(stub_broker, stub_worker):
    # Given that I have a database
    attempts, successes = [], []

    # And an actor with a time limit
    @dramatiq.actor(max_retries=0, time_limit=1000)
    def do_work():
        attempts.append(1)
        time.sleep(3)
        successes.append(1)

    # When I send it a message
    do_work.send()

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect it to fail
    assert sum(attempts) == 1
    assert sum(successes) == 0


@skip_on_pypy
def test_actor_messages_can_be_assigned_time_limits(stub_broker, stub_worker):
    # Given that I have a database
    attempts, successes = [], []

    # And an actor without an explicit time limit
    @dramatiq.actor(max_retries=0)
    def do_work():
        attempts.append(1)
        time.sleep(2)
        successes.append(1)

    # If I send it a message with a custom time limit
    do_work.send_with_options(time_limit=1000)

    # Then join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # I expect it to fail
    assert sum(attempts) == 1
    assert sum(successes) == 0


def test_actors_can_be_assigned_message_age_limits(stub_broker):
    # Given that I have a database
    runs = []

    # And an actor whose messages have an age limit
    @dramatiq.actor(max_age=100)
    def do_work():
        runs.append(1)

    # When I send it a message
    do_work.send()

    # And wait for its age limit to pass
    time.sleep(0.1)

    # Then join on its queue
    with worker(stub_broker, worker_timeout=100) as stub_worker:
        stub_broker.join(do_work.queue_name)
        stub_worker.join()

        # I expect the message to have been skipped
        assert sum(runs) == 0


def test_actors_can_be_assigned_message_max_retries(stub_broker, stub_worker):
    # Given that I have a database
    attempts = []

    # And an actor that fails every time and is retried with huge backoff
    @dramatiq.actor(max_retries=99, min_backoff=5000, max_backoff=50000)
    def do_work():
        attempts.append(1)
        raise RuntimeError("failure")

    # When I send it a message with tight backoff and custom max retries
    do_work.send_with_options(max_retries=4, min_backoff=50, max_backoff=500)

    # And join on the queue
    stub_broker.join(do_work.queue_name)
    stub_worker.join()

    # Then I expect it to be retried as specified in the message options
    assert sum(attempts) == 5


def test_actors_can_delay_messages_independent_of_each_other(stub_broker, stub_worker):
    # Given that I have a database
    results = []

    # And an actor that appends a number to the database
    @dramatiq.actor
    def append(x):
        results.append(x)

    # If I send it a delayed message
    append.send_with_options(args=(1,), delay=1500)

    # And then another delayed message with a smaller delay
    append.send_with_options(args=(2,), delay=1000)

    # Then join on the queue
    stub_broker.join(append.queue_name)
    stub_worker.join()

    # I expect the latter message to have been run first
    assert results == [2, 1]


def test_messages_belonging_to_missing_actors_are_rejected(stub_broker, stub_worker):
    # Given that I have a broker without actors
    # If I send it a message
    message = Message(
        queue_name="some-queue",
        actor_name="some-actor",
        args=(), kwargs={},
        options={},
    )
    stub_broker.declare_queue("some-queue")
    stub_broker.enqueue(message)

    # Then join on the queue
    stub_broker.join("some-queue")
    stub_worker.join()

    # I expect the message to end up on the dead letter queue
    assert stub_broker.dead_letters == [message]


def test_before_and_after_signal_failures_are_ignored(stub_broker, stub_worker):
    # Given that I have a middleware that raises exceptions when it
    # tries to process messages.
    class BrokenMiddleware(Middleware):
        def before_process_message(self, broker, message):
            raise RuntimeError("before process message error")

        def after_process_message(self, broker, message, *, result=None, exception=None):
            raise RuntimeError("after process message error")

    # And a database
    database = []

    # And an actor that appends values to the database
    @dramatiq.actor
    def append(x):
        database.append(x)

    # If add that middleware to my broker
    stub_broker.add_middleware(BrokenMiddleware())

    # And send my actor a message
    append.send(1)

    # Then join on the queue
    stub_broker.join(append.queue_name)
    stub_worker.join()

    # I expect the task to complete successfully
    assert database == [1]


def test_middleware_can_decide_to_skip_messages(stub_broker, stub_worker):
    # Given a middleware that skips all messages
    skipped_messages = []

    class SkipMiddleware(Middleware):
        def before_process_message(self, broker, message):
            raise SkipMessage()

        def after_skip_message(self, broker, message):
            skipped_messages.append(1)

    stub_broker.add_middleware(SkipMiddleware())

    # And an actor that keeps track of its calls
    calls = []

    @dramatiq.actor
    def track_call():
        calls.append(1)

    # When I send that actor a message
    track_call.send()

    # And join on the broker and the worker
    stub_broker.join(track_call.queue_name)
    stub_worker.join()

    # Then I expect the call list to be empty
    assert sum(calls) == 0

    # And the skipped_messages list to contain one item
    assert sum(skipped_messages) == 1


def test_workers_can_be_paused(stub_broker, stub_worker):
    # Given a paused worker
    stub_worker.pause()

    # And an actor that keeps track of its calls
    calls = []

    @dramatiq.actor
    def track_call():
        calls.append(1)

    # When I send that actor a message
    track_call.send()

    # And wait for 100ms
    time.sleep(0.1)

    # Then no calls should be made
    assert calls == []

    # When I resume the worker and join on it
    stub_worker.resume()
    stub_broker.join(track_call.queue_name)
    stub_worker.join()

    # Then one call should be made
    assert calls == [1]


def test_actors_can_prioritize_work(stub_broker):
    with worker(stub_broker, worker_timeout=100, worker_threads=1) as stub_worker:
        # Given that I a paused worker
        stub_worker.pause()

        # And actors with different priorities
        calls = []

        @dramatiq.actor(priority=0)
        def hi():
            calls.append("hi")

        @dramatiq.actor(priority=10)
        def lo():
            calls.append("lo")

        # When I send both actors a nubmer of messages
        for _ in range(10):
            lo.send()
            hi.send()

        # Then resume the worker and join on the queue
        stub_worker.resume()
        stub_broker.join(lo.queue_name)
        stub_worker.join()

        # Then the high priority actor should run first
        assert calls == ["hi"] * 10 + ["lo"] * 10


def test_actors_can_conditionally_retry(stub_broker, stub_worker):
    # Given that I have a retry predicate
    def should_retry(retry_count, exception):
        return retry_count < 3 and isinstance(exception, RuntimeError)

    # And an actor that raises different types of errors
    attempts = []

    @dramatiq.actor(retry_when=should_retry, max_retries=0, min_backoff=100, max_backoff=100)
    def raises_errors(raise_runtime_error):
        attempts.append(1)
        if raise_runtime_error:
            raise RuntimeError("Runtime error")
        raise ValueError("Value error")

    # When I send that actor a message that makes it raise a value error
    raises_errors.send(False)

    # And wait for it
    stub_broker.join(raises_errors.queue_name)
    stub_worker.join()

    # Then I expect the actor not to retry
    assert sum(attempts) == 1

    # When I send that actor a message that makes it raise a runtime error
    attempts[:] = []
    raises_errors.send(True)

    # And wait for it
    stub_broker.join(raises_errors.queue_name)
    stub_worker.join()

    # Then I expect the actor to retry 3 times
    assert sum(attempts) == 4


def test_can_call_str_on_actors():
    # Given that I have an actor
    @dramatiq.actor
    def test():
        pass

    # When I call str on it
    # Then I should get back its representation as a string
    assert str(test) == "Actor(test)"


def test_can_call_repr_on_actors():
    # Given that I have an actor
    @dramatiq.actor
    def test():
        pass

    # When I call repr on it
    # Then I should get back its representation
    assert repr(test) == "Actor(%(fn)r, queue_name='default', actor_name='test')" % vars(test)


def test_workers_log_rate_limit_exceeded_errors_differently(stub_broker, stub_worker):
    # Given that I've mocked the logging class
    with patch("logging.Logger.debug") as debug_mock:
        # And I have an actor that raises RateLimitExceeded
        @dramatiq.actor(max_retries=0)
        def raise_rate_limit_exceeded():
            raise RateLimitExceeded("exceeded")

        # When I send that actor a message
        raise_rate_limit_exceeded.send()

        # And wait for the message to get processed
        stub_broker.join(raise_rate_limit_exceeded.queue_name)
        stub_worker.join()

        # Then debug mock should be called with a special message
        debug_messages = [args[0] for _, args, _ in debug_mock.mock_calls]
        assert "Rate limit exceeded in message %s: %s." in debug_messages


def test_currrent_message_middleware_exposes_the_current_message(stub_broker, stub_worker):
    # Given that I have a CurrentMessage middleware
    stub_broker.add_middleware(CurrentMessage())

    # And an actor that accesses the current message
    sent_messages = []
    received_messages = []

    @dramatiq.actor
    def accessor(x):
        message_proxy = CurrentMessage.get_current_message()
        received_messages.append(message_proxy._message)

    # When I send it a couple messages
    sent_messages.append(accessor.send(1))
    sent_messages.append(accessor.send(2))

    # And wait for it to finish its work
    stub_broker.join(accessor.queue_name)

    # Then the sent messages and the received messages should be the same
    assert sorted(sent_messages) == sorted(received_messages)

    # When I try to access the current message from a non-worker thread
    # Then I should get back None
    assert CurrentMessage.get_current_message() is None


class CustomException1(Exception):
    pass


class CustomException2(Exception):
    pass


@pytest.mark.parametrize("exn,throws", [
    [CustomException1, CustomException1],
    [CustomException1, (CustomException1,)],
    [CustomException2, (CustomException1, CustomException2)],
])
def test_actor_with_throws_logs_info_and_does_not_retry(stub_broker, stub_worker, exn, throws):
    # Given that I have a database
    attempts = []

    # And an actor that raises expected exceptions
    @dramatiq.actor(throws=throws)
    def do_work():
        attempts.append(1)
        if sum(attempts) == 1:
            raise exn("Expected Failure")

    # And that I've mocked the logging classes
    with patch("logging.Logger.error") as error_mock, \
         patch("logging.Logger.warning") as warning_mock, \
         patch("logging.Logger.info") as info_mock:
        # When I send that actor a message
        do_work.send()

        # And join on the queue
        stub_broker.join(do_work.queue_name)
        stub_worker.join()

        # Then no errors and or warnings should be logged
        assert [args[0] for _, args, _ in itertools.chain(error_mock.mock_calls, warning_mock.mock_calls)] == []

        # And two info messages should be logged
        info_messages = [args[0] for _, args, _ in info_mock.mock_calls]
        assert "Failed to process message %s with expected exception %s." in info_messages
        assert "Aborting message %r." in info_messages

        # And the message should not be retried
        assert sum(attempts) == 1
