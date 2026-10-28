from __future__ import annotations

import os
import selectors
import subprocess
import time

PROC_TIMEOUT_SECONDS = 30
STDOUT_LIMIT_BYTES = 1_048_576
STDERR_LIMIT_BYTES = 1_048_576


class HyperliquidUnavailable(RuntimeError):
    def __init__(self, message: str, public: str | None = None) -> None:
        super().__init__(message)
        self.public = public


def _reap_process(proc: subprocess.Popen) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def communicate_bounded(proc: subprocess.Popen, timeout: float) -> tuple[str, str]:
    sel = selectors.DefaultSelector()
    pipes = []
    try:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                os.set_blocking(pipe.fileno(), False)
                sel.register(pipe, selectors.EVENT_READ)
                pipes.append(pipe)
        buffers: dict = {pipe: bytearray() for pipe in pipes}
        deadline = time.monotonic() + timeout
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _reap_process(proc)
                raise HyperliquidUnavailable("hl-trader 超时", public="hl-trader 超时")
            for key, _ in sel.select(remaining):
                pipe = key.fileobj
                try:
                    chunk = os.read(pipe.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    _reap_process(proc)
                    raise HyperliquidUnavailable(
                        "读取 hl-trader 输出失败", public="hl-trader 输出读取失败"
                    ) from exc
                if not chunk:
                    sel.unregister(pipe)
                    continue
                buffers[pipe] += chunk
                limit = STDOUT_LIMIT_BYTES if pipe is proc.stdout else STDERR_LIMIT_BYTES
                if len(buffers[pipe]) > limit:
                    which = "stdout" if pipe is proc.stdout else "stderr"
                    _reap_process(proc)
                    raise HyperliquidUnavailable(
                        f"hl-trader {which} 超过 1 MiB 上限", public="hl-trader 输出超限"
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _reap_process(proc)
            raise HyperliquidUnavailable("hl-trader 超时", public="hl-trader 超时")
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _reap_process(proc)
            raise HyperliquidUnavailable("hl-trader 超时", public="hl-trader 超时") from exc
        return (
            bytes(buffers.get(proc.stdout, b"")).decode("utf-8", "replace"),
            bytes(buffers.get(proc.stderr, b"")).decode("utf-8", "replace"),
        )
    finally:
        sel.close()
        for pipe in pipes:
            try:
                pipe.close()
            except OSError:
                pass
