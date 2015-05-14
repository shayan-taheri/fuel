import errno
import os
import shutil
import tempfile
import time
from numpy.testing import assert_raises, assert_equal
from six.moves import range, cPickle
import zmq

from fuel import config
from fuel.iterator import DataIterator
from fuel.utils import do_not_pickle_attributes, find_in_data_path
from fuel.utils.zmq import uninterruptible
from fuel.utils.zmq import (DivideAndConquerVentilator, DivideAndConquerSink,
                            DivideAndConquerWorker,
                            LocalhostDivideAndConquerManager)


@do_not_pickle_attributes("non_picklable", "bulky_attr")
class DummyClass(object):
    def __init__(self):
        self.load()

    def load(self):
        self.bulky_attr = list(range(100))
        self.non_picklable = lambda x: x


class FaultyClass(object):
    pass


@do_not_pickle_attributes("iterator")
class UnpicklableClass(object):
    def __init__(self):
        self.load()

    def load(self):
        self.iterator = DataIterator(None)


@do_not_pickle_attributes("attribute")
class NonLoadingClass(object):
    def load(self):
        pass


class TestFindInDataPath(object):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.tempdir, 'dir1'))
        os.mkdir(os.path.join(self.tempdir, 'dir2'))
        self.original_data_path = config.data_path
        config.data_path = os.path.pathsep.join(
            [os.path.join(self.tempdir, 'dir1'),
             os.path.join(self.tempdir, 'dir2')])
        with open(os.path.join(self.tempdir, 'dir1', 'file_1.txt'), 'w'):
            pass
        with open(os.path.join(self.tempdir, 'dir2', 'file_1.txt'), 'w'):
            pass
        with open(os.path.join(self.tempdir, 'dir2', 'file_2.txt'), 'w'):
            pass

    def tearDown(self):
        config.data_path = self.original_data_path
        shutil.rmtree(self.tempdir)

    def test_returns_file_path(self):
        assert_equal(find_in_data_path('file_2.txt'),
                     os.path.join(self.tempdir, 'dir2', 'file_2.txt'))

    def test_returns_first_file_found(self):
        assert_equal(find_in_data_path('file_1.txt'),
                     os.path.join(self.tempdir, 'dir1', 'file_1.txt'))

    def test_raises_error_on_file_not_found(self):
        assert_raises(IOError, find_in_data_path, 'dummy.txt')


class TestDoNotPickleAttributes(object):
    def test_load(self):
        instance = cPickle.loads(cPickle.dumps(DummyClass()))
        assert_equal(instance.bulky_attr, list(range(100)))
        assert instance.non_picklable is not None

    def test_value_error_no_load_method(self):
        assert_raises(ValueError, do_not_pickle_attributes("x"), FaultyClass)

    def test_value_error_iterator(self):
        assert_raises(ValueError, cPickle.dumps, UnpicklableClass())

    def test_value_error_attribute_non_loaded(self):
        assert_raises(ValueError, getattr, NonLoadingClass(), 'attribute')


def test_uninterruptible():
    foo = []

    def interrupter(a, b):
        if len(foo) < 3:
            foo.append(0)
            raise zmq.ZMQError(errno=errno.EINTR)
        return (len(foo) + a) / b

    def noninterrupter():
        return -1

    assert uninterruptible(interrupter, 5,  2) == 4


class DummyVentilator(DivideAndConquerVentilator):
    def send(self, socket, number):
        socket.send_pyobj(number)

    def produce(self):
        # temporary workaround for race with workers on first message
        time.sleep(0.25)
        for i in range(1, 51):
            yield i


class DummyWorker(DivideAndConquerWorker):
    def recv(self, socket):
        return socket.recv_pyobj()

    def send(self, socket, number):
        socket.send_pyobj(number)

    def process(self, number):
        yield number ** 2


class DummySink(DivideAndConquerSink):
    def __init__(self, result_port, **kwargs):
        super(DummySink, self).__init__(**kwargs)
        self.result_port = result_port
        self.messages_received = 0
        self.sum = 0

    def recv(self, socket):
        received = socket.recv_pyobj()
        self.messages_received += 1
        return received

    def done(self):
        return self.messages_received >= 50

    def initialize_sockets(self, context, *args, **kwargs):
        super(DummySink, self).initialize_sockets(context, *args, **kwargs)
        self.result_socket = self.context.socket(zmq.PUSH)
        self.result_socket.bind('tcp://*:{}'.format(self.result_port))

    def process(self, number_squared):
        self.sum += number_squared

    def teardown(self):
        self._receiver.close()
        self.result_socket.send_pyobj(self.sum)


def test_localhost_divide_and_conquer_manager():
    result_port = 59581
    ventilator_port = 59583
    sink_port = 59584
    manager = LocalhostDivideAndConquerManager(DummyVentilator(),
                                               DummySink(result_port),
                                               [DummyWorker(), DummyWorker()],
                                               ventilator_port, sink_port)
    context = zmq.Context()
    socket = context.socket(zmq.PULL)
    socket.connect('tcp://localhost:{}'.format(result_port))
    manager.launch()
    result = socket.recv_pyobj()
    manager.wait()
    assert result == sum(i ** 2 for i in range(1, 51))
