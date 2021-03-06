from __future__ import absolute_import

from abc import ABCMeta, abstractproperty
from logging import getLogger
import uuid

import eventlet
from eventlet.event import Event
from eventlet.greenpool import GreenPool
import greenlet

from nameko.dependencies import (
    prepare_dependencies, DependencySet, is_entrypoint_provider,
    is_injection_provider)
from nameko.exceptions import RemoteError
from nameko.logging import log_time

WORKER_CALL_ID_STACK_KEY = 'call_id_stack'

_log = getLogger(__name__)


MAX_WORKERS_KEY = 'max_workers'
PARENT_CALLS_KEY = 'parent_calls_tracked'

DEFAULT_MAX_WORKERS = 10
DEFAULT_PARENT_CALLS_TRACKED = 10

NAMEKO_CONTEXT_KEYS = (
    'language',
    'user_id',
    'auth_token',
    WORKER_CALL_ID_STACK_KEY,
)


def get_service_name(service_cls):
    return getattr(service_cls, "name", service_cls.__name__.lower())


def log_worker_exception(worker_ctx, exc):
    if isinstance(exc, RemoteError):
        exc = "RemoteError"
    _log.debug('error handling worker %s: %s', worker_ctx, exc, exc_info=True,
               extra=worker_ctx.extra_for_logging)


def new_call_id():
    return str(uuid.uuid4())


class WorkerContextBase(object):
    """ Abstract base class for a WorkerContext
    """
    __metaclass__ = ABCMeta

    def __init__(self, container, service, method_name, args=None, kwargs=None,
                 data=None):
        self.container = container
        self.config = container.config  # TODO: remove?

        self.parent_calls_tracked = self.config.get(
            PARENT_CALLS_KEY, DEFAULT_PARENT_CALLS_TRACKED)

        self.service = service
        self.method_name = method_name
        self.service_name = self.container.service_name

        self.args = args if args is not None else ()
        self.kwargs = kwargs if kwargs is not None else {}

        self.data = data if data is not None else {}

        self.parent_call_stack, self.unique_id = self._init_call_id()
        self.call_id = '{}.{}.{}'.format(
            self.service_name, self.method_name, self.unique_id
        )
        n = -self.parent_calls_tracked
        self.call_id_stack = self.parent_call_stack[n:]
        self.call_id_stack.append(self.call_id)
        try:
            self.immediate_parent_call_id = self.parent_call_stack[-1]
        except IndexError:
            self.immediate_parent_call_id = None

    @abstractproperty
    def context_keys(self):
        """ Return a tuple of keys describing data kept on this WorkerContext.
        """

    @property
    def context_data(self):
        """
        Contextual data to pass with each call originating from the active
        worker.

        Comprises items from ``self.data`` where the key is included in
        ``context_keys``, as well as the call stack.
        """
        key_data = {k: v for k, v in self.data.iteritems()
                    if k in self.context_keys}
        key_data[WORKER_CALL_ID_STACK_KEY] = self.call_id_stack
        return key_data

    @property
    def extra_for_logging(self):
        """
        Get a dictionary of extra data to apply to log statements.
        """
        return {}

    @classmethod
    def get_context_data(cls, incoming):
        data = {k: v for k, v in incoming.iteritems()
                if k in cls.context_keys}
        return data

    def __str__(self):
        cls_name = type(self).__name__
        return '<{} {}.{} at 0x{:x}>'.format(
            cls_name, self.service_name, self.method_name, id(self))

    def _init_call_id(self):
        parent_call_stack = self.data.pop(WORKER_CALL_ID_STACK_KEY, [])
        unique_id = new_call_id()
        return parent_call_stack, unique_id


class WorkerContext(WorkerContextBase):
    """ Default WorkerContext implementation
    """
    context_keys = NAMEKO_CONTEXT_KEYS


