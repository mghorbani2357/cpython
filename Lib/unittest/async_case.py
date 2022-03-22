import asyncio
import contextvars
import inspect
import warnings

from .case import TestCase


class IsolatedAsyncioTestCase(TestCase):
    # Names intentionally have a long prefix
    # to reduce a chance of clashing with user-defined attributes
    # from inherited test case
    #
    # The class doesn't call loop.run_until_complete(self.setUp()) and family
    # but uses a different approach:
    # 1. create a long-running task that reads self.setUp()
    #    awaitable from queue along with a future
    # 2. await the awaitable object passing in and set the result
    #    into the future object
    # 3. Outer code puts the awaitable and the future object into a queue
    #    with waiting for the future
    # The trick is necessary because every run_until_complete() call
    # creates a new task with embedded ContextVar context.
    # To share contextvars between setUp(), test and tearDown() we need to execute
    # them inside the same task.

    # Note: the test case modifies event loop policy if the policy was not instantiated
    # yet.
    # asyncio.get_event_loop_policy() creates a default policy on demand but never
    # returns None
    # I believe this is not an issue in user level tests but python itself for testing
    # should reset a policy in every test module
    # by calling asyncio.set_event_loop_policy(None) in tearDownModule()

    def __init__(self, methodName='runTest'):
        super().__init__(methodName)
        self._asyncioTestLoop = None
        self._asyncioTestContext = contextvars.copy_context()

    async def asyncSetUp(self):
        pass

    async def asyncTearDown(self):
        pass

    def addAsyncCleanup(self, func, /, *args, **kwargs):
        # A trivial trampoline to addCleanup()
        # the function exists because it has a different semantics
        # and signature:
        # addCleanup() accepts regular functions
        # but addAsyncCleanup() accepts coroutines
        #
        # We intentionally don't add inspect.iscoroutinefunction() check
        # for func argument because there is no way
        # to check for async function reliably:
        # 1. It can be "async def func()" itself
        # 2. Class can implement "async def __call__()" method
        # 3. Regular "def func()" that returns awaitable object
        self.addCleanup(*(func, *args), **kwargs)

    def _callSetUp(self):
        self._asyncioTestContext.run(self.setUp)
        self._callAsync(self.asyncSetUp)

    def _callTestMethod(self, method):
        if self._callMaybeAsync(method) is not None:
            warnings.warn(f'It is deprecated to return a value!=None from a '
                          f'test case ({method})', DeprecationWarning, stacklevel=4)

    def _callTearDown(self):
        self._callAsync(self.asyncTearDown)
        self._asyncioTestContext.run(self.tearDown)

    def _callCleanup(self, function, *args, **kwargs):
        self._callMaybeAsync(function, *args, **kwargs)

    def _callAsync(self, func, /, *args, **kwargs):
        assert self._asyncioTestLoop is not None, 'asyncio test loop is not initialized'
        assert inspect.iscoroutinefunction(func), f'{func!r} is not an async function'
        task = self._asyncioTestLoop.create_task(
            func(*args, **kwargs),
            context=self._asyncioTestContext,
        )
        return self._asyncioTestLoop.run_until_complete(task)

    def _callMaybeAsync(self, func, /, *args, **kwargs):
        assert self._asyncioTestLoop is not None, 'asyncio test loop is not initialized'
        if inspect.iscoroutinefunction(func):
            task = self._asyncioTestLoop.create_task(
                func(*args, **kwargs),
                context=self._asyncioTestContext,
            )
            return self._asyncioTestLoop.run_until_complete(task)
        else:
            return self._asyncioTestContext.run(func, *args, **kwargs)

    def _setupAsyncioLoop(self):
        assert self._asyncioTestLoop is None, 'asyncio test loop already initialized'
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_debug(True)
        self._asyncioTestLoop = loop

    def _tearDownAsyncioLoop(self):
        assert self._asyncioTestLoop is not None, 'asyncio test loop is not initialized'
        loop = self._asyncioTestLoop
        self._asyncioTestLoop = None

        try:
            # cancel all tasks
            to_cancel = asyncio.all_tasks(loop)
            if not to_cancel:
                return

            for task in to_cancel:
                task.cancel()

            loop.run_until_complete(
                asyncio.gather(*to_cancel, return_exceptions=True))

            for task in to_cancel:
                if task.cancelled():
                    continue
                if task.exception() is not None:
                    loop.call_exception_handler({
                        'message': 'unhandled exception during test shutdown',
                        'exception': task.exception(),
                        'task': task,
                    })
            # shutdown asyncgens
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    def run(self, result=None):
        self._setupAsyncioLoop()
        try:
            return super().run(result)
        finally:
            self._tearDownAsyncioLoop()

    def debug(self):
        self._setupAsyncioLoop()
        super().debug()
        self._tearDownAsyncioLoop()

    def __del__(self):
        if self._asyncioTestLoop is not None:
            self._tearDownAsyncioLoop()
