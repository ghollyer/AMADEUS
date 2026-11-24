"""MCP tool: STEM autofocus by an iterated focus sweep.

Each iteration steps a focus lens (by role, default ``focus.fine``: JEOL's
OLf) across a range of device values, takes a fast STEM frame at every step
and scores it with ``client.analysis.imaging.sharpness``. The focus values of
the two sharpest frames become the next iteration's range, so the step
shrinks each time. This is the sweep that found OLf 32541-32601 by hand on
the F200 on 2026-11-12, without the agent having to drive it frame by frame.
The lens's valid range comes from the driver (``get_focus``), not from a
vendor constant.

Everything goes through the relay (set_focus, stem_acquire), so the monitor
and the operator pause gate see every step.
"""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from client.analysis import imaging as _img
from client.devices import device_params
from client.state.manager import StateManager
from client.state.models import ImageCalibration
from client.transport.zmq_client import ZMQClient

_NO_DM = "needs a 'dm' driver"


async def _doc_ids(zmq: ZMQClient) -> set[int] | None | bool:
    """Open DM image documents, ``None`` if they can't be listed, or
    ``False`` when this microscope has no DM (no windows to tidy)."""
    try:
        listing = await zmq.send_command("dm.list_windows", {})
    except Exception as exc:
        return False if _NO_DM in str(exc) else None
    return {w["doc_id"] for w in listing.get("windows", []) if w.get("doc_id") is not None}


async def _close_new_frames(zmq: ZMQClient, before: set[int] | None,
                            names: set[str]) -> dict:
    """Close the DM windows the sweep opened: image documents that weren't
    open before it and carry one of the frame names DM reported. Anything
    else (the operator's own acquisitions) is left alone."""
    if before is False:
        return None
    if before is None:
        return {"closed": 0, "note": "window list unavailable before the sweep; "
                                     "frames left open in DM"}
    try:
        listing = await zmq.send_command("dm.list_windows", {})
    except Exception as exc:
        return {"closed": 0, "note": f"couldn't list DM windows: {exc}"}
    closed, failed = 0, 0
    for w in listing.get("windows", []):
        doc_id = w.get("doc_id")
        if doc_id is None or doc_id in before:
            continue
        if w.get("doc_name") not in names and w.get("title") not in names:
            continue
        try:
            await zmq.send_command("dm.close_window",
                                   {"doc_id": doc_id, "discard_unsaved": True})
            closed += 1
        except Exception:
            failed += 1
    out: dict = {"closed": closed}
    if failed:
        out["note"] = f"{failed} frame window(s) could not be closed"
    return out