class ServiceContainer(object):

    def __init__(self, service_cls, worker_ctx_cls, config):

        self.service_cls = service_cls
        self.worker_ctx_cls = worker_ctx_cls

        self.service_name = get_service_name(service_cls)

        self.config = config
        self.max_workers = config.get(MAX_WORKERS_KEY) or DEFAULT_MAX_WORKERS

        self.dependencies = DependencySet()
        for dep in prepare_dependencies(self):
            self.dependencies.add(dep)

        self.started = False
        self._worker_pool = GreenPool(size=self.max_workers)

        self._active_threads = set()
        self._protected_threads = set()
        self._being_killed = False
        self._died = Event()

    @property
    def entrypoints(self):
        return filter(is_entrypoint_provider, self.dependencies)

    @property
    def injections(self):
        return filter(is_injection_provider, self.dependencies)

    def start(self):
        """ Start a container by starting all the dependency providers.
        """
        _log.debug('starting %s', self)
        self.started = True

        with log_time(_log.debug, 'started %s in %0.3f sec', self):
            self.dependencies.all.prepare()
            self.dependencies.all.start()

    def stop(self):
        """ Stop the container gracefully.

        First all entrypoints are asked to ``stop()``.
        This ensures that no new worker threads are started.

        It is the providers' responsibility to gracefully shut down when
        ``stop()`` is called on them and only return when they have stopped.

        After all entrypoints have stopped the container waits for any
        active workers to complete.

        After all active workers have stopped the container stops all
        injections.

        At this point there should be no more managed threads. In case there
        are any managed threads, they are killed by the container.
        """
        if self._died.ready():
            _log.debug('already stopped %s', self)
            return

        _log.debug('stopping %s', self)

        with log_time(_log.debug, 'stopped %s in %0.3f sec', self):
            dependencies = self.dependencies

            # entrypoint deps have to be stopped before injection deps
            # to ensure that running workers can successfully complete
            dependencies.entrypoints.all.stop()

            # there might still be some running workers, which we have to
            # wait for to complete before we can stop injection dependencies
            self._worker_pool.waitall()

            # it should be safe now to stop any injection as there is no
            # active worker which could be using it
            dependencies.injections.all.stop()

            # finally, stop nested dependencies
            dependencies.nested.all.stop()

            # just in case there was a provider not taking care of its workers,
            # or a dependency not taking care of its protected threads
            self._kill_active_threads()
            self._kill_protected_threads()

            self.started = False
            self._died.send(None)

    def kill(self, exc):
        """ Kill the container in a semi-graceful way.

        All non-protected managed threads are killed first. This includes
        all active workers generated by :meth:`ServiceContainer.spawn_worker`.
        Next, dependencies are killed. Finally, any remaining protected threads
        are killed.

        The container dies with the given ``exc``.
        """
        if self._being_killed:
            # this happens if a managed thread exits with an exception
            # while the container is being killed or another caller
            # behaves in a similar manner
            _log.debug('already killing %s ... waiting for death', self)
            self._died.wait()

        self._being_killed = True

        if self._died.ready():
            _log.debug('already stopped %s', self)
            return

        _log.info('killing %s due to "%s"', self, exc)

        self.dependencies.entrypoints.all.kill(exc)
        self._kill_active_threads()
        self.dependencies.all.kill(exc)
        self._kill_protected_threads()

        self.started = False
        self._died.send_exception(exc)

    def wait(self):
        """ Block until the container has been stopped.

        If the container was stopped using ``kill(exc)``,
        ``wait()`` raises ``exc``.
        Any unhandled exception raised in a managed thread or in the
        life-cycle management code also causes the container to be
        ``kill()``ed, which causes an exception to be raised from ``wait()``.
        """
        return self._died.wait()

    def spawn_worker(self, provider, args, kwargs,
                     context_data=None, handle_result=None):
        """ Spawn a worker thread for running the service method decorated
        with an entrypoint ``provider``.

        ``args`` and ``kwargs`` are used as arguments for the service
        method.

        ``context_data`` is used to initialize a ``WorkerContext``.

        ``handle_result`` is an optional callback which may be passed
        in by the calling entrypoint provider. It is called with the
        result returned or error raised by the service method.
        """
        service = self.service_cls()
        worker_ctx = self.worker_ctx_cls(
            self, service, provider.name, args, kwargs, data=context_data)

        _log.debug('spawning %s', worker_ctx,
                   extra=worker_ctx.extra_for_logging)
        gt = self._worker_pool.spawn(self._run_worker, worker_ctx,
                                     handle_result)
        self._active_threads.add(gt)
        gt.link(self._handle_thread_exited)
        return worker_ctx

    def spawn_managed_thread(self, run_method, protected=False):
        """ Spawn a managed thread to run ``run_method``.

        Threads can be marked as ``protected``, which means the container will
        not forcibly kill them until after all dependencies have been killed.
        Dependencies that require a managed thread to complete their kill
        procedure should ensure to mark them as ``protected``.

        Any uncaught errors inside ``run_method`` cause the container to be
        killed.

        It is the caller's responsibility to terminate their spawned threads.
        Threads are killed automatically if they are still running after
        all dependencies are stopped during :meth:`ServiceContainer.stop`.

        Entrypoints may only create separate threads using this method,
        to ensure they are life-cycle managed.
        """
        gt = eventlet.spawn(run_method)
        if not protected:
            self._active_threads.add(gt)
        else:
            self._protected_threads.add(gt)
        gt.link(self._handle_thread_exited)
        return gt

    def _run_worker(self, worker_ctx, handle_result):
        _log.debug('setting up %s', worker_ctx,
                   extra=worker_ctx.extra_for_logging)

        if not worker_ctx.parent_call_stack:
            _log.debug('starting call chain',
                       extra=worker_ctx.extra_for_logging)
        _log.debug('call stack for %s: %s',
                   worker_ctx, '->'.join(worker_ctx.call_id_stack),
                   extra=worker_ctx.extra_for_logging)

        with log_time(_log.debug, 'ran worker %s in %0.3fsec', worker_ctx):

            self.dependencies.injections.all.inject(worker_ctx)
            self.dependencies.all.worker_setup(worker_ctx)

            result = exc = None
            try:
                _log.debug('calling handler for %s', worker_ctx,
                           extra=worker_ctx.extra_for_logging)

                method = getattr(worker_ctx.service, worker_ctx.method_name)

                with log_time(_log.debug, 'ran handler for %s in %0.3fsec',
                              worker_ctx):
                    result = method(*worker_ctx.args, **worker_ctx.kwargs)
            except Exception as e:
                log_worker_exception(worker_ctx, e)
                exc = e

            with log_time(_log.debug, 'tore down worker %s in %0.3fsec',
                          worker_ctx):

                _log.debug('signalling result for %s', worker_ctx,
                           extra=worker_ctx.extra_for_logging)
                self.dependencies.injections.all.worker_result(
                    worker_ctx, result, exc)

                _log.debug('tearing down %s', worker_ctx,
                           extra=worker_ctx.extra_for_logging)
                self.dependencies.all.worker_teardown(worker_ctx)
                self.dependencies.injections.all.release(worker_ctx)

            if handle_result is not None:
                _log.debug('handling result for %s', worker_ctx,
                           extra=worker_ctx.extra_for_logging)

                with log_time(_log.debug, 'handled result for %s in %0.3fsec',
                              worker_ctx):
                    handle_result(worker_ctx, result, exc)

    def _kill_active_threads(self):
        """ Kill all managed threads that were not marked as "protected" when
        they were spawned.

        This set will include all worker threads generated by
        :meth:`ServiceContainer.spawn_worker`.

        See :meth:`ServiceContainer.spawn_managed_thread`
        """
        num_active_threads = len(self._active_threads)

        if num_active_threads:
            _log.warning('killing %s active thread(s)', num_active_threads)
            for gt in list(self._active_threads):
                gt.kill()

    def _kill_protected_threads(self):
        """ Kill any managed threads marked as protected when they were
        spawned.

        See :meth:`ServiceContainer.spawn_managed_thread`
        """
        num_protected_threads = len(self._protected_threads)

        if num_protected_threads:
            _log.warning('killing %s protected thread(s)',
                         num_protected_threads)
            for gt in list(self._protected_threads):
                gt.kill()

    def _handle_thread_exited(self, gt):
        self._active_threads.discard(gt)
        self._protected_threads.discard(gt)

        try:
            gt.wait()

        except greenlet.GreenletExit:
            # we don't care much about threads killed by the container
            # this can happen in stop() and kill() if providers
            # don't properly take care of their threads
            _log.warning('%s thread killed by container', self)

        except Exception as exc:
            _log.error('%s thread exited with error', self,
                       exc_info=True)
            # any error raised inside an active thread is unexpected behavior
            # and probably a bug in the providers or container
            # to be safe we kill the container
            self.kill(exc)

    def __str__(self):
        return '<ServiceContainer [{}] at 0x{:x}>'.format(
            self.service_name, id(self))
