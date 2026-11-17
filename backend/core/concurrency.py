"""进程内容量限制、会话串行化与后台画像更新。"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from contextvars import copy_context
from functools import partial

logger = logging.getLogger(__name__)


class ConversationBusyError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


class CapacityExceeded(RuntimeError):
    pass


class Capacity:
    """Bound active work and its waiters on one event loop."""

    def __init__(self, active, waiting, wait_seconds, *, per_user=None):
        self._slots = asyncio.Semaphore(active)
        self._max_waiting = waiting
        self._wait_seconds = wait_seconds
        self._per_user = per_user
        self._users = {}
        self.active = self.waiting = self.rejected = 0

    @classmethod
    def from_env(cls, prefix, active, waiting, *, per_user=None):
        return cls(max(1, int(os.getenv(f"{prefix}_ACTIVE", str(active)))),
                   max(0, int(os.getenv(f"{prefix}_WAITING", str(waiting)))),
                   max(.001, float(os.getenv("DIAN_QUEUE_TIMEOUT_S", "2"))),
                   per_user=per_user)

    @asynccontextmanager
    async def hold(self, user=None):
        if user is not None and self._per_user is not None and self._users.get(user, 0) >= self._per_user:
            self.rejected += 1
            raise CapacityExceeded("当前账户的请求过多，请稍后重试")
        queued = self._slots.locked()
        if queued and self.waiting >= self._max_waiting:
            self.rejected += 1
            raise CapacityExceeded("服务繁忙，请稍后重试")
        if user is not None:
            self._users[user] = self._users.get(user, 0) + 1
        try:
            if queued:
                self.waiting += 1
                try:
                    async with asyncio.timeout(self._wait_seconds):
                        await self._slots.acquire()
                except TimeoutError as exc:
                    self.rejected += 1
                    raise CapacityExceeded("等待执行额度超时，请稍后重试") from exc
                finally:
                    self.waiting -= 1
            else:
                await self._slots.acquire()
            self.active += 1
            try:
                yield
            finally:
                self.active -= 1
                self._slots.release()
        finally:
            if user is not None:
                self._users[user] -= 1
                if not self._users[user]:
                    del self._users[user]


async def finish_blocking_call(fn, *args):
    """Keep the caller's locks and slots until the submitted thread exits."""
    future = asyncio.get_running_loop().run_in_executor(None, partial(copy_context().run, fn, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not future.cancelled():
            future.exception()
        raise


class KeyedLocks:
    """同进程按键 FIFO 排队，无等待者时回收锁。"""
    def __init__(self):
        self._locks = {}

    @asynccontextmanager
    async def hold(self, key):
        lock, users = self._locks.get(key, (asyncio.Lock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)


@asynccontextmanager
async def redis_lease(client, key, wait_s=30, lease_s=120):
    """跨进程互斥；续租失败取消持有者，避免失去锁后继续写入。"""
    lock = client.lock(key, timeout=lease_s, blocking_timeout=wait_s, thread_local=False)
    if not await lock.acquire():
        raise ConversationBusyError("当前会话正在处理请求，请稍后重试")
    owner = asyncio.current_task()
    lost = False

    async def renew():
        nonlocal lost
        try:
            while True:
                await asyncio.sleep(lease_s / 3)
                if not await lock.extend(lease_s, replace_ttl=True):
                    raise LeaseLostError("会话锁续租失败")
        except asyncio.CancelledError:
            raise
        except Exception:
            lost = True
            owner.cancel()

    renewal = asyncio.create_task(renew())
    try:
        yield
    except asyncio.CancelledError:
        if lost:
            raise LeaseLostError("会话锁已失效，本次请求已中止") from None
        raise
    finally:
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        try:
            await lock.release()
        except Exception as ex:
            logger.warning("释放会话锁失败: %s", type(ex).__name__)


class ProfileUpdater:
    """固定 worker 数量；同一用户的待处理更新合并为最新会话。"""
    def __init__(self, memory, workers=4, max_pending=256):
        self._memory = memory
        self._worker_count = max(1, workers)
        self._queue = asyncio.Queue(maxsize=max_pending)
        self._pending = {}
        self._scheduled = set()
        self._tasks = []
        self._closed = False

    def start(self):
        self._tasks = [asyncio.create_task(self._worker()) for _ in range(self._worker_count)]

    def submit(self, user_id, conv_id):
        if self._closed:
            return False
        if user_id not in self._scheduled:
            try:
                self._queue.put_nowait(user_id)
            except asyncio.QueueFull:
                logger.warning("画像更新队列已满，本次跳过")
                return False
            self._scheduled.add(user_id)
        self._pending[user_id] = conv_id
        return True

    async def _worker(self):
        while not self._closed:
            user_id = await self._queue.get()
            try:
                while user_id in self._pending and not self._closed:
                    conv_id = self._pending.pop(user_id)
                    try:
                        await (self._memory.maintain(user_id, conv_id) if hasattr(self._memory, "maintain") else self._memory.update_profile(user_id, conv_id))
                    except Exception as ex:
                        logger.warning("后台画像更新失败: %s", type(ex).__name__)
            finally:
                self._scheduled.discard(user_id)
                self._queue.task_done()

    async def close(self):
        self._closed = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        while not self._queue.empty():
            self._queue.get_nowait()
            self._queue.task_done()
        self._pending.clear()
        self._scheduled.clear()
        self._tasks.clear()
