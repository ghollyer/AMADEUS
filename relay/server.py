"""The relay proper — async ROUTER front door + per-backend forwarding.

Design notes:

* Frontend is a ROUTER socket so several clients (MCP server, manual GUI) can
  be connected at once and slow commands on one backend don't block the other:
  each request runs as its own asyncio task, serialised per backend by that
  backend's lock (the DM server is a single-threaded REP loop and the
  hardware backend has one worker thread — one in-flight request each is a
  feature, not a limitation).
* Replies are forwarded frame-for-frame, so multipart image responses from the
  DM server pass through byte-identical.
* A backend timeout abandons the in-flight REQ socket (closed and rebuilt on
  next use — a REQ that missed its reply is wedged otherwise) and returns a
  structured JSON error to the client.
* A request may carry ``"timeout_ms"`` (the client's own patience). The relay
  uses it instead of the backend default, and answers a little before the
  client would give up.
* A backend reply whose data holds ``"_continue": {"cmd", "params",
  "interval_s"}`` is followed: the relay re-issues that command until a
  reply has no ``_continue``, then sends the client the final reply. A
  waiting ``move_stage`` uses this: the move runs in a separate process,
  and the hardware worker is free between polls (``stop_stage`` included).
* The hardware backend (PyJEM / sim) runs *in this process* as backend
  ``"hw"`` (:class:`InProcessLink`). Every hardware call runs on one dedicated
  worker thread, which also constructed the backend, so the vendor API sees a
  single thread exactly as it did under the old standalone REP loop.
* The DM server's live-frame PUB is bound to 127.0.0.1 only. The relay
  republishes it unchanged (XSUB -> XPUB) on the public stream port, so only
  this process needs inbound network access.
"""

import argparse
import asyncio
import json
import sys
import time

import zmq
import zmq.asyncio

from common.commands import HW_SHUTDOWN_COMMAND, RELAY_LOCAL_COMMANDS, SLOT_COMMANDS
from common.protocol import error_response, make_request, ok_response, parse_request
from relay.config import RelayConfig, load_config
from relay.links import (BackendTimeout, DriverDown, HostedLink, InProcessLink,
                         RemoteLink, request_info)
from relay.policy import Policy, PolicyError
from relay.recovery import READBACK, RequestLedger
from relay.routing import RoutingError, RoutingTable

_PING_TIMEOUT_MS = 2_000
# Answer this fraction of the client's timeout in, so a slow command gets a
# structured reply instead of a client-side timeout.
_CLIENT_BUDGET = 0.9
_MAX_REQUEST_TIMEOUT_MS = 30 * 60 * 1000
_READBACK_TIMEOUT_MS = 2_000


