import queue
import threading
import time


class BoundedAsyncWriter:
    """Run a write callback off-thread with bounded, non-blocking input."""

    _STOP = object()

    def __init__(self, write_item, max_queue_size: int) -> None:
        if max_queue_size <= 0:
            raise ValueError("max_queue_size must be positive")
        self._write_item = write_item
        self._queue = queue.Queue(maxsize=max_queue_size)
        self._failure = None
        self._failure_lock = threading.Lock()
        self._closed = False
        self.dropped_count = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, item) -> bool:
        if self._closed:
            raise RuntimeError("async writer is closed")
        self.raise_if_failed()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped_count += 1
            return False
        return True

    def submit_factory(self, make_item) -> bool:
        """Build an item only when the bounded queue currently has room."""
        if self._closed:
            raise RuntimeError("async writer is closed")
        self.raise_if_failed()
        if self._queue.full():
            self.dropped_count += 1
            return False
        return self.submit(make_item())

    def raise_if_failed(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            raise RuntimeError(f"async writer failed: {failure}") from failure

    def close(self, timeout: float = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out while closing async writer")
            try:
                self._queue.put(self._STOP, timeout=min(0.05, remaining))
                break
            except queue.Full:
                continue
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            raise TimeoutError("timed out while closing async writer")
        self.raise_if_failed()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                with self._failure_lock:
                    failed = self._failure is not None
                if not failed:
                    self._write_item(item)
            except Exception as exc:  # noqa: BLE001
                with self._failure_lock:
                    if self._failure is None:
                        self._failure = exc
            finally:
                self._queue.task_done()