def register(mcp: FastMCP, zmq: ZMQClient, state: StateManager) -> None:

    @mcp.tool()
    async def stem_autofocus(
        focus_min: int | None = None,
        focus_max: int | None = None,
        half_range: int = 200,
        steps: int = 5,
        iterations: int = 1,
        width: int = 512,
        height: int = 512,
        dwell_us: float = 1.0,
        detector: str | int | None = None,
        settle_s: float = 0.2,
        set_best: bool = True,
        close_frames: bool = True,
        keep_frames: bool = False,
        lens: str = "focus.fine",
    ) -> dict:
        """Autofocus in STEM by sweeping a focus lens and scoring the
        sharpness of a fast frame at each step.

        Iteration 1 takes *steps* frames evenly spaced from focus_min to
        focus_max. The focus values of the two sharpest frames become the
        range of the next iteration, and so on for *iterations* rounds (it
        stops early once the range is under 2 DAC). The sharpest frame of the
        last round sets the focus. On any error the original focus is
        restored.

        Use a field with sharp features (particles, edges) at the
        magnification you'll image at. Frames are 512x512 at 1 us by default
        (~0.3 s each). Keep it short: use no more than 5 steps and no more
        than 2 iterations (at most 10 frames). The default is 5 steps x 1
        iteration. Only use a second iteration when you are confident the
        range contains the optimum focus (e.g. a previous sweep peaked inside
        it, not at an edge); otherwise narrowing just homes in on the edge of
        a wrong range. If the best focus lands on the edge of the range, the
        true focus may lie outside it: shift or widen the range and rerun a
        single iteration. On the F200 STEM focus was within ~60 DAC of OLf
        32571 on 2026-11-12.

        Args:
            focus_min, focus_max: Sweep range for iteration 1, in the lens's
                device units (get_focus gives its range). Default: the
                current value +/- half_range.
            half_range: Half-width of the default range (device units).
            steps: Frames per iteration (>= 3; use no more than 5).
            iterations: Number of sweep rounds (>= 1; use no more than 2,
                and 2 only when the range surely contains the optimum).
            width, height: Frame size in pixels.
            dwell_us: Pixel dwell time (us); keep it short.
            detector: Detector to score: a role ("HAADF"), device id or
                index. Default: the scan unit's default detector.
            settle_s: Wait after each focus change before scanning.
            set_best: Leave the lens at the best focus (default). False
                restores the starting focus.
            close_frames: Close the sweep's frame windows in DM afterwards
                (only the windows this sweep opened).
            keep_frames: Save every frame to the image store (default: only
                the sharpest).
            lens: The focus lens role to sweep (get_focus lists them under
                "lenses"; default "focus.fine").

        Returns:
            lens, start, best, final (device values), best_image_id, one entry per
            iteration (range, points with sharpness, the two sharpest), and
            warnings.
        """
        if steps < 3:
            raise ValueError("steps must be >= 3")
        if iterations < 1:
            raise ValueError("iterations must be >= 1")

        mode = await zmq.send_command("get_instrument_mode")
        if str(mode.get("mode", "")).upper() != "STEM":
            raise ValueError(f"stem_autofocus needs STEM mode; the instrument reports "
                             f"{mode.get('mode')!r}. Switch with set_instrument_mode.")
        focus = await zmq.send_command("get_focus")
        start, (dac_min, dac_max) = _lens_state(focus, lens)
        lo = int(focus_min) if focus_min is not None else start - int(half_range)
        hi = int(focus_max) if focus_max is not None else start + int(half_range)
        if not (dac_min <= lo < hi <= dac_max):
            raise ValueError(f"focus range must satisfy {dac_min} <= min < max <= "
                             f"{dac_max} for {lens}, got [{lo}, {hi}]")

        params = {"mode": "capture", "width": width, "height": height,
                  "dwell_us": dwell_us, "rotation": 0.0,
                  **device_params(detector, "detector")}
        detector_tag = params.get("device")
        before = await _doc_ids(zmq) if close_frames else None
        frame_names: set[str] = set()
        rounds: list[dict] = []
        warnings: list[str] = []
        best: dict | None = None            # sharpest frame of the latest round
        kept: list[str] = []

        async def set_lens(v: int) -> None:
            await zmq.send_command("set_focus", {"lens": lens, "value": int(v)})

        try:
            for it in range(1, iterations + 1):
                values = _img.sweep_values(lo, hi, steps)
                if len(values) < 3 and it > 1:
                    break                                  # range collapsed
                points, round_best = [], None
                for v in values:
                    await set_lens(v)
                    if settle_s > 0:
                        await asyncio.sleep(settle_s)
                    meta, arr = await zmq.send_command_image("stem_acquire", params)
                    image_meta = meta.get("image_meta", {})
                    if image_meta.get("name"):
                        frame_names.add(image_meta["name"])
                    s = _img.sharpness(arr)
                    points.append((v, s))
                    entry = {"value": v, "sharpness": s, "arr": arr, "lens": lens,
                             "calibration": image_meta.get("calibration", {})}
                    if keep_frames:
                        entry["image_id"] = _save(state, entry, it, dwell_us, detector_tag)
                        kept.append(entry["image_id"])
                    if round_best is None or s > round_best["sharpness"]:
                        round_best = entry
                new_lo, new_hi = _img.two_sharpest_range(points)
                rounds.append({
                    "iteration": it, "range": [values[0], values[-1]],
                    "step": round((values[-1] - values[0]) / max(len(values) - 1, 1), 1),
                    "points": [{"value": v, "sharpness": round(s, 4)} for v, s in points],
                    "best": round_best["value"],
                    "next_range": [new_lo, new_hi],
                })
                if it == 1 and round_best["value"] in (values[0], values[-1]):
                    warnings.append(
                        f"the sharpest frame of iteration 1 is at the edge of the range "
                        f"({lens} {round_best['value']}); best focus may lie outside "
                        f"[{values[0]}, {values[-1]}]. Widen the range and rerun.")
                best = round_best
                lo, hi = new_lo, new_hi
                if hi - lo < 2:
                    break
            final = best["value"] if set_best else start
            await set_lens(final)
        except BaseException as exc:                  # incl. cancellation
            try:
                await set_lens(start)
                restored = f"focus restored to {lens} {start}"
            except Exception as exc2:
                restored = f"FAILED to restore focus to {lens} {start}: {exc2}"
            if isinstance(exc, Exception):
                raise RuntimeError(f"stem_autofocus aborted ({exc}); {restored}") from exc
            raise
        finally:
            cleanup = (await _close_new_frames(zmq, before, frame_names)
                       if close_frames else None)

        best_id = best.get("image_id") or _save(state, best, len(rounds), dwell_us,
                                                detector_tag)
        out = {
            "lens": lens,
            "start": start,
            "best": best["value"],
            "best_sharpness": round(best["sharpness"], 4),
            "final": final,
            "best_image_id": best_id,
            "n_frames": sum(len(r["points"]) for r in rounds),
            "iterations": rounds,
        }
        if keep_frames:
            out["frame_image_ids"] = kept
        if cleanup is not None:
            out["dm_frames"] = cleanup
        if warnings:
            out["warnings"] = warnings
        return out


def _lens_state(focus: dict, lens: str) -> "tuple[int, tuple[int, int]]":
    """(current value, (min, max)) of a focus lens role from a get_focus reply."""
    lenses = focus.get("lenses") or {}
    if lens in lenses:
        entry = lenses[lens]
        if not entry.get("range"):
            raise ValueError(f"the driver reports no range for {lens!r}; it must "
                             f"(drivers/base.py conventions)")
        lo, hi = entry["range"]
        return int(entry["value"]), (int(lo), int(hi))
    raise ValueError(f"this instrument has no focus lens {lens!r} (get_focus lists: "
                     f"{', '.join(lenses) or 'none'})")


def _save(state: StateManager, entry: dict, iteration: int, dwell_us: float,
          detector) -> str:
    cal = entry.get("calibration") or {}
    sidecar = state.images.save(
        entry["arr"],
        name=f"autofocus_it{iteration}_{entry['lens']}_{entry['value']}",
        calibration=ImageCalibration(x_scale=cal.get("x_scale", 1.0),
                                     y_scale=cal.get("y_scale", 1.0),
                                     units=cal.get("units", "")),
        tags={"type": "autofocus_frame", "imaging_mode": "STEM",
              "lens": entry["lens"], "value": entry["value"], "sharpness": entry["sharpness"],
              "iteration": iteration, "dwell_us": dwell_us,
              "detector": detector},
    )
    return sidecar.image_id