class Relay:
    def __init__(self, config: RelayConfig, amadeus_cfg: "dict | None" = None):
        self.config = config
        self.amadeus_cfg = amadeus_cfg   # served to laptops via get_config
        self._context = zmq.asyncio.Context()
        self._frontend: "zmq.asyncio.Socket | None" = None
        self._links = {}
        self._stop = asyncio.Event()
        self._tasks: set = set()
        self.drivers = config.effective_drivers()
        self.routing = RoutingTable(self.drivers, config.roles)
        self.policy = Policy(config.policy, config.exclusive, self.routing)
        self.ledger = RequestLedger()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def route_for(self, cmd: str) -> str:
        """Driver name for *cmd* with no parameters (relay-local commands
        never reach here)."""
        return self.routing.resolve(cmd, {})[0]

    def _hw_name(self) -> "str | None":
        """The driver ``shutdown_hw`` releases: the column holder."""
        return self.routing.holder("column")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def _make_link(self, name: str, spec: dict):
        kind = spec.get("kind")
        host = spec.get("host", "in_process")
        timeout_ms = self.config.timeout_ms
        if kind == "dm":
            from drivers import dm as dm_adapter
            return RemoteLink(name, host, self._context, timeout_ms, kind="dm",
                              translate=dm_adapter.translate)
        if kind == "remote" or str(host).startswith(("tcp://", "ipc://")):
            return RemoteLink(name, host, self._context, timeout_ms, kind=kind or "remote")
        settings = {k: spec[k] for k in ("kind", "offline", "calibration", "options",
                                          "class") if spec.get(k) is not None}
        if spec.get("instrument") is not None:
            settings["instrument"] = spec["instrument"]
        elif self.config.instrument:
            settings["instrument"] = self.config.instrument
        if host == "process":
            link = HostedLink(name, {**spec, **settings}, self._context, timeout_ms,
                              log_dir=self.config.log_dir)
            link.start()
            return link
        link = InProcessLink(name, settings, timeout_ms)
        link.start()
        return link

    async def serve(self) -> None:
        self._frontend = self._context.socket(zmq.ROUTER)
        self._frontend.bind(self.config.listen)
        for name, spec in self.drivers.items():
            self._links[name] = self._make_link(name, spec)
            if isinstance(self._links[name], HostedLink):
                self._spawn(self._links[name].supervise(self._stop))
        print(f"[relay] listening on {self.config.listen}")
        for name, spec in self.drivers.items():
            link = self._links[name]
            print(f"[relay] driver {name}: {spec.get('kind')} via {link.address} -> "
                  f"{', '.join(spec.get('slots', [])) or 'no slots'}")
        streams = self._stream_sources()
        if self.config.stream_listen and streams:
            self._spawn(self._stream_proxy(streams))
        try:
            while not self._stop.is_set():
                if not await self._frontend.poll(250):
                    continue
                message = await self._frontend.recv_multipart()
                task = asyncio.create_task(self._handle(message))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        finally:
            for task in list(self._tasks):
                task.cancel()
            for link in self._links.values():
                link.close()
            if self._frontend is not None:
                self._frontend.close()
            self._context.term()
            print("[relay] shutdown complete")

    def stop(self) -> None:
        self._stop.set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _stream_sources(self) -> list:
        """Internal live-frame PUB addresses to republish: every driver's
        ``stream``."""
        out = []
        for spec in self.drivers.values():
            if spec.get("stream") and spec["stream"] not in out:
                out.append(spec["stream"])
        return out

    async def _stream_proxy(self, sources: list) -> None:
        """Republish the drivers' live frames (XSUB) on the public port (XPUB).

        One XSUB connects to every driver's stream, so frames from several
        devices share the public port; each frame's meta says which detector
        or camera it came from. Subscriptions flow back upstream so a source
        only sends while someone listens. A small send buffer drops stale
        frames under load. Frames pass through untouched."""
        xsub = self._context.socket(zmq.XSUB)
        xsub.setsockopt(zmq.LINGER, 0)
        for source in sources:
            xsub.connect(source)
        xpub = self._context.socket(zmq.XPUB)
        xpub.setsockopt(zmq.LINGER, 0)
        xpub.setsockopt(zmq.SNDHWM, 4)
        xpub.bind(self.config.stream_listen)
        print(f"[relay] live stream {', '.join(sources)} -> {self.config.stream_listen}")
        poller = zmq.asyncio.Poller()
        poller.register(xsub, zmq.POLLIN)
        poller.register(xpub, zmq.POLLIN)
        try:
            while not self._stop.is_set():
                events = dict(await poller.poll(250))
                if xsub in events:
                    await xpub.send_multipart(await xsub.recv_multipart())
                if xpub in events:
                    await xsub.send_multipart(await xpub.recv_multipart())
        finally:
            xsub.close()
            xpub.close()

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------
    async def _handle(self, message: "list[bytes]") -> None:
        # ROUTER framing from a REQ client: [identity..., b"", payload...]
        try:
            split = message.index(b"")
        except ValueError:
            return  # not REQ-style framing; drop
        envelope, payload = message[:split + 1], message[split + 1:]
        if not payload:
            return

        try:
            request = json.loads(payload[0])
            cmd = request.get("cmd", "")
            request_id = request.get("request_id", "")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be a JSON object")
        except (json.JSONDecodeError, AttributeError, ValueError) as exc:
            await self._reply_json(envelope, error_response(f"JSON parse error: {exc}"))
            return

        if cmd in RELAY_LOCAL_COMMANDS:
            try:
                reply = await self._local(cmd, request_id, params)
            except Exception as exc:
                reply = error_response(f"relay error: {type(exc).__name__}: {exc}",
                                       request_id)
            await self._reply_json(envelope, reply)
            return

        earlier = await self.ledger.lookup(request_id)
        if earlier is not None:                       # a re-sent request
            await self._frontend.send_multipart(envelope + earlier)
            return
        self.ledger.begin(request_id)
        frames = await self._forward(cmd, params, request, request_id)
        self.ledger.finish(request_id, frames)
        await self._frontend.send_multipart(envelope + frames)

    async def _forward(self, cmd: str, params: dict, request: dict,
                       request_id: str) -> "list[bytes]":
        def err(message: str, **extra) -> "list[bytes]":
            return [json.dumps({**error_response(message, request_id), **extra}).encode()]

        name = None
        try:
            if cmd == HW_SHUTDOWN_COMMAND:
                name, cmd, params = self._hw_name(), "shutdown", {}
                if name is None:
                    return err("no hardware driver to shut down")
            else:
                try:
                    name, cmd, params = self.routing.resolve(cmd, params)
                except RoutingError:
                    # An unconfigured role: guess it from the hardware (as
                    # the client's capabilities do), then try once more.
                    if not await self._guess_roles(params.get("device")):
                        raise
                    name, cmd, params = self.routing.resolve(cmd, params)
                params = await self.policy.check(
                    name, cmd, params, read_position=self._stage_reader())
            link = self._links[name]
            wire_cmd, wire_params = link.translate(cmd, params)
            timeout_ms = _request_timeout_ms(request)
            # A client-supplied timeout is that client's patience: answer a
            # little before it. Otherwise use the backend's configured timeout.
            budget_s = (timeout_ms * _CLIENT_BUDGET if timeout_ms
                        else link.timeout_ms) / 1000.0
            deadline = time.monotonic() + budget_s
            reply = await link.request([make_request(wire_cmd, wire_params, request_id)],
                                       timeout_ms=int(budget_s * 1000))
            return await self._follow_continuations(link, reply, request_id, deadline)
        except PolicyError as exc:
            if exc.confirmable:
                return err(str(exc), needs_confirmation=True)
            return err(str(exc))
        except RoutingError as exc:
            return err(str(exc))
        except DriverDown as exc:
            return err(str(exc), driver=name, driver_down=True)
        except BackendTimeout as exc:
            state = await self._read_back(name, cmd, params)
            message = str(exc)
            if state is not None:
                message += f" State read back after the timeout: {json.dumps(state['data'])}" \
                    if "data" in state else f" {state['note']}"
            return err(message, state_after=state)
        except Exception as exc:
            return err(f"relay error: {type(exc).__name__}: {exc}")

    async def _guess_roles(self, ref) -> bool:
        """Fill an unconfigured detector/camera role from the names the
        hardware reports (``common/roles.py``). Configured roles always win.
        True if the role is now known."""
        text = str(ref or "")
        kind, sep, role = text.partition(":")
        if not sep or kind not in ("detector", "camera") or "/" in kind:
            return False
        table = self.routing.roles.setdefault(kind + "s", {})
        if role in table:
            return False
        from common.roles import guess_camera_roles, guess_detector_roles
        holder = self.routing.holder("scan" if kind == "detector" else "camera")
        if holder is None:
            return False
        cmd, key = ("get_detectors", "detectors") if kind == "detector" \
            else ("list_cameras", "cameras")
        try:
            found = (await self._ask(holder, cmd)).get(key) or []
        except Exception:
            return False
        guess = guess_detector_roles(found) if kind == "detector" \
            else guess_camera_roles(found)
        for name, value in guess.items():
            table.setdefault(name, value)
        return role in table

    def _stage_reader(self):
        holder = self.routing.holder("stage")
        if holder is None:
            return None

        async def read() -> dict:
            return await self._ask(holder, "get_stage_position",
                                   timeout_ms=_READBACK_TIMEOUT_MS * 2)
        return read

    async def _read_back(self, name: "str | None", cmd: str, params: dict) -> "dict | None":
        """After a timeout: the state the command affects, if the driver can
        answer now. ``None`` when there is nothing to read."""
        read = READBACK.get(cmd)
        if name is None or read is None:
            return None
        link = self._links.get(name)
        busy = getattr(link, "busy", None)
        if isinstance(link, InProcessLink) and busy is not None:
            return {"note": f"The state can't be read back yet: the driver is still "
                            f"busy with {busy[0]!r}."}
        try:
            data = await self._ask(name, read, {"device": params["device"]}
                                   if params.get("device") else None,
                                   timeout_ms=_READBACK_TIMEOUT_MS)
            return {"command": read, "data": data}
        except Exception as exc:
            return {"note": f"The state couldn't be read back ({read}: {exc}); treat it "
                            f"as unknown and check before retrying."}

    async def _follow_continuations(self, link, reply: "list[bytes]",
                                    request_id: str, deadline: float) -> "list[bytes]":
        """Re-issue ``_continue`` commands until the backend gives a final
        reply. Each poll is its own short request, so other commands to the
        same backend (e.g. ``stop_stage``) run in between. Once the budget is
        spent, a last poll with ``finalize`` gets the current state."""
        while len(reply) == 1:
            try:
                obj = json.loads(reply[0])
            except (ValueError, TypeError):
                return reply
            data = obj.get("data") if obj.get("status") == "ok" else None
            if not isinstance(data, dict) or "_continue" not in data:
                return reply
            cont = data["_continue"]
            interval = float(cont.get("interval_s", 0.1))
            params = dict(cont.get("params") or {})
            remaining = deadline - time.monotonic()
            finalize = remaining <= interval + 1.0
            if finalize:
                params["finalize"] = True
            else:
                await asyncio.sleep(interval)
            reply = await link.request(
                [make_request(cont["cmd"], params, request_id)],
                timeout_ms=int(max(1.0, deadline - time.monotonic() + 1.0) * 1000))
            if finalize:
                try:
                    data = json.loads(reply[0]).get("data")
                except (ValueError, TypeError, IndexError):
                    return reply
                if isinstance(data, dict) and "_continue" in data:
                    return [json.dumps(error_response(
                        f"backend '{link.name}' kept asking to continue "
                        f"{cont['cmd']!r} after finalize", request_id)).encode()]
                return reply
        return reply

    async def _reply_json(self, envelope: "list[bytes]", obj: dict) -> None:
        await self._frontend.send_multipart(envelope + [json.dumps(obj).encode()])

    # ------------------------------------------------------------------
    # Relay-local commands
    # ------------------------------------------------------------------
    async def _local(self, cmd: str, request_id: str, params: "dict | None" = None) -> dict:
        params = params or {}
        if cmd == "ping":
            statuses = await self._probe_backends()
            return ok_response({
                "reply": "pong",
                "python_version": sys.version,
                "relay": True,
                "backends": statuses,
                "routing": self.routing.summary(),
            }, request_id)
        if cmd == "backend_status":
            statuses = await self._probe_backends()
            return ok_response({
                "backends": statuses,
                "routing": self.routing.summary(),
                "policy": self.policy.describe(),
            }, request_id)
        if cmd == "get_config":
            if self.amadeus_cfg is None:
                return error_response("relay was started without amadeus_config.json",
                                      request_id)
            from common.config import public
            return ok_response({"config": public(self.amadeus_cfg)}, request_id)
        if cmd == "get_capabilities":
            return ok_response(await self._capabilities(), request_id)
        if cmd == "restart_driver":
            name = params.get("driver")
            link = self._links.get(name)
            if not isinstance(link, HostedLink):
                return error_response(f"restart_driver: {name!r} is not a driver running in "
                                      f"its own process (have: " + ", ".join(
                                          n for n, l in self._links.items()
                                          if isinstance(l, HostedLink)) + ")", request_id)
            await link.restart()
            return ok_response({"restarted": name, **link.status()}, request_id)
        if cmd == "shutdown_relay":
            self.stop()
            return ok_response({"message": "relay shutting down"}, request_id)
        return error_response(f"Unknown relay command: {cmd!r}", request_id)

    async def _ask(self, backend: str, cmd: str, params: "dict | None" = None,
                   timeout_ms: int = _PING_TIMEOUT_MS) -> dict:
        """One short JSON request to *backend*; returns its data or raises."""
        link = self._links.get(backend)
        if link is None:
            raise RuntimeError(f"no {backend!r} backend configured")
        wire_cmd, wire_params = link.translate(cmd, dict(params or {}))
        frames = await link.request([make_request(wire_cmd, wire_params)],
                                    timeout_ms=timeout_ms)
        reply = json.loads(frames[0])
        if reply.get("status") != "ok":
            raise RuntimeError(reply.get("error", "error"))
        return reply.get("data") or {}

    async def _driver_manifest(self, name: str, spec: dict) -> dict:
        """One driver's capabilities, restricted to the slots it holds here."""
        kind = spec.get("kind")
        slots = list(spec.get("slots", []))
        out = {"kind": kind, "slots": slots, "host": self._links[name].address}
        if kind == "dm":
            from drivers import dm as dm_adapter
            from drivers.base import support_level
            out.update(ok=True, commands=dm_adapter.commands_for(slots),
                       extras=[f"dm.{e}" for e in dm_adapter.EXTRAS], vendor_call=True,
                       modes=["TEM", "STEM"] if "column" in slots else [], features=[])
            cameras = detectors = None
            probes = []
            if set(slots) & {"camera", "live"}:
                probes.append(("list_cameras", "cameras"))
            if set(slots) & {"scan", "live"}:
                probes.append(("get_detectors", "detectors"))
            if not probes:
                probes.append(("live_status", None))
            for cmd, key in probes:
                try:
                    data = await self._ask(name, cmd)
                    if key == "cameras":
                        cameras = data.get("cameras", [])
                    elif key == "detectors":
                        detectors = data.get("detectors", [])
                except (BackendTimeout, DriverDown) as exc:
                    out["ok"] = False
                    out["error"] = str(exc)
                    break            # DM is down: don't wait out a second timeout
                except Exception as exc:
                    # DM answered, with an error: it is up but has no such
                    # hardware (an offline DM has no cameras or DigiScan).
                    out.setdefault("probe_errors", {})[cmd] = str(exc).splitlines()[0]
                    if key == "cameras":
                        cameras = []
                    elif key == "detectors":
                        detectors = []
            out["cameras"], out["detectors"] = cameras, detectors
            out["devices"] = dm_adapter.devices_from_probe(cameras, detectors)
            out["level"] = support_level(out["commands"])
            return out
        try:
            caps = await self._ask(name, "get_driver_capabilities")
        except Exception as exc:
            return {**out, "ok": False, "error": str(exc)}
        wanted = set()
        for slot in slots:
            wanted |= SLOT_COMMANDS.get(slot, frozenset())
        commands = caps.get("commands")
        out.update(caps)
        out.update(ok=True, kind=kind, slots=slots,
                   commands=sorted(set(commands) & wanted) if commands is not None
                   else sorted(wanted),
                   all_commands=sorted(commands or []))
        return out

    async def _capabilities(self) -> dict:
        """The capability manifest clients check action blocks against.

        Every source is best-effort: a driver that is down is reported as
        ``{"ok": false, "error": ...}`` instead of failing the whole call, so
        the client can still say *why* a block is unavailable."""
        names = list(self.drivers)
        manifests = await asyncio.gather(
            *(self._driver_manifest(n, self.drivers[n]) for n in names))
        drivers = dict(zip(names, manifests))
        devices = []
        for name, man in drivers.items():
            for dev in man.get("devices") or []:
                devices.append({**dev, "id": f"{name}/{dev['id']}", "driver": name})

        instrument = {}
        if self.amadeus_cfg is not None:
            instrument = (self.amadeus_cfg.get("instrument") or {})
        return {"instrument": instrument, "drivers": drivers, "devices": devices,
                "routing": self.routing.summary(), "roles": self.config.roles,
                "policy": self.policy.describe()}

    async def _probe_backends(self) -> dict:
        async def probe(name: str, link) -> "tuple[str, dict]":
            spec = self.drivers.get(name, {})
            base = {"address": link.address, "kind": spec.get("kind"),
                    "slots": spec.get("slots", []), **link.status()}
            try:
                frames = await link.request([make_request("ping")],
                                            timeout_ms=_PING_TIMEOUT_MS)
                reply = json.loads(frames[0])
                data = reply.get("data", {})
                ok = reply.get("status") == "ok" and data.get("driver_ok", True)
                out = {**base, "ok": ok,
                       "python_version": data.get("python_version"),
                       "backend": data.get("backend")}
                if not ok:
                    out["error"] = data.get("error") or reply.get("error")
                return name, out
            except Exception as exc:
                return name, {**base, "ok": False, "error": str(exc)}

        results = await asyncio.gather(
            *(probe(n, l) for n, l in self._links.items()))
        return dict(results)


