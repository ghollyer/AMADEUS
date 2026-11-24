"""``amadeus-web``: the browser console, the action queue and an HTTP MCP endpoint.

One process owns the instrument connection:

* ``/``        the single-page UI (``client/web/static``)
* ``/api/*``   REST: catalog, capabilities, queue, sequences, images, operator chat
* ``/events``  Server-Sent Events: every queue/chat/status change, pushed
* ``/mcp``     the AMADEUS MCP server over streamable HTTP. Attach Claude Code
               here (the sidecar agent); every tool call it makes becomes an
               ``agent`` block in the queue.

Listens on 127.0.0.1 only: this process can move the stage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from client.blocks.capabilities import Capabilities, check
from client.blocks.catalog import build_catalog
from client.blocks.model import load_sequence
from client.server import build_server, driver_kinds_from, resolve_settings
from client.tools import operator_channel as _operator_tools
from client.web.agent_channel import OperatorChannel, WebConfirmer
from client.web.mcp_queued import QueuedFastMCP
from client.web.queue import BlockQueue, EventBus

STATIC_DIR = Path(__file__).parent / "static"
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,80}$")
_STATUS_PERIOD_S = 1.0
_STATE_POLL_S = 3.0
_READOUT_KEYS = ("magnification", "mag_index", "high_tension_kV", "spot_size",
                 "tem_stem_mode", "operation_mode", "function_mode", "beam_blanked",
                 "column_valves_open", "focus_dac", "focus_nm", "stage")


def readouts(state: dict) -> dict:
    """The header's readouts from a ``get_microscope_state`` reply."""
    return {k: state[k] for k in _READOUT_KEYS
            if isinstance(state, dict) and state.get(k) is not None}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class WebConsole:
    """Everything the routes need, built once per process."""

    def __init__(self, address: str, data_dir: Path, *, host: str = "127.0.0.1",
                 port: int = 8765, stream_address: str | None = None,
                 timeout_ms: int = 30_000, instrument: dict | None = None,
                 allow_scripting: bool = True, enable_monitor: bool = True,
                 connect: bool = True, driver_kinds: set | None = None):
        self.mcp = QueuedFastMCP("AMADEUS", host=host, port=port)
        # Held commands are confirmed in the browser first (client/confirm.py).
        self.confirmer = WebConfirmer()
        self.mcp, self.zmq, self.state, self.subscriber = build_server(
            address, Path(data_dir), enable_monitor=enable_monitor,
            stream_address=stream_address, timeout_ms=timeout_ms,
            instrument=instrument, allow_scripting=allow_scripting, mcp=self.mcp,
            driver_kinds=driver_kinds, confirmers=[self.confirmer])
        self.instrument = instrument or {}
        self.allow_scripting = allow_scripting
        self.connect = connect
        self.bus = EventBus()
        self.channel = OperatorChannel(self.bus)
        self.confirmer.channel = self.channel
        _operator_tools.register(self.mcp, self.channel)
        self.caps = Capabilities.from_manifest(None, self._tool_names(), allow_scripting,
                                               instrument=self.instrument)
        self.caps_known = False
        self.caps_error: str | None = "not fetched yet"
        self.queue: BlockQueue | None = None
        self.data_dir = self.state.data_dir
        self.seq_dir = self.data_dir / "sequences"
        self.seq_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.data_dir / "web_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.relay_ok: bool | None = None
        self.params: dict = {}             # header readouts (poll + monitor)
        # Readouts poll on its own socket and without the monitor, so it never
        # waits behind a block or fills the monitor's command history.
        from client.transport.zmq_client import ZMQClient
        self._poll = ZMQClient(address, timeout_ms=5_000)
        self._tasks: list[asyncio.Task] = []

    # -- helpers ---------------------------------------------------------------
    def _tool_names(self) -> list[str]:
        return [t.name for t in self.mcp._tool_manager.list_tools()]

    def schemas(self) -> dict[str, dict]:
        return {t.name: t.parameters for t in self.mcp._tool_manager.list_tools()}

    async def refresh_capabilities(self) -> None:
        manifest = None
        try:
            manifest = await self.zmq.send_command_out_of_band("get_capabilities",
                                                              timeout_ms=20_000)
            self.caps_error = None
        except Exception as exc:
            self.caps_error = str(exc)
        instrument = None
        if manifest is not None and not manifest.get("instrument"):
            instrument = self.instrument          # relay without a config: use ours
        if manifest is None:
            instrument = self.instrument
        self.caps = Capabilities.from_manifest(manifest, self._tool_names(),
                                               self.allow_scripting, instrument=instrument)
        self.caps_known = manifest is not None
        if not self.caps_known:
            self.caps.notes.insert(0, "capabilities unknown (relay unreachable or too old "
                                      f"for get_capabilities: {self.caps_error}); "
                                      "blocks are not greyed out")
        self.bus.publish("capabilities", self.caps_payload())

    def caps_payload(self) -> dict:
        return {**self.caps.to_json(), "known": self.caps_known, "error": self.caps_error}

    def catalog(self) -> list[dict]:
        entries = build_catalog(self._mcp_tools())
        for e in entries:
            missing = [c for c in [f"tool:{e['name']}", *e["requires"]]
                       if not self.caps.has(c)]
            if not self.caps_known:
                missing = [c for c in missing if c.startswith(("tool:", "feature:scripting"))]
            e["missing"] = missing
            e["available"] = not missing
        return entries

    def _mcp_tools(self):
        from mcp.types import Tool
        return [Tool(name=t.name, description=t.description, inputSchema=t.parameters)
                for t in self.mcp._tool_manager.list_tools()]

    async def _ask(self, question: str, options: list[str]) -> str:
        return await self.channel.ask(question, options, who="sequence")

    # -- lifecycle -------------------------------------------------------------
    async def start(self) -> None:
        self.queue = BlockQueue(self.mcp.run_raw, self.bus, lambda: self.caps,
                                ask=self._ask, schemas=self.schemas)
        self.mcp.queue = self.queue
        self.mcp.channel = self.channel
        self.queue.start()
        if self.connect:
            await self.zmq.connect()
            await self._poll.connect()
            await self.subscriber.start()
            self._tasks.append(asyncio.create_task(self._status_loop()))
            self._tasks.append(asyncio.create_task(self.refresh_capabilities()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.queue:
            await self.queue.stop()
        if self.connect:
            with contextlib.suppress(Exception):
                await self.subscriber.stop()
            with contextlib.suppress(Exception):
                await self.zmq.disconnect()
            with contextlib.suppress(Exception):
                await self._poll.disconnect()

    async def _status_loop(self) -> None:
        """Push monitor readouts, relay reachability and live-frame updates."""
        snap_path = self.data_dir / "monitor" / "snapshot.json"
        live_path = self.data_dir / "monitor" / "live.json"
        last_snap = last_live = 0.0
        last_ping = last_state = 0.0
        while True:
            try:
                now = time.time()
                if now - last_ping > 10:
                    last_ping = now
                    try:
                        # "ping" also probes DM (2 s timeout when DM is down)
                        await self.zmq.send_command_out_of_band("ping", timeout_ms=8_000)
                        ok = True
                    except Exception:
                        ok = False
                    if ok != self.relay_ok:
                        self.relay_ok = ok
                        self.bus.publish("relay", {"ok": ok})
                        if ok and not self.caps_known:
                            asyncio.create_task(self.refresh_capabilities())
                changed = False
                if (self.relay_ok and now - last_state > _STATE_POLL_S
                        and self.queue is not None and self.queue.current is None):
                    last_state = now
                    with contextlib.suppress(Exception):
                        state = await self._poll.send_command("get_microscope_state")
                        self.params.update(readouts(state))
                        changed = True
                if snap_path.exists() and snap_path.stat().st_mtime != last_snap:
                    last_snap = snap_path.stat().st_mtime
                    with contextlib.suppress(ValueError, OSError):
                        snap = json.loads(snap_path.read_text(encoding="utf-8"))
                        self.params.update({k: v for k, v in (snap.get("params") or {}).items()
                                            if v is not None})
                        changed = True
                if changed:
                    self.bus.publish("status", {"params": self.params})
                if live_path.exists() and live_path.stat().st_mtime != last_live:
                    last_live = live_path.stat().st_mtime
                    with contextlib.suppress(ValueError, OSError):
                        self.bus.publish("live", {**json.loads(live_path.read_text(
                            encoding="utf-8")), "mtime": last_live})
            except Exception as exc:          # never let the status loop die
                _log(f"[amadeus-web] status loop: {exc}")
            await asyncio.sleep(_STATUS_PERIOD_S)

    # -- images ----------------------------------------------------------------
    def list_images(self, limit: int = 200) -> list[dict]:
        out = []
        for iid in self.state.images.list_ids():
            try:
                side = self.state.images.load_sidecar(iid)
            except Exception:
                continue
            out.append(side.model_dump(mode="json"))
        out.sort(key=lambda s: s.get("timestamp") or "", reverse=True)
        return out[:limit]

    def png_for(self, npy: Path, key: str, max_px: int = 1024) -> Path:
        from client.analysis.imaging import to_png
        from client.gui.render import _to_2d
        import numpy as np
        out = self.cache_dir / f"{key}_{max_px}.png"
        if not out.exists() or out.stat().st_mtime < npy.stat().st_mtime:
            arr = _to_2d(np.load(npy))
            tmp = out.with_suffix(".tmp.png")
            to_png(arr, tmp, max_px=max_px)
            tmp.replace(out)
        return out

    def fft_png_for(self, npy: Path, key: str, max_px: int = 1024) -> Path:
        """Log power spectrum of the centred square crop of the image.

        The crop is square so reciprocal pixels are isotropic: one FFT pixel is
        ``1 / (n * pixel_size)`` with ``n = min(height, width)``, which is what
        the viewer's reciprocal scale bar assumes. Larger spectra are
        block-averaged (not strided) down to *max_px* so lattice spots survive.
        """
        from client.analysis.imaging import to_png
        from client.gui.render import _to_2d
        import numpy as np
        out = self.cache_dir / f"{key}_fft_{max_px}.png"
        if not out.exists() or out.stat().st_mtime < npy.stat().st_mtime:
            a = _to_2d(np.load(npy)).astype(np.float64)
            n = min(a.shape)
            y0, x0 = (a.shape[0] - n) // 2, (a.shape[1] - n) // 2
            a = a[y0:y0 + n, x0:x0 + n]
            a = a - a.mean()
            win = np.hanning(n) if n > 1 else np.ones(1)
            a = a * np.outer(win, win)
            p = np.log1p(np.abs(np.fft.fftshift(np.fft.fft2(a))) ** 2)
            f = int(np.ceil(n / max_px))
            if f > 1:
                m = (n // f) * f
                p = p[:m, :m].reshape(m // f, f, m // f, f).mean(axis=(1, 3))
            # Floor at the median so the noise background goes dark and spots stand out.
            lo, hi = np.percentile(p, [50.0, 99.95])
            p = np.clip(p, lo, hi if hi > lo else lo + 1.0)
            tmp = out.with_suffix(".tmp.png")
            to_png(p, tmp, max_px=None)
            tmp.replace(out)
        return out


# --------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------

def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def build_app(console: WebConsole) -> Starlette:
    mcp_app = console.mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with console.mcp.session_manager.run():
            await console.start()
            try:
                yield
            finally:
                await console.stop()

    async def catalog(request):
        return JSONResponse({"tools": console.catalog(),
                             "capabilities": console.caps_payload()})

    async def capabilities(request):
        if request.query_params.get("refresh"):
            await console.refresh_capabilities()
        return JSONResponse(console.caps_payload())

    async def queue_get(request):
        return JSONResponse(console.queue.snapshot())

    async def queue_call(request):
        body = await _json(request)
        tool = body.get("tool")
        if tool not in console._tool_names():
            return _err(f"unknown tool {tool!r}")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            return _err("args must be an object")
        block, _ = console.queue.enqueue_call(tool, args, origin="manual",
                                              label=body.get("label"))
        return JSONResponse(block.to_json())

    async def queue_sequence(request):
        body = await _json(request)
        try:
            seq = load_sequence(body.get("sequence") or {})
        except Exception as exc:
            return _err(f"invalid sequence: {exc}")
        report = check(seq, console.caps)
        if not report.ok and console.caps_known and not body.get("force"):
            return JSONResponse({"error": "sequence is not compatible with this "
                                 "instrument", "report": report.to_json()}, status_code=409)
        block, _ = console.queue.enqueue_sequence(seq, body.get("params") or {})
        return JSONResponse({"block": block.to_json(), "report": report.to_json()})

    async def queue_control(request):
        action = (await _json(request)).get("action")
        q = console.queue
        if action == "pause":
            q.pause()
        elif action == "resume":
            q.resume()
        elif action == "step":
            q.step()
        elif action == "abort":
            q.abort()
        elif action == "clear":
            return JSONResponse({"removed": q.clear(), **q.state()})
        else:
            return _err(f"unknown action {action!r}")
        return JSONResponse(q.state())

    async def queue_remove(request):
        ok = console.queue.remove(request.path_params["block_id"])
        return JSONResponse({"removed": ok}, status_code=200 if ok else 409)

    async def stop_stage(request):
        try:
            data = await console.zmq.send_command_out_of_band("stop_stage", timeout_ms=5_000)
        except Exception as exc:
            return _err(f"stop_stage failed: {exc}", 502)
        console.channel.tell("STOP STAGE pressed", who="system")
        return JSONResponse({"ok": True, "data": data})

    # -- sequences ---------------------------------------------------------------
    def _seq_path(name: str) -> Path | None:
        if not _NAME_OK.match(name or ""):
            return None
        return console.seq_dir / f"{name}.amseq.json"

    async def seq_list(request):
        out = []
        for p in sorted(console.seq_dir.glob("*.amseq.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                seq = load_sequence(data)
                report = check(seq, console.caps)
                out.append({"name": p.name[:-len(".amseq.json")], "title": seq.name,
                            "description": seq.description, "ok": report.ok,
                            "missing": [m.cap for m in report.missing]})
            except Exception as exc:
                out.append({"name": p.name[:-len(".amseq.json")], "error": str(exc)})
        return JSONResponse({"sequences": out})

    async def seq_get(request):
        path = _seq_path(request.path_params["name"])
        if path is None or not path.exists():
            return _err("no such sequence", 404)
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))

    async def seq_put(request):
        path = _seq_path(request.path_params["name"])
        if path is None:
            return _err("sequence names: letters, digits, space, _ . - (max 80)")
        try:
            seq = load_sequence(await _json(request))
        except Exception as exc:
            return _err(f"invalid sequence: {exc}")
        path.write_text(json.dumps(seq.dump(), indent=2), encoding="utf-8")
        return JSONResponse({"saved": path.name, "report": check(seq, console.caps).to_json()})

    async def seq_delete(request):
        path = _seq_path(request.path_params["name"])
        if path is None or not path.exists():
            return _err("no such sequence", 404)
        path.unlink()
        return JSONResponse({"deleted": path.name})

    async def seq_check(request):
        try:
            seq = load_sequence(await _json(request))
        except Exception as exc:
            return _err(f"invalid sequence: {exc}")
        return JSONResponse({**check(seq, console.caps).to_json(),
                             "capabilities_known": console.caps_known})

    # -- images ------------------------------------------------------------------
    async def images(request):
        return JSONResponse({"images": console.list_images(
            int(request.query_params.get("limit", 200)))})

    async def image_meta(request):
        iid = request.path_params["image_id"]
        if not console.state.images.exists(iid):
            return _err("no such image", 404)
        return JSONResponse(console.state.images.load_sidecar(iid).model_dump(mode="json"))

    async def image_png(request):
        iid = request.path_params["image_id"]
        if not console.state.images.exists(iid):
            return _err("no such image", 404)
        max_px = min(4096, max(64, int(request.query_params.get("max_px", 1024))))
        npy = console.state.images.root / f"{iid}.npy"
        png = await asyncio.to_thread(console.png_for, npy, iid, max_px)
        return FileResponse(png, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

    async def image_fft_png(request):
        iid = request.path_params["image_id"]
        if not console.state.images.exists(iid):
            return _err("no such image", 404)
        max_px = min(4096, max(64, int(request.query_params.get("max_px", 1024))))
        npy = console.state.images.root / f"{iid}.npy"
        png = await asyncio.to_thread(console.fft_png_for, npy, iid, max_px)
        return FileResponse(png, media_type="image/png",
                            headers={"Cache-Control": "no-cache"})

    async def live_png(request):
        npy = console.data_dir / "monitor" / "live.npy"
        if not npy.exists():
            return _err("no live frame", 404)
        png = await asyncio.to_thread(console.png_for, npy, "live", 768)
        return FileResponse(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    # -- operator chat ------------------------------------------------------------
    async def chat_get(request):
        ch = console.channel
        return JSONResponse({"transcript": [e.to_json() for e in ch.transcript],
                             "agent_seen": ch.agent_seen, "agent_waiting": ch.agent_waiting})

    async def chat_post(request):
        text = str((await _json(request)).get("text", "")).strip()
        if not text:
            return _err("empty message")
        return JSONResponse(console.channel.post_instruction(text).to_json())

    async def chat_answer(request):
        body = await _json(request)
        ok = console.channel.answer(str(body.get("question_id")), str(body.get("answer", "")))
        return JSONResponse({"ok": ok}, status_code=200 if ok else 409)

    async def status(request):
        return JSONResponse({"relay_ok": console.relay_ok, "queue": console.queue.state(),
                             "capabilities_known": console.caps_known,
                             "agent_seen": console.channel.agent_seen,
                             "agent_waiting": console.channel.agent_waiting})

    async def events(request):
        q = console.bus.subscribe()

        async def gen():
            try:
                yield {"event": "hello", "data": json.dumps({
                    "queue": console.queue.snapshot(),
                    "capabilities": console.caps_payload(),
                    "relay_ok": console.relay_ok,
                    "params": console.params,
                    "chat": [e.to_json() for e in console.channel.transcript],
                    "agent_seen": console.channel.agent_seen,
                    "agent_waiting": console.channel.agent_waiting}, default=str)}
                while True:
                    ev = await q.get()
                    yield {"event": ev["kind"], "data": json.dumps(ev["data"], default=str)}
            finally:
                console.bus.unsubscribe(q)

        return EventSourceResponse(gen(), ping=15)

    async def index(request):
        return FileResponse(STATIC_DIR / "index.html")

    routes = [
        Route("/", index),
        Route("/api/catalog", catalog),
        Route("/api/capabilities", capabilities),
        Route("/api/status", status),
        Route("/api/queue", queue_get),
        Route("/api/queue/call", queue_call, methods=["POST"]),
        Route("/api/queue/sequence", queue_sequence, methods=["POST"]),
        Route("/api/queue/control", queue_control, methods=["POST"]),
        Route("/api/queue/{block_id}", queue_remove, methods=["DELETE"]),
        Route("/api/stop_stage", stop_stage, methods=["POST"]),
        Route("/api/sequences", seq_list),
        Route("/api/sequences/check", seq_check, methods=["POST"]),
        Route("/api/sequences/{name}", seq_get),
        Route("/api/sequences/{name}", seq_put, methods=["PUT"]),
        Route("/api/sequences/{name}", seq_delete, methods=["DELETE"]),
        Route("/api/images", images),
        Route("/api/images/{image_id}/fft.png", image_fft_png),
        Route("/api/images/{image_id}.png", image_png),
        Route("/api/images/{image_id}", image_meta),
        Route("/api/live.png", live_png),
        Route("/api/chat", chat_get),
        Route("/api/chat", chat_post, methods=["POST"]),
        Route("/api/chat/answer", chat_answer, methods=["POST"]),
        Route("/events", events),
        *mcp_app.routes,
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser(description="AMADEUS web console")
    parser.add_argument("--amadeus-config", default=None)
    parser.add_argument("--relay-address", default=None)
    parser.add_argument("--stream-address", default=None)
    parser.add_argument("--data-dir", default=None, type=Path)
    parser.add_argument("--timeout-s", default=None, type=float)
    parser.add_argument("--no-scripting", action="store_true")
    parser.add_argument("--no-monitor", action="store_true")
    parser.add_argument("--port", type=int, default=None,
                        help="HTTP port on 127.0.0.1 (default: config client.web_port, "
                             "else 8765)")
    args = parser.parse_args()

    from common import config as amadeus_config
    try:
        cfg = amadeus_config.load_or_none(args.amadeus_config)
    except amadeus_config.ConfigError as exc:
        parser.error(str(exc))
    settings = resolve_settings(args, cfg)
    port = args.port or int(((cfg or {}).get("client") or {}).get("web_port") or 8765)
    console = WebConsole(settings["address"], settings["data_dir"], port=port,
                         stream_address=settings["stream_address"],
                         timeout_ms=settings["timeout_ms"],
                         instrument=(cfg or {}).get("instrument") or None,
                         allow_scripting=settings["allow_scripting"],
                         enable_monitor=not args.no_monitor,
                         driver_kinds=driver_kinds_from(cfg))
    app = build_app(console)
    _log(f"[amadeus-web] relay {settings['address']}; data {settings['data_dir']}")
    _log(f"[amadeus-web] console: http://127.0.0.1:{port}/   MCP: http://127.0.0.1:{port}/mcp")
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