def _request_timeout_ms(request: dict) -> "int | None":
    """The client's per-request timeout, if it sent a sane one."""
    value = request.get("timeout_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return int(min(value, _MAX_REQUEST_TIMEOUT_MS))


def use_selector_loop_on_windows() -> None:
    """pyzmq's asyncio integration needs add_reader, which Windows' default
    Proactor loop lacks (it falls back to a tornado selector thread and warns).
    Use the selector loop instead."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AMADEUS server: relay, drivers and live-stream proxy")
    parser.add_argument("--amadeus-config", default=None,
                        help="unified amadeus_config.json (default: AMADEUS_CONFIG "
                             "env var, then <repo>/amadeus_config.json)")
    parser.add_argument("--listen", default=None, help="frontend bind address")
    parser.add_argument("--dm", default=None, help="DM driver address (overrides the config)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--online", dest="hw_mode", action="store_const", const="online",
                      help="column driver: the real column via PyJEM")
    mode.add_argument("--offline", dest="hw_mode", action="store_const", const="offline",
                      help="column driver: PyJEM's offline simulator")
    mode.add_argument("--sim", dest="hw_mode", action="store_const", const="sim",
                      help="column driver: AMADEUS's simulator (no PyJEM)")
    parser.add_argument("--calibration", default=None,
                        help="calibration JSON layered on the column driver's")
    parser.add_argument("--timeout-ms", type=int, default=None,
                        help="per-driver request timeout")
    args = parser.parse_args()

    from common import config as amadeus_config
    try:
        unified = amadeus_config.load_or_none(args.amadeus_config)
    except amadeus_config.ConfigError as exc:
        parser.error(str(exc))
    if unified is not None:
        print(f"[relay] using config {unified['_path']}")

    config = load_config(unified, listen=args.listen, dm=args.dm,
                         timeout_ms=args.timeout_ms, hw_mode=args.hw_mode,
                         calibration=args.calibration)
    use_selector_loop_on_windows()
    try:
        asyncio.run(Relay(config, unified).serve())
    except KeyboardInterrupt:
        print("[relay] interrupted")


if __name__ == "__main__":
    main()
